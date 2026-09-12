import datetime
import functools
import os
import sys
import threading
import time
import types
from contextlib import contextmanager
from zoneinfo import ZoneInfo

import psycopg2
import psycopg2.pool

IRAN_TZ = ZoneInfo("Asia/Tehran")


def _tehran_today_str():
    """The current date (YYYY-MM-DD) in Iran time - the daily key growth stamps into
    last_grown, and therefore the day a rolled perk is valid for."""
    return datetime.datetime.now(IRAN_TZ).date().isoformat()

# Connection string for the Supabase Postgres database.
# Grab it from your Supabase project: Settings -> Database -> Connection string
# (use the "Transaction" pooler URI, port 6543, for a long running bot).
# Example:
#   postgresql://postgres.<ref>:<password>@aws-0-<region>.pooler.supabase.com:6543/postgres
DB_URL = os.environ.get('SUPABASE_DB_URL') or os.environ.get('DATABASE_URL')

# Rate applied once, retroactively, to deposits made before the deposit fee existed.
# Kept here rather than imported from bot.py because init_db must not depend on bot.
BACKFILL_DEPOSIT_FEE_RATIO = 0.02

# Credit scoring. Kept here rather than in bot.py because settle_loan applies the score
# change in the same transaction that moves the money.
CREDIT_BASE = 100
CREDIT_MIN, CREDIT_MAX = 0, 200
CREDIT_ON_TIME = 10       # paid up before the due date
CREDIT_LATE = -12         # paid voluntarily, but after the due date
CREDIT_FORCED = -20       # the nightly sweep had to take it from their wallet
CREDIT_BANK_SEIZED = -30  # ...and had to reach into their bank deposit
CREDIT_SHORTFALL = -45    # ...and they still could not cover it

# Anti-farming. Without these, the cheapest way to a perfect score is to borrow the
# minimum, repay it seconds later, and repeat - no risk taken, full reward. Three
# independent brakes, because any one of them alone is dodgeable:
#   1. the gain scales with how big the loan was RELATIVE TO THE BORROWER, so a token
#      loan earns a token amount (a 10 on a 1000-size player is worth nothing);
#   2. a loan repaid almost immediately earns nothing at all - you carried no risk;
#   3. a hard daily ceiling on credit gained, so grinding many loans cannot substitute.
# Penalties are deliberately NOT scaled the same way. Credit should be slow to build and
# quick to lose, and a cheap "practice default" should still hurt.
CREDIT_MIN_HOLD_RATIO = 0.25   # of the term, before an early repayment counts at all
CREDIT_DAILY_GAIN_CAP = 12


# Connections are POOLED, and this is the single biggest thing standing between the
# game and feeling slow.
#
# This used to open a brand-new connection per call. Against Supabase that is a TCP
# handshake plus a TLS handshake every time, and measured from production that cost
# ~0.8-1.0s PER CALL - on a database whose largest table is under 2 MB, so essentially
# none of it was query time. It compounded badly: one Mini App home screen makes about a
# dozen db calls (~10s to load), the crypto tick made two (7s, every minute, and it was
# blocking the event loop while it did), and every bot command makes several.
#
# Sizing: each process gets its own pool, so the total against Postgres is roughly
# DB_POOL_MAX x (bot + gunicorn workers for the panel + workers for the Mini App).
# Keep the product comfortably under the server's max_connections.
DB_POOL_MIN = int(os.environ.get('DB_POOL_MIN', '1'))
DB_POOL_MAX = int(os.environ.get('DB_POOL_MAX', '8'))

_POOL = None
_POOL_PID = None
_POOL_LOCK = threading.Lock()

_CONNECT_KWARGS = dict(
    connect_timeout=10,
    # TCP keepalives so a connection silently dropped by the Supabase pooler (the
    # recurring "SSL connection has been closed unexpectedly" in production) is
    # detected instead of hanging.
    keepalives=1, keepalives_idle=30, keepalives_interval=10, keepalives_count=3,
)


def _get_pool():
    """The process's connection pool, created lazily.

    Keyed on the PID as well: gunicorn forks its workers, and a pool created before the
    fork would hand the same socket to two processes, which corrupts both. Nothing in
    this repo touches the database at import time, so in practice the pool is always
    born after the fork - the check is here so that stays true if that ever changes.
    """
    global _POOL, _POOL_PID
    pid = os.getpid()
    if _POOL is not None and _POOL_PID == pid:
        return _POOL
    with _POOL_LOCK:
        if _POOL is None or _POOL_PID != pid:
            _POOL = psycopg2.pool.ThreadedConnectionPool(
                DB_POOL_MIN, DB_POOL_MAX, DB_URL, **_CONNECT_KWARGS)
            _POOL_PID = pid
    return _POOL


# How long to wait for a pooled connection before giving up and opening a private one.
DB_POOL_WAIT_SECONDS = float(os.environ.get('DB_POOL_WAIT_SECONDS', '2.0'))


def _acquire(pool):
    """(connection, is_pooled) - wait briefly for a pooled connection, then fall back.

    ThreadedConnectionPool.getconn() RAISES PoolError the moment all DB_POOL_MAX
    connections are checked out; it does not queue. That matters here because the bot
    runs with concurrent_updates(True), so a burst of handlers can easily want more at
    once - and PoolError is neither OperationalError nor InterfaceError, so
    _retry_transient would not catch it and the player would just see the generic
    "temporary problem". Pooling must never be able to fail a command that the old
    connect-every-time code would have served.

    So: wait a little for one to come back, and if the pool is still saturated, open a
    private connection and close it at the end instead of pooling it. The worst case
    degrades to exactly the old behaviour rather than to an error.
    """
    deadline = time.monotonic() + DB_POOL_WAIT_SECONDS
    while True:
        try:
            return pool.getconn(), True
        except psycopg2.pool.PoolError:
            if time.monotonic() >= deadline:
                return psycopg2.connect(DB_URL, **_CONNECT_KWARGS), False
            time.sleep(0.02)


@contextmanager
def get_connection():
    """Borrow a pooled connection, commit on success, and always hand it back.

    A connection is returned to the pool for reuse UNLESS it looks broken, in which case
    it is closed and the pool opens a fresh one next time. That distinction is what makes
    pooling safe here: a plain SQL error (a constraint violation, say) rolls back and the
    connection is perfectly good, while an OperationalError/InterfaceError means the
    socket is gone and handing it back would poison the next caller.

    _retry_transient already re-runs every public function once on exactly those two
    errors, so a connection that died while idle in the pool costs one silent retry
    rather than a failed command.
    """
    if not DB_URL:
        raise RuntimeError(
            "Supabase connection string is not configured. "
            "Set the SUPABASE_DB_URL (or DATABASE_URL) environment variable to your "
            "Supabase Postgres connection string."
        )
    pool = _get_pool()
    conn, pooled = _acquire(pool)
    broken = False
    try:
        if conn.closed:
            # Handed back a dead one. Raise the error _retry_transient already knows how
            # to recover from rather than inventing a new failure mode.
            broken = True
            raise psycopg2.InterfaceError("pooled connection was closed")
        yield conn
        conn.commit()
    except (psycopg2.OperationalError, psycopg2.InterfaceError):
        broken = True
        raise
    except Exception:
        try:
            conn.rollback()
        except Exception:
            # Rollback itself failing means the socket is gone, whatever the original
            # error was. Let the original propagate, but don't reuse this connection.
            broken = True
        raise
    finally:
        try:
            if pooled:
                pool.putconn(conn, close=broken)
            else:
                conn.close()
        except Exception:
            try:
                conn.close()
            except Exception:
                pass


def init_db():
    with get_connection() as conn:
        c = conn.cursor()
        c.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT,
                chat_id BIGINT,
                username TEXT,
                first_name TEXT,
                size DOUBLE PRECISION DEFAULT 0,
                last_grown TEXT DEFAULT '',
                perk TEXT DEFAULT 'عادی',
                active_item TEXT DEFAULT '',
                joined_at TIMESTAMPTZ DEFAULT now(),
                wins INTEGER DEFAULT 0,
                losses INTEGER DEFAULT 0,
                PRIMARY KEY (user_id, chat_id)
            )
        ''')
        c.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS joined_at TIMESTAMPTZ")
        # Backfill pre-existing players as if they joined a month ago, so this migration
        # doesn't retroactively block everyone's /dd the moment it ships.
        c.execute("UPDATE users SET joined_at = now() - interval '30 days' WHERE joined_at IS NULL")
        c.execute("ALTER TABLE users ALTER COLUMN joined_at SET DEFAULT now()")
        c.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS wins INTEGER DEFAULT 0")
        c.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS losses INTEGER DEFAULT 0")
        # Consecutive days of growth (see claim_daily_growth), the anti-spam clock for
        # /dozdi, and how long a betrayed-the-king player wears the خائن mark.
        c.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS streak INTEGER DEFAULT 0")
        c.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS best_streak INTEGER DEFAULT 0")
        c.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS last_theft_at TIMESTAMPTZ")
        c.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS traitor_until TIMESTAMPTZ")
        # Per-player moderation dials, both 1.0 = untouched. theft_luck multiplies the
        # final /dozdi success chance; growth_mult narrows the top of the daily growth
        # roll before the dice are thrown. They exist so a suspected cheater can be
        # quietly throttled instead of banned outright - see the admin commands in
        # bot.py. Neither one ever makes the bot report a number that isn't real: the
        # theft chance is no longer published at all, and a throttled growth roll is
        # credited and displayed as exactly the number that was rolled.
        # When this player last had a ویاگرا/قرص اورژانسی applied *to* them. The limit
        # is on the receiving end, not the giver: otherwise four people could each
        # spend one item on the same target in a row and swing them 160cm in a minute.
        c.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS last_dosed_at TIMESTAMPTZ")
        c.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS theft_luck DOUBLE PRECISION DEFAULT 1.0")
        c.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS growth_mult DOUBLE PRECISION DEFAULT 1.0")
        c.execute("UPDATE users SET theft_luck = 1.0 WHERE theft_luck IS NULL")
        c.execute("UPDATE users SET growth_mult = 1.0 WHERE growth_mult IS NULL")
        # Set when a human deliberately pins a player's dials with /setgrowth or
        # /setluck. The nightly auto-handicap skips locked players entirely, so an
        # owner's manual decision is never quietly undone a few hours later by the
        # rebalancer - the two systems write the same two columns and this flag is
        # what keeps them from fighting over them.
        c.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS dials_locked BOOLEAN DEFAULT FALSE")
        c.execute("UPDATE users SET dials_locked = FALSE WHERE dials_locked IS NULL")
        # A tiny key/value table for one-shot migrations. init_db runs on every single
        # startup, so a backfill that must happen exactly once needs somewhere to
        # record that it already did - without this, the lock migration below would
        # re-fire on each restart and permanently freeze every dial the nightly
        # handicap had legitimately moved.
        c.execute('CREATE TABLE IF NOT EXISTS bot_meta (key TEXT PRIMARY KEY, value TEXT)')
        c.execute("SELECT value FROM bot_meta WHERE key = 'dials_lock_migrated'")
        if not c.fetchone():
            # Any dial already off 1.0 when this ships was set by hand by an owner, so
            # pin it: the auto-handicap must not quietly undo a deliberate decision on
            # its very first night. They can be handed back to the automatic system
            # with /setgrowth ... 1 (setting a dial to exactly 1.0 releases the pin).
            c.execute("UPDATE users SET dials_locked = TRUE "
                      "WHERE COALESCE(theft_luck, 1.0) <> 1.0 OR COALESCE(growth_mult, 1.0) <> 1.0")
            c.execute("INSERT INTO bot_meta (key, value) VALUES ('dials_lock_migrated', '1') "
                      "ON CONFLICT (key) DO NOTHING")
        # Theft items are activated into their own slot rather than the challenge slot:
        # one shared slot meant arming a glove silently disarmed your condom, and the
        # two are used in completely different moments.
        c.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS active_theft_item TEXT DEFAULT ''")
        c.execute("UPDATE users SET active_theft_item = '' WHERE active_theft_item IS NULL")

        c.execute('''
            CREATE TABLE IF NOT EXISTS chats (
                chat_id BIGINT PRIMARY KEY
            )
        ''')
        # The owner's manual verdict on whether a group may be a transfer SOURCE:
        # 'auto' (judge it by get_xfer_source_stats), 'trusted' (always allowed) or
        # 'blocked' (never). Heuristics can be gamed by someone patient enough with
        # enough alt accounts, so the last word has to be a human's.
        c.execute("ALTER TABLE chats ADD COLUMN IF NOT EXISTS xfer_policy TEXT DEFAULT 'auto'")
        # A group's lifecycle. `active` is what every scheduled job iterates, so a dead
        # group stops costing the bot a nightly report, a tax run, a boss and a decree
        # forever. `deactivated_reason` is the part that matters: 'idle' lifts the moment
        # somebody speaks again, 'admin' does NOT - otherwise one message would undo the
        # owner's decision.
        c.execute('ALTER TABLE chats ADD COLUMN IF NOT EXISTS active BOOLEAN DEFAULT TRUE')
        c.execute('ALTER TABLE chats ADD COLUMN IF NOT EXISTS deactivated_at TIMESTAMPTZ')
        c.execute('ALTER TABLE chats ADD COLUMN IF NOT EXISTS deactivated_reason TEXT')
        c.execute('ALTER TABLE chats ADD COLUMN IF NOT EXISTS last_seen_at TIMESTAMPTZ')
        # Group name, so the web app's group picker can say "خانواده" instead of
        # "-1001858630001". Recorded opportunistically from whatever update comes in -
        # Telegram is the only source of it and the bot never asked before.
        c.execute("ALTER TABLE chats ADD COLUMN IF NOT EXISTS title TEXT")
        # The group's public copy style. Existing groups keep the game's original
        # adult voice; an administrator may switch the whole league to the polite
        # renderer without changing any rules, item ids, perks or stored event text.
        c.execute("ALTER TABLE chats ADD COLUMN IF NOT EXISTS tone_mode TEXT DEFAULT 'adult'")
        c.execute("UPDATE chats SET tone_mode = 'adult' "
                  "WHERE tone_mode IS NULL OR tone_mode NOT IN ('adult', 'polite')")

        c.execute('''
            CREATE TABLE IF NOT EXISTS inventory (
                user_id BIGINT,
                chat_id BIGINT,
                item_name TEXT,
                quantity INTEGER DEFAULT 0,
                PRIMARY KEY (user_id, chat_id, item_name)
            )
        ''')

        c.execute('''
            CREATE TABLE IF NOT EXISTS chat_instances (
                chat_instance TEXT PRIMARY KEY,
                chat_id BIGINT
            )
        ''')

        # THE EVENT LOG. The app's feed reads this, not Telegram - a chat message is a
        # rendering of an event, never the event itself. Kept forever on the server;
        # only the VIEW is scoped to a day.
        c.execute('''
            CREATE TABLE IF NOT EXISTS events (
                id BIGSERIAL PRIMARY KEY,
                chat_id BIGINT,
                day TEXT,
                at TIMESTAMPTZ DEFAULT now(),
                kind TEXT,
                audience TEXT DEFAULT 'group',
                actor_id BIGINT,
                actor_name TEXT,
                target_id BIGINT,
                target_name TEXT,
                amount DOUBLE PRECISION,
                text TEXT
            )
        ''')
        # The feed's only two reads: this group's day, and one player's own day.
        c.execute('CREATE INDEX IF NOT EXISTS events_chat_day ON events (chat_id, day, id DESC)')
        c.execute('CREATE INDEX IF NOT EXISTS events_actor ON events (actor_id, day)')

        # Telegram Stars orders are persisted before an invoice is created. The
        # successful-payment update may be delivered again after a reconnect, so the
        # order row and Telegram charge id are both unique and fulfilment happens in
        # the same transaction as the item/size grant.
        c.execute('''
            CREATE TABLE IF NOT EXISTS star_orders (
                id UUID PRIMARY KEY,
                user_id BIGINT NOT NULL,
                chat_id BIGINT NOT NULL,
                sku TEXT NOT NULL,
                kind TEXT NOT NULL,
                quantity INTEGER NOT NULL,
                stars INTEGER NOT NULL,
                status TEXT DEFAULT 'created',
                telegram_charge_id TEXT UNIQUE,
                created_at TIMESTAMPTZ DEFAULT now(),
                checkout_at TIMESTAMPTZ,
                paid_at TIMESTAMPTZ,
                refunded_at TIMESTAMPTZ
            )
        ''')
        c.execute('ALTER TABLE star_orders ADD COLUMN IF NOT EXISTS checkout_at TIMESTAMPTZ')
        c.execute('CREATE INDEX IF NOT EXISTS star_orders_user_chat_idx '
                  'ON star_orders (user_id, chat_id, created_at DESC)')

        # Tonight's decree hand. This lived in a dict in bot.py, which meant two things:
        # a deploy between the deal and the signature lost the hand (the pvp_matches
        # lesson again), and the Mini App - a SEPARATE PROCESS - could not see it at all,
        # so /farman could never exist outside Telegram.
        c.execute("""
            CREATE TABLE IF NOT EXISTS pending_decrees (
                chat_id BIGINT PRIMARY KEY,
                day TEXT,
                king_id BIGINT,
                codes TEXT
            )
        """)

        # Open challenges. The stake and challenger used to live only inside a signed
        # callback_data blob, which is unlistable: a browser has no button to read it
        # off. The row is the listing; claimed_challenges is still the atomic claim, so
        # the accept race is settled exactly where it always was.
        c.execute("""
            CREATE TABLE IF NOT EXISTS open_challenges (
                nonce TEXT PRIMARY KEY,
                chat_id BIGINT,
                challenger_id BIGINT,
                challenger_name TEXT,
                bet DOUBLE PRECISION,
                created_at TIMESTAMPTZ DEFAULT now(),
                status TEXT DEFAULT 'open'
            )
        """)
        c.execute('CREATE INDEX IF NOT EXISTS open_challenges_chat '
                  'ON open_challenges (chat_id, status, created_at DESC)')

        # One message per group per night, edited in place as each nightly job lands its
        # section, instead of seven separate messages between 00:00 and 00:20.
        # Price history for the market chart. Downsampled on write (see
        # crypto_record_history) and pruned on a schedule - it is a display artefact,
        # and the live price is the only thing the game ever settles against.
        c.execute('''
            CREATE TABLE IF NOT EXISTS crypto_history (
                symbol TEXT,
                at TIMESTAMPTZ DEFAULT now(),
                price DOUBLE PRECISION
            )
        ''')
        c.execute('CREATE INDEX IF NOT EXISTS crypto_history_sym_at '
                  'ON crypto_history (symbol, at DESC)')

        c.execute('''
            CREATE TABLE IF NOT EXISTS night_reports (
                chat_id BIGINT,
                day TEXT,
                message_id BIGINT,
                PRIMARY KEY (chat_id, day)
            )
        ''')

        c.execute('''
            CREATE TABLE IF NOT EXISTS night_report_sections (
                chat_id BIGINT,
                day TEXT,
                key TEXT,
                rank INTEGER DEFAULT 0,
                body TEXT,
                PRIMARY KEY (chat_id, day, key)
            )
        ''')

        c.execute('''
            CREATE TABLE IF NOT EXISTS consensus_votes (
                id SERIAL PRIMARY KEY,
                chat_id BIGINT,
                target_id BIGINT,
                target_name TEXT,
                initiator_id BIGINT,
                amount DOUBLE PRECISION,
                required_votes INTEGER,
                total_players INTEGER,
                status TEXT DEFAULT 'open',
                created_at TIMESTAMPTZ DEFAULT now(),
                resolved_at TIMESTAMPTZ
            )
        ''')
        c.execute("ALTER TABLE consensus_votes ADD COLUMN IF NOT EXISTS total_players INTEGER")

        c.execute('''
            CREATE TABLE IF NOT EXISTS consensus_vote_casts (
                vote_id INTEGER REFERENCES consensus_votes(id),
                user_id BIGINT,
                first_name TEXT,
                choice TEXT DEFAULT 'yes',
                PRIMARY KEY (vote_id, user_id)
            )
        ''')
        c.execute("ALTER TABLE consensus_vote_casts ADD COLUMN IF NOT EXISTS first_name TEXT")
        c.execute("ALTER TABLE consensus_vote_casts ADD COLUMN IF NOT EXISTS choice TEXT DEFAULT 'yes'")

        # One row per protected target. Editable directly in Supabase's Table Editor:
        # delete a row to lift the protection early, or edit protected_until to change its length.
        c.execute('''
            CREATE TABLE IF NOT EXISTS consensus_protection (
                chat_id BIGINT,
                target_id BIGINT,
                target_name TEXT,
                protected_until TIMESTAMPTZ,
                reason TEXT,
                PRIMARY KEY (chat_id, target_id)
            )
        ''')

        # The football betting feature was removed; drop its tables so they stop
        # taking up space. Every bet that ever existed belonged to a market that had
        # already finished and paid out, so nothing is owed to anyone here.
        c.execute("DROP TABLE IF EXISTS football_bets")
        c.execute("DROP TABLE IF EXISTS football_markets")

        # The throne. One row per group: who currently wears the crown, who they took
        # as a consort, and the date the crown last paid its tax (so the daily tax can
        # only ever be collected once per Tehran day even if the job runs twice).
        c.execute('''
            CREATE TABLE IF NOT EXISTS kingdom (
                chat_id BIGINT PRIMARY KEY,
                king_id BIGINT,
                king_name TEXT,
                crowned_at TIMESTAMPTZ,
                consort_id BIGINT,
                consort_name TEXT,
                consort_since TIMESTAMPTZ,
                last_consort_date TEXT DEFAULT '',
                last_tax_date TEXT DEFAULT ''
            )
        ''')

        # Daily co-op boss. One live boss per group at a time; every player may hit it
        # once, and the reward is split by damage dealt if the group kills it in time.
        c.execute('''
            CREATE TABLE IF NOT EXISTS bosses (
                id SERIAL PRIMARY KEY,
                chat_id BIGINT,
                name TEXT,
                max_hp INTEGER,
                hp INTEGER,
                status TEXT DEFAULT 'alive',
                message_id BIGINT,
                spawn_date TEXT,
                created_at TIMESTAMPTZ DEFAULT now()
            )
        ''')
        c.execute('''
            CREATE TABLE IF NOT EXISTS boss_hits (
                boss_id INTEGER REFERENCES bosses(id),
                user_id BIGINT,
                first_name TEXT,
                damage INTEGER,
                PRIMARY KEY (boss_id, user_id)
            )
        ''')

        # Daily lottery. Tickets are keyed by the Tehran date they were bought for, so
        # the midnight draw only ever looks at that day's pot.
        c.execute('''
            CREATE TABLE IF NOT EXISTS lottery_tickets (
                chat_id BIGINT,
                draw_date TEXT,
                user_id BIGINT,
                first_name TEXT,
                tickets INTEGER DEFAULT 0,
                PRIMARY KEY (chat_id, draw_date, user_id)
            )
        ''')
        # `tickets` is entries (odds); `paid` is the size actually spent on them. They
        # used to be the same thing, which meant any bonus entry - a perk, a golden
        # ticket - silently inflated the prize as well as the odds, paying out money
        # nobody put in. Keeping them apart lets a bonus change who wins without
        # changing how much is won.
        c.execute("ALTER TABLE lottery_tickets ADD COLUMN IF NOT EXISTS paid INTEGER")
        c.execute("UPDATE lottery_tickets SET paid = tickets * 10 WHERE paid IS NULL")

        # Every movement of size, ever. Written from inside update_size/try_deduct_size
        # so coverage is complete by construction rather than depending on 50-odd call
        # sites remembering to log. `source` is the calling function's name, which is
        # what makes "where did this player's size come from" answerable at all.
        c.execute('''
            CREATE TABLE IF NOT EXISTS size_log (
                id BIGSERIAL PRIMARY KEY,
                chat_id BIGINT,
                user_id BIGINT,
                delta DOUBLE PRECISION,
                balance_after DOUBLE PRECISION,
                source TEXT,
                note TEXT,
                created_at TIMESTAMPTZ DEFAULT now()
            )
        ''')

        # Every decision the nightly auto-handicap makes, so a player asking "why did my
        # growth drop?" has an answer that can be looked up instead of guessed at.
        c.execute('''
            CREATE TABLE IF NOT EXISTS rebalance_log (
                id BIGSERIAL PRIMARY KEY,
                chat_id BIGINT,
                user_id BIGINT,
                run_date TEXT,
                net_recent DOUBLE PRECISION,
                group_median DOUBLE PRECISION,
                growth_before DOUBLE PRECISION,
                growth_after DOUBLE PRECISION,
                luck_before DOUBLE PRECISION,
                luck_after DOUBLE PRECISION,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        ''')
        c.execute('CREATE INDEX IF NOT EXISTS rebalance_log_chat_idx ON rebalance_log (chat_id, run_date)')

        # ---------------------------------------------------------------- bank
        # Banked size lives OUTSIDE users.size on purpose. The leaderboard, the crown
        # and /dozdi all read users.size, so parking size here really does buy safety
        # from theft at the cost of dropping down the table - that trade is the whole
        # point of the feature, and it only works if the two balances stay separate.
        c.execute('''
            CREATE TABLE IF NOT EXISTS bank_accounts (
                user_id BIGINT,
                chat_id BIGINT,
                balance DOUBLE PRECISION DEFAULT 0,
                deposit_date TEXT DEFAULT '',
                deposited_today DOUBLE PRECISION DEFAULT 0,
                opened_at TIMESTAMPTZ DEFAULT now(),
                PRIMARY KEY (user_id, chat_id)
            )
        ''')
        # NOT a vault any more. The treasury is ONE pot for the whole bot and lives in
        # central_bank.reserve; what is left in this table is per-group BOOKKEEPING -
        # which day this group's interest was last paid, and when its last heist was
        # attempted. Those genuinely are per group. The money is not.
        #
        # The `balance` column is DROPPED by the migration below rather than left
        # sitting at zero: a stale column that still looks authoritative is exactly the
        # drift this codebase keeps getting bitten by, and code that still reads it
        # should fail loudly instead of quietly seeing 0.
        c.execute('''
            CREATE TABLE IF NOT EXISTS bank_treasury (
                chat_id BIGINT PRIMARY KEY,
                last_interest_date TEXT DEFAULT '',
                last_heist_at TIMESTAMPTZ
            )
        ''')
        c.execute('''
            CREATE TABLE IF NOT EXISTS bank_log (
                id BIGSERIAL PRIMARY KEY,
                chat_id BIGINT,
                user_id BIGINT,
                kind TEXT,
                amount DOUBLE PRECISION,
                balance_after DOUBLE PRECISION,
                note TEXT,
                created_at TIMESTAMPTZ DEFAULT now()
            )
        ''')
        c.execute('CREATE INDEX IF NOT EXISTS bank_log_chat_idx ON bank_log (chat_id, created_at DESC)')

        # The central bank, and the ONE treasury the whole bot shares. `reserve` is the
        # single stored balance: there is no per-group vault and no per-group share of
        # this one, so there is nothing for "pool == sum of shares" to drift away from.
        # Balances stay strictly per group in `users.size`; the bank does not.
        c.execute('''
            CREATE TABLE IF NOT EXISTS central_bank (
                id INTEGER PRIMARY KEY,
                loans_out DOUBLE PRECISION DEFAULT 0,
                created_at TIMESTAMPTZ DEFAULT now()
            )
        ''')
        c.execute('ALTER TABLE central_bank ADD COLUMN IF NOT EXISTS reserve DOUBLE PRECISION DEFAULT 0')
        c.execute('INSERT INTO central_bank (id) VALUES (1) ON CONFLICT (id) DO NOTHING')


        # ---------------------------------------------------------------- crypto market
        # One market for the whole bot, exactly like the central bank: a coin is worth
        # the same in every group, so players can actually argue about the price. Only
        # the CURRENT price is stored - `prev_price` is what the last tick moved it from,
        # which is all the UI needs to draw an arrow. No history table: nobody has asked
        # for a chart and an unbounded per-minute log would dwarf every other table here.
        c.execute('''
            CREATE TABLE IF NOT EXISTS crypto_prices (
                symbol TEXT PRIMARY KEY,
                name TEXT,
                price DOUBLE PRECISION,
                prev_price DOUBLE PRECISION,
                base_price DOUBLE PRECISION,
                volatility DOUBLE PRECISION,
                updated_at TIMESTAMPTZ DEFAULT now()
            )
        ''')
        # The real-market feed. feed_id is the upstream's id for the coin this one
        # parodies; feed_scale maps that coin's dollar price onto this one's in-game
        # price and is fixed the FIRST time a price is seen, so the game price tracks
        # the real coin's percentage moves from wherever base_price put it. Re-deriving
        # the scale on every tick would pin the price to base and track nothing.
        for col, decl in (('feed_id', 'TEXT'),
                          ('feed_scale', 'DOUBLE PRECISION'),
                          ('feed_usd', 'DOUBLE PRECISION'),
                          ('feed_at', 'TIMESTAMPTZ')):
            c.execute(f'ALTER TABLE crypto_prices ADD COLUMN IF NOT EXISTS {col} {decl}')
        # Holdings are per (user, chat) because size is. `avg_cost` is what makes a sale
        # separable into "my money coming back" and "what I actually made" - see
        # crypto_sell, and get_recent_net_by_user for why that split is load-bearing.
        c.execute('''
            CREATE TABLE IF NOT EXISTS crypto_holdings (
                user_id BIGINT,
                chat_id BIGINT,
                symbol TEXT,
                amount DOUBLE PRECISION DEFAULT 0,
                avg_cost DOUBLE PRECISION DEFAULT 0,
                bought_date TEXT DEFAULT \'\',
                bought_today DOUBLE PRECISION DEFAULT 0,
                PRIMARY KEY (user_id, chat_id, symbol)
            )
        ''')
        c.execute('CREATE INDEX IF NOT EXISTS crypto_holdings_owner_idx '
                  'ON crypto_holdings (user_id, chat_id)')
        # The market's net long position, in units, across every player in every group.
        # This is what makes buying push the price up: `price` stays the random-walk
        # MID, and what anyone actually pays is that mid shifted by the inventory (see
        # _crypto_exec_price). Stored rather than derived from crypto_holdings only
        # because the sum would have to be recomputed on every quote.
        c.execute('ALTER TABLE crypto_prices ADD COLUMN IF NOT EXISTS '
                  'net_units DOUBLE PRECISION DEFAULT 0')

        # ---------------------------------------------------------------- economy
        # One row per group. `inflation` is a price index: everything the game charges
        # or pays out is multiplied by it, so a group that prints money finds its shop
        # getting expensive. `unrest` is what a looted population does about it.
        # The three multipliers are the levers the crown actually holds.
        c.execute('''
            CREATE TABLE IF NOT EXISTS economy (
                chat_id BIGINT PRIMARY KEY,
                inflation DOUBLE PRECISION DEFAULT 1.0,
                unrest DOUBLE PRECISION DEFAULT 0,
                fee_mult DOUBLE PRECISION DEFAULT 1.0,
                interest_mult DOUBLE PRECISION DEFAULT 1.0,
                growth_mult DOUBLE PRECISION DEFAULT 1.0,
                supply_last DOUBLE PRECISION,
                last_decree_date TEXT DEFAULT '',
                last_tick_date TEXT DEFAULT '',
                decrees_good INTEGER DEFAULT 0,
                decrees_bad INTEGER DEFAULT 0
            )
        ''')
        # The decrees a king was offered on a given day, and which one he signed.
        c.execute('''
            CREATE TABLE IF NOT EXISTS decree_log (
                id BIGSERIAL PRIMARY KEY,
                chat_id BIGINT,
                king_id BIGINT,
                king_name TEXT,
                decree_date TEXT,
                code TEXT,
                title TEXT,
                kind TEXT,
                inflation_before DOUBLE PRECISION,
                inflation_after DOUBLE PRECISION,
                king_delta DOUBLE PRECISION,
                unrest_after DOUBLE PRECISION,
                created_at TIMESTAMPTZ DEFAULT now()
            )
        ''')
        c.execute('CREATE INDEX IF NOT EXISTS decree_log_chat_idx ON decree_log (chat_id, id DESC)')

        # One row per (group, item): the shared purchase counters that make shop limits
        # global rather than per-player - one player buying the day's last unit must
        # actually leave nothing for anyone else, not five-per-player. day/week are reset
        # lazily the moment a stale value is read, the same trick perks use for expiring
        # at Tehran midnight, so no scheduled reset job is needed.
        c.execute('''
            CREATE TABLE IF NOT EXISTS shop_item_state (
                chat_id BIGINT NOT NULL,
                item_name TEXT NOT NULL,
                day TEXT DEFAULT '',
                day_count INTEGER DEFAULT 0,
                week TEXT DEFAULT '',
                week_count INTEGER DEFAULT 0,
                PRIMARY KEY (chat_id, item_name)
            )
        ''')

        # ---------------------------------------------------------------- loans
        # lender_id IS NULL means the group's treasury is the lender (the official
        # /vam loan); any other value is a player-to-player نزول. Both settle through
        # exactly the same repayment and collection code so the two can never drift.
        c.execute('''
            CREATE TABLE IF NOT EXISTS loans (
                id BIGSERIAL PRIMARY KEY,
                chat_id BIGINT,
                lender_id BIGINT,
                lender_name TEXT,
                borrower_id BIGINT,
                borrower_name TEXT,
                principal DOUBLE PRECISION,
                rate DOUBLE PRECISION,
                due_amount DOUBLE PRECISION,
                paid DOUBLE PRECISION DEFAULT 0,
                status TEXT DEFAULT 'offered',
                created_at TIMESTAMPTZ DEFAULT now(),
                accepted_at TIMESTAMPTZ,
                due_at TIMESTAMPTZ,
                closed_at TIMESTAMPTZ
            )
        ''')
        c.execute('CREATE INDEX IF NOT EXISTS loans_chat_status_idx ON loans (chat_id, status)')
        c.execute('CREATE INDEX IF NOT EXISTS loans_due_idx ON loans (status, due_at)')
        c.execute("ALTER TABLE loans ADD COLUMN IF NOT EXISTS size_at_accept DOUBLE PRECISION")
        # How many times this player has been force-collected. Worn publicly as بدهکار
        # and used to price future loans.
        c.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS loan_defaults INTEGER DEFAULT 0")
        c.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS last_xfer_at TIMESTAMPTZ")
        # Credit history. The score starts at CREDIT_BASE and is the multiplier on how
        # much a player is allowed to borrow, so behaviour feeds straight back into
        # access to money rather than sitting in a cosmetic stat.
        c.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS credit_score INTEGER DEFAULT 100")
        c.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS loans_repaid INTEGER DEFAULT 0")
        c.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS loans_late INTEGER DEFAULT 0")
        c.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS credit_gain_date TEXT DEFAULT ''")
        c.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS credit_gain_today INTEGER DEFAULT 0")
        # Every inter-group raid that has actually happened, for the announcement, the
        # history, and so a repeat pairing is visible rather than looking like a bug.
        c.execute('''
            CREATE TABLE IF NOT EXISTS group_wars (
                id BIGSERIAL PRIMARY KEY,
                war_date TEXT,
                attacker_chat BIGINT,
                defender_chat BIGINT,
                loot DOUBLE PRECISION,
                attackers INTEGER,
                defenders INTEGER,
                created_at TIMESTAMPTZ DEFAULT now()
            )
        ''')
        c.execute('CREATE INDEX IF NOT EXISTS group_wars_date_idx ON group_wars (war_date DESC)')

        # Jester duty: whoever called a vote the king dissolved, and until when.
        c.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS jester_until TIMESTAMPTZ")
        # Martial law is rationed per group, not per king, so abdicating and being
        # re-crowned cannot refresh it.
        c.execute("ALTER TABLE economy ADD COLUMN IF NOT EXISTS last_martial_at TIMESTAMPTZ")

        # A busted heist sentences the thief: heist_prison_until is the hard lockout
        # (can't grow, challenge, steal, or heist again), heist_labor_until runs longer
        # and just skims a cut of daily growth to the king (see grow_callback). The bail
        # price is frozen at sentencing time (priced off the vault they were going for),
        # so it can't drift with inflation while they're stuck inside.
        c.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS heist_prison_until TIMESTAMPTZ")
        c.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS heist_labor_until TIMESTAMPTZ")
        c.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS heist_bail_amount DOUBLE PRECISION")

        # A single in-progress vault-cracking attempt. Persisted (not held in memory) so
        # a restart mid-game gets picked up by recover_stuck_heist_attempts instead of
        # leaving the message stuck forever with the group's one heist slot on cooldown -
        # the exact bug class pvp_matches was built to avoid.
        c.execute('''
            CREATE TABLE IF NOT EXISTS heist_attempts (
                id UUID PRIMARY KEY,
                chat_id BIGINT,
                thief_id BIGINT,
                thief_name TEXT,
                sequence TEXT,
                progress INTEGER DEFAULT 0,
                would_be DOUBLE PRECISION,
                message_chat_id BIGINT,
                message_id BIGINT,
                status TEXT DEFAULT 'pending',
                expires_at TIMESTAMPTZ,
                created_at TIMESTAMPTZ DEFAULT now()
            )
        ''')
        c.execute("CREATE INDEX IF NOT EXISTS heist_attempts_pending_idx "
                  "ON heist_attempts (status, expires_at)")
        # A heist is a THREE-STAGE job needing TWO people, so the row carries the
        # accomplice and which stage is live. `stage_deadline` is per stage; the row's
        # `expires_at` stays the whole-run deadline the recovery sweep reads, so a
        # process that dies between stages is still swept exactly as before.
        c.execute("ALTER TABLE heist_attempts ADD COLUMN IF NOT EXISTS partner_id BIGINT")
        c.execute("ALTER TABLE heist_attempts ADD COLUMN IF NOT EXISTS partner_name TEXT")
        c.execute("ALTER TABLE heist_attempts ADD COLUMN IF NOT EXISTS stage INTEGER DEFAULT 2")
        c.execute("ALTER TABLE heist_attempts ADD COLUMN IF NOT EXISTS stage_deadline TIMESTAMPTZ")
        c.execute("ALTER TABLE heist_attempts ADD COLUMN IF NOT EXISTS wire INTEGER")
        c.execute("ALTER TABLE heist_attempts ADD COLUMN IF NOT EXISTS alarm_armed BOOLEAN DEFAULT FALSE")
        c.execute("ALTER TABLE heist_attempts ADD COLUMN IF NOT EXISTS escape_thief BOOLEAN DEFAULT FALSE")
        c.execute("ALTER TABLE heist_attempts ADD COLUMN IF NOT EXISTS escape_partner BOOLEAN DEFAULT FALSE")
        # The two moments the run's timing hangs off, stored rather than left implicit
        # in a scheduled job. The bot still schedules its edits off them, but the Mini
        # App - which has no scheduler at all - can derive the whole run from these two
        # timestamps, so a heist is playable from either surface without a second copy
        # of the clock. See heist_tick().
        c.execute("ALTER TABLE heist_attempts ADD COLUMN IF NOT EXISTS alarm_at TIMESTAMPTZ")
        c.execute("ALTER TABLE heist_attempts ADD COLUMN IF NOT EXISTS vault_at TIMESTAMPTZ")

        # One-time: charge the deposit fee on money that was banked before the fee
        # existed. Everyone who deposited in that window got in free, which is both
        # unfair to whoever deposits next and the reason the treasury is empty while
        # the vault is full. Guarded by bot_meta so it can only ever run once, and it
        # moves the fee into the treasury rather than deleting it - the same
        # conservation rule every other fee follows.
        c.execute("SELECT value FROM bot_meta WHERE key = 'deposit_fee_backfilled'")
        if not c.fetchone():
            c.execute("""
                WITH fees AS (
                    SELECT user_id, chat_id,
                           round((balance * %s)::numeric, 2)::float8 AS fee
                    FROM bank_accounts WHERE COALESCE(balance,0) > 0
                ),
                deb AS (
                    UPDATE bank_accounts b SET balance = b.balance - f.fee
                    FROM fees f
                    WHERE b.user_id = f.user_id AND b.chat_id = f.chat_id AND f.fee > 0
                    RETURNING b.user_id, b.chat_id, f.fee, b.balance AS new_bal
                ),
                lg AS (
                    INSERT INTO bank_log (chat_id, user_id, kind, amount, balance_after, note)
                    SELECT chat_id, user_id, 'fee_backfill', -fee, new_bal,
                           'کارمزد واریزهای قبلی'
                    FROM deb RETURNING 1
                ),
                agg AS (SELECT SUM(fee) AS tot FROM deb)
                UPDATE central_bank SET reserve = COALESCE(reserve,0)
                                               + COALESCE((SELECT tot FROM agg), 0)
                WHERE id = 1
            """, (BACKFILL_DEPOSIT_FEE_RATIO,))
            c.execute("INSERT INTO bot_meta (key, value) VALUES ('deposit_fee_backfilled', '1') "
                      "ON CONFLICT (key) DO NOTHING")
        c.execute("CREATE INDEX IF NOT EXISTS size_log_chat_user_idx ON size_log (chat_id, user_id, created_at DESC)")
        c.execute("CREATE INDEX IF NOT EXISTS size_log_created_idx ON size_log (created_at DESC)")

        # One-time: reopen /enteghal now that a source group has to prove it is a real
        # league before size can leave it (see get_xfer_source_stats). Guarded, because
        # init_db runs on every startup and the owner must stay free to close it again
        # from the panel without the next restart reopening it behind their back.
        c.execute("SELECT value FROM bot_meta WHERE key = 'xfer_reopened_with_source_gate'")
        if not c.fetchone():
            c.execute('INSERT INTO bot_meta (key, value) VALUES (%s, %s) '
                      'ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value',
                      (XFER_ENABLED_KEY, '1'))
            c.execute("INSERT INTO bot_meta (key, value) "
                      "VALUES ('xfer_reopened_with_source_gate', '1') "
                      "ON CONFLICT (key) DO NOTHING")

        # Permanent badges. The PK is what makes each one award-once.
        c.execute('''
            CREATE TABLE IF NOT EXISTS achievements (
                user_id BIGINT,
                chat_id BIGINT,
                code TEXT,
                earned_at TIMESTAMPTZ DEFAULT now(),
                PRIMARY KEY (user_id, chat_id, code)
            )
        ''')

        # Persists PvP challenge matches (and spectator bets on them) so a match that's
        # mid-way through its 20-second betting window survives a bot restart instead of
        # being orphaned forever with the escrowed bet gone and the message stuck showing
        # stale buttons - see resolve_pvp_match / recover_stuck_pvp_matches in bot.py.
        c.execute('''
            CREATE TABLE IF NOT EXISTS pvp_matches (
                id UUID PRIMARY KEY,
                chat_id BIGINT,
                challenger_id BIGINT,
                challenger_name TEXT,
                acceptor_id BIGINT,
                acceptor_name TEXT,
                bet DOUBLE PRECISION,
                message_id BIGINT,
                inline_message_id TEXT,
                status TEXT DEFAULT 'pending',
                created_at TIMESTAMPTZ DEFAULT now()
            )
        ''')
        c.execute("ALTER TABLE pvp_matches ADD COLUMN IF NOT EXISTS inline_message_id TEXT")

        c.execute('''
            CREATE TABLE IF NOT EXISTS pvp_match_bets (
                match_id UUID REFERENCES pvp_matches(id),
                user_id BIGINT,
                first_name TEXT,
                side TEXT,
                amount DOUBLE PRECISION,
                PRIMARY KEY (match_id, user_id)
            )
        ''')

        # One row per accepted challenge button, keyed by the nonce in that button's
        # callback_data, so a challenge can only ever be accepted once - even by two
        # people tapping it in the same instant. See claim_challenge().
        c.execute('''
            CREATE TABLE IF NOT EXISTS claimed_challenges (
                nonce TEXT PRIMARY KEY,
                claimed_at TIMESTAMPTZ DEFAULT now()
            )
        ''')

        # One-time: fold the per-group vaults into that single reserve. Guarded by
        # bot_meta like every other data migration here, because init_db() runs on every
        # startup and an unguarded fold would re-add the same money on each restart.
        # Written to be a no-op on a fresh database (there is no `balance` column to
        # fold) and to drop the column in the same pass, so there is never a window in
        # which two different numbers both claim to be the treasury.
        c.execute("SELECT value FROM bot_meta WHERE key = 'treasury_merged_global'")
        if not c.fetchone():
            c.execute("SELECT 1 FROM information_schema.columns "
                      "WHERE table_name = 'bank_treasury' AND column_name = 'balance'")
            if c.fetchone():
                c.execute('UPDATE central_bank SET reserve = COALESCE(reserve,0) '
                          '  + COALESCE((SELECT SUM(balance) FROM bank_treasury), 0) '
                          'WHERE id = 1')
                c.execute('ALTER TABLE bank_treasury DROP COLUMN balance')
            # get_money_supply no longer counts a treasury share, so every group's
            # stored baseline was measured with the old formula. Clearing it makes
            # tonight a "first night" per group - tick_inflation just re-records the
            # baseline - instead of reading the definition change as a collapse in the
            # money supply and deflating every price in the game overnight.
            c.execute('UPDATE economy SET supply_last = NULL')
            c.execute("INSERT INTO bot_meta (key, value) VALUES ('treasury_merged_global', '1') "
                      "ON CONFLICT (key) DO NOTHING")


def get_last_chat(user_id):
    """Returns this user's one and only active group's chat_id, or None if they've
    never played in a group, or have played in more than one. Deliberately returns
    None (instead of guessing) when ambiguous: Telegram inline queries never reveal
    which group they were typed in, so for a user active in multiple groups there is
    no way to know which one's data to show - callers must fall back to resolving
    chat_id from a real posted message instead of ever guessing wrong."""
    with get_connection() as conn:
        c = conn.cursor()
        c.execute('SELECT DISTINCT chat_id FROM users WHERE user_id = %s AND chat_id < 0', (user_id,))
        rows = c.fetchall()
        return rows[0][0] if len(rows) == 1 else None


def crypto_record_history(rows):
    """Append one price point per coin, in a single statement.

    This runs on a timer forever, so it is written the same way crypto_set_prices is:
    one round trip for the whole board rather than ten. `rows` is [(symbol, price)].
    """
    if not rows:
        return
    with get_connection() as conn:
        c = conn.cursor()
        args = b','.join(c.mogrify('(%s,%s)', (sym, float(px))) for sym, px in rows)
        c.execute(b'INSERT INTO crypto_history (symbol, price) VALUES ' + args)


def crypto_history(symbol, hours=24, limit=240):
    """Oldest-first price points for one coin over the last `hours`.

    Capped at `limit` points because a chart on a phone cannot show more, and sending
    1,400 of them would cost more than the rest of the screen put together.
    """
    with get_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT EXTRACT(EPOCH FROM at)::bigint, price FROM crypto_history "
                  "WHERE symbol = %s AND at > now() - (%s || ' hours')::interval "
                  "ORDER BY at DESC LIMIT %s", (symbol, str(int(hours)), int(limit)))
        return [(int(t), float(p)) for t, p in reversed(c.fetchall())]


def crypto_prune_history(keep_days=7):
    with get_connection() as conn:
        c = conn.cursor()
        c.execute("DELETE FROM crypto_history WHERE at < now() - (%s || ' days')::interval",
                  (str(int(keep_days)),))


def log_event(chat_id, day, kind, text, audience='group', actor_id=None,
              actor_name=None, target_id=None, target_name=None, amount=None):
    """Record that something happened. Never raises - a feed entry must not be able to
    undo the thing it describes."""
    try:
        with get_connection() as conn:
            c = conn.cursor()
            c.execute('INSERT INTO events (chat_id, day, kind, audience, actor_id, '
                      'actor_name, target_id, target_name, amount, text) '
                      'VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                      (chat_id, day, kind, audience, actor_id, actor_name,
                       target_id, target_name,
                       float(amount) if amount is not None else None, text))
    except Exception:
        import logging
        logging.exception('event log write failed')


def get_events(chat_id, day, user_id, limit=200):
    """One day's feed for one player: everything the group can see, plus that player's
    own private entries. Somebody else's bank balance is not group news.

    The day is passed in rather than derived here so the caller decides what "today"
    means - db.py has no business owning the game's calendar.
    """
    with get_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT id, EXTRACT(EPOCH FROM at)::bigint, kind, audience, actor_id, "
                  "actor_name, target_id, target_name, amount, text FROM events "
                  "WHERE chat_id = %s AND day = %s AND (audience = 'group' "
                  "  OR actor_id = %s OR target_id = %s) "
                  "ORDER BY id DESC LIMIT %s",
                  (chat_id, day, user_id, user_id, int(limit)))
        return c.fetchall()


def count_events_since(chat_id, day, user_id, after_id):
    """How many entries a player has not seen, for the badge on the bell."""
    with get_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM events WHERE chat_id = %s AND day = %s "
                  "AND id > %s AND (audience = 'group' OR actor_id = %s OR target_id = %s)",
                  (chat_id, day, int(after_id or 0), user_id, user_id))
        return int(c.fetchone()[0])


def night_report_add(chat_id, day, key, rank, body):
    """Add (or replace) one section of tonight's single report and return the whole thing.

    Returns (message_id, full_text). `message_id` is None until the report has actually
    been posted once - the caller posts it then and calls night_report_set_message.

    The key is unique per night, so a job that runs twice (a restart, a recovery sweep)
    overwrites its own section instead of printing it again. That is the same
    idempotence the nightly claims already give the money; this gives it to the text.
    """
    with get_connection() as conn:
        c = conn.cursor()
        c.execute('INSERT INTO night_reports (chat_id, day) VALUES (%s, %s) '
                  'ON CONFLICT (chat_id, day) DO NOTHING', (chat_id, day))
        c.execute('INSERT INTO night_report_sections (chat_id, day, key, rank, body) '
                  'VALUES (%s, %s, %s, %s, %s) '
                  'ON CONFLICT (chat_id, day, key) DO UPDATE SET '
                  'rank = EXCLUDED.rank, body = EXCLUDED.body',
                  (chat_id, day, key, rank, body))
        c.execute('SELECT body FROM night_report_sections WHERE chat_id = %s AND day = %s '
                  'ORDER BY rank, key', (chat_id, day))
        parts = [r[0] for r in c.fetchall()]
        c.execute('SELECT message_id FROM night_reports WHERE chat_id = %s AND day = %s',
                  (chat_id, day))
        row = c.fetchone()
    return (row[0] if row else None), "\n\n".join(parts)


def night_report_set_message(chat_id, day, message_id):
    with get_connection() as conn:
        c = conn.cursor()
        c.execute('INSERT INTO night_reports (chat_id, day, message_id) VALUES (%s, %s, %s) '
                  'ON CONFLICT (chat_id, day) DO UPDATE SET message_id = EXCLUDED.message_id',
                  (chat_id, day, message_id))


def night_report_prune(keep_days=7):
    """The report is a display artefact, not a ledger - nothing reads an old one."""
    with get_connection() as conn:
        c = conn.cursor()
        for table in ('night_report_sections', 'night_reports'):
            c.execute(f"DELETE FROM {table} WHERE day < %s",
                      ((datetime.datetime.now(IRAN_TZ).date()
                        - datetime.timedelta(days=keep_days)).isoformat(),))


def track_chat(chat_id, title=None):
    """`title` is written only when one is supplied, so the ~40 call sites that don't
    have it can never blank a name the logger already recorded."""
    if chat_id < 0:
        with get_connection() as conn:
            c = conn.cursor()
            c.execute('INSERT INTO chats (chat_id, title) VALUES (%s, %s) '
                      'ON CONFLICT (chat_id) DO UPDATE SET '
                      'title = COALESCE(EXCLUDED.title, chats.title)', (chat_id, title))


def get_chat_titles(chat_ids):
    """{chat_id: title} for the ones that have a name recorded."""
    if not chat_ids:
        return {}
    with get_connection() as conn:
        c = conn.cursor()
        c.execute('SELECT chat_id, title FROM chats WHERE chat_id = ANY(%s)', (list(chat_ids),))
        return {row[0]: row[1] for row in c.fetchall() if row[1]}


def remove_chat(chat_id):
    """Forget a chat the bot can no longer post to (kicked, or the group was deleted),
    so the nightly reminder stops erroring on it forever. The group's users rows stay -
    if the bot is ever re-added, /d re-tracks the chat and everyone's sizes are intact."""
    with get_connection() as conn:
        c = conn.cursor()
        c.execute('DELETE FROM chats WHERE chat_id = %s', (chat_id,))


def track_chat_instance(chat_instance, chat_id):
    with get_connection() as conn:
        c = conn.cursor()
        c.execute(
            'INSERT INTO chat_instances (chat_instance, chat_id) VALUES (%s, %s) '
            'ON CONFLICT (chat_instance) DO UPDATE SET chat_id = EXCLUDED.chat_id',
            (chat_instance, chat_id)
        )


def get_chat_id_from_instance(chat_instance):
    with get_connection() as conn:
        c = conn.cursor()
        c.execute('SELECT chat_id FROM chat_instances WHERE chat_instance = %s', (chat_instance,))
        row = c.fetchone()
        return row[0] if row else None


def get_all_chats(include_inactive=False):
    """Every group the bot works for.

    Deactivated groups are excluded by DEFAULT, because this is what the nightly jobs
    iterate and a dead group should stop costing a report, a tax run, a boss and a
    decree every single day. Pass include_inactive=True only to administer them.
    """
    with get_connection() as conn:
        c = conn.cursor()
        if include_inactive:
            c.execute('SELECT chat_id FROM chats')
        else:
            c.execute('SELECT chat_id FROM chats WHERE COALESCE(active, TRUE)')
        return [r[0] for r in c.fetchall()]


def chats_without_title():
    """Groups we only know by id. The picker shows them as "گروه 717026", which is the
    bot admitting it never saw a message from them - the daily job asks Telegram.

    Deactivated groups are included deliberately. They run nothing, so a name buys them
    no gameplay - but the panel's inactive filter is a list the owner has to make
    decisions from (reactivate this one? delete that one?), and a column of bare ids is
    the same complaint this backfill exists to answer.
    """
    with get_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT chat_id FROM chats WHERE (title IS NULL OR title = '') "
                  "ORDER BY COALESCE(active, TRUE) DESC, chat_id")
        return [r[0] for r in c.fetchall()]


def mark_chat_seen(chat_id):
    """Somebody spoke. Stamps the activity clock and lifts an IDLE deactivation.

    Deliberately does not lift an 'admin' one: the owner turned that group off on
    purpose, and a single message must not undo it. Called from log_incoming only -
    track_chat is called from forty places that are not evidence of anybody being there.
    """
    if chat_id >= 0:
        return
    with get_connection() as conn:
        c = conn.cursor()
        c.execute("UPDATE chats SET last_seen_at = now(), "
                  "active = CASE WHEN COALESCE(deactivated_reason,'') = 'idle' THEN TRUE "
                  "              ELSE COALESCE(active, TRUE) END, "
                  "deactivated_at = CASE WHEN COALESCE(deactivated_reason,'') = 'idle' "
                  "                      THEN NULL ELSE deactivated_at END, "
                  "deactivated_reason = CASE WHEN COALESCE(deactivated_reason,'') = 'idle' "
                  "                          THEN NULL ELSE deactivated_reason END "
                  "WHERE chat_id = %s", (chat_id,))


def set_chat_active(chat_id, active, reason=None):
    """The owner's switch. `reason` is stored so the idle sweep can tell its own work
    apart from a decision a human made."""
    with get_connection() as conn:
        c = conn.cursor()
        if active:
            c.execute('UPDATE chats SET active = TRUE, deactivated_at = NULL, '
                      'deactivated_reason = NULL WHERE chat_id = %s', (chat_id,))
        else:
            c.execute('UPDATE chats SET active = FALSE, deactivated_at = now(), '
                      'deactivated_reason = %s WHERE chat_id = %s',
                      (reason or 'admin', chat_id))
        return c.rowcount


def sweep_idle_chats(days):
    """Deactivate groups nobody has spoken in for `days`. Returns the chat_ids.

    A group with no last_seen_at yet is left ALONE rather than swept: the column was
    added after these groups existed, and reading "never recorded" as "never active"
    would switch off every live group on the first night.
    """
    with get_connection() as conn:
        c = conn.cursor()
        c.execute("UPDATE chats SET active = FALSE, deactivated_at = now(), "
                  "deactivated_reason = 'idle' "
                  "WHERE COALESCE(active, TRUE) AND last_seen_at IS NOT NULL "
                  "  AND last_seen_at < now() - (%s || ' days')::interval "
                  "RETURNING chat_id", (str(int(days)),))
        return [r[0] for r in c.fetchall()]


def admin_list_chats(status='active'):
    """Groups for the panel: name, players, size, and when anybody last spoke.
    `status` is 'active', 'inactive' or 'all'."""
    where = {'active': 'WHERE COALESCE(c.active, TRUE)',
             'inactive': 'WHERE NOT COALESCE(c.active, TRUE)'}.get(status, '')
    sql = (
        "SELECT c.chat_id, c.title, COALESCE(c.active, TRUE), c.deactivated_reason, "
        "       EXTRACT(EPOCH FROM (now() - c.last_seen_at))::bigint, "
        "       COALESCE(u.players, 0), COALESCE(u.total, 0) "
        "FROM chats c "
        "LEFT JOIN (SELECT chat_id, COUNT(*) players, SUM(GREATEST(size,0)) total "
        "           FROM users GROUP BY chat_id) u ON u.chat_id = c.chat_id "
        + where +
        " ORDER BY COALESCE(u.total, 0) DESC, c.chat_id")
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(sql)
        return cur.fetchall()


# Every table keyed by chat_id. Deleting a group means deleting its league: there is no
# archive here, and the size in it is gone for good.
CHAT_SCOPED_TABLES = (
    'size_log', 'bank_log', 'events', 'night_report_sections', 'night_reports',
    'rebalance_log', 'crypto_holdings', 'bank_accounts', 'inventory', 'achievements',
    'consensus_vote_casts', 'consensus_votes', 'consensus_protection', 'boss_hits',
    'bosses', 'lottery_tickets', 'pvp_match_bets', 'pvp_matches', 'heist_attempts',
    'loans', 'decree_log', 'shop_purchases', 'shop_item_state', 'star_orders',
    'kingdom', 'economy', 'bank_treasury', 'claimed_challenges', 'chat_instances',
    'pending_decrees', 'open_challenges', 'users', 'chats',
)

# Nothing may be keyed by chat_id and missing from that tuple, or a deleted group leaves
# rows behind that still answer queries about it - a stale chat_instances row in
# particular would keep resolving an inline button into a league that no longer exists.
# There is a regression test that diffs the tuple against information_schema.


def delete_chat(chat_id):
    """Erase a group and every row belonging to it, in ONE transaction.

    Irreversible, and it destroys real players' size - the panel makes the caller type
    the group id to confirm. A table that does not exist is skipped rather than failing
    the whole delete.
    """
    removed = {}
    with get_connection() as conn:
        c = conn.cursor()
        for table in CHAT_SCOPED_TABLES:
            c.execute('SELECT 1 FROM information_schema.columns '
                      'WHERE table_name = %s AND column_name = %s', (table, 'chat_id'))
            if not c.fetchone():
                continue
            c.execute('DELETE FROM ' + table + ' WHERE chat_id = %s', (chat_id,))
            if c.rowcount:
                removed[table] = c.rowcount
    return removed


def get_user(user_id, chat_id, username, first_name):
    with get_connection() as conn:
        c = conn.cursor()
        c.execute('SELECT size, last_grown, perk FROM users WHERE user_id = %s AND chat_id = %s', (user_id, chat_id))
        row = c.fetchone()
        if row is None:
            c.execute('INSERT INTO users (user_id, chat_id, username, first_name) VALUES (%s, %s, %s, %s)',
                      (user_id, chat_id, username, first_name))
            return (0.0, '', 'عادی')

        # Update username/first_name if they changed (only if not None)
        if username is not None or first_name is not None:
            updates = []
            params = []
            if username is not None:
                updates.append('username = %s')
                params.append(username)
            if first_name is not None:
                updates.append('first_name = %s')
                params.append(first_name)
            params.extend([user_id, chat_id])
            c.execute(f'UPDATE users SET {", ".join(updates)} WHERE user_id = %s AND chat_id = %s', params)
        # A NULL size should never happen through normal gameplay (columns default to 0),
        # but guard against a stray row (e.g. a manual DB edit) crashing every numeric
        # comparison callers make against this value.
        if row[0] is None:
            row = (0.0,) + row[1:]
        # Perks only last the day they were rolled (Iran time). A perk is granted
        # together with the daily growth, so last_grown IS the perk's date: past
        # Tehran midnight it reads back as عادی until the user grows again.
        if row[2] != 'عادی' and row[1] != _tehran_today_str():
            row = row[:2] + ('عادی',)
        return row


DOSE_COOLDOWN_HOURS = 24


def try_claim_dose(target_id, chat_id):
    """Atomically claims the target's once-per-24h slot for a ویاگرا / قرص اورژانسی.

    Returns (True, None) if the item may be applied, or (False, seconds_remaining) if
    they've already been dosed inside the window. The claim and the check are one
    statement so two givers hitting the same target at once can't both get through."""
    with get_connection() as conn:
        c = conn.cursor()
        c.execute(
            'UPDATE users SET last_dosed_at = now() '
            'WHERE user_id = %s AND chat_id = %s '
            'AND (last_dosed_at IS NULL OR last_dosed_at < now() - make_interval(hours => %s))',
            (target_id, chat_id, DOSE_COOLDOWN_HOURS)
        )
        if c.rowcount > 0:
            return True, None
        c.execute(
            'SELECT EXTRACT(EPOCH FROM (last_dosed_at + make_interval(hours => %s) - now())) '
            'FROM users WHERE user_id = %s AND chat_id = %s',
            (DOSE_COOLDOWN_HOURS, target_id, chat_id)
        )
        row = c.fetchone()
        return False, int(row[0]) if row and row[0] else 0


def release_dose(target_id, chat_id):
    """Hands the slot back when an item couldn't actually be applied after claiming."""
    with get_connection() as conn:
        c = conn.cursor()
        c.execute('UPDATE users SET last_dosed_at = NULL WHERE user_id = %s AND chat_id = %s',
                  (target_id, chat_id))


DONATION_MIN_DAYS = 7


def get_donation_wait_remaining(user_id, chat_id):
    """Returns a timedelta if this user still needs to wait before they can use /dd in this
    group (within DONATION_MIN_DAYS of their first activity here), else None."""
    with get_connection() as conn:
        c = conn.cursor()
        c.execute(
            "SELECT joined_at + make_interval(days => %s) - now() FROM users "
            "WHERE user_id = %s AND chat_id = %s AND joined_at + make_interval(days => %s) > now()",
            (DONATION_MIN_DAYS, user_id, chat_id, DONATION_MIN_DAYS)
        )
        row = c.fetchone()
        return row[0] if row else None


def record_match_result(winner_id, loser_id, chat_id):
    """Increments the winner's wins and the loser's losses for a decided (non-tie) challenge/rematch."""
    with get_connection() as conn:
        c = conn.cursor()
        c.execute("UPDATE users SET wins = wins + 1 WHERE user_id = %s AND chat_id = %s", (winner_id, chat_id))
        c.execute("UPDATE users SET losses = losses + 1 WHERE user_id = %s AND chat_id = %s", (loser_id, chat_id))


def get_win_loss(user_id, chat_id):
    """Returns (wins, losses) for a user in a group, defaulting to (0, 0) if they have no row."""
    with get_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT wins, losses FROM users WHERE user_id = %s AND chat_id = %s", (user_id, chat_id))
        row = c.fetchone()
        return row if row else (0, 0)


def get_global_user(user_id, username, first_name):
    with get_connection() as conn:
        c = conn.cursor()
        c.execute('SELECT SUM(size) FROM users WHERE user_id = %s', (user_id,))
        total_size = c.fetchone()[0]
    if total_size is None:
        return 0.0
    return total_size


def set_user_perk(user_id, chat_id, perk):
    with get_connection() as conn:
        c = conn.cursor()
        c.execute('UPDATE users SET perk = %s WHERE user_id = %s AND chat_id = %s', (perk, user_id, chat_id))


def find_user_by_username(username, chat_id):
    # Telegram usernames are case-insensitive, so @Ali_Reza and @ali_reza are the same
    # account - an exact match made targeting fail on any capitalization mismatch.
    username = username.replace('@', '')
    with get_connection() as conn:
        c = conn.cursor()
        c.execute('SELECT user_id, first_name, size FROM users WHERE lower(username) = lower(%s) AND chat_id = %s', (username, chat_id))
        row = c.fetchone()
        if row and row[2] is None:
            row = (row[0], row[1], 0.0)
        return row


def get_user_info(user_id, chat_id):
    with get_connection() as conn:
        c = conn.cursor()
        c.execute('SELECT first_name, size FROM users WHERE user_id = %s AND chat_id = %s', (user_id, chat_id))
        row = c.fetchone()
        if row and row[1] is None:
            row = (row[0], 0.0)
        return row


def get_top_users(chat_id, limit=None):
    """Returns every player in the group ordered by size, unless limit caps it."""
    with get_connection() as conn:
        c = conn.cursor()
        if limit:
            c.execute('SELECT first_name, size FROM users WHERE chat_id = %s ORDER BY size DESC NULLS LAST LIMIT %s', (chat_id, limit))
        else:
            c.execute('SELECT first_name, size FROM users WHERE chat_id = %s ORDER BY size DESC NULLS LAST', (chat_id,))
        return c.fetchall()


def get_global_top_users(limit=10):
    with get_connection() as conn:
        c = conn.cursor()
        # Need to group by user_id to sum sizes
        c.execute('SELECT MAX(first_name), SUM(size) as total_size FROM users GROUP BY user_id ORDER BY total_size DESC LIMIT %s', (limit,))
        return c.fetchall()


def get_random_victim(chat_id, exclude_user_id, min_size):
    """Pick a random other user in the group with size > min_size. Returns (user_id, first_name, size) or None."""
    with get_connection() as conn:
        c = conn.cursor()
        c.execute(
            'SELECT user_id, first_name, size FROM users '
            'WHERE chat_id = %s AND user_id != %s AND size > %s '
            'ORDER BY random() LIMIT 1',
            (chat_id, exclude_user_id, min_size)
        )
        return c.fetchone()


def get_user_rank(user_id, chat_id):
    with get_connection() as conn:
        c = conn.cursor()
        c.execute('SELECT size FROM users WHERE user_id = %s AND chat_id = %s', (user_id, chat_id))
        row = c.fetchone()
        if row is None:
            return '-'
        c.execute('SELECT COUNT(*) FROM users WHERE chat_id = %s AND size > %s', (chat_id, row[0]))
        higher = c.fetchone()[0]
        return higher + 1


_THIS_FILE = os.path.abspath(__file__)


def _caller_name():
    """Name of the first function *outside this module* on the stack, used as the audit
    `source` on every ledger row.

    Walking until the frame leaves db.py matters: every public function here is wrapped
    by _retry_transient, so a fixed depth lands on that wrapper and records the same
    meaningless name for every single row. sys._getframe is used rather than
    inspect.stack() because the latter reads source files off disk on each call, which
    is far too heavy for something on the path of every payout."""
    try:
        frame = sys._getframe(1)
        for _ in range(12):
            if frame is None:
                break
            if os.path.abspath(frame.f_code.co_filename) != _THIS_FILE:
                return frame.f_code.co_name
            frame = frame.f_back
    except Exception:
        pass
    return "unknown"


def update_size(user_id, chat_id, size_delta, current_date_str=None, note=None):
    # A single relative UPDATE, not read-then-write: with concurrent_updates(True)
    # two handlers settling money for the same user at once (e.g. a bet payout and
    # a donation) must both land instead of one silently overwriting the other.
    source = _caller_name()
    with get_connection() as conn:
        c = conn.cursor()
        if current_date_str:
            c.execute('UPDATE users SET size = COALESCE(size, 0) + %s, last_grown = %s '
                      'WHERE user_id = %s AND chat_id = %s RETURNING size',
                      (size_delta, current_date_str, user_id, chat_id))
        else:
            c.execute('UPDATE users SET size = COALESCE(size, 0) + %s '
                      'WHERE user_id = %s AND chat_id = %s RETURNING size',
                      (size_delta, user_id, chat_id))
        row = c.fetchone()
        if row is not None:
            # Same transaction as the balance change, so the ledger can never disagree
            # with the balance it is describing.
            c.execute('INSERT INTO size_log (chat_id, user_id, delta, balance_after, source, note) '
                      'VALUES (%s, %s, %s, %s, %s, %s)',
                      (chat_id, user_id, size_delta, row[0], source, note))


def try_deduct_size(user_id, chat_id, amount, note=None):
    """Atomically escrows `amount` out of a user's size, refusing (returns False) if
    their balance is short or they have no row. The balance check and the deduction
    are one UPDATE, so two concurrent stakes can never both spend the same centimeters."""
    source = _caller_name()
    with get_connection() as conn:
        c = conn.cursor()
        c.execute(
            'UPDATE users SET size = COALESCE(size, 0) - %s '
            'WHERE user_id = %s AND chat_id = %s AND COALESCE(size, 0) >= %s RETURNING size',
            (amount, user_id, chat_id, amount)
        )
        row = c.fetchone()
        if row is None:
            return False
        c.execute('INSERT INTO size_log (chat_id, user_id, delta, balance_after, source, note) '
                  'VALUES (%s, %s, %s, %s, %s, %s)',
                  (chat_id, user_id, -amount, row[0], source, note))
        return True


def claim_challenge(nonce):
    """Atomically claims a challenge button by the nonce in its callback_data. Returns
    True only for the caller that won the race; everyone else tapping the same button
    (including the same user double-tapping) gets False."""
    with get_connection() as conn:
        c = conn.cursor()
        c.execute('INSERT INTO claimed_challenges (nonce) VALUES (%s) ON CONFLICT (nonce) DO NOTHING', (nonce,))
        return c.rowcount > 0


def release_challenge(nonce):
    """Un-claims a challenge whose acceptance couldn't be completed (e.g. the acceptor
    turned out to be short on size), so the button stays tappable by someone else."""
    with get_connection() as conn:
        c = conn.cursor()
        c.execute('DELETE FROM claimed_challenges WHERE nonce = %s', (nonce,))


def get_user_active_item(user_id, chat_id):
    with get_connection() as conn:
        c = conn.cursor()
        c.execute('SELECT active_item FROM users WHERE user_id = %s AND chat_id = %s', (user_id, chat_id))
        row = c.fetchone()
        return row[0] if row else ""


def set_user_active_item(user_id, chat_id, item_name):
    with get_connection() as conn:
        c = conn.cursor()
        c.execute('UPDATE users SET active_item = %s WHERE user_id = %s AND chat_id = %s', (item_name, user_id, chat_id))


def add_inventory(user_id, chat_id, item_name, amount=1):
    with get_connection() as conn:
        c = conn.cursor()
        c.execute(
            'INSERT INTO inventory (user_id, chat_id, item_name, quantity) VALUES (%s, %s, %s, %s) '
            'ON CONFLICT (user_id, chat_id, item_name) DO UPDATE SET quantity = inventory.quantity + EXCLUDED.quantity',
            (user_id, chat_id, item_name, amount)
        )


def get_inventory(user_id, chat_id):
    with get_connection() as conn:
        c = conn.cursor()
        c.execute('SELECT item_name, quantity FROM inventory WHERE user_id = %s AND chat_id = %s AND quantity > 0', (user_id, chat_id))
        return c.fetchall()


def use_inventory(user_id, chat_id, item_name):
    # Check-and-decrement in one UPDATE so two concurrent uses of a last remaining
    # item can't both succeed and drive the quantity negative.
    with get_connection() as conn:
        c = conn.cursor()
        c.execute(
            'UPDATE inventory SET quantity = quantity - 1 '
            'WHERE user_id = %s AND chat_id = %s AND item_name = %s AND quantity > 0',
            (user_id, chat_id, item_name)
        )
        return c.rowcount > 0


def clear_user_active_item(user_id, chat_id):
    with get_connection() as conn:
        c = conn.cursor()
        c.execute("UPDATE users SET active_item = '' WHERE user_id = %s AND chat_id = %s", (user_id, chat_id))


def get_user_active_theft_item(user_id, chat_id):
    """The item armed for the player's next /dozdi attempt, or '' if none."""
    with get_connection() as conn:
        c = conn.cursor()
        c.execute('SELECT COALESCE(active_theft_item, %s) FROM users WHERE user_id = %s AND chat_id = %s',
                  ('', user_id, chat_id))
        row = c.fetchone()
        return row[0] if row else ''


def set_user_active_theft_item(user_id, chat_id, item_name):
    with get_connection() as conn:
        c = conn.cursor()
        c.execute('UPDATE users SET active_theft_item = %s WHERE user_id = %s AND chat_id = %s',
                  (item_name, user_id, chat_id))


def clear_user_active_theft_item(user_id, chat_id):
    with get_connection() as conn:
        c = conn.cursor()
        c.execute('UPDATE users SET active_theft_item = %s WHERE user_id = %s AND chat_id = %s',
                  ('', user_id, chat_id))



def get_active_today_count(chat_id, today_str):
    """Counts only members who grew (used /d) today - the pool اجماع quorum is based on."""
    with get_connection() as conn:
        c = conn.cursor()
        c.execute('SELECT COUNT(*) FROM users WHERE chat_id = %s AND last_grown = %s', (chat_id, today_str))
        return c.fetchone()[0]


def set_consensus_protection(chat_id, target_id, target_name, days, reason):
    """Upserts a row in consensus_protection - editable directly in Supabase's Table
    Editor to lift a protection early (delete the row) or extend/shorten it (edit
    protected_until)."""
    with get_connection() as conn:
        c = conn.cursor()
        c.execute(
            "INSERT INTO consensus_protection (chat_id, target_id, target_name, protected_until, reason) "
            "VALUES (%s, %s, %s, now() + make_interval(days => %s), %s) "
            "ON CONFLICT (chat_id, target_id) DO UPDATE SET "
            "target_name = EXCLUDED.target_name, protected_until = EXCLUDED.protected_until, reason = EXCLUDED.reason",
            (chat_id, target_id, target_name, days, reason)
        )


def get_consensus_protection_remaining(chat_id, target_id):
    """Returns a timedelta if the target is still protected (per consensus_protection), else None."""
    with get_connection() as conn:
        c = conn.cursor()
        c.execute(
            "SELECT protected_until - now() FROM consensus_protection "
            "WHERE chat_id = %s AND target_id = %s AND protected_until > now()",
            (chat_id, target_id)
        )
        row = c.fetchone()
        return row[0] if row else None


def get_open_consensus(chat_id, target_id):
    """Returns (id, seconds_since_created) for an open consensus against this target, or None."""
    with get_connection() as conn:
        c = conn.cursor()
        c.execute(
            "SELECT id, EXTRACT(EPOCH FROM (now() - created_at)) FROM consensus_votes "
            "WHERE chat_id = %s AND target_id = %s AND status = 'open'",
            (chat_id, target_id)
        )
        return c.fetchone()


def get_expired_open_consensus(window_seconds):
    """Open votes whose one-hour window has already elapsed - i.e. their timeout job was
    lost to a restart. Returns (id, chat_id, target_id, target_name) for each."""
    with get_connection() as conn:
        c = conn.cursor()
        c.execute(
            "SELECT id, chat_id, target_id, target_name FROM consensus_votes "
            "WHERE status = 'open' AND created_at < now() - make_interval(secs => %s)",
            (window_seconds,)
        )
        return c.fetchall()


def fail_open_consensus(chat_id, target_id, target_name):
    with get_connection() as conn:
        c = conn.cursor()
        c.execute(
            "UPDATE consensus_votes SET status = 'failed', resolved_at = now() "
            "WHERE chat_id = %s AND target_id = %s AND status = 'open'",
            (chat_id, target_id)
        )
    set_consensus_protection(chat_id, target_id, target_name, 3, 'failed')


def create_consensus(chat_id, target_id, target_name, initiator_id, initiator_name, amount, required_votes, total_players):
    with get_connection() as conn:
        c = conn.cursor()
        c.execute(
            "INSERT INTO consensus_votes (chat_id, target_id, target_name, initiator_id, amount, required_votes, total_players, status) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, 'open') RETURNING id",
            (chat_id, target_id, target_name, initiator_id, amount, required_votes, total_players)
        )
        vote_id = c.fetchone()[0]
        c.execute(
            'INSERT INTO consensus_vote_casts (vote_id, user_id, first_name, choice) VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING',
            (vote_id, initiator_id, initiator_name, 'yes')
        )
        return vote_id


def get_consensus(vote_id):
    """Returns (chat_id, target_id, target_name, initiator_id, amount, required_votes,
    total_players, status, seconds_since_created) or None."""
    with get_connection() as conn:
        c = conn.cursor()
        c.execute(
            'SELECT chat_id, target_id, target_name, initiator_id, amount, required_votes, total_players, status, '
            'EXTRACT(EPOCH FROM (now() - created_at)) '
            'FROM consensus_votes WHERE id = %s',
            (vote_id,)
        )
        return c.fetchone()


def cast_consensus_vote(vote_id, user_id, first_name, choice):
    """Returns True if this vote was newly recorded, False if the user already voted."""
    with get_connection() as conn:
        c = conn.cursor()
        c.execute(
            'INSERT INTO consensus_vote_casts (vote_id, user_id, first_name, choice) VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING',
            (vote_id, user_id, first_name, choice)
        )
        return c.rowcount > 0


def get_consensus_vote_counts(vote_id):
    with get_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT choice, COUNT(*) FROM consensus_vote_casts WHERE vote_id = %s GROUP BY choice", (vote_id,))
        counts = dict(c.fetchall())
        return counts.get('yes', 0), counts.get('no', 0)


def get_consensus_voters(vote_id):
    with get_connection() as conn:
        c = conn.cursor()
        c.execute(
            'SELECT first_name, choice FROM consensus_vote_casts WHERE vote_id = %s ORDER BY user_id',
            (vote_id,)
        )
        return c.fetchall()


def resolve_consensus_success(vote_id, chat_id, target_id, target_name):
    """Atomically flips an open consensus to succeeded and grants 6 days of protection.
    Returns True only for the caller that won the race."""
    with get_connection() as conn:
        c = conn.cursor()
        c.execute(
            "UPDATE consensus_votes SET status = 'succeeded', resolved_at = now() WHERE id = %s AND status = 'open'",
            (vote_id,)
        )
        won = c.rowcount > 0
    if won:
        set_consensus_protection(chat_id, target_id, target_name, 6, 'succeeded')
    return won














def create_pvp_match(match_id, chat_id, challenger_id, challenger_name, acceptor_id, acceptor_name, bet):
    with get_connection() as conn:
        c = conn.cursor()
        c.execute(
            'INSERT INTO pvp_matches (id, chat_id, challenger_id, challenger_name, acceptor_id, acceptor_name, bet) '
            'VALUES (%s, %s, %s, %s, %s, %s, %s)',
            (match_id, chat_id, challenger_id, challenger_name, acceptor_id, acceptor_name, bet)
        )


def set_pvp_match_message(match_id, message_id=None, inline_message_id=None):
    with get_connection() as conn:
        c = conn.cursor()
        c.execute(
            'UPDATE pvp_matches SET message_id = %s, inline_message_id = %s WHERE id = %s',
            (message_id, inline_message_id, match_id)
        )


def get_pvp_match(match_id):
    """Returns (chat_id, challenger_id, challenger_name, acceptor_id, acceptor_name, bet,
    message_id, inline_message_id, status) or None."""
    with get_connection() as conn:
        c = conn.cursor()
        c.execute(
            'SELECT chat_id, challenger_id, challenger_name, acceptor_id, acceptor_name, bet, '
            'message_id, inline_message_id, status FROM pvp_matches WHERE id = %s',
            (match_id,)
        )
        return c.fetchone()


def claim_pvp_match(match_id):
    """Atomically flips a pending match straight to 'resolved' so the scheduled job and
    the startup-recovery sweep can never both settle (or double-pay) the same match.
    Returns True only for the caller that won the race."""
    with get_connection() as conn:
        c = conn.cursor()
        c.execute("UPDATE pvp_matches SET status = 'resolved' WHERE id = %s AND status = 'pending'", (match_id,))
        return c.rowcount > 0


def place_pvp_bet(match_id, user_id, first_name, side, amount):
    """Returns True if the bet was recorded, False if this user already has a bet on
    this match (two rapid taps used to raise a PK violation AFTER the stake was
    already escrowed, silently eating the second stake)."""
    with get_connection() as conn:
        c = conn.cursor()
        c.execute(
            'INSERT INTO pvp_match_bets (match_id, user_id, first_name, side, amount) VALUES (%s, %s, %s, %s, %s) '
            'ON CONFLICT (match_id, user_id) DO NOTHING',
            (match_id, user_id, first_name, side, amount)
        )
        return c.rowcount > 0


def get_pvp_bets(match_id):
    """Returns (user_id, side, amount, first_name) for every bet on this match."""
    with get_connection() as conn:
        c = conn.cursor()
        c.execute(
            'SELECT user_id, side, amount, first_name FROM pvp_match_bets WHERE match_id = %s',
            (match_id,)
        )
        return c.fetchall()


def get_stale_pending_pvp_matches(window_seconds):
    """Returns the id of every match whose betting window closed before this call was
    made - i.e. the process died before ever running (or scheduling) its resolution,
    leaving it orphaned mid-flight. Picked up once at startup to settle them instead of
    leaving the group staring at a dead betting message forever."""
    with get_connection() as conn:
        c = conn.cursor()
        c.execute(
            "SELECT id FROM pvp_matches WHERE status = 'pending' AND created_at < now() - make_interval(secs => %s)",
            (window_seconds,)
        )
        return [r[0] for r in c.fetchall()]


# ---------------------------------------------------------------- streaks

def claim_daily_growth_with_streak(user_id, chat_id, today_str, yesterday_str):
    """Atomically stamps today's growth and rolls the streak forward in the same
    statement: +1 if they also grew yesterday, otherwise back to 1. Returns the new
    streak, or None if they had already grown today (so a double tap changes nothing)."""
    with get_connection() as conn:
        c = conn.cursor()
        c.execute(
            'UPDATE users SET last_grown = %s, '
            'streak = CASE WHEN last_grown = %s THEN COALESCE(streak, 0) + 1 ELSE 1 END '
            'WHERE user_id = %s AND chat_id = %s AND last_grown IS DISTINCT FROM %s '
            'RETURNING streak',
            (today_str, yesterday_str, user_id, chat_id, today_str)
        )
        row = c.fetchone()
        if not row:
            return None
        c.execute(
            'UPDATE users SET best_streak = GREATEST(COALESCE(best_streak, 0), COALESCE(streak, 0)) '
            'WHERE user_id = %s AND chat_id = %s',
            (user_id, chat_id)
        )
        return row[0]


def get_streak(user_id, chat_id):
    with get_connection() as conn:
        c = conn.cursor()
        c.execute('SELECT COALESCE(streak, 0), COALESCE(best_streak, 0) FROM users WHERE user_id = %s AND chat_id = %s',
                  (user_id, chat_id))
        return c.fetchone() or (0, 0)


def get_top_users_full(chat_id):
    """Leaderboard rows with the extras the renderer decorates names with:
    (user_id, first_name, size, streak)."""
    with get_connection() as conn:
        c = conn.cursor()
        c.execute(
            'SELECT user_id, first_name, size, COALESCE(streak, 0) FROM users '
            # user_id is the tie-break so equal sizes give a stable winner instead of
            # whatever heap order Postgres happens to return - an unstable order here
            # would re-crown (and so re-eject the consort) on every refresh.
            'WHERE chat_id = %s ORDER BY size DESC NULLS LAST, user_id ASC',
            (chat_id,)
        )
        return c.fetchall()


# ---------------------------------------------------------------- theft

def try_start_theft(user_id, chat_id, cooldown_seconds):
    """Stamps the theft clock only if the cooldown has elapsed, so spamming /dozdi
    can't get two attempts in. Returns (True, None) when the attempt may proceed, or
    (False, seconds_remaining) when it's still on cooldown."""
    with get_connection() as conn:
        c = conn.cursor()
        c.execute(
            'UPDATE users SET last_theft_at = now() '
            'WHERE user_id = %s AND chat_id = %s '
            'AND (last_theft_at IS NULL OR last_theft_at < now() - make_interval(secs => %s))',
            (user_id, chat_id, cooldown_seconds)
        )
        if c.rowcount > 0:
            return True, None
        c.execute(
            'SELECT EXTRACT(EPOCH FROM (last_theft_at + make_interval(secs => %s) - now())) '
            'FROM users WHERE user_id = %s AND chat_id = %s',
            (cooldown_seconds, user_id, chat_id)
        )
        row = c.fetchone()
        return False, int(row[0]) if row and row[0] else 0


def mark_traitor(user_id, chat_id, days):
    with get_connection() as conn:
        c = conn.cursor()
        c.execute(
            'UPDATE users SET traitor_until = now() + make_interval(days => %s) WHERE user_id = %s AND chat_id = %s',
            (days, user_id, chat_id)
        )


def is_traitor(user_id, chat_id):
    with get_connection() as conn:
        c = conn.cursor()
        c.execute(
            'SELECT 1 FROM users WHERE user_id = %s AND chat_id = %s AND traitor_until > now()',
            (user_id, chat_id)
        )
        return c.fetchone() is not None


# ---------------------------------------------------------------- kingdom

def get_kingdom(chat_id):
    """Returns (king_id, king_name, consort_id, consort_name, last_consort_date,
    last_tax_date) for a group, or None if no one has been crowned there yet."""
    with get_connection() as conn:
        c = conn.cursor()
        c.execute(
            'SELECT king_id, king_name, consort_id, consort_name, '
            "COALESCE(last_consort_date, ''), COALESCE(last_tax_date, '') "
            'FROM kingdom WHERE chat_id = %s',
            (chat_id,)
        )
        return c.fetchone()


def crown_king(chat_id, king_id, king_name):
    """Crowns a new king. A change of ruler empties the throne's other seat too - the
    consort belongs to the crown, not to the person, so a new king starts unpartnered."""
    with get_connection() as conn:
        c = conn.cursor()
        c.execute(
            'INSERT INTO kingdom (chat_id, king_id, king_name, crowned_at) VALUES (%s, %s, %s, now()) '
            'ON CONFLICT (chat_id) DO UPDATE SET king_id = EXCLUDED.king_id, '
            'king_name = EXCLUDED.king_name, crowned_at = now(), '
            'consort_id = CASE WHEN kingdom.king_id IS DISTINCT FROM EXCLUDED.king_id THEN NULL ELSE kingdom.consort_id END, '
            'consort_name = CASE WHEN kingdom.king_id IS DISTINCT FROM EXCLUDED.king_id THEN NULL ELSE kingdom.consort_name END, '
            # The once-a-day appointment limit belongs to the ruler, not the group: a
            # brand-new king must be able to appoint on their coronation day even if
            # the previous king already used the group's slot that morning.
            "last_consort_date = CASE WHEN kingdom.king_id IS DISTINCT FROM EXCLUDED.king_id THEN '' ELSE kingdom.last_consort_date END",
            (chat_id, king_id, king_name)
        )


def set_consort(chat_id, king_id, consort_id, consort_name, today_str):
    """Seats a consort, but only for the current king and only once per Tehran day.
    Returns True if it took effect."""
    with get_connection() as conn:
        c = conn.cursor()
        c.execute(
            'UPDATE kingdom SET consort_id = %s, consort_name = %s, consort_since = now(), '
            'last_consort_date = %s '
            'WHERE chat_id = %s AND king_id = %s AND COALESCE(last_consort_date, %s) <> %s',
            (consort_id, consort_name, today_str, chat_id, king_id, '', today_str)
        )
        return c.rowcount > 0


def clear_consort(chat_id):
    """Empties the consort seat and reports whether anyone was actually sitting in it,
    so only the caller that really removed someone announces a betrayal/divorce."""
    with get_connection() as conn:
        c = conn.cursor()
        c.execute(
            'UPDATE kingdom SET consort_id = NULL, consort_name = NULL, consort_since = NULL '
            'WHERE chat_id = %s AND consort_id IS NOT NULL',
            (chat_id,)
        )
        return c.rowcount > 0


def mark_tax_collected(chat_id, today_str):
    """Claims the day's tax for this group. Returns True only for the first caller, so
    a re-run of the midnight job can't tax everyone twice."""
    with get_connection() as conn:
        c = conn.cursor()
        c.execute(
            'UPDATE kingdom SET last_tax_date = %s WHERE chat_id = %s AND COALESCE(last_tax_date, %s) <> %s',
            (today_str, chat_id, '', today_str)
        )
        return c.rowcount > 0


def get_taxable_players(chat_id, king_id, min_size):
    """Everyone in the group who can actually afford to be taxed."""
    with get_connection() as conn:
        c = conn.cursor()
        c.execute(
            'SELECT user_id, first_name, size FROM users '
            'WHERE chat_id = %s AND user_id <> %s AND size >= %s',
            (chat_id, king_id, min_size)
        )
        return c.fetchall()


# ---------------------------------------------------------------- boss

def spawn_boss(chat_id, name, hp, spawn_date):
    """Creates the day's boss unless this group already has one alive or already had
    one today. Returns the new boss id, or None if neither applies."""
    with get_connection() as conn:
        c = conn.cursor()
        c.execute(
            "SELECT 1 FROM bosses WHERE chat_id = %s AND (status = 'alive' OR spawn_date = %s)",
            (chat_id, spawn_date)
        )
        if c.fetchone():
            return None
        c.execute(
            'INSERT INTO bosses (chat_id, name, max_hp, hp, spawn_date) VALUES (%s, %s, %s, %s, %s) RETURNING id',
            (chat_id, name, hp, hp, spawn_date)
        )
        return c.fetchone()[0]


def set_boss_message(boss_id, message_id):
    with get_connection() as conn:
        c = conn.cursor()
        c.execute('UPDATE bosses SET message_id = %s WHERE id = %s', (message_id, boss_id))


def get_boss(boss_id):
    """Returns (chat_id, name, max_hp, hp, status, message_id) or None."""
    with get_connection() as conn:
        c = conn.cursor()
        c.execute('SELECT chat_id, name, max_hp, hp, status, message_id FROM bosses WHERE id = %s', (boss_id,))
        return c.fetchone()


def hit_boss(boss_id, user_id, first_name, damage):
    """Records one player's single hit and applies it to the boss's HP in the same
    transaction. Returns (accepted, remaining_hp): accepted is False when this player
    has already hit this boss, in which case no damage is applied."""
    with get_connection() as conn:
        c = conn.cursor()
        c.execute(
            'INSERT INTO boss_hits (boss_id, user_id, first_name, damage) VALUES (%s, %s, %s, %s) '
            'ON CONFLICT (boss_id, user_id) DO NOTHING',
            (boss_id, user_id, first_name, damage)
        )
        if c.rowcount == 0:
            return False, None
        c.execute(
            "UPDATE bosses SET hp = GREATEST(0, hp - %s) WHERE id = %s AND status = 'alive' RETURNING hp",
            (damage, boss_id)
        )
        row = c.fetchone()
        if row is None:
            # The boss died or escaped between the insert and the damage. Take the hit
            # row back out rather than reporting a landed hit that dealt nothing, which
            # would silently burn the player's single attack.
            c.execute('DELETE FROM boss_hits WHERE boss_id = %s AND user_id = %s', (boss_id, user_id))
            return False, None
        return True, row[0]


def claim_boss_kill(boss_id):
    """Flips a boss to 'dead' exactly once, so only one hit triggers the payout."""
    with get_connection() as conn:
        c = conn.cursor()
        c.execute("UPDATE bosses SET status = 'dead' WHERE id = %s AND status = 'alive' AND hp <= 0", (boss_id,))
        return c.rowcount > 0


def get_boss_hits(boss_id):
    with get_connection() as conn:
        c = conn.cursor()
        c.execute('SELECT user_id, first_name, damage FROM boss_hits WHERE boss_id = %s ORDER BY damage DESC', (boss_id,))
        return c.fetchall()


def expire_bosses():
    """Marks every still-alive boss as escaped. Returns (id, chat_id, name, message_id,
    max_hp, hp) for each, so the group can be told it got away."""
    with get_connection() as conn:
        c = conn.cursor()
        c.execute(
            "UPDATE bosses SET status = 'escaped' WHERE status = 'alive' "
            'RETURNING id, chat_id, name, message_id, max_hp, hp'
        )
        return c.fetchall()


# ---------------------------------------------------------------- lottery

def buy_lottery_tickets(chat_id, draw_date, user_id, first_name, tickets, paid=None):
    """Adds `tickets` entries to a day's pot. `paid` is what the player actually spent;
    it defaults to full price so existing callers keep their old meaning. A bonus entry
    passes paid=0 - it buys odds, not prize money."""
    if paid is None:
        paid = tickets * 10
    with get_connection() as conn:
        c = conn.cursor()
        c.execute(
            'INSERT INTO lottery_tickets (chat_id, draw_date, user_id, first_name, tickets, paid) '
            'VALUES (%s, %s, %s, %s, %s, %s) '
            'ON CONFLICT (chat_id, draw_date, user_id) DO UPDATE SET '
            'tickets = lottery_tickets.tickets + EXCLUDED.tickets, '
            'paid = COALESCE(lottery_tickets.paid, 0) + EXCLUDED.paid, '
            'first_name = EXCLUDED.first_name',
            (chat_id, draw_date, user_id, first_name, tickets, paid)
        )


def get_lottery_entries(chat_id, draw_date):
    with get_connection() as conn:
        c = conn.cursor()
        c.execute(
            'SELECT user_id, first_name, tickets, COALESCE(paid, tickets * 10) FROM lottery_tickets '
            'WHERE chat_id = %s AND draw_date = %s AND tickets > 0 ORDER BY user_id',
            (chat_id, draw_date)
        )
        return c.fetchall()


def claim_lottery_draw(chat_id, draw_date):
    """Deletes and returns the day's entries in one statement, so the draw can only
    ever pay out once even if the midnight job runs twice."""
    with get_connection() as conn:
        c = conn.cursor()
        c.execute(
            'DELETE FROM lottery_tickets WHERE chat_id = %s AND draw_date = %s AND tickets > 0 '
            'RETURNING user_id, first_name, tickets, COALESCE(paid, tickets * 10)',
            (chat_id, draw_date)
        )
        return sorted(c.fetchall(), key=lambda r: r[0])


def get_lottery_chats(draw_date):
    with get_connection() as conn:
        c = conn.cursor()
        c.execute('SELECT DISTINCT chat_id FROM lottery_tickets WHERE draw_date = %s', (draw_date,))
        return [r[0] for r in c.fetchall()]


def get_pending_lottery_draws(before_date):
    """Every (chat_id, draw_date) whose draw never happened - the midnight job was
    missed (a restart, an outage), and the tickets were already paid for. Without this
    sweep that pot would sit escrowed forever."""
    with get_connection() as conn:
        c = conn.cursor()
        c.execute(
            'SELECT DISTINCT chat_id, draw_date FROM lottery_tickets '
            'WHERE draw_date < %s AND tickets > 0 ORDER BY draw_date',
            (before_date,)
        )
        return c.fetchall()


# ---------------------------------------------------------------- achievements

def grant_achievement(user_id, chat_id, code):
    """Returns True only the first time a player earns a badge, so the announcement
    fires once and never again."""
    with get_connection() as conn:
        c = conn.cursor()
        c.execute(
            'INSERT INTO achievements (user_id, chat_id, code) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING',
            (user_id, chat_id, code)
        )
        return c.rowcount > 0


def get_achievements(user_id, chat_id):
    with get_connection() as conn:
        c = conn.cursor()
        c.execute('SELECT code FROM achievements WHERE user_id = %s AND chat_id = %s', (user_id, chat_id))
        return [r[0] for r in c.fetchall()]


def get_achievement_counts(chat_id):
    with get_connection() as conn:
        c = conn.cursor()
        c.execute('SELECT user_id, count(*) FROM achievements WHERE chat_id = %s GROUP BY user_id', (chat_id,))
        return dict(c.fetchall())


def get_all_players(chat_id):
    with get_connection() as conn:
        c = conn.cursor()
        c.execute('SELECT user_id, first_name, size FROM users WHERE chat_id = %s', (chat_id,))
        return c.fetchall()


# ---------------------------------------------------------------- moderation dials

def get_modifiers(user_id, chat_id):
    """(theft_luck, growth_mult) for a player, defaulting to 1.0/1.0 for anyone who has
    never been touched (including a user_id with no row in this chat yet)."""
    with get_connection() as conn:
        c = conn.cursor()
        c.execute(
            'SELECT COALESCE(theft_luck, 1.0), COALESCE(growth_mult, 1.0) '
            'FROM users WHERE user_id = %s AND chat_id = %s',
            (user_id, chat_id)
        )
        return c.fetchone() or (1.0, 1.0)


def set_modifier(user_id, chat_id, column, value):
    """Sets one dial. column must be 'theft_luck' or 'growth_mult' - it is interpolated
    into the SQL, so it is checked against a literal allow-list rather than trusted."""
    if column not in ('theft_luck', 'growth_mult'):
        raise ValueError(f"unknown modifier column: {column}")
    with get_connection() as conn:
        c = conn.cursor()
        c.execute(
            f'UPDATE users SET {column} = %s WHERE user_id = %s AND chat_id = %s',
            (value, user_id, chat_id)
        )
        return c.rowcount > 0


def get_group_modifiers(chat_id):
    """(user_id, first_name, username, size, theft_luck, growth_mult) for every player
    in a group, biggest first - the admin overview behind /luck."""
    with get_connection() as conn:
        c = conn.cursor()
        c.execute(
            'SELECT user_id, first_name, username, size, '
            'COALESCE(theft_luck, 1.0), COALESCE(growth_mult, 1.0) '
            'FROM users WHERE chat_id = %s ORDER BY size DESC NULLS LAST, user_id ASC',
            (chat_id,)
        )
        return c.fetchall()


def is_dials_locked(user_id, chat_id):
    with get_connection() as conn:
        c = conn.cursor()
        c.execute('SELECT COALESCE(dials_locked, FALSE) FROM users WHERE user_id = %s AND chat_id = %s',
                  (user_id, chat_id))
        row = c.fetchone()
        return bool(row[0]) if row else False


def set_dials_locked(user_id, chat_id, locked):
    """Pins (or unpins) a player's dials against the nightly auto-handicap."""
    with get_connection() as conn:
        c = conn.cursor()
        c.execute('UPDATE users SET dials_locked = %s WHERE user_id = %s AND chat_id = %s',
                  (bool(locked), user_id, chat_id))
        return c.rowcount > 0


def get_recent_net_by_user(chat_id, days):
    """(user_id, first_name, net_delta, events) per player over the last `days` days of
    the ledger - the input the nightly auto-handicap reads the group's shape from.

    Deliberately ledger-derived rather than size-derived: what matters for a handicap is
    how much a player *gained recently*, not how big they happen to be. Someone sitting
    on a big balance they earned a week ago is not the one running away with the game.
    Only players who actually did something in the window appear here.

    Transfers between a player's own pockets are excluded, and so is loan principal.

    A bank deposit leaves the wallet and lands in the ledger as a large negative delta,
    which would read here as "this player is losing badly" and hand them a growth bonus
    - making a deposit/withdraw round trip the cheapest handicap exploit in the game.
    Loan principal is the same story from the other direction: borrowing would look like
    a windfall and repaying like a disaster, when in truth neither is income.

    Loan *interest* is deliberately NOT excluded. That is the one part of a loan that is
    real profit for the lender and a real cost to the borrower, so a player getting rich
    from usury gets throttled by the handicap exactly like one getting rich from dice.

    Crypto splits the same way and for the same reason: `crypto_principal` (the stake
    going into a position and the cost basis coming back out) is excluded, while
    `crypto_pnl` and `crypto_fee` are counted. Without that split, dumping a wallet into
    a coin before the nightly job reads the ledger would look like a catastrophic loss
    and pay a growth bonus for it - the deposit exploit wearing a different hat."""
    with get_connection() as conn:
        c = conn.cursor()
        c.execute(
            'SELECT l.user_id, MAX(COALESCE(u.first_name, %s)), SUM(l.delta), COUNT(*) '
            'FROM size_log l LEFT JOIN users u '
            '  ON u.user_id = l.user_id AND u.chat_id = l.chat_id '
            'WHERE l.chat_id = %s AND l.created_at >= NOW() - (%s || %s)::interval '
            '  AND COALESCE(l.source, %s) NOT IN (%s, %s, %s, %s, %s) '
            'GROUP BY l.user_id',
            ('', chat_id, days, ' days', '', 'bank_deposit', 'bank_withdraw',
             'loan_principal', 'xfer_principal', 'crypto_principal')
        )
        return c.fetchall()


def record_rebalance(chat_id, user_id, run_date, net_recent, group_median,
                     growth_before, growth_after, luck_before, luck_after):
    with get_connection() as conn:
        c = conn.cursor()
        c.execute(
            'INSERT INTO rebalance_log (chat_id, user_id, run_date, net_recent, group_median, '
            'growth_before, growth_after, luck_before, luck_after) '
            'VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)',
            (chat_id, user_id, run_date, net_recent, group_median,
             growth_before, growth_after, luck_before, luck_after)
        )


def get_last_rebalance(chat_id, limit=25):
    """Newest auto-handicap decisions for a group, names joined on for display."""
    with get_connection() as conn:
        c = conn.cursor()
        c.execute(
            'SELECT r.run_date, COALESCE(u.first_name, %s), r.net_recent, r.group_median, '
            'r.growth_before, r.growth_after, r.luck_before, r.luck_after '
            'FROM rebalance_log r LEFT JOIN users u '
            '  ON u.user_id = r.user_id AND u.chat_id = r.chat_id '
            'WHERE r.chat_id = %s ORDER BY r.id DESC LIMIT %s',
            ('', chat_id, limit)
        )
        return c.fetchall()



# ---------------------------------------------------------------- audit ledger

def get_size_log(chat_id=None, user_id=None, source=None, limit=200, offset=0):
    """Ledger rows newest-first, with the player's name joined on for display."""
    where, params = [], []
    if chat_id is not None:
        where.append('l.chat_id = %s'); params.append(chat_id)
    if user_id is not None:
        where.append('l.user_id = %s'); params.append(user_id)
    if source:
        where.append('l.source = %s'); params.append(source)
    clause = ('WHERE ' + ' AND '.join(where)) if where else ''
    params.extend([limit, offset])
    with get_connection() as conn:
        c = conn.cursor()
        c.execute(
            'SELECT l.id, l.created_at, l.chat_id, l.user_id, COALESCE(u.first_name, %s), '
            'l.delta, l.balance_after, l.source, l.note '
            'FROM size_log l LEFT JOIN users u ON u.user_id = l.user_id AND u.chat_id = l.chat_id '
            f'{clause} ORDER BY l.id DESC LIMIT %s OFFSET %s',
            ['?'] + params
        )
        return c.fetchall()


def get_size_log_sources(chat_id=None):
    """Distinct sources, for the panel's filter dropdown."""
    with get_connection() as conn:
        c = conn.cursor()
        if chat_id is None:
            c.execute('SELECT source, count(*) FROM size_log GROUP BY source ORDER BY count(*) DESC')
        else:
            c.execute('SELECT source, count(*) FROM size_log WHERE chat_id = %s GROUP BY source ORDER BY count(*) DESC',
                      (chat_id,))
        return c.fetchall()


def get_player_totals(chat_id, user_id):
    """Where one player's size came from: net movement grouped by source."""
    with get_connection() as conn:
        c = conn.cursor()
        c.execute(
            'SELECT source, count(*), sum(delta) FROM size_log '
            'WHERE chat_id = %s AND user_id = %s GROUP BY source ORDER BY sum(delta) DESC',
            (chat_id, user_id)
        )
        return c.fetchall()


# ---------------------------------------------------------------- admin editing

# Only these columns can be written through the panel, each with a validator. The
# panel interpolates the column name into SQL, so this dict is also the allow-list
# that stops anything else being addressed at all.
EDITABLE_USER_FIELDS = {
    'size':        ('number', 'سایز'),
    'streak':      ('int',    'استریک'),
    'best_streak': ('int',    'رکورد استریک'),
    'wins':        ('int',    'برد'),
    'losses':      ('int',    'باخت'),
    'perk':        ('text',   'پرک امروز'),
    'active_item': ('text',   'آیتم فعال'),
    'theft_luck':  ('mult',   'ضریب دزدی'),
    'growth_mult': ('mult',   'ضریب رشد'),
    'last_grown':  ('text',   'آخرین رشد (YYYY-MM-DD)'),
    'credit_score': ('credit', 'امتیاز اعتباری'),
}


def admin_set_user_field(user_id, chat_id, column, value):
    if column not in EDITABLE_USER_FIELDS:
        raise ValueError(f"field not editable: {column}")
    with get_connection() as conn:
        c = conn.cursor()
        c.execute(f'UPDATE users SET {column} = %s WHERE user_id = %s AND chat_id = %s',
                  (value, user_id, chat_id))
        return c.rowcount > 0


def admin_adjust_size(user_id, chat_id, delta, note):
    """Size changes from the panel go through the ledger like everything else, so an
    admin edit is visible in the same history as the gameplay that surrounds it."""
    with get_connection() as conn:
        c = conn.cursor()
        c.execute('UPDATE users SET size = COALESCE(size, 0) + %s WHERE user_id = %s AND chat_id = %s RETURNING size',
                  (delta, user_id, chat_id))
        row = c.fetchone()
        if row is None:
            return None
        c.execute('INSERT INTO size_log (chat_id, user_id, delta, balance_after, source, note) '
                  'VALUES (%s, %s, %s, %s, %s, %s)',
                  (chat_id, user_id, delta, row[0], 'admin_panel', note))
        return row[0]


def admin_set_inventory(user_id, chat_id, item_name, quantity):
    """Sets an exact quantity; 0 or less removes the row entirely."""
    with get_connection() as conn:
        c = conn.cursor()
        if quantity <= 0:
            c.execute('DELETE FROM inventory WHERE user_id = %s AND chat_id = %s AND item_name = %s',
                      (user_id, chat_id, item_name))
        else:
            c.execute(
                'INSERT INTO inventory (user_id, chat_id, item_name, quantity) VALUES (%s, %s, %s, %s) '
                'ON CONFLICT (user_id, chat_id, item_name) DO UPDATE SET quantity = EXCLUDED.quantity',
                (user_id, chat_id, item_name, quantity)
            )


def get_player_detail(user_id, chat_id):
    with get_connection() as conn:
        c = conn.cursor()
        c.execute(
            'SELECT user_id, chat_id, username, first_name, size, last_grown, perk, active_item, '
            'joined_at, wins, losses, COALESCE(streak,0), COALESCE(best_streak,0), last_theft_at, '
            'traitor_until, last_dosed_at, COALESCE(theft_luck,1.0), COALESCE(growth_mult,1.0), '
            'COALESCE(credit_score,100) '
            'FROM users WHERE user_id = %s AND chat_id = %s',
            (user_id, chat_id)
        )
        return c.fetchone()


def get_consensus_protections(chat_id):
    with get_connection() as conn:
        c = conn.cursor()
        c.execute('SELECT target_id, target_name, protected_until, reason FROM consensus_protection '
                  'WHERE chat_id = %s AND protected_until > now() ORDER BY protected_until DESC', (chat_id,))
        return c.fetchall()


def clear_consensus_protection(chat_id, target_id):
    with get_connection() as conn:
        c = conn.cursor()
        c.execute('DELETE FROM consensus_protection WHERE chat_id = %s AND target_id = %s', (chat_id, target_id))
        return c.rowcount > 0


def get_group_stats(chat_id):
    with get_connection() as conn:
        c = conn.cursor()
        c.execute('SELECT count(*), COALESCE(sum(size),0), COALESCE(max(size),0) FROM users WHERE chat_id = %s',
                  (chat_id,))
        players, total, biggest = c.fetchone()
        c.execute('SELECT count(*) FROM users WHERE chat_id = %s AND last_grown = %s',
                  (chat_id, _tehran_today_str()))
        active = c.fetchone()[0]
        c.execute('SELECT count(*) FROM size_log WHERE chat_id = %s', (chat_id,))
        events = c.fetchone()[0]
        return {'players': players, 'total_size': total, 'biggest': biggest,
                'active_today': active, 'log_events': events}


# ---------------------------------------------------------------- tone + Telegram Stars

def get_chat_tone(chat_id):
    """`adult` or `polite`; unknown/private chats keep the original adult voice."""
    if not chat_id or int(chat_id) >= 0:
        return 'adult'
    with get_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT COALESCE(tone_mode, 'adult') FROM chats WHERE chat_id = %s",
                  (chat_id,))
        row = c.fetchone()
        return row[0] if row and row[0] in ('adult', 'polite') else 'adult'


def get_chat_tones(chat_ids):
    """Bulk variant for the Mini App group picker (one query, not one per league)."""
    ids = [int(cid) for cid in chat_ids if int(cid) < 0]
    if not ids:
        return {}
    with get_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT chat_id, COALESCE(tone_mode, 'adult') FROM chats "
                  'WHERE chat_id = ANY(%s)', (ids,))
        return {cid: mode if mode in ('adult', 'polite') else 'adult'
                for cid, mode in c.fetchall()}


def set_chat_tone(chat_id, mode):
    """Set one league's public copy style. Returns False for an invalid mode/chat."""
    if int(chat_id) >= 0 or mode not in ('adult', 'polite'):
        return False
    with get_connection() as conn:
        c = conn.cursor()
        c.execute('INSERT INTO chats (chat_id, tone_mode) VALUES (%s, %s) '
                  'ON CONFLICT (chat_id) DO UPDATE SET tone_mode = EXCLUDED.tone_mode',
                  (chat_id, mode))
        return True


def create_star_order(order_id, user_id, chat_id, sku, kind, quantity, stars):
    """Persist a single-use order before its Telegram invoice is created."""
    if (int(chat_id) >= 0 or kind not in ('item', 'size') or
            int(quantity) <= 0 or int(stars) <= 0):
        return False
    with get_connection() as conn:
        c = conn.cursor()
        # Membership is checked again here rather than trusting the web/handler seam.
        c.execute('SELECT 1 FROM users WHERE user_id = %s AND chat_id = %s',
                  (user_id, chat_id))
        if c.fetchone() is None:
            return False
        c.execute('INSERT INTO star_orders '
                  '(id, user_id, chat_id, sku, kind, quantity, stars) '
                  'VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (id) DO NOTHING',
                  (order_id, user_id, chat_id, sku, kind, int(quantity), int(stars)))
        return c.rowcount > 0


def fail_star_order(order_id):
    """Close an order whose invoice could not be created; it can never be paid later."""
    with get_connection() as conn:
        c = conn.cursor()
        c.execute("UPDATE star_orders SET status = 'failed' "
                  "WHERE id = %s AND status = 'created'", (order_id,))


def get_star_order(order_id, user_id=None):
    with get_connection() as conn:
        c = conn.cursor()
        sql = ('SELECT id::text, user_id, chat_id, sku, kind, quantity, stars, status, '
               'telegram_charge_id, created_at, paid_at FROM star_orders WHERE id = %s')
        args = [order_id]
        if user_id is not None:
            sql += ' AND user_id = %s'
            args.append(user_id)
        c.execute(sql, tuple(args))
        row = c.fetchone()
        if row is None:
            return None
        keys = ('id', 'user_id', 'chat_id', 'sku', 'kind', 'quantity', 'stars',
                'status', 'charge_id', 'created_at', 'paid_at')
        return dict(zip(keys, row))


def claim_star_checkout(order_id, user_id, currency, total_amount):
    """Atomically make an invoice single-use at pre-checkout time.

    Telegram requires an answer within ten seconds. The short transaction both checks
    the signed-in buyer/amount and flips `created -> checkout`, so a forwarded or
    double-clicked invoice cannot be approved twice.
    """
    if currency != 'XTR':
        return False
    with get_connection() as conn:
        c = conn.cursor()
        c.execute("UPDATE star_orders SET status = 'checkout', checkout_at = now() "
                  "WHERE id = %s AND user_id = %s AND stars = %s "
                  "AND status = 'created' RETURNING id",
                  (order_id, user_id, int(total_amount)))
        return c.fetchone() is not None


def fulfill_star_order(order_id, user_id, currency, total_amount, charge_id):
    """Grant a paid order exactly once, in the same transaction as its receipt row.

    Returns a result dict, including `duplicate=True` for Telegram redelivery of the
    same SuccessfulPayment, or None when any signed payment field disagrees with the
    persisted order.
    """
    if currency != 'XTR' or not charge_id:
        return None
    with get_connection() as conn:
        c = conn.cursor()
        c.execute('SELECT user_id, chat_id, sku, kind, quantity, stars, status, '
                  'telegram_charge_id FROM star_orders WHERE id = %s FOR UPDATE',
                  (order_id,))
        row = c.fetchone()
        if row is None:
            return None
        oid, chat_id, sku, kind, quantity, stars, status, stored_charge = row
        if oid != user_id or int(stars) != int(total_amount):
            return None
        result = {
            'order_id': str(order_id), 'user_id': oid, 'chat_id': chat_id,
            'sku': sku, 'kind': kind, 'quantity': int(quantity),
            'stars': int(stars), 'charge_id': charge_id,
        }
        if status == 'fulfilled':
            if stored_charge != charge_id:
                return None
            result['duplicate'] = True
            return result
        if status != 'checkout':
            return None

        # A Telegram charge id may only ever fulfil one order, even if a forged update
        # tries to reuse it with a different payload.
        c.execute('SELECT id::text FROM star_orders WHERE telegram_charge_id = %s',
                  (charge_id,))
        used = c.fetchone()
        if used is not None and used[0] != str(order_id):
            return None

        if kind == 'item':
            c.execute('INSERT INTO inventory (user_id, chat_id, item_name, quantity) '
                      'VALUES (%s,%s,%s,%s) ON CONFLICT (user_id, chat_id, item_name) '
                      'DO UPDATE SET quantity = inventory.quantity + EXCLUDED.quantity',
                      (user_id, chat_id, sku, int(quantity)))
        elif kind == 'size':
            c.execute('UPDATE users SET size = COALESCE(size,0) + %s '
                      'WHERE user_id = %s AND chat_id = %s RETURNING size',
                      (int(quantity), user_id, chat_id))
            bal = c.fetchone()
            if bal is None:
                return None
            c.execute('INSERT INTO size_log '
                      '(chat_id, user_id, delta, balance_after, source, note) '
                      'VALUES (%s,%s,%s,%s,%s,%s)',
                      (chat_id, user_id, int(quantity), bal[0], 'telegram_stars',
                       f'Stars order {order_id}'))
            result['balance'] = float(bal[0])
        else:
            return None

        c.execute("UPDATE star_orders SET status = 'fulfilled', "
                  'telegram_charge_id = %s, paid_at = now() WHERE id = %s',
                  (charge_id, order_id))
        result['duplicate'] = False
        return result


def _retry_transient(fn):
    """Re-runs a db function once when the connection died mid-operation (Supabase's
    pooler occasionally drops connections: "SSL connection has been closed
    unexpectedly" in production, which used to kill the whole handler and leave the
    user's command silently unanswered). Every function here opens a fresh connection
    and commits a single transaction, so when one fails it either fully applied or
    fully rolled back - a single blind retry on a fresh connection is safe."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (psycopg2.OperationalError, psycopg2.InterfaceError):
            time.sleep(0.3)
            return fn(*args, **kwargs)
    return wrapper


# Wrap every public db function (everything except the connection manager itself and
# init_db, which must fail loudly at startup) in the transient-error retry above.
for _name, _obj in list(globals().items()):
    if (isinstance(_obj, types.FunctionType) and _obj.__module__ == __name__
            and not _name.startswith('_') and _name not in ('get_connection', 'init_db')):
        globals()[_name] = _retry_transient(_obj)


# ---------------------------------------------------------------- bank
# Two rules hold this feature together and every function below is written to keep
# them true:
#   1. Banked size is not wallet size. It lives in bank_accounts, so the leaderboard,
#      the crown and /dozdi (all of which read users.size) simply never see it.
#   2. The bank cannot mint. Interest is paid only out of the one reserve, which is
#      filled by real sinks (shop, burnt lottery rake, lost spectator bets, /ejma).
#      When the treasury is empty, interest is zero. There is no other path in.

def get_bank(user_id, chat_id):
    """(balance, deposit_date, deposited_today), creating the account row on first look."""
    with get_connection() as conn:
        c = conn.cursor()
        c.execute('INSERT INTO bank_accounts (user_id, chat_id) VALUES (%s, %s) '
                  'ON CONFLICT (user_id, chat_id) DO NOTHING', (user_id, chat_id))
        c.execute('SELECT COALESCE(balance,0), COALESCE(deposit_date,%s), COALESCE(deposited_today,0) '
                  'FROM bank_accounts WHERE user_id = %s AND chat_id = %s',
                  ('', user_id, chat_id))
        return c.fetchone() or (0.0, '', 0.0)


def _bank_log(c, chat_id, user_id, kind, amount, balance_after, note=None):
    c.execute('INSERT INTO bank_log (chat_id, user_id, kind, amount, balance_after, note) '
              'VALUES (%s, %s, %s, %s, %s, %s)',
              (chat_id, user_id, kind, amount, balance_after, note))


def bank_deposit(user_id, chat_id, amount, today_str, daily_cap, fee_ratio=0.0):
    """Moves `amount` from wallet into the bank in ONE transaction.

    Returns (True, new_balance, deposited_today, fee) or (False, reason, remaining_cap).
    The wallet deduction, the cap accounting and the bank credit all happen together:
    a crash between them would otherwise either eat the size or duplicate it. The
    daily cap counts *gross* deposits, so deposit->withdraw->deposit cannot be used to
    refill it and sneak a whole balance in behind one day's allowance."""
    with get_connection() as conn:
        c = conn.cursor()
        c.execute('INSERT INTO bank_accounts (user_id, chat_id) VALUES (%s, %s) '
                  'ON CONFLICT (user_id, chat_id) DO NOTHING', (user_id, chat_id))
        # Roll the per-day allowance over first, so a stale date can't block today.
        c.execute('UPDATE bank_accounts SET deposit_date = %s, deposited_today = 0 '
                  'WHERE user_id = %s AND chat_id = %s AND COALESCE(deposit_date,%s) <> %s',
                  (today_str, user_id, chat_id, '', today_str))
        c.execute('SELECT COALESCE(deposited_today,0) FROM bank_accounts '
                  'WHERE user_id = %s AND chat_id = %s FOR UPDATE', (user_id, chat_id))
        row = c.fetchone()
        used = float(row[0]) if row else 0.0
        remaining = daily_cap - used
        if remaining <= 0:
            return (False, 'cap', 0.0, 0.0)
        if amount > remaining:
            return (False, 'cap', remaining, 0.0)

        # Atomic check-and-take on the wallet, same pattern as try_deduct_size.
        c.execute('UPDATE users SET size = COALESCE(size,0) - %s '
                  'WHERE user_id = %s AND chat_id = %s AND COALESCE(size,0) >= %s RETURNING size',
                  (amount, user_id, chat_id, amount))
        wrow = c.fetchone()
        if wrow is None:
            return (False, 'funds', remaining, 0.0)
        c.execute('INSERT INTO size_log (chat_id, user_id, delta, balance_after, source, note) '
                  'VALUES (%s, %s, %s, %s, %s, %s)',
                  (chat_id, user_id, -amount, wrow[0], 'bank_deposit', None))

        # The fee comes out of the amount, not on top of it: you send `amount`, the
        # vault keeps `fee`, and the rest lands in your account. The daily cap counts
        # the gross, so a fee can never be dodged by splitting a deposit up.
        fee = round(amount * fee_ratio, 2)
        credited = round(amount - fee, 2)
        c.execute('UPDATE bank_accounts SET balance = COALESCE(balance,0) + %s, '
                  'deposited_today = COALESCE(deposited_today,0) + %s '
                  'WHERE user_id = %s AND chat_id = %s RETURNING balance, deposited_today',
                  (credited, amount, user_id, chat_id))
        brow = c.fetchone()
        _bank_log(c, chat_id, user_id, 'deposit', credited, brow[0])
        if fee > 0:
            tbal = _reserve_credit(c, fee)
            _bank_log(c, chat_id, user_id, 'treasury_in', fee, tbal, 'کارمزد واریز')
        return (True, float(brow[0]), float(brow[1]), fee)


def bank_withdraw(user_id, chat_id, amount, fee_ratio=0.0):
    """Moves `amount` out of the bank, atomically, minus the vault's cut. Returns
    (True, new_bank_balance, paid_out, fee), (False, None, 0, 0) if the account is
    short, or (False, 'run', available, 0) when the BANK is short. `amount` is what
    leaves the bank; `paid_out` is what reaches the wallet.

    That second failure is the bank run, and it is the honest cost of lending deposits
    out: the money is real but it is currently inside somebody's /vam loan, so it isn't
    there to hand back today. CB_RESERVE_RATIO is sized to make this rare - ordinary
    withdrawals always clear - but it can and should happen if enough savers head for
    the door at once."""
    with get_connection() as conn:
        c = conn.cursor()
        # Check the bank's own liquidity before touching the account, so a refused
        # withdrawal leaves the saver's balance exactly where it was.
        c.execute('SELECT COALESCE(loans_out,0) FROM central_bank WHERE id = %s FOR UPDATE',
                  (CB_SINGLETON,))
        crow = c.fetchone()
        loans_out = float(crow[0]) if crow else 0.0
        c.execute('SELECT COALESCE(SUM(balance),0) FROM bank_accounts WHERE COALESCE(balance,0) > 0')
        deposits = float(c.fetchone()[0] or 0.0)
        reserve = _reserve_balance(c)
        cash = reserve + deposits - loans_out
        if amount > cash:
            return (False, 'run', max(0.0, round(cash, 2)), 0.0)

        c.execute('UPDATE bank_accounts SET balance = COALESCE(balance,0) - %s '
                  'WHERE user_id = %s AND chat_id = %s AND COALESCE(balance,0) >= %s '
                  'RETURNING balance', (amount, user_id, chat_id, amount))
        brow = c.fetchone()
        if brow is None:
            return (False, None, 0.0, 0.0)
        fee = round(amount * fee_ratio, 2)
        paid_out = round(amount - fee, 2)
        c.execute('UPDATE users SET size = COALESCE(size,0) + %s '
                  'WHERE user_id = %s AND chat_id = %s RETURNING size',
                  (paid_out, user_id, chat_id))
        wrow = c.fetchone()
        if wrow is None:
            # No wallet row to receive it - undo rather than vanish the size.
            raise RuntimeError('no users row to withdraw into')
        c.execute('INSERT INTO size_log (chat_id, user_id, delta, balance_after, source, note) '
                  'VALUES (%s, %s, %s, %s, %s, %s)',
                  (chat_id, user_id, paid_out, wrow[0], 'bank_withdraw', None))
        _bank_log(c, chat_id, user_id, 'withdraw', -amount, brow[0])
        if fee > 0:
            tbal = _reserve_credit(c, fee)
            _bank_log(c, chat_id, user_id, 'treasury_in', fee, tbal, 'کارمزد برداشت')
        return (True, float(brow[0]), paid_out, fee)


# ---------------------------------------------------------------- the central bank
# ONE treasury for the whole bot. Not a pool of member accounts, not a vault per group:
# a single stored number, central_bank.reserve. Every sink in the game feeds it and
# every payout comes out of it, whichever group the player was standing in.
#
# The line this draws is worth stating plainly, because it is the design:
#
#   BALANCES ARE LOCAL. THE BANK IS GLOBAL.
#
# `users.size` is still per (user, chat) and always will be - 100 in one group and 1000
# in another are two unrelated numbers, and every league is still independent. What is
# no longer per group is the *institution*: one reserve, one deposit rate, one loan
# book, one set of books.
#
# An earlier version kept a treasury row per group and derived the pool as their SUM.
# That was a safe way to merge the behaviour without touching live balances, but it
# left the storage saying something the game no longer meant, and every read had to
# decide whether it wanted the share or the sum. There is now only one number to read.
#
# Two things stay per group, and both are load-bearing:
#   - Deposits remember which group they were made in, so the bank can never be used as
#     a free cross-group transfer (that would reopen the farm-group exploit the
#     /enteghal source gate exists to stop). There is a regression test asserting it.
#   - A group's CLAIM on the shared reserve is bounded by its weight in the bot - see
#     _group_weight. The reserve is one pot, but one lucky heist or one corrupt decree
#     in the smallest group on the bot must not be able to empty the vault that backs
#     every other group's savings.

CB_RESERVE_RATIO = 0.35      # share of deposits that must stay liquid, never lent out
CB_SINGLETON = 1


def get_central_bank():
    """The whole balance sheet in one read.

    reserve   - pooled member accounts: the bank's own equity, fed by every sink
    deposits  - what savers are owed (a LIABILITY, not an asset - see the docs)
    loans_out - principal currently lent out of those deposits (the bank's asset)
    cash      - what could actually be paid out right now
    lendable  - headroom left under the reserve requirement
    """
    with get_connection() as conn:
        c = conn.cursor()
        c.execute('INSERT INTO central_bank (id) VALUES (%s) ON CONFLICT (id) DO NOTHING',
                  (CB_SINGLETON,))
        c.execute('SELECT COALESCE(SUM(balance),0) FROM bank_accounts WHERE COALESCE(balance,0) > 0')
        deposits = float(c.fetchone()[0] or 0.0)
        c.execute('SELECT COALESCE(reserve,0), COALESCE(loans_out,0) FROM central_bank '
                  'WHERE id = %s', (CB_SINGLETON,))
        crow = c.fetchone() or (0.0, 0.0)
        reserve = float(crow[0] or 0.0)
        loans_out = float(crow[1] or 0.0)
        return {
            'reserve': reserve,
            'deposits': deposits,
            'loans_out': loans_out,
            # Cash is the only number that decides whether a withdrawal can be honoured.
            'cash': reserve + deposits - loans_out,
            # Deposits are the primary funding source, but the bank's own equity counts
            # too - a real bank lends its capital as well as its depositors' money, and
            # without this /vam would be dead in a world where nobody has banked
            # anything yet. CB_RESERVE_RATIO still keeps a slice of DEPOSITS
            # permanently un-lent so ordinary withdrawals clear.
            'lendable': max(0.0, deposits * (1.0 - CB_RESERVE_RATIO) + reserve - loans_out),
            'coverage': (reserve / deposits) if deposits > 0 else None,
        }


def _reserve_balance(c, lock=False):
    """The one treasury balance. `lock` takes a row lock for a read-modify-write."""
    c.execute('INSERT INTO central_bank (id) VALUES (%s) ON CONFLICT (id) DO NOTHING',
              (CB_SINGLETON,))
    c.execute('SELECT COALESCE(reserve,0) FROM central_bank WHERE id = %s'
              + (' FOR UPDATE' if lock else ''), (CB_SINGLETON,))
    row = c.fetchone()
    return float(row[0] or 0.0) if row else 0.0


def _reserve_credit(c, amount):
    """Puts size into the one treasury. Returns the balance afterwards, which is what
    every caller wants for its bank_log row."""
    if amount <= 0:
        return _reserve_balance(c)
    c.execute('INSERT INTO central_bank (id, reserve) VALUES (%s, %s) ON CONFLICT (id) '
              'DO UPDATE SET reserve = COALESCE(central_bank.reserve,0) + %s '
              'RETURNING reserve', (CB_SINGLETON, amount, amount))
    return float(c.fetchone()[0] or 0.0)


def _reserve_take(c, amount):
    """Draws up to `amount` out of the one treasury, never past zero. Returns
    (taken, balance_after). The bank cannot mint, so a caller owed more than this
    returns has to decide for itself what to do about the shortfall."""
    if amount <= 0:
        return (0.0, _reserve_balance(c))
    available = _reserve_balance(c, lock=True)
    take = min(float(amount), max(0.0, available))
    if take <= 0:
        return (0.0, available)
    c.execute('UPDATE central_bank SET reserve = COALESCE(reserve,0) - %s '
              'WHERE id = %s RETURNING reserve', (take, CB_SINGLETON))
    return (take, float(c.fetchone()[0] or 0.0))


def _group_weight(c, chat_id):
    """How big a slice of the whole bot one group is: its money (wallets + deposits) as
    a fraction of every group's money.

    This is DERIVED, never stored - there is no per-group treasury for it to be a
    balance of. It exists for one job: bounding what a single group can pull OUT of the
    shared reserve. A heist takes a fraction of "the treasury", and so does a corrupt
    decree; against one global pot those would let a player in the smallest group on the
    bot walk off with the vault backing every other group's savings. Scaling the draw by
    the group's weight keeps the pot single while keeping the raid local in size.

    Negative wallets are floored at zero. Debt is real - _collect drives a defaulter's
    size below zero on purpose - but a group carrying a big debtor is not thereby
    *smaller*, and letting one negative wallet shrink a group's whole claim would make
    the bound move for reasons that have nothing to do with the group's size."""
    c.execute('SELECT COALESCE(SUM(GREATEST(COALESCE(size,0),0)),0) FROM users '
              'WHERE chat_id = %s', (chat_id,))
    mine = float(c.fetchone()[0] or 0.0)
    c.execute('SELECT COALESCE(SUM(balance),0) FROM bank_accounts '
              'WHERE chat_id = %s AND COALESCE(balance,0) > 0', (chat_id,))
    mine += float(c.fetchone()[0] or 0.0)
    c.execute('SELECT COALESCE(SUM(GREATEST(COALESCE(size,0),0)),0) FROM users')
    total = float(c.fetchone()[0] or 0.0)
    c.execute('SELECT COALESCE(SUM(balance),0) FROM bank_accounts WHERE COALESCE(balance,0) > 0')
    total += float(c.fetchone()[0] or 0.0)
    if total <= 0:
        return 1.0
    return max(0.0, min(1.0, mine / total))


def _group_claim(c, chat_id):
    """The most of the shared reserve this group could take at once - see _group_weight."""
    return _reserve_balance(c) * _group_weight(c, chat_id)


def group_reserve_claim(chat_id):
    """Public read of the above, so /sarghat can quote the player the same number
    heist_take will actually hand over. A displayed number drifting from the real one is
    a bug class this codebase has already been bitten by."""
    with get_connection() as conn:
        c = conn.cursor()
        return round(_group_claim(c, chat_id), 2)


def treasury_add(chat_id, amount, note=None):
    """The only way size enters the treasury: a sink hands over what it just destroyed.
    Called from the spots that used to simply delete size."""
    if amount <= 0:
        return
    with get_connection() as conn:
        c = conn.cursor()
        tbal = _reserve_credit(c, amount)
        _bank_log(c, chat_id, None, 'treasury_in', amount, tbal, note)


def treasury_take_up_to(chat_id, amount, note=None):
    """Draws up to `amount` out of the treasury, never past zero, and returns how much
    it actually got. The caller decides what to do about the shortfall.

    This is the house bankroll being spent, as opposed to pay_interest which scales
    everyone down to fit what's there - a bet payout is owed to one named player in full,
    so the choice is 'treasury pays what it can, the rest is minted' rather than
    'everybody gets a haircut'."""
    if amount <= 0:
        return 0.0
    with get_connection() as conn:
        c = conn.cursor()
        take, after = _reserve_take(c, amount)
        if take <= 0:
            return 0.0
        _bank_log(c, chat_id, None, 'treasury_out', -take, after, note)
        return take


def get_treasury(chat_id):
    """(reserve, last_interest_date, last_heist_at).

    The balance is the WHOLE bot's - there is no per-group treasury to return - while
    the two stamps are this group's own. Callers that want to know what this group could
    actually draw out of it want group_reserve_claim() instead."""
    with get_connection() as conn:
        c = conn.cursor()
        c.execute('SELECT COALESCE(last_interest_date,%s), last_heist_at '
                  'FROM bank_treasury WHERE chat_id = %s', ('', chat_id))
        row = c.fetchone() or ('', None)
        return (_reserve_balance(c), row[0], row[1])


def get_bank_totals(chat_id):
    """(total_deposits, depositor_count) for a group - what a heist is sizing up."""
    with get_connection() as conn:
        c = conn.cursor()
        c.execute('SELECT COALESCE(SUM(balance),0), COUNT(*) FROM bank_accounts '
                  'WHERE chat_id = %s AND COALESCE(balance,0) > 0', (chat_id,))
        return c.fetchone() or (0.0, 0)


def get_bank_holders(chat_id):
    """(user_id, first_name, balance) for everyone with size in the bank, biggest first."""
    with get_connection() as conn:
        c = conn.cursor()
        c.execute('SELECT b.user_id, COALESCE(u.first_name, %s), COALESCE(b.balance,0) '
                  'FROM bank_accounts b LEFT JOIN users u '
                  '  ON u.user_id = b.user_id AND u.chat_id = b.chat_id '
                  'WHERE b.chat_id = %s AND COALESCE(b.balance,0) > 0 '
                  'ORDER BY b.balance DESC', ('?', chat_id))
        return c.fetchall()


def claim_interest_run(chat_id, today_str):
    """Atomically claims the right to pay interest for `today_str` in this group.
    Returns True for exactly one caller per day, so a restart can't pay twice."""
    with get_connection() as conn:
        c = conn.cursor()
        c.execute('INSERT INTO bank_treasury (chat_id, last_interest_date) VALUES (%s, %s) '
                  'ON CONFLICT (chat_id) DO UPDATE SET last_interest_date = %s '
                  'WHERE COALESCE(bank_treasury.last_interest_date, %s) <> %s '
                  'RETURNING chat_id', (chat_id, today_str, today_str, '', today_str))
        return c.fetchone() is not None


def pay_interest(chat_id, rate, max_share):
    """Pays one day's interest to ONE group's depositors, out of the ONE treasury.

    Returns (rows_paid, total_paid, reserve_left), where `reserve_left` is the whole
    bot's - that is the number the rate and the next payout are judged against.

    A group whose own players have paid nothing into the treasury still pays its savers,
    funded by the groups that have. That is what a shared bank *is*; there is no local
    vault left to run dry on its own. The reserve is still a hard ceiling - the bank
    cannot mint - and if what everyone is owed exceeds what it can afford (capped
    further by `max_share`, so one day never drains the whole thing) every depositor is
    scaled down by the same factor rather than the early rows being paid in full and the
    late ones getting nothing.

    Note the draw is deliberately NOT weight-capped the way a heist or a decree is:
    interest is the bank honouring a debt it owes to named savers, not a group helping
    itself to the pot."""
    with get_connection() as conn:
        c = conn.cursor()
        treasury = _reserve_balance(c)
        if treasury <= 0:
            return (0, 0.0, treasury)

        c.execute('SELECT user_id, COALESCE(balance,0) FROM bank_accounts '
                  'WHERE chat_id = %s AND COALESCE(balance,0) > 0', (chat_id,))
        holders = c.fetchall()
        if not holders:
            return (0, 0.0, treasury)

        owed = {uid: bal * rate for uid, bal in holders}
        want = sum(owed.values())
        budget = min(treasury * max_share, treasury)
        if want <= 0:
            return (0, 0.0, treasury)
        factor = min(1.0, budget / want)

        paid_total = 0.0
        paid_rows = 0
        for uid, amount in owed.items():
            pay = round(amount * factor, 2)
            if pay <= 0:
                continue
            c.execute('UPDATE bank_accounts SET balance = COALESCE(balance,0) + %s '
                      'WHERE user_id = %s AND chat_id = %s RETURNING balance', (pay, uid, chat_id))
            brow = c.fetchone()
            if brow is None:
                continue
            _bank_log(c, chat_id, uid, 'interest', pay, brow[0])
            paid_total += pay
            paid_rows += 1

        if paid_total > 0:
            _, treasury = _reserve_take(c, paid_total)
            _bank_log(c, chat_id, None, 'interest_out', -paid_total, treasury,
                      'از خزانهٔ مشترک')
        return (paid_rows, paid_total, treasury)


# ---------------------------------------------------------------- treasury income

def get_treasury_income(days):
    """What the CENTRAL bank actually earned over the last `days` days.

    Only rows tagged 'treasury_in' count, which is deliberately narrower than "the
    treasury went up": a crypto buyer parking size against a position is logged as
    'crypto_in' and must NOT read as income, because the bank may have to hand every
    centimetre of it back on the next sale. Fees, shop purchases and the lottery rake
    are income; a position is not.

    Pooled rather than per group, matching the pooled rate: the reserve that pays your
    interest is everyone's now, so the earnings that fund it are everyone's too."""
    with get_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT COALESCE(SUM(amount),0) FROM bank_log "
                  "WHERE kind = 'treasury_in' AND created_at >= NOW() - (%s || %s)::interval",
                  (days, ' days'))
        return float(c.fetchone()[0] or 0.0)


def charge_maintenance(chat_id, ratio):
    """Charges one group's depositors a day's account-maintenance fee, straight into
    that group's own member account of the reserve.

    This is the piece that makes the deposit rate self-funding: the fee is levied on
    exactly the same number the interest bill is levied on, so the two grow together
    instead of the liability outrunning the income. Charged to the group's own share
    (not spread across the pool) because it is this group's savers paying it - unlike
    interest, which the pool cross-subsidises.

    Returns (rows_charged, total_charged)."""
    if ratio <= 0:
        return (0, 0.0)
    with get_connection() as conn:
        c = conn.cursor()
        c.execute('SELECT user_id, COALESCE(balance,0) FROM bank_accounts '
                  'WHERE chat_id = %s AND COALESCE(balance,0) > 0 FOR UPDATE', (chat_id,))
        holders = c.fetchall()
        total, rows = 0.0, 0
        for uid, bal in holders:
            fee = round(float(bal) * ratio, 2)
            # Never overdraw an account into debt on a fee, and skip the ones where a
            # day's fee rounds away to nothing rather than logging a no-op row.
            fee = min(fee, float(bal))
            if fee <= 0:
                continue
            c.execute('UPDATE bank_accounts SET balance = COALESCE(balance,0) - %s '
                      'WHERE user_id = %s AND chat_id = %s RETURNING balance', (fee, uid, chat_id))
            brow = c.fetchone()
            if brow is None:
                continue
            _bank_log(c, chat_id, uid, 'maintenance', -fee, brow[0], 'کارمزد نگهداری حساب')
            total += fee
            rows += 1
        if total > 0:
            tbal = _reserve_credit(c, total)
            _bank_log(c, chat_id, None, 'treasury_in', total, tbal,
                      'کارمزد نگهداری حساب')
        return (rows, round(total, 2))


# ---------------------------------------------------------------- crypto market

def crypto_seed(coins):
    """Registers the coin list once. Existing rows are left exactly as they are, so a
    restart never resets a live price back to its base - only genuinely new coins are
    inserted. Same discipline as every other init_db write."""
    with get_connection() as conn:
        c = conn.cursor()
        for symbol, name, base, vol, feed_id in coins:
            c.execute('INSERT INTO crypto_prices (symbol, name, price, prev_price, '
                      'base_price, volatility, feed_id) VALUES (%s, %s, %s, %s, %s, %s, %s) '
                      'ON CONFLICT (symbol) DO UPDATE SET name = EXCLUDED.name, '
                      'base_price = EXCLUDED.base_price, volatility = EXCLUDED.volatility, '
                      'feed_id = EXCLUDED.feed_id',
                      (symbol, name, base, base, base, vol, feed_id))


def crypto_feed_rows():
    """(symbol, feed_id, feed_scale, base_price, price) for the coins that track a real
    market. Deliberately separate from crypto_all(), whose 7-tuple is unpacked
    positionally in a dozen places - widening it would break every one of them."""
    with get_connection() as conn:
        c = conn.cursor()
        c.execute('SELECT symbol, feed_id, feed_scale, base_price, price FROM crypto_prices '
                  "WHERE feed_id IS NOT NULL AND feed_id <> '' ORDER BY symbol")
        return c.fetchall()


def crypto_apply_feed(rows):
    """One statement for the whole board, like crypto_set_prices.

    `rows` is [(symbol, in_game_price, usd, scale)]. The scale is written with
    COALESCE so it is fixed the first time only: every later tick keeps whatever the
    first observation set, which is what makes the game price track the real coin's
    MOVES rather than being re-pinned to base every minute.
    """
    rows = [(str(sym), float(px), float(usd), float(sc))
            for sym, px, usd, sc in rows if px and px > 0 and sc and sc > 0]
    if not rows:
        return 0
    values = ','.join(['(%s, %s::double precision, %s::double precision, '
                       '%s::double precision)'] * len(rows))
    args = [x for r in rows for x in r]
    with get_connection() as conn:
        c = conn.cursor()
        c.execute('UPDATE crypto_prices AS cp SET prev_price = cp.price, price = v.price, '
                  'feed_usd = v.usd, feed_scale = COALESCE(cp.feed_scale, v.scale), '
                  'feed_at = NOW(), updated_at = NOW() '
                  f'FROM (VALUES {values}) AS v(symbol, price, usd, scale) '
                  'WHERE cp.symbol = v.symbol', args)
        return c.rowcount


def crypto_feed_status():
    """{symbol: (feed_id, feed_usd, age_seconds)} - what the board shows about the feed,
    and what tells the tick whether the feed is live enough to trust."""
    with get_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT symbol, feed_id, feed_usd, "
                  "EXTRACT(EPOCH FROM (now() - feed_at)) FROM crypto_prices "
                  "WHERE feed_id IS NOT NULL AND feed_id <> ''")
        return {r[0]: (r[1], float(r[2]) if r[2] is not None else None,
                       float(r[3]) if r[3] is not None else None) for r in c.fetchall()}


def crypto_all():
    """(symbol, name, mid, prev_mid, base_price, volatility, net_units) for the market.

    `mid` is the random walk's price, NOT what a trade executes at - apply the inventory
    impact on top (bot.crypto_display_price) before showing or quoting anything."""
    with get_connection() as conn:
        c = conn.cursor()
        c.execute('SELECT symbol, name, price, prev_price, base_price, volatility, '
                  'COALESCE(net_units,0) FROM crypto_prices ORDER BY base_price DESC, symbol')
        return c.fetchall()


def crypto_set_prices(pairs):
    """Writes one tick for the whole market in a SINGLE statement.

    `prev_price` is taken from the row's own current price inside that statement, so the
    arrow a player sees always describes the move that just happened. One round trip
    matters here in a way it doesn't elsewhere: this runs every minute forever, and a
    per-coin loop would be ten Supabase round trips a minute for the life of the bot."""
    pairs = [(sym, float(px)) for sym, px in pairs if px and px > 0]
    if not pairs:
        return
    values = ','.join(['(%s, %s::double precision)'] * len(pairs))
    args = [x for pair in pairs for x in pair]
    with get_connection() as conn:
        c = conn.cursor()
        c.execute('UPDATE crypto_prices AS cp SET prev_price = cp.price, price = v.price, '
                  'updated_at = NOW() '
                  f'FROM (VALUES {values}) AS v(symbol, price) '
                  'WHERE cp.symbol = v.symbol', args)


def _crypto_impact(net_notional, depth, cap):
    """How far the market's net position has pushed a coin off its mid, as a fraction.

    Linear in the inventory, which is not an aesthetic choice - it is what makes
    _crypto_exec_price exact. Clamped so no amount of buying can drive a coin to the
    moon or to zero on inventory alone."""
    if depth <= 0:
        return 0.0
    return max(-cap, min(cap, float(net_notional) / float(depth)))


def _crypto_exec_price(mid, base_price, net_units, delta_units, depth, cap):
    """The AVERAGE price a trade of `delta_units` actually executes at.

    This is the whole anti-pump design, and it is worth understanding before touching
    it. Price is a function of inventory alone, so the cost of a trade is the integral
    of that function along the path the trade walks. Charging the average - which for a
    linear impact is just the price at the MIDPOINT inventory - means:

        buy n units at inventory q  -> pay      mid * n * (1 + imp(q + n/2))
        sell n units at inventory q+n -> receive mid * n * (1 + imp(q + n/2))

    Identical. A player can never profit from the price move their own order caused, no
    matter how large: an immediate round trip returns exactly what it cost, and the two
    trading fees are pure loss. Pump-and-dump is not merely discouraged here, it is
    arithmetically impossible.

    Quoting the post-trade price instead (or the pre-trade one) breaks that equality and
    hands a big wallet free size, so do not "simplify" this to a single lookup."""
    midpoint = (float(net_units) + float(delta_units) / 2.0) * float(base_price)
    return max(1e-6, float(mid) * (1.0 + _crypto_impact(midpoint, depth, cap)))


def _crypto_solve_units(mid, base_price, net_units, spend, depth, cap, sign):
    """Units whose execution cost lands on `spend`.

    The execution price depends on how many units are traded, and the unit count depends
    on the price, so this is a fixed point. Three passes converge to well under a
    rounding unit - the correction is second-order in the trade size - and unlike a
    quadratic solve it stays correct when the impact clamps."""
    px = _crypto_exec_price(mid, base_price, net_units, 0.0, depth, cap)
    units = float(spend) / px if px > 0 else 0.0
    for _ in range(3):
        px = _crypto_exec_price(mid, base_price, net_units, sign * units, depth, cap)
        if px <= 0:
            return (0.0, 0.0)
        units = float(spend) / px
    return (units, px)


def crypto_holdings_of(user_id, chat_id):
    """(symbol, amount, avg_cost) for everything this player holds in this group."""
    with get_connection() as conn:
        c = conn.cursor()
        c.execute('SELECT symbol, amount, avg_cost FROM crypto_holdings '
                  'WHERE user_id = %s AND chat_id = %s AND COALESCE(amount,0) > 0 '
                  'ORDER BY symbol', (user_id, chat_id))
        return c.fetchall()


def crypto_buy(user_id, chat_id, symbol, spend, fee_ratio, today_str, daily_cap,
               impact_depth, impact_cap):
    """Spends `spend` size on a coin at the live impact-adjusted price, in ONE
    transaction.

    The CENTRAL BANK's pooled reserve is the counterparty, exactly like the spectator
    book's house: the size a buyer spends is not destroyed, it is held against the
    position and paid back out on a sale. Only the FEE is income, and it is the only
    part logged as 'treasury_in'.

    The unit count and the price are both worked out INSIDE the transaction, off the
    inventory read under lock - quoting outside it would let two racing buyers both
    execute at the pre-trade price and skip each other's impact.

    Returns (True, units, price, spent, fee, new_amount, new_avg)
    or (False, reason, remaining_cap)."""
    if spend <= 0:
        return (False, 'amount', 0.0)
    with get_connection() as conn:
        c = conn.cursor()
        c.execute('INSERT INTO crypto_holdings (user_id, chat_id, symbol) VALUES (%s,%s,%s) '
                  'ON CONFLICT (user_id, chat_id, symbol) DO NOTHING',
                  (user_id, chat_id, symbol))
        # Roll the day's allowance over first, so a stale stamp can't block today. The
        # cap counts GROSS spend for the same reason the deposit cap does: otherwise
        # buy -> sell -> buy refills it and the whole wallet goes in behind one day's
        # allowance.
        c.execute('UPDATE crypto_holdings SET bought_date = %s, bought_today = 0 '
                  'WHERE user_id = %s AND chat_id = %s '
                  '  AND COALESCE(bought_date,%s) <> %s',
                  (today_str, user_id, chat_id, '', today_str))
        c.execute('SELECT COALESCE(SUM(bought_today),0) FROM crypto_holdings '
                  'WHERE user_id = %s AND chat_id = %s', (user_id, chat_id))
        used = float(c.fetchone()[0] or 0.0)
        remaining = daily_cap - used
        if remaining <= 0:
            return (False, 'cap', 0.0)
        if spend > remaining:
            return (False, 'cap', remaining)

        # Locked for the whole trade: the inventory decides the price, so two buyers
        # landing together must queue rather than both quoting off the same mid.
        c.execute('SELECT price, base_price, COALESCE(net_units,0) FROM crypto_prices '
                  'WHERE symbol = %s FOR UPDATE', (symbol,))
        prow = c.fetchone()
        if prow is None:
            return (False, 'amount', remaining)
        mid, base_price, net_units = float(prow[0]), float(prow[1]), float(prow[2])
        units, price = _crypto_solve_units(mid, base_price, net_units, spend,
                                           impact_depth, impact_cap, +1)
        units = round(units, 6)
        if units <= 0:
            return (False, 'amount', remaining)
        gross = round(units * price, 2)
        fee = round(gross * fee_ratio, 2)
        total = round(gross + fee, 2)
        if gross <= 0:
            return (False, 'amount', remaining)

        c.execute('UPDATE users SET size = COALESCE(size,0) - %s '
                  'WHERE user_id = %s AND chat_id = %s AND COALESCE(size,0) >= %s RETURNING size',
                  (total, user_id, chat_id, total))
        wrow = c.fetchone()
        if wrow is None:
            return (False, 'funds', remaining)
        after = float(wrow[0])
        # Two ledger rows, and the split is load-bearing: the stake is a transfer between
        # the player's own pockets and must not read to the nightly handicap as a loss
        # (see get_recent_net_by_user), while the fee is a real cost and must.
        c.execute('INSERT INTO size_log (chat_id, user_id, delta, balance_after, source, note) '
                  'VALUES (%s,%s,%s,%s,%s,%s)',
                  (chat_id, user_id, -gross, after + fee, 'crypto_principal', symbol))
        if fee > 0:
            c.execute('INSERT INTO size_log (chat_id, user_id, delta, balance_after, source, note) '
                      'VALUES (%s,%s,%s,%s,%s,%s)',
                      (chat_id, user_id, -fee, after, 'crypto_fee', symbol))

        # The market's counterparty is the one treasury, on both legs. When the vault
        # was still split per group this had to be spread proportionally in both
        # directions or it was farmable - crediting one group's share while debiting
        # everyone's is a transfer dressed up as a trade. With a single pot there is
        # nothing left to spread and nothing left to farm.
        tbal = _reserve_credit(c, total)
        _bank_log(c, chat_id, user_id, 'crypto_in', gross, tbal, f'خرید {symbol}')
        if fee > 0:
            _bank_log(c, chat_id, user_id, 'treasury_in', fee, tbal, 'کارمزد معاملهٔ کریپتو')

        c.execute('SELECT COALESCE(amount,0), COALESCE(avg_cost,0) FROM crypto_holdings '
                  'WHERE user_id = %s AND chat_id = %s AND symbol = %s FOR UPDATE',
                  (user_id, chat_id, symbol))
        hrow = c.fetchone()
        held, avg = (float(hrow[0]), float(hrow[1])) if hrow else (0.0, 0.0)
        new_amount = held + units
        new_avg = ((held * avg) + gross) / new_amount if new_amount > 0 else 0.0
        c.execute('UPDATE crypto_holdings SET amount = %s, avg_cost = %s, '
                  'bought_today = COALESCE(bought_today,0) + %s '
                  'WHERE user_id = %s AND chat_id = %s AND symbol = %s',
                  (new_amount, new_avg, gross, user_id, chat_id, symbol))
        # The market is now longer by these units, so the next quote is dearer. This is
        # the demand side of the price.
        c.execute('UPDATE crypto_prices SET net_units = COALESCE(net_units,0) + %s '
                  'WHERE symbol = %s', (units, symbol))
        return (True, units, price, total, fee, new_amount, new_avg)


def crypto_sell(user_id, chat_id, symbol, units, fee_ratio, impact_depth, impact_cap):
    """Sells up to `units` at the live impact-adjusted price, treasury -> wallet, in
    ONE transaction.

    PARTIALLY FILLS rather than minting. The central bank's POOLED reserve is the
    counterparty, so a sale it cannot cover is a market with no liquidity, not a licence
    to create size: whatever the bank can actually pay is sold and the rest of the
    position simply stays put. That is the one rule keeping this feature from becoming a
    money printer, since a coin that has doubled would otherwise pay out size nobody
    ever put in.

    Liquidity is pooled and the cost is spread across every member account, the same way
    pay_interest works: one deep book for the whole bot instead of a market whose depth
    depends on which group you happen to be in.

    Selling pushes the price DOWN by the same curve buying pushes it up, and executes
    at the average along the way - see _crypto_exec_price for why that symmetry is what
    makes pumping your own bags pointless.

    Returns (True, sold_units, price, net, fee, pnl, left) or (False, reason, held)."""
    if units <= 0:
        return (False, 'amount', 0.0)
    with get_connection() as conn:
        c = conn.cursor()
        c.execute('SELECT COALESCE(amount,0), COALESCE(avg_cost,0) FROM crypto_holdings '
                  'WHERE user_id = %s AND chat_id = %s AND symbol = %s FOR UPDATE',
                  (user_id, chat_id, symbol))
        hrow = c.fetchone()
        if hrow is None or float(hrow[0]) <= 0:
            return (False, 'none', 0.0)
        held, avg = float(hrow[0]), float(hrow[1])
        units = min(units, held)

        # Liquidity is the CENTRAL bank's pooled reserve, not this group's slice of it.
        # A market whose depth depended on which group you happened to be in would be
        # arbitrary - a small group could barely trade while a rich one traded freely.
        available = _reserve_balance(c)

        c.execute('SELECT price, base_price, COALESCE(net_units,0) FROM crypto_prices '
                  'WHERE symbol = %s FOR UPDATE', (symbol,))
        prow = c.fetchone()
        if prow is None:
            return (False, 'none', held)
        mid, base_price, net_units = float(prow[0]), float(prow[1]), float(prow[2])

        def _net_for(u):
            px = _crypto_exec_price(mid, base_price, net_units, -u, impact_depth, impact_cap)
            return (u * px * (1.0 - fee_ratio), px)

        proceeds, price = _net_for(units)
        if price <= 0:
            return (False, 'amount', held)
        if proceeds > available:
            # Trim to what the bank can actually pay. This has to be solved, not
            # divided: fewer units means less impact means a HIGHER price per unit, so
            # `available / price` overshoots and the shortfall would be minted -
            # _reserve_take caps what it hands over while the wallet is credited in
            # full. Proceeds are monotonically increasing in units (the
            # impact cap keeps the curve well inside the turning point), so a bisection
            # lands on the largest fill the bank can honour, exactly.
            lo, hi = 0.0, units
            for _ in range(48):
                probe = (lo + hi) / 2.0
                if _net_for(probe)[0] <= available:
                    lo = probe
                else:
                    hi = probe
            units = lo
            proceeds, price = _net_for(units)
        units = round(min(units, held), 6)
        if units <= 0:
            return (False, 'liquidity', held)

        gross = round(units * price, 2)
        fee = round(gross * fee_ratio, 2)
        net = round(gross - fee, 2)
        if net <= 0:
            return (False, 'liquidity', held)
        basis = round(units * avg, 2)
        pnl = round(net - basis, 2)

        _, tbal = _reserve_take(c, net)
        # -gross out as a position payout, +fee back in as income: the two sum to the
        # -net actually debited, and only the fee lands in the window get_treasury_income
        # reads.
        _bank_log(c, chat_id, user_id, 'crypto_out', -gross, tbal, f'فروش {symbol}')
        if fee > 0:
            _bank_log(c, chat_id, user_id, 'treasury_in', fee, tbal, 'کارمزد معاملهٔ کریپتو')

        c.execute('UPDATE users SET size = COALESCE(size,0) + %s '
                  'WHERE user_id = %s AND chat_id = %s RETURNING size', (net, user_id, chat_id))
        wrow = c.fetchone()
        if wrow is None:
            raise RuntimeError('seller has no users row')
        after = float(wrow[0])
        # Same split as the buy, from the other side: the cost basis coming back is not
        # income, the profit on top of it is. Without this, holding a position over
        # midnight would read to the handicap as a loss and pay a growth bonus for it.
        c.execute('INSERT INTO size_log (chat_id, user_id, delta, balance_after, source, note) '
                  'VALUES (%s,%s,%s,%s,%s,%s)',
                  (chat_id, user_id, basis, after - pnl, 'crypto_principal', symbol))
        c.execute('INSERT INTO size_log (chat_id, user_id, delta, balance_after, source, note) '
                  'VALUES (%s,%s,%s,%s,%s,%s)',
                  (chat_id, user_id, pnl, after, 'crypto_pnl', symbol))

        left = round(held - units, 6)
        if left <= 1e-9:
            c.execute('UPDATE crypto_holdings SET amount = 0, avg_cost = 0 '
                      'WHERE user_id = %s AND chat_id = %s AND symbol = %s',
                      (user_id, chat_id, symbol))
            left = 0.0
        else:
            c.execute('UPDATE crypto_holdings SET amount = %s '
                      'WHERE user_id = %s AND chat_id = %s AND symbol = %s',
                      (left, user_id, chat_id, symbol))
        # The market is now shorter by these units, so the next quote is cheaper.
        c.execute('UPDATE crypto_prices SET net_units = COALESCE(net_units,0) - %s '
                  'WHERE symbol = %s', (units, symbol))
        return (True, units, price, net, fee, pnl, left)


def crypto_market_totals():
    """(holders, total_units_by_symbol) - what the market as a whole is holding, so
    /crypto can show how exposed the treasury actually is."""
    with get_connection() as conn:
        c = conn.cursor()
        c.execute('SELECT symbol, COALESCE(SUM(amount),0), COUNT(DISTINCT user_id) '
                  'FROM crypto_holdings WHERE COALESCE(amount,0) > 0 GROUP BY symbol')
        return {row[0]: (float(row[1]), int(row[2])) for row in c.fetchall()}


def try_start_heist(chat_id, cooldown_seconds):
    """Group-wide heist cooldown, claimed atomically so two simultaneous attempts
    can't both rob the same vault. Returns (True, 0) or (False, seconds_remaining)."""
    with get_connection() as conn:
        c = conn.cursor()
        c.execute('INSERT INTO bank_treasury (chat_id, last_heist_at) VALUES (%s, NOW()) '
                  'ON CONFLICT (chat_id) DO UPDATE SET last_heist_at = NOW() '
                  'WHERE bank_treasury.last_heist_at IS NULL '
                  '   OR bank_treasury.last_heist_at < NOW() - (%s || %s)::interval '
                  'RETURNING last_heist_at', (chat_id, cooldown_seconds, ' seconds'))
        if c.fetchone() is not None:
            return (True, 0)
        c.execute('SELECT CEIL(EXTRACT(EPOCH FROM (last_heist_at + (%s || %s)::interval - NOW()))) '
                  'FROM bank_treasury WHERE chat_id = %s', (cooldown_seconds, ' seconds', chat_id))
        row = c.fetchone()
        return (False, int(row[0]) if row and row[0] and row[0] > 0 else 0)


def release_heist_slot(chat_id):
    """Hands the group's cooldown slot back when a heist never actually happened - the
    accomplice declined, or the invitation expired unanswered. Without this a player
    could burn the group's three days by @-ing someone who was asleep, which is both a
    grief vector and the same "one bad draw silently burns the day" bug release_war_day
    exists to prevent."""
    with get_connection() as conn:
        c = conn.cursor()
        c.execute('UPDATE bank_treasury SET last_heist_at = NULL WHERE chat_id = %s',
                  (chat_id,))


def heist_take(chat_id, thief_id, partner_id, treasury_ratio, deposit_ratio,
               partner_share):
    """Drains the vault for a successful heist: `treasury_ratio` of the group's claim on
    the treasury plus `deposit_ratio` of every OTHER depositor's balance, split between
    the two conspirators, all in one transaction.

    Strictly zero-sum - every centimetre handed out is one taken from the treasury or
    from a named depositor, and the per-victim amounts are returned so the group can be
    told exactly who paid for it.

    Neither conspirator is robbed: you don't rob your own deposit, and you don't rob
    your accomplice's either. Skipping only the thief would have quietly taken a slice
    off the partner and handed most of it back to them, which is not a bug so much as an
    insult.

    Returns (total_loot, treasury_part, victims, thief_cut, partner_cut)."""
    with get_connection() as conn:
        c = conn.cursor()
        # `treasury_ratio` of what this GROUP could claim, not of the whole bot's
        # reserve. The vault is one pot now, so without the weight bound a single lucky
        # memory game in the smallest group would empty the savings of every player in
        # every group - see _group_weight.
        want = round(max(0.0, _group_claim(c, chat_id)) * treasury_ratio, 2)
        treasury_part, after = _reserve_take(c, want)
        treasury_part = round(treasury_part, 2)
        if treasury_part > 0:
            _bank_log(c, chat_id, thief_id, 'heist_treasury', -treasury_part, after)

        c.execute('SELECT b.user_id, COALESCE(u.first_name, %s), COALESCE(b.balance,0) '
                  'FROM bank_accounts b LEFT JOIN users u '
                  '  ON u.user_id = b.user_id AND u.chat_id = b.chat_id '
                  'WHERE b.chat_id = %s AND COALESCE(b.balance,0) > 0 '
                  'ORDER BY b.balance DESC FOR UPDATE OF b', ('?', chat_id))
        victims = []
        deposit_part = 0.0
        for uid, name, bal in c.fetchall():
            if uid == thief_id or uid == partner_id:
                continue  # neither conspirator robs their own deposit
            cut = round(float(bal) * deposit_ratio, 2)
            if cut <= 0:
                continue
            c.execute('UPDATE bank_accounts SET balance = COALESCE(balance,0) - %s '
                      'WHERE user_id = %s AND chat_id = %s RETURNING balance', (cut, uid, chat_id))
            brow = c.fetchone()
            if brow is None:
                continue
            _bank_log(c, chat_id, uid, 'heist_loss', -cut, brow[0])
            victims.append((uid, name, cut))
            deposit_part += cut

        total = round(treasury_part + deposit_part, 2)
        # Split the take. The partner's cut is rounded first and the thief gets the
        # remainder, so the two payouts sum to `total` EXACTLY - a heist has to stay as
        # zero-sum as it was when one person carried the whole bag, and splitting by two
        # independent roundings is how you mint a centimetre out of nowhere.
        partner_cut = round(total * partner_share, 2) if partner_id else 0.0
        thief_cut = round(total - partner_cut, 2)
        if total > 0:
            for uid, cut, note in ((thief_id, thief_cut, 'سرقت از بانک'),
                                   (partner_id, partner_cut, 'سهم شریک سرقت')):
                if not uid or cut <= 0:
                    continue
                c.execute('UPDATE users SET size = COALESCE(size,0) + %s '
                          'WHERE user_id = %s AND chat_id = %s RETURNING size',
                          (cut, uid, chat_id))
                wrow = c.fetchone()
                if wrow is None:
                    raise RuntimeError(f'heist payee {uid} has no users row')
                c.execute('INSERT INTO size_log (chat_id, user_id, delta, balance_after, '
                          'source, note) VALUES (%s, %s, %s, %s, %s, %s)',
                          (chat_id, uid, cut, wrow[0], 'bank_heist', note))
        return (total, treasury_part, victims, thief_cut, partner_cut)


# ---------------------------------------------------------------- heist mini-game
# /sarghat used to be a single hidden dice roll, then a single memory game. It is now a
# THREE-STAGE job that TWO people have to pull off together, settled from a persisted
# attempt row rather than in-process state - the same restart-survives-a-window lesson
# from pvp_matches applies here too.
#
#   stage 0  the offer      the named accomplice has to actually accept
#   stage 1  the alarm      the ACCOMPLICE cuts the right wire, on an unpredictable cue
#   stage 2  the vault      the THIEF plays the symbol-memory game
#   stage 3  the getaway    BOTH have to tap out before the clock runs
#
# The partner is structurally mandatory, not a courtesy: stage 1 is tapped by the
# accomplice and stage 3 needs both. One player literally cannot complete the job, which
# is a stronger guarantee than a rule saying they may not - and it means a heist now
# costs a conspiracy, since losing jails both of them.
#
# One ordering rule, copied from claim_war_day/release_war_day rather than reinvented:
# the group's cooldown slot is claimed BEFORE the offer is posted, so two players can
# never both open a heist, and released again if the offer is declined or expires. A
# refused invitation must not silently burn the group's three days.

HEIST_FIELDS = ('id', 'chat_id', 'thief_id', 'thief_name', 'partner_id', 'partner_name',
                'sequence', 'progress', 'would_be', 'message_chat_id', 'message_id',
                'status', 'stage', 'stage_deadline', 'wire', 'alarm_armed',
                'escape_thief', 'escape_partner', 'expires_at',
                # The two stored moments the run's timing hangs off. A surface with no
                # scheduler derives the whole run from these, so they have to be read.
                'alarm_at', 'vault_at')


def create_heist_offer(attempt_id, chat_id, thief_id, thief_name, partner_id,
                       partner_name, sequence, wire, would_be, message_chat_id,
                       message_id, expires_at):
    """Opens a heist at stage 0: everything is decided up front (the wire, the sequence)
    but nothing runs until the accomplice accepts. Rolling both here rather than at each
    stage keeps the whole run reproducible from one row, which is what lets the recovery
    sweep settle a half-finished job without having to re-roll anything."""
    with get_connection() as conn:
        c = conn.cursor()
        c.execute(
            'INSERT INTO heist_attempts (id, chat_id, thief_id, thief_name, partner_id, '
            'partner_name, sequence, wire, would_be, message_chat_id, message_id, '
            "status, stage, expires_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'offered',0,%s)",
            (attempt_id, chat_id, thief_id, thief_name, partner_id, partner_name,
             sequence, wire, would_be, message_chat_id, message_id, expires_at)
        )


def get_heist_attempt(attempt_id):
    """A dict, not a tuple. There are eighteen columns now and a positional unpack of
    that many is a bug waiting to happen every time one is added."""
    with get_connection() as conn:
        c = conn.cursor()
        c.execute('SELECT ' + ', '.join(HEIST_FIELDS) +
                  ' FROM heist_attempts WHERE id = %s', (attempt_id,))
        row = c.fetchone()
        return dict(zip(HEIST_FIELDS, row)) if row else None


def accept_heist_offer(attempt_id, partner_id, stage_deadline, alarm_seconds=None):
    """The accomplice signs up: 'offered' -> 'pending' at stage 1, atomically, so a
    double-tap or two clients racing can only ever start the job once.

    `alarm_seconds` fixes WHEN the cue lands, here, once. It used to exist only as the
    delay on a scheduled job, which meant the cue was a fact known to one process; a
    browser had no way to learn it and a restart re-rolled it. Storing it makes the
    moment the same for everybody and survives a deploy, exactly like the wire and the
    sequence being rolled once at create_heist_offer.
    """
    with get_connection() as conn:
        c = conn.cursor()
        c.execute("UPDATE heist_attempts SET status = 'pending', stage = 1, "
                  'stage_deadline = %s, '
                  "alarm_at = CASE WHEN %s::float8 IS NULL THEN NULL "
                  "                ELSE now() + make_interval(secs => %s) END "
                  "WHERE id = %s AND status = 'offered' AND partner_id = %s "
                  'RETURNING id',
                  (stage_deadline, alarm_seconds, alarm_seconds, attempt_id, partner_id))
        return c.fetchone() is not None


def cancel_heist_offer(attempt_id):
    """A declined or expired invitation. Deliberately 'cancelled', NOT 'lost': nobody
    tried to rob anything, so nobody goes to prison - and the caller releases the
    group's cooldown slot afterwards. Same shape as a /hokm-cancelled consensus vote."""
    with get_connection() as conn:
        c = conn.cursor()
        c.execute("UPDATE heist_attempts SET status = 'cancelled' "
                  "WHERE id = %s AND status = 'offered' RETURNING id", (attempt_id,))
        return c.fetchone() is not None


def arm_heist_alarm(attempt_id, stage_deadline):
    """The cue lands: the wire buttons go up and the accomplice's short window opens."""
    with get_connection() as conn:
        c = conn.cursor()
        c.execute('UPDATE heist_attempts SET alarm_armed = TRUE, stage_deadline = %s '
                  "WHERE id = %s AND status = 'pending' AND stage = 1 RETURNING id",
                  (stage_deadline, attempt_id))
        return c.fetchone() is not None


def cut_heist_wire(attempt_id, user_id, wire_index):
    """The accomplice's one tap at stage 1.

    Returns 'done' (right wire, on to the vault), 'wrong' (busted - already flipped to
    'lost' here, exactly like a wrong symbol tap), 'early' (the cue hasn't landed yet),
    'late' (the window closed), or None if the attempt is gone or not theirs."""
    with get_connection() as conn:
        c = conn.cursor()
        c.execute('SELECT partner_id, wire, alarm_armed, stage_deadline '
                  "FROM heist_attempts WHERE id = %s AND status = 'pending' AND stage = 1 "
                  'FOR UPDATE', (attempt_id,))
        row = c.fetchone()
        if row is None:
            return None
        partner_id, wire, armed, deadline = row
        if user_id != partner_id:
            return None
        if not armed:
            return 'early'
        c.execute('SELECT now() > %s', (deadline,))
        if c.fetchone()[0]:
            return 'late'
        if wire_index != wire:
            c.execute("UPDATE heist_attempts SET status = 'lost' WHERE id = %s",
                      (attempt_id,))
            return 'wrong'
        c.execute('UPDATE heist_attempts SET stage = 2, alarm_armed = FALSE, '
                  'stage_deadline = NULL WHERE id = %s', (attempt_id,))
        return 'done'


def start_heist_escape(attempt_id, stage_deadline):
    with get_connection() as conn:
        c = conn.cursor()
        c.execute('UPDATE heist_attempts SET stage = 3, stage_deadline = %s '
                  "WHERE id = %s AND status = 'pending' AND stage = 2 RETURNING id",
                  (stage_deadline, attempt_id))
        return c.fetchone() is not None


def tap_heist_escape(attempt_id, user_id):
    """Stage 3. Either conspirator may tap, in either order, but the vault only opens
    when BOTH are out - which is the moment the partnership stops being decorative.

    Returns 'waiting' (you're out, they aren't), 'done' (both out - won, already flipped
    here), 'again' (you already tapped), or None if it isn't yours or isn't live."""
    with get_connection() as conn:
        c = conn.cursor()
        c.execute('SELECT thief_id, partner_id, escape_thief, escape_partner '
                  "FROM heist_attempts WHERE id = %s AND status = 'pending' AND stage = 3 "
                  'FOR UPDATE', (attempt_id,))
        row = c.fetchone()
        if row is None:
            return None
        thief_id, partner_id, out_thief, out_partner = row
        if user_id == thief_id:
            if out_thief:
                return 'again'
            out_thief = True
            c.execute('UPDATE heist_attempts SET escape_thief = TRUE WHERE id = %s',
                      (attempt_id,))
        elif user_id == partner_id:
            if out_partner:
                return 'again'
            out_partner = True
            c.execute('UPDATE heist_attempts SET escape_partner = TRUE WHERE id = %s',
                      (attempt_id,))
        else:
            return None
        if out_thief and out_partner:
            c.execute("UPDATE heist_attempts SET status = 'won' WHERE id = %s",
                      (attempt_id,))
            return 'done'
        return 'waiting'


def advance_heist_attempt(attempt_id, tapped_index):
    """Stage 2: checks one tap against the next expected symbol, atomically. Returns
    'correct' (more remain), 'done' (the vault is open - but the job is NOT won yet,
    stage 3 still has to be survived), 'wrong' (busted - already flipped to 'lost' by
    this call), or None if the attempt is gone, already resolved, or not at stage 2.

    'done' deliberately no longer sets status='won'. The getaway is a real stage: a pair
    who cracked the vault and then failed to run still go to prison."""
    with get_connection() as conn:
        c = conn.cursor()
        c.execute(
            "SELECT sequence, progress FROM heist_attempts "
            "WHERE id = %s AND status = 'pending' AND stage = 2 FOR UPDATE", (attempt_id,)
        )
        row = c.fetchone()
        if row is None:
            return None
        sequence = [int(x) for x in row[0].split(',')]
        progress = row[1]
        if tapped_index != sequence[progress]:
            c.execute("UPDATE heist_attempts SET status = 'lost' WHERE id = %s", (attempt_id,))
            return 'wrong'
        progress += 1
        c.execute("UPDATE heist_attempts SET progress = %s WHERE id = %s", (progress, attempt_id))
        return 'done' if progress >= len(sequence) else 'correct'


def claim_expired_heist_attempt(attempt_id):
    """Atomically flips a still-pending, timed-out attempt to 'lost'. The WHERE clause is
    what stops the scheduled timeout job and the startup recovery sweep (or a last-second
    tap landing at the same instant) from ever settling the same attempt twice."""
    with get_connection() as conn:
        c = conn.cursor()
        c.execute(
            "UPDATE heist_attempts SET status = 'lost' WHERE id = %s AND status = 'pending' "
            'RETURNING id', (attempt_id,)
        )
        return c.fetchone() is not None


def claim_heist_stage_timeout(attempt_id, stage):
    """Atomically flips an attempt to 'lost' ONLY if it is still sitting at `stage`.

    The stage is part of the WHERE clause on purpose. Each stage schedules its own
    timeout job, so a pair who cleared the alarm with a second to spare has a stage-1
    timeout still in flight; without the stage check that job would happily kill them in
    the middle of the vault. Read-then-act in Python would leave the same race - this
    has to be one statement."""
    with get_connection() as conn:
        c = conn.cursor()
        c.execute("UPDATE heist_attempts SET status = 'lost' "
                  "WHERE id = %s AND status = 'pending' AND stage = %s RETURNING id",
                  (attempt_id, stage))
        return c.fetchone() is not None


def get_expired_heist_attempts():
    """(id, status, chat_id) for anything still live past its whole-run deadline - the
    startup recovery sweep's input, same role get_stale_pending_pvp_matches plays for
    challenges. Unanswered OFFERS come back too: they are cancelled rather than settled
    as a bust, and the caller hands the group's cooldown slot back."""
    with get_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT id, status, chat_id FROM heist_attempts "
                  "WHERE status IN ('pending', 'offered') "
                  "  AND (expires_at <= now() "
                  # A blown STAGE deadline counts too. The chat schedules a job per
                  # stage, but a heist played from the app has no scheduler behind it,
                  # so without this its stages would only ever be settled by the
                  # whole-run backstop - a player who let the vault clock run out would
                  # sit there for the rest of the run before being told they lost.
                  "       OR (status = 'pending' AND stage_deadline <= now()))")
        return c.fetchall()


# ---------------------------------------------------------------- heist prison & labor
# A busted heist doesn't just cost a fine anymore: heist_prison_until locks the thief out
# of growing, challenging, stealing, and heisting again; heist_labor_until runs longer
# and, once prison ends, just skims a cut of their daily growth to the king. Bail only
# ever buys out the prison half - the labor debt still has to be served.

def is_in_heist_prison(user_id, chat_id):
    """Cheap boolean check for the grow/challenge/theft gates - same shape as is_jester."""
    with get_connection() as conn:
        c = conn.cursor()
        c.execute('SELECT 1 FROM users WHERE user_id = %s AND chat_id = %s '
                  'AND heist_prison_until > now()', (user_id, chat_id))
        return c.fetchone() is not None


def get_heist_status(user_id, chat_id):
    """(prison_until, labor_until, bail_amount) - None for any field that was never set
    or has already lapsed doesn't matter here, the caller compares against now() itself."""
    with get_connection() as conn:
        c = conn.cursor()
        c.execute(
            'SELECT heist_prison_until, heist_labor_until, heist_bail_amount '
            'FROM users WHERE user_id = %s AND chat_id = %s', (user_id, chat_id)
        )
        return c.fetchone() or (None, None, None)


def send_to_heist_prison(user_id, chat_id, prison_days, labor_days, bail_amount):
    """Sentences a busted thief: prison_days locked out entirely, then labor_days more
    of paying tribute to the king. Returns (prison_until, labor_until)."""
    with get_connection() as conn:
        c = conn.cursor()
        c.execute(
            "UPDATE users SET heist_prison_until = NOW() + make_interval(days => %s), "
            "heist_labor_until = NOW() + make_interval(days => %s), "
            'heist_bail_amount = %s '
            'WHERE user_id = %s AND chat_id = %s '
            'RETURNING heist_prison_until, heist_labor_until',
            (prison_days, prison_days + labor_days, bail_amount, user_id, chat_id)
        )
        return c.fetchone()


def pay_heist_bail(user_id, chat_id):
    """One transaction: checks the bail is still owed and affordable, deducts it, and
    frees the player from heist prison - so a crash mid-operation can never take the
    money without releasing them, or release them without taking it. The labor-for-king
    period (if any remains) is untouched; bail buys freedom of movement, not freedom
    from the debt. Bail is a fee like any other, so it lands in the treasury rather than
    being destroyed. Returns (True, bail_amount) or (False, reason)."""
    with get_connection() as conn:
        c = conn.cursor()
        c.execute(
            "SELECT COALESCE(size,0), heist_bail_amount FROM users "
            "WHERE user_id = %s AND chat_id = %s AND heist_prison_until > now() FOR UPDATE",
            (user_id, chat_id)
        )
        row = c.fetchone()
        if row is None or row[1] is None:
            return (False, 'not_in_prison')
        size, bail = float(row[0]), float(row[1])
        if size < bail:
            return (False, 'funds')
        c.execute(
            'UPDATE users SET size = size - %s, heist_prison_until = NULL '
            'WHERE user_id = %s AND chat_id = %s RETURNING size',
            (bail, user_id, chat_id)
        )
        new_size = float(c.fetchone()[0])
        c.execute('INSERT INTO size_log (chat_id, user_id, delta, balance_after, source, note) '
                  'VALUES (%s, %s, %s, %s, %s, %s)',
                  (chat_id, user_id, -bail, new_size, 'heist_bail', 'وثیقهٔ سرقت بانک'))
        tbal = _reserve_credit(c, bail)
        _bank_log(c, chat_id, user_id, 'treasury_in', bail, tbal, 'وثیقهٔ سرقت بانک')
        return (True, bail)


def pardon_heist_prisoner(user_id, chat_id):
    """The king's pardon: clears prison AND the labor debt, and drops the bail owed.

    Deliberately wider than bail, which only ever buys out the prison half. The labor
    tribute is the king's *own* claim on the thief's growth, so he is the only one who
    can waive it - and waiving it costs him real size he would otherwise have collected.
    That is what makes a pardon a political act rather than a free favour.

    Returns True only if there was actually a live sentence to lift, so the caller can
    tell 'pardoned' apart from 'this player wasn't serving anything'."""
    with get_connection() as conn:
        c = conn.cursor()
        c.execute(
            'UPDATE users SET heist_prison_until = NULL, heist_labor_until = NULL, '
            'heist_bail_amount = NULL '
            'WHERE user_id = %s AND chat_id = %s '
            '  AND (heist_prison_until > now() OR heist_labor_until > now()) '
            'RETURNING user_id',
            (user_id, chat_id)
        )
        return c.fetchone() is not None


def get_bank_log(chat_id, limit=20):
    with get_connection() as conn:
        c = conn.cursor()
        c.execute('SELECT l.created_at, COALESCE(u.first_name, %s), l.kind, l.amount, l.note '
                  'FROM bank_log l LEFT JOIN users u '
                  '  ON u.user_id = l.user_id AND u.chat_id = l.chat_id '
                  'WHERE l.chat_id = %s ORDER BY l.id DESC LIMIT %s', ('—', chat_id, limit))
        return c.fetchall()


# ---------------------------------------------------------------- loans
# The ledger split is the subtle part. A loan's *principal* is a transfer between two
# pockets - it is not income for the borrower and not a loss for the lender - so it is
# logged under 'loan_principal', which get_recent_net_by_user ignores exactly the way it
# ignores bank transfers. The *interest* is the only real profit and loss in the whole
# arrangement, so it is logged separately under 'loan_interest' and does count. Without
# that split, taking a loan would look like a catastrophic loss to the nightly handicap
# and quietly pay the borrower a growth bonus for borrowing money.

def _size_move(c, chat_id, user_id, delta, source, note=None):
    """Applies a size change and writes the matching ledger row with an EXPLICIT source
    (rather than the caller-name guess update_size makes), inside the caller's
    transaction. Returns the new balance, or None if the user has no row."""
    c.execute('UPDATE users SET size = COALESCE(size,0) + %s '
              'WHERE user_id = %s AND chat_id = %s RETURNING size',
              (delta, user_id, chat_id))
    row = c.fetchone()
    if row is None:
        return None
    c.execute('INSERT INTO size_log (chat_id, user_id, delta, balance_after, source, note) '
              'VALUES (%s, %s, %s, %s, %s, %s)',
              (chat_id, user_id, delta, row[0], source, note))
    return float(row[0])


def create_loan_offer(chat_id, lender_id, lender_name, borrower_id, borrower_name,
                      principal, rate, term_days):
    """Records a pending offer. Nothing moves until the borrower accepts - so an offer
    that is never taken up costs the lender nothing and cannot be used to lock up
    someone's balance."""
    due_amount = round(principal * (1.0 + rate), 2)
    with get_connection() as conn:
        c = conn.cursor()
        c.execute(
            'INSERT INTO loans (chat_id, lender_id, lender_name, borrower_id, borrower_name, '
            'principal, rate, due_amount, status) '
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'offered') RETURNING id",
            (chat_id, lender_id, lender_name, borrower_id, borrower_name,
             principal, rate, due_amount)
        )
        return c.fetchone()[0], due_amount


def get_loan(loan_id):
    with get_connection() as conn:
        c = conn.cursor()
        c.execute('SELECT id, chat_id, lender_id, lender_name, borrower_id, borrower_name, '
                  'principal, rate, due_amount, COALESCE(paid,0), status, due_at '
                  'FROM loans WHERE id = %s', (loan_id,))
        return c.fetchone()


def count_active_loans(chat_id, user_id, as_lender):
    col = 'lender_id' if as_lender else 'borrower_id'
    with get_connection() as conn:
        c = conn.cursor()
        c.execute(f"SELECT COUNT(*) FROM loans WHERE chat_id = %s AND {col} = %s "
                  "AND status IN ('offered','active')", (chat_id, user_id))
        return c.fetchone()[0]


def accept_loan(loan_id, borrower_id, term_days, origination_ratio=0.0):
    """Atomically turns an offer into an active loan and hands over the principal.

    Claims the row with a conditional UPDATE first, so two taps on the same button
    cannot disburse twice. Returns (True, principal, due_amount, fee) or (False, reason).

    `origination_ratio` applies to the bank's own /vam only and is withheld from the
    DISBURSEMENT rather than billed later: the borrower receives principal - fee but
    still owes the full due_amount. That ordering is the point of the fee - it is the
    one part of a loan the bank collects even when the loan later defaults, which is
    what turns lending from a gamble into a business."""
    with get_connection() as conn:
        c = conn.cursor()
        c.execute("UPDATE loans SET status = 'active', accepted_at = NOW(), "
                  "due_at = NOW() + (%s || %s)::interval "
                  "WHERE id = %s AND status = 'offered' AND borrower_id = %s "
                  'RETURNING chat_id, lender_id, principal, due_amount',
                  (term_days, ' days', loan_id, borrower_id))
        row = c.fetchone()
        if row is None:
            return (False, 'gone', 0, 0.0)
        chat_id, lender_id, principal, due_amount = row

        if lender_id is None:
            # /vam is funded by the central bank out of everyone's DEPOSITS, not out of
            # the sink-fed reserve. That is the whole point of the modern model: savers'
            # money works instead of sitting in a box, and the interest borrowers pay is
            # what funds the interest savers earn.
            #
            # Two limits, and both are real. The reserve requirement keeps
            # CB_RESERVE_RATIO of deposits permanently un-lent so ordinary withdrawals
            # always clear, and the cash check makes sure the bank can actually hand the
            # principal over today.
            c.execute('SELECT COALESCE(loans_out,0) FROM central_bank WHERE id = %s FOR UPDATE',
                      (CB_SINGLETON,))
            crow = c.fetchone()
            loans_out = float(crow[0]) if crow else 0.0
            c.execute('SELECT COALESCE(SUM(balance),0) FROM bank_accounts '
                      'WHERE COALESCE(balance,0) > 0')
            deposits = float(c.fetchone()[0] or 0.0)
            reserve = _reserve_balance(c)

            lendable = deposits * (1.0 - CB_RESERVE_RATIO) + reserve - loans_out
            cash = reserve + deposits - loans_out
            if principal > lendable or principal > cash:
                c.execute("UPDATE loans SET status = 'offered', accepted_at = NULL, due_at = NULL "
                          'WHERE id = %s', (loan_id,))
                return (False, 'treasury', 0, 0.0)
            c.execute('UPDATE central_bank SET loans_out = COALESCE(loans_out,0) + %s '
                      'WHERE id = %s RETURNING loans_out', (principal, CB_SINGLETON))
            _bank_log(c, chat_id, borrower_id, 'loan_out', -principal, c.fetchone()[0],
                      f'وام #{loan_id} از بانک مرکزی')
        else:
            # Player lender: atomic check-and-take, so they cannot lend size they no
            # longer have by the time the borrower gets around to tapping accept.
            c.execute('UPDATE users SET size = COALESCE(size,0) - %s '
                      'WHERE user_id = %s AND chat_id = %s AND COALESCE(size,0) >= %s '
                      'RETURNING size', (principal, lender_id, chat_id, principal))
            lrow = c.fetchone()
            if lrow is None:
                c.execute("UPDATE loans SET status = 'offered', accepted_at = NULL, due_at = NULL "
                          'WHERE id = %s', (loan_id,))
                return (False, 'lender_broke', 0, 0.0)
            c.execute('INSERT INTO size_log (chat_id, user_id, delta, balance_after, source, note) '
                      'VALUES (%s, %s, %s, %s, %s, %s)',
                      (chat_id, lender_id, -principal, lrow[0], 'loan_principal', f'نزول #{loan_id}'))

        c.execute('SELECT COALESCE(size,0) FROM users WHERE user_id = %s AND chat_id = %s',
                  (borrower_id, chat_id))
        brow = c.fetchone()
        # Snapshot taken BEFORE the principal lands, so it measures what they were worth
        # when they asked - which is what makes the loan large or trivial for them.
        c.execute('UPDATE loans SET size_at_accept = %s WHERE id = %s',
                  (float(brow[0]) if brow else 0.0, loan_id))

        # The bank's origination fee comes out of the money handed over, never out of a
        # wallet that might be empty - so there is no path where the fee fails to be
        # collected. A player lender charges nothing of the sort: /nozul is unregulated,
        # which is half the reason it exists.
        fee = round(float(principal) * origination_ratio, 2) if lender_id is None else 0.0
        fee = max(0.0, min(fee, float(principal)))
        handed = round(float(principal) - fee, 2)
        if _size_move(c, chat_id, borrower_id, handed, 'loan_principal', f'وام #{loan_id}') is None:
            raise RuntimeError('borrower has no users row')
        if fee > 0:
            tbal = _reserve_credit(c, fee)
            _bank_log(c, chat_id, borrower_id, 'treasury_in', fee, tbal,
                      f'کارمزد صدور وام #{loan_id}')
        return (True, float(principal), float(due_amount), fee)


def _collect(c, chat_id, borrower_id, principal, interest, loan_id):
    """Pulls a whole debt out of a borrower, across every league they play in.

    Order, and the order is the whole point:

        1. the home group's wallet      (where the loan was taken)
        2. the home group's deposit
        3. EVERY OTHER GROUP, richest first: wallet, then deposit
        4. only then, the home wallet goes negative for whatever is still short

    Reaching into the bank is deliberate. The bank is safe from *theft*, but if it were
    safe from *debt* too then borrowing and immediately hiding the money in it would be
    a free money printer.

    Reaching into OTHER GROUPS is deliberate for the same reason, one level up. `/vam`
    is funded from the central bank's pooled deposits, which every group's savers paid
    into - so a debt to it is a debt to the whole bot, not to one league. If collection
    stopped at the home group, the dodge would be obvious and unstoppable: borrow in a
    group you keep empty, let that one wallet go negative, and keep the size you already
    had everywhere else. The lender's money is global, so the collector has to be too.

    Home first, though, and that is not arbitrary: the loan was taken against that
    group's standing and its wallet is the one that agreed to it. Other leagues are only
    reached for what the home group genuinely could not cover.

    LEDGER. Each seizure is logged in the group it was actually taken from - that is
    where the size left, so that is where size_log has to show it. Interest is charged
    first and against the home group where possible (it is the home loan's cost), which
    also keeps the invariant that each group's rows sum to exactly the change that
    group's wallet saw. The ledger cannot book money a wallet never paid.

    Returns (from_wallet, from_bank, shortfall, cross), where `cross` is
    [(chat_id, from_wallet, from_bank), ...] for the other groups that were reached."""
    total = round(principal + interest, 2)

    c.execute('SELECT COALESCE(size,0) FROM users WHERE user_id = %s AND chat_id = %s FOR UPDATE',
              (borrower_id, chat_id))
    row = c.fetchone()
    wallet = float(row[0]) if row else 0.0

    from_wallet = round(min(max(wallet, 0.0), total), 2)
    remaining = round(total - from_wallet, 2)

    def _seize_deposit(cid, want):
        """Takes up to `want` out of one group's deposit. bank_log only: the wallet
        never saw this size, so size_log must not claim it did."""
        if want <= 0.009:
            return 0.0
        c.execute('SELECT COALESCE(balance,0) FROM bank_accounts '
                  'WHERE user_id = %s AND chat_id = %s FOR UPDATE', (borrower_id, cid))
        brow = c.fetchone()
        got = round(min(max(float(brow[0]) if brow else 0.0, 0.0), want), 2)
        if got <= 0:
            return 0.0
        c.execute('UPDATE bank_accounts SET balance = COALESCE(balance,0) - %s '
                  'WHERE user_id = %s AND chat_id = %s RETURNING balance',
                  (got, borrower_id, cid))
        note = f'بدهی #{loan_id}' if cid == chat_id else f'بدهی #{loan_id} (گروه دیگر)'
        _bank_log(c, cid, borrower_id, 'debt_seized', -got, c.fetchone()[0], note)
        return got

    from_bank = _seize_deposit(chat_id, remaining)
    remaining = round(remaining - from_bank, 2)

    # --- every other league this borrower plays in, richest first ---------------
    # Richest first so the debt clears in the fewest groups touched, and so it lands on
    # the hoard the borrower actually moved the money to rather than nibbling every
    # league they ever said hello in.
    cross = []
    if remaining > 0.009:
        c.execute('SELECT u.chat_id, GREATEST(COALESCE(u.size,0), 0) AS wallet, '
                  '       GREATEST(COALESCE(b.balance,0), 0) AS deposit '
                  'FROM users u '
                  'LEFT JOIN bank_accounts b '
                  '  ON b.user_id = u.user_id AND b.chat_id = u.chat_id '
                  'WHERE u.user_id = %s AND u.chat_id < 0 AND u.chat_id <> %s '
                  '  AND (COALESCE(u.size,0) > 0 OR COALESCE(b.balance,0) > 0) '
                  'ORDER BY (GREATEST(COALESCE(u.size,0),0) '
                  '          + GREATEST(COALESCE(b.balance,0),0)) DESC, u.chat_id '
                  'FOR UPDATE OF u', (borrower_id, chat_id))
        others = c.fetchall()
        for other_id, other_wallet, _other_dep in others:
            if remaining <= 0.009:
                break
            took_w = round(min(float(other_wallet), remaining), 2)
            if took_w > 0.009:
                remaining = round(remaining - took_w, 2)
            else:
                took_w = 0.0
            took_b = _seize_deposit(other_id, remaining)
            remaining = round(remaining - took_b, 2)
            if took_w > 0 or took_b > 0:
                cross.append((other_id, took_w, took_b))

    # Nothing left anywhere: the debt is still owed in full, so the HOME wallet goes
    # negative for the rest. The lender is made whole either way - that is what the
    # borrower agreed to - and the hole is the borrower's problem to dig out of. It is
    # deliberately the home group that carries the hole: the other leagues were reached
    # for what they actually had, never pushed into debt of their own.
    shortfall = remaining if remaining > 0.009 else 0.0

    # --- book the wallet-borne seizures, interest first -------------------------
    interest_left = round(interest, 2)

    def _book(cid, amount):
        nonlocal interest_left
        if amount <= 0.009:
            return
        i = round(min(interest_left, amount), 2)
        p = round(amount - i, 2)
        interest_left = round(interest_left - i, 2)
        tail = '' if cid == chat_id else ' (گروه دیگر)'
        if p > 0.009:
            _size_move(c, cid, borrower_id, -p, 'loan_principal',
                       f'بازپرداخت #{loan_id}{tail}')
        if i > 0.009:
            _size_move(c, cid, borrower_id, -i, 'loan_interest',
                       f'سود بدهی #{loan_id}{tail}')

    _book(chat_id, round(from_wallet + shortfall, 2))
    for other_id, took_w, _took_b in cross:
        _book(other_id, took_w)

    return (from_wallet, from_bank, shortfall, cross)


def settle_loan(loan_id, forced, today_str=''):
    """Collects a loan in full and pays the lender. One transaction, so the borrower is
    never debited without the lender being credited.

    The money is split at payout: `principal` goes back under 'loan_principal' (a
    transfer the handicap ignores) and the interest under 'loan_interest' (real profit
    that it counts). Returns a dict describing what happened, or None if already closed."""
    with get_connection() as conn:
        c = conn.cursor()
        c.execute("UPDATE loans SET status = CASE WHEN %s THEN 'defaulted' ELSE 'repaid' END, "
                  'closed_at = NOW(), paid = due_amount '
                  "WHERE id = %s AND status = 'active' "
                  'RETURNING chat_id, lender_id, lender_name, borrower_id, borrower_name, '
                  'principal, due_amount', (forced, loan_id))
        row = c.fetchone()
        if row is None:
            return None
        chat_id, lender_id, lender_name, borrower_id, borrower_name, principal, due_amount = row
        principal = float(principal); due_amount = float(due_amount)
        interest = round(due_amount - principal, 2)

        from_wallet, from_bank, shortfall, cross = _collect(c, chat_id, borrower_id,
                                                            principal, interest, loan_id)
        from_other_groups = round(sum(w + b for _cid, w, b in cross), 2)

        if lender_id is None:
            # The principal was lent out of deposits, so retiring the debt is what puts
            # it back - loans_out comes down and the bank's cash rises by the same
            # amount. Only the INTEREST is earnings, and that is what lands in the
            # reserve to pay savers with. Booking the whole due_amount as reserve (which
            # is what this used to do) would have counted the principal twice.
            # Note there is no default loss to absorb here: _collect drives the
            # borrower's wallet negative for anything they can't cover, so the bank is
            # made whole every time and the hole stays the borrower's problem. That is a
            # deliberate pre-existing choice, and it is why this branch books the full
            # due_amount rather than only what was recoverable.
            interest_earned = round(due_amount - principal, 2)
            c.execute('UPDATE central_bank SET loans_out = GREATEST(0, COALESCE(loans_out,0) - %s) '
                      'WHERE id = %s RETURNING loans_out', (principal, CB_SINGLETON))
            loans_left = c.fetchone()[0]
            if interest_earned > 0:
                tbal = _reserve_credit(c, interest_earned)
                _bank_log(c, chat_id, borrower_id, 'loan_interest', interest_earned,
                          tbal, f'سود وام #{loan_id}')
            _bank_log(c, chat_id, borrower_id, 'loan_repaid', principal, loans_left,
                      f'اصل وام #{loan_id} برگشت به سپرده‌ها')
        else:
            _size_move(c, chat_id, lender_id, principal, 'loan_principal', f'اصل نزول #{loan_id}')
            if interest > 0:
                _size_move(c, chat_id, lender_id, interest, 'loan_interest', f'سود نزول #{loan_id}')

        # Score the borrower's behaviour in the same transaction that settles the
        # money, so a credit rating can never disagree with the loan book it describes.
        # The penalty is graded by how far the collector had to go: paying late is a
        # slip, being force-collected is a failure, and having to be dug out of your
        # bank deposit or left in the red is a worse one.
        c.execute('SELECT due_at < NOW(), COALESCE(size_at_accept,0), '
                  '       EXTRACT(EPOCH FROM (NOW() - accepted_at)), '
                  '       EXTRACT(EPOCH FROM (due_at - accepted_at)) '
                  'FROM loans WHERE id = %s', (loan_id,))
        drow = c.fetchone()
        was_late = bool(drow and drow[0])
        size_at_accept = float(drow[1]) if drow else 0.0
        held = float(drow[2] or 0) if drow else 0.0
        term = float(drow[3] or 1) if drow else 1.0

        if not forced:
            if was_late:
                delta, outcome = CREDIT_LATE, 'late'
            else:
                # A loan is only evidence of creditworthiness in proportion to what it
                # was worth to the borrower, and only if they actually carried it.
                significance = min(1.0, principal / max(1.0, size_at_accept))
                held_enough = term <= 0 or (held / term) >= CREDIT_MIN_HOLD_RATIO
                delta = int(round(CREDIT_ON_TIME * significance)) if held_enough else 0
                outcome = 'on_time' if delta > 0 else 'token'
        elif shortfall > 0:
            delta, outcome = CREDIT_SHORTFALL, 'shortfall'
        elif from_bank > 0 or from_other_groups > 0:
            # Having the collector dig into your deposit or reach into another league
            # are the same grade of failure: in both cases the home wallet could not
            # cover what you borrowed against it. Folded into one tier rather than given
            # a fifth constant, because the difference isn't one a player would feel.
            delta, outcome = CREDIT_BANK_SEIZED, 'bank_seized'
        else:
            delta, outcome = CREDIT_FORCED, 'forced'

        if delta > 0:
            # Roll the day's allowance over, then spend from it. Losses are never capped.
            c.execute("UPDATE users SET credit_gain_date = %s, credit_gain_today = 0 "
                      "WHERE user_id = %s AND chat_id = %s "
                      "AND COALESCE(credit_gain_date,'') <> %s",
                      (today_str, borrower_id, chat_id, today_str))
            c.execute('SELECT COALESCE(credit_gain_today,0) FROM users '
                      'WHERE user_id = %s AND chat_id = %s FOR UPDATE', (borrower_id, chat_id))
            grow = c.fetchone()
            used_today = int(grow[0]) if grow else 0
            delta = max(0, min(delta, CREDIT_DAILY_GAIN_CAP - used_today))
            if delta > 0:
                c.execute('UPDATE users SET credit_gain_today = COALESCE(credit_gain_today,0) + %s '
                          'WHERE user_id = %s AND chat_id = %s', (delta, borrower_id, chat_id))
            else:
                outcome = 'capped'

        c.execute('UPDATE users SET credit_score = LEAST(%s, GREATEST(%s, '
                  'COALESCE(credit_score, %s) + %s)) '
                  'WHERE user_id = %s AND chat_id = %s RETURNING credit_score',
                  (CREDIT_MAX, CREDIT_MIN, CREDIT_BASE, delta, borrower_id, chat_id))
        srow = c.fetchone()
        new_score = int(srow[0]) if srow else CREDIT_BASE

        if forced:
            c.execute('UPDATE users SET loan_defaults = COALESCE(loan_defaults,0) + 1 '
                      'WHERE user_id = %s AND chat_id = %s', (borrower_id, chat_id))
        elif was_late:
            c.execute('UPDATE users SET loans_late = COALESCE(loans_late,0) + 1, '
                      'loans_repaid = COALESCE(loans_repaid,0) + 1 '
                      'WHERE user_id = %s AND chat_id = %s', (borrower_id, chat_id))
        else:
            c.execute('UPDATE users SET loans_repaid = COALESCE(loans_repaid,0) + 1 '
                      'WHERE user_id = %s AND chat_id = %s', (borrower_id, chat_id))

        return {
            'chat_id': chat_id, 'lender_id': lender_id, 'lender_name': lender_name,
            'borrower_id': borrower_id, 'borrower_name': borrower_name,
            'principal': principal, 'due_amount': due_amount, 'interest': interest,
            'from_wallet': from_wallet, 'from_bank': from_bank, 'shortfall': shortfall,
            'cross': cross, 'from_other_groups': from_other_groups,
            'forced': forced, 'outcome': outcome, 'credit_delta': delta,
            'credit_score': new_score, 'was_late': was_late,
        }


def get_overdue_loans():
    """Active loans whose due date has passed, oldest first - the collection sweep's input."""
    with get_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT id FROM loans WHERE status = 'active' AND due_at IS NOT NULL "
                  'AND due_at <= NOW() ORDER BY due_at')
        return [r[0] for r in c.fetchall()]


def expire_loan_offers(ttl_seconds):
    """Drops offers nobody accepted. No money has moved, so this is pure cleanup."""
    with get_connection() as conn:
        c = conn.cursor()
        c.execute("UPDATE loans SET status = 'expired', closed_at = NOW() "
                  "WHERE status = 'offered' AND created_at < NOW() - (%s || %s)::interval",
                  (ttl_seconds, ' seconds'))
        return c.rowcount


def get_user_loans(chat_id, user_id):
    """(as_borrower, as_lender) active loans for the /bedehi screen."""
    with get_connection() as conn:
        c = conn.cursor()
        c.execute('SELECT id, lender_name, lender_id, due_amount, due_at, principal, rate '
                  "FROM loans WHERE chat_id = %s AND borrower_id = %s AND status = 'active' "
                  'ORDER BY due_at', (chat_id, user_id))
        borrowed = c.fetchall()
        c.execute('SELECT id, borrower_name, due_amount, due_at, principal, rate '
                  "FROM loans WHERE chat_id = %s AND lender_id = %s AND status = 'active' "
                  'ORDER BY due_at', (chat_id, user_id))
        lent = c.fetchall()
        return borrowed, lent


def get_debt_exposure(user_id, home_chat_id):
    """Everything a lender needs to price this borrower, across the WHOLE bot.

    A debt to the central bank is already a bot-wide fact: /vam is funded out of pooled
    deposits, and `_collect` reaches every league the borrower plays in. So a lender
    looking only at this group's loan book was seeing a fraction of the claim that
    already outranks theirs. Returns a dict:

        debt_here / loans_here       active borrowing in the group being asked from
        debt_away / loans_away       active borrowing everywhere else
        groups_away                  how many other groups that is spread over
        soonest_due                  the nearest due date of any of them
        to_bank                      how much of the total is owed to the bank
        assets                       wallets + deposits the collector could reach,
                                     floored at zero per group

    It deliberately does NOT return which groups. The number is what a lending decision
    turns on; the list would publish the borrower's group membership into a chat, which
    is the cross-group leak every other feature here is careful about.
    """
    with get_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT chat_id, due_amount, due_at, lender_id FROM loans "
                  "WHERE borrower_id = %s AND status = 'active'", (user_id,))
        rows = c.fetchall()
        c.execute('SELECT COALESCE(SUM(GREATEST(size, 0)), 0) FROM users WHERE user_id = %s',
                  (user_id,))
        wallets = float(c.fetchone()[0] or 0)
        c.execute('SELECT COALESCE(SUM(GREATEST(balance, 0)), 0) FROM bank_accounts '
                  'WHERE user_id = %s', (user_id,))
        deposits = float(c.fetchone()[0] or 0)

    here = [r for r in rows if r[0] == home_chat_id]
    away = [r for r in rows if r[0] != home_chat_id]
    dues = [r[2] for r in rows if r[2]]
    return {
        'debt_here': sum(float(r[1] or 0) for r in here),
        'loans_here': len(here),
        'debt_away': sum(float(r[1] or 0) for r in away),
        'loans_away': len(away),
        'groups_away': len({r[0] for r in away}),
        'soonest_due': min(dues) if dues else None,
        'to_bank': sum(float(r[1] or 0) for r in rows if r[3] is None),
        'assets': wallets + deposits,
    }


def get_loan_defaults(chat_id, user_id):
    with get_connection() as conn:
        c = conn.cursor()
        c.execute('SELECT COALESCE(loan_defaults,0) FROM users WHERE user_id = %s AND chat_id = %s',
                  (user_id, chat_id))
        row = c.fetchone()
        return int(row[0]) if row else 0


# ---------------------------------------------------------------- admin debt management
# The panel's own quiet override of the loan book, on top of the normal
# accept_loan/settle_loan flow above. Both functions here touch nothing but the `loans`
# row itself - no size moves, no credit_score change, no Telegram message from db.py or
# admin_panel.py - because this is meant to be invisible to every player involved.

def admin_list_active_loans(chat_id):
    """Every currently-outstanding loan in a group, for the admin panel's debt view.
    Soonest due first, same ordering as the collection sweep would process them in."""
    with get_connection() as conn:
        c = conn.cursor()
        c.execute(
            'SELECT id, lender_id, lender_name, borrower_id, borrower_name, '
            'principal, rate, due_amount, accepted_at, due_at '
            "FROM loans WHERE chat_id = %s AND status = 'active' ORDER BY due_at",
            (chat_id,)
        )
        return c.fetchall()


def admin_forgive_loan(loan_id):
    """Silently closes an active loan: no collection from the borrower, no payout to the
    lender (or treasury), no credit_score change. Marked 'forgiven' rather than
    'repaid'/'defaulted' so it stays distinguishable in the raw loan history, and that
    status change alone is what makes it vanish from every place that only reads
    'active' loans - get_overdue_loans (the collection sweep), get_user_loans (/bedehi),
    and count_active_loans (the per-player loan cap) - the instant this runs."""
    with get_connection() as conn:
        c = conn.cursor()
        c.execute("UPDATE loans SET status = 'forgiven', closed_at = NOW(), paid = 0 "
                  "WHERE id = %s AND status = 'active' RETURNING id", (loan_id,))
        return c.fetchone() is not None


def admin_set_loan_due_amount(loan_id, new_due_amount):
    """Overrides what an active loan will collect whenever it does eventually settle -
    on time, late, or force-collected - without moving any size right now. Settlement
    still runs through the normal settle_loan path later (so it still charges interest
    as due_amount minus principal, still pays the lender, and still scores the borrower's
    credit as usual) - only the amount owed is different from what was originally
    agreed. Dropping it below principal is allowed and works out as a partial forgiveness
    that the lender also absorbs a share of, same as the game's other zero-sum rules."""
    with get_connection() as conn:
        c = conn.cursor()
        c.execute("UPDATE loans SET due_amount = %s WHERE id = %s AND status = 'active' "
                  'RETURNING id', (new_due_amount, loan_id))
        return c.fetchone() is not None


# ---------------------------------------------------------------- cross-group transfer
# Every group is otherwise a completely separate league - the same player has an
# independent size in each. This is the one seam between them, and it is priced steeply
# on purpose: without a heavy fee, a player who is rich in one group could simply import
# that lead into another and skip the game entirely. It was closed once already after
# players farmed size in a friction-free side group and imported most of it back; the
# owner can now flip it back open from the admin panel (with the fee reset higher) via
# XFER_ENABLED_KEY / XFER_FEE_RATIO_KEY in bot_meta, rather than a code change.
XFER_ENABLED_KEY = 'xfer_enabled'
XFER_FEE_RATIO_KEY = 'xfer_fee_ratio'
XFER_DEFAULT_FEE_RATIO = 0.40


def is_xfer_enabled():
    with get_connection() as conn:
        c = conn.cursor()
        c.execute('SELECT value FROM bot_meta WHERE key = %s', (XFER_ENABLED_KEY,))
        row = c.fetchone()
        return row is not None and row[0] == '1'


def set_xfer_enabled(enabled):
    with get_connection() as conn:
        c = conn.cursor()
        c.execute(
            'INSERT INTO bot_meta (key, value) VALUES (%s, %s) '
            'ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value',
            (XFER_ENABLED_KEY, '1' if enabled else '0')
        )


def get_xfer_fee_ratio():
    with get_connection() as conn:
        c = conn.cursor()
        c.execute('SELECT value FROM bot_meta WHERE key = %s', (XFER_FEE_RATIO_KEY,))
        row = c.fetchone()
        if row is None:
            return XFER_DEFAULT_FEE_RATIO
        try:
            return float(row[0])
        except (TypeError, ValueError):
            return XFER_DEFAULT_FEE_RATIO


def set_xfer_fee_ratio(ratio):
    with get_connection() as conn:
        c = conn.cursor()
        c.execute(
            'INSERT INTO bot_meta (key, value) VALUES (%s, %s) '
            'ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value',
            (XFER_FEE_RATIO_KEY, str(ratio))
        )


def get_user_groups(user_id, exclude_chat_id=None):
    """Group chats where this player already has a row. Positive chat_ids are private
    chats with the bot, not groups, so they are never transfer destinations."""
    with get_connection() as conn:
        c = conn.cursor()
        c.execute('SELECT u.chat_id, COALESCE(u.size,0) FROM users u '
                  'WHERE u.user_id = %s AND u.chat_id < 0 '
                  '  AND (%s::bigint IS NULL OR u.chat_id <> %s) '
                  'ORDER BY u.size DESC',
                  (user_id, exclude_chat_id, exclude_chat_id))
        return c.fetchall()


XFER_POLICIES = ('auto', 'trusted', 'blocked')

# What a group has to prove before size is allowed to LEAVE it, judged against
# get_xfer_source_stats by bot.check_xfer_source.
#
# These live here rather than with the other game-balance constants in bot.py for one
# reason: admin_panel.py has to show the owner the same numbers the bot enforces, and
# the panel deliberately never imports bot.py. Two copies of a threshold is exactly the
# drift bug this codebase has been bitten by before, so there is one copy, here.
XFER_SOURCE_WINDOW_DAYS = 30       # the window "recently" means in all of the below
XFER_MIN_SOURCE_AGE_DAYS = 30      # a group spun up to farm is new
XFER_MIN_SOURCE_PLAYERS = 10       # ...and thin: real leagues have real crowds
XFER_MIN_SOURCE_MATCHES = 5        # ...and quiet: nobody there to challenge
XFER_MIN_SOURCE_MATCH_PLAYERS = 4  # five matches between two alts is not competition
XFER_MAX_SOURCE_SHARE = 0.60       # ...and owned outright by the farmer
XFER_MIN_TENURE_DAYS = 14          # and you can't parachute in to carry money out


def get_chat_xfer_policy(chat_id):
    """'auto' | 'trusted' | 'blocked' - the owner's manual override for this group as a
    transfer source. 'auto' (the default, including for groups with no row yet) means
    judge it on the numbers instead."""
    with get_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT COALESCE(xfer_policy, 'auto') FROM chats WHERE chat_id = %s",
                  (chat_id,))
        row = c.fetchone()
        policy = row[0] if row else 'auto'
        return policy if policy in XFER_POLICIES else 'auto'


def set_chat_xfer_policy(chat_id, policy):
    """Sets the override. Creates the chats row if the group was never tracked, so the
    owner can pre-block a group before anyone in it has played."""
    if policy not in XFER_POLICIES:
        raise ValueError(f'unknown xfer policy: {policy!r}')
    with get_connection() as conn:
        c = conn.cursor()
        c.execute('INSERT INTO chats (chat_id, xfer_policy) VALUES (%s, %s) '
                  'ON CONFLICT (chat_id) DO UPDATE SET xfer_policy = EXCLUDED.xfer_policy',
                  (chat_id, policy))


def get_xfer_source_stats(chat_id, user_id, window_days):
    """The evidence a group's fitness to be a transfer SOURCE is judged on.

    This exists because the farm-group exploit cannot be priced away: a group the player
    built themselves prints size with no theft, no challenges and no consensus votes to
    lose it to, so *any* fee under 100% leaves farming profitable. The fix has to be
    structural - refuse to let size leave a group that isn't a real league in the first
    place - and that means measuring the group, not the transfer.

    Returns a dict of facts only; the thresholds live in bot.check_xfer_source, next to
    the other game-balance constants.

      policy         owner override ('auto' | 'trusted' | 'blocked')
      age_days       group age, from its oldest player row
      active_players distinct players who grew inside the window
      matches        resolved 1v1 challenges inside the window
      match_players  distinct people who took part in those matches
      user_share     this player's share of every centimetre in the group (wallet+bank)
      tenure_days    how long this player has been in this group
    """
    with get_connection() as conn:
        c = conn.cursor()

        c.execute("SELECT COALESCE(xfer_policy, 'auto') FROM chats WHERE chat_id = %s",
                  (chat_id,))
        row = c.fetchone()
        policy = row[0] if row and row[0] in XFER_POLICIES else 'auto'

        # joined_at is the only per-group timestamp every player row carries, so the
        # oldest one is the closest thing to "when did this group start playing".
        c.execute('SELECT EXTRACT(EPOCH FROM (NOW() - MIN(joined_at))) / 86400.0 '
                  'FROM users WHERE chat_id = %s', (chat_id,))
        row = c.fetchone()
        age_days = float(row[0]) if row and row[0] is not None else 0.0

        # last_grown is a 'YYYY-MM-DD' text stamp, which sorts correctly as text - and
        # the '' default of a player who never grew sorts below every real date.
        c.execute("SELECT COUNT(*) FROM users WHERE chat_id = %s "
                  "  AND last_grown >= to_char(CURRENT_DATE - %s::int, 'YYYY-MM-DD')",
                  (chat_id, window_days))
        active_players = int(c.fetchone()[0] or 0)

        c.execute("SELECT COUNT(*) FROM pvp_matches WHERE chat_id = %s "
                  "  AND status = 'resolved' AND created_at >= NOW() - (%s || %s)::interval",
                  (chat_id, window_days, ' days'))
        matches = int(c.fetchone()[0] or 0)

        # Distinct people on either side of those matches: five challenges between the
        # same two alt accounts is not a competitive league.
        c.execute("SELECT COUNT(DISTINCT p) FROM ("
                  "  SELECT challenger_id AS p FROM pvp_matches WHERE chat_id = %s "
                  "    AND status = 'resolved' AND created_at >= NOW() - (%s || %s)::interval "
                  "  UNION "
                  "  SELECT acceptor_id AS p FROM pvp_matches WHERE chat_id = %s "
                  "    AND status = 'resolved' AND created_at >= NOW() - (%s || %s)::interval"
                  ") t",
                  (chat_id, window_days, ' days', chat_id, window_days, ' days'))
        match_players = int(c.fetchone()[0] or 0)

        # Wallets + deposits, because hiding the hoard in the bank must not make someone
        # look like a modest member of a group they in fact own outright.
        c.execute('SELECT COALESCE((SELECT SUM(size) FROM users WHERE chat_id = %s), 0) '
                  '     + COALESCE((SELECT SUM(balance) FROM bank_accounts WHERE chat_id = %s), 0)',
                  (chat_id, chat_id))
        total = float(c.fetchone()[0] or 0.0)
        c.execute('SELECT COALESCE((SELECT size FROM users WHERE user_id = %s AND chat_id = %s), 0) '
                  '     + COALESCE((SELECT balance FROM bank_accounts WHERE user_id = %s AND chat_id = %s), 0)',
                  (user_id, chat_id, user_id, chat_id))
        mine = float(c.fetchone()[0] or 0.0)
        # An empty (or net-negative) group counts as fully owned by the asker: that is
        # the safe direction to fail, since it's exactly the shape a fresh farm has.
        user_share = 1.0 if total <= 0 else max(0.0, min(1.0, mine / total))

        c.execute('SELECT EXTRACT(EPOCH FROM (NOW() - joined_at)) / 86400.0 '
                  'FROM users WHERE user_id = %s AND chat_id = %s', (user_id, chat_id))
        row = c.fetchone()
        tenure_days = float(row[0]) if row and row[0] is not None else 0.0

        return {
            'policy': policy,
            'age_days': age_days,
            'active_players': active_players,
            'matches': matches,
            'match_players': match_players,
            'user_share': user_share,
            'tenure_days': tenure_days,
        }


def try_start_xfer(user_id, chat_id, cooldown_seconds):
    """Per-player transfer cooldown, claimed atomically. (True, 0) or (False, seconds)."""
    with get_connection() as conn:
        c = conn.cursor()
        c.execute('UPDATE users SET last_xfer_at = NOW() '
                  'WHERE user_id = %s AND chat_id = %s '
                  '  AND (last_xfer_at IS NULL OR last_xfer_at < NOW() - (%s || %s)::interval) '
                  'RETURNING last_xfer_at',
                  (user_id, chat_id, cooldown_seconds, ' seconds'))
        if c.fetchone() is not None:
            return (True, 0)
        c.execute('SELECT CEIL(EXTRACT(EPOCH FROM (last_xfer_at + (%s || %s)::interval - NOW()))) '
                  'FROM users WHERE user_id = %s AND chat_id = %s',
                  (cooldown_seconds, ' seconds', user_id, chat_id))
        row = c.fetchone()
        return (False, int(row[0]) if row and row[0] and row[0] > 0 else 0)


def get_xfer_wait_remaining(user_id, chat_id, cooldown_seconds):
    """Seconds left on the transfer cooldown, WITHOUT claiming it.

    try_start_xfer is a claim: calling it to find out whether you may transfer would
    consume the slot for anyone merely opening the screen. The Mini App needs to show
    the countdown before the player commits, so it needs a read that costs nothing."""
    with get_connection() as conn:
        c = conn.cursor()
        c.execute('SELECT GREATEST(0, CEIL(EXTRACT(EPOCH FROM '
                  '  (last_xfer_at + (%s || %s)::interval - NOW())))) '
                  'FROM users WHERE user_id = %s AND chat_id = %s',
                  (cooldown_seconds, ' seconds', user_id, chat_id))
        row = c.fetchone()
        return int(row[0]) if row and row[0] else 0


def cross_group_transfer(user_id, from_chat, to_chat, amount, fee_ratio):
    """Moves one player's own size from one group to another, minus a heavy fee.

    One transaction across both groups, so the size can never exist in both at once or
    in neither. The fee goes to the one central reserve like every other fee - it is
    logged against the *source* group, because that is the group the size left.

    The principal is logged as 'xfer_principal' on both sides - it is the same player's
    money moving between leagues, not winnings, so the nightly handicap ignores it the
    way it ignores bank and loan transfers. The fee is a genuine cost and is logged as
    'xfer_fee', which does count.

    Returns (True, delivered, fee) or (False, reason, 0)."""
    fee = round(amount * fee_ratio, 2)
    delivered = round(amount - fee, 2)
    with get_connection() as conn:
        c = conn.cursor()
        # Atomic check-and-take at the source.
        c.execute('UPDATE users SET size = COALESCE(size,0) - %s '
                  'WHERE user_id = %s AND chat_id = %s AND COALESCE(size,0) >= %s RETURNING size',
                  (amount, user_id, from_chat, amount))
        srow = c.fetchone()
        if srow is None:
            return (False, 'funds', 0.0)
        c.execute('INSERT INTO size_log (chat_id, user_id, delta, balance_after, source, note) '
                  'VALUES (%s, %s, %s, %s, %s, %s)',
                  (from_chat, user_id, -delivered, srow[0], 'xfer_principal',
                   f'انتقال به گروه {to_chat}'))
        if fee > 0:
            c.execute('INSERT INTO size_log (chat_id, user_id, delta, balance_after, source, note) '
                      'VALUES (%s, %s, %s, %s, %s, %s)',
                      (from_chat, user_id, -fee, srow[0], 'xfer_fee', 'کارمزد انتقال'))
            tbal = _reserve_credit(c, fee)
            _bank_log(c, from_chat, user_id, 'treasury_in', fee, tbal, 'کارمزد انتقال')

        # The destination row must already exist - you can only send to a league you
        # actually play in, which is what stops this being a way to seed a brand new
        # account somewhere.
        c.execute('UPDATE users SET size = COALESCE(size,0) + %s '
                  'WHERE user_id = %s AND chat_id = %s RETURNING size',
                  (delivered, user_id, to_chat))
        drow = c.fetchone()
        if drow is None:
            raise RuntimeError('no destination users row')
        c.execute('INSERT INTO size_log (chat_id, user_id, delta, balance_after, source, note) '
                  'VALUES (%s, %s, %s, %s, %s, %s)',
                  (to_chat, user_id, delivered, drow[0], 'xfer_principal',
                   f'انتقال از گروه {from_chat}'))
        return (True, delivered, fee)


def get_credit(user_id, chat_id):
    """(score, repaid, late, defaults) - the whole credit file for one player."""
    with get_connection() as conn:
        c = conn.cursor()
        c.execute('SELECT COALESCE(credit_score, %s), COALESCE(loans_repaid,0), '
                  'COALESCE(loans_late,0), COALESCE(loan_defaults,0) '
                  'FROM users WHERE user_id = %s AND chat_id = %s',
                  (CREDIT_BASE, user_id, chat_id))
        return c.fetchone() or (CREDIT_BASE, 0, 0, 0)


def charge_credit_check(user_id, chat_id, fee):
    """Takes the credit-check fee and puts it in the treasury, atomically. Returns True
    if the caller could afford it. Like every other fee this is a transfer, not a burn."""
    with get_connection() as conn:
        c = conn.cursor()
        c.execute('UPDATE users SET size = COALESCE(size,0) - %s '
                  'WHERE user_id = %s AND chat_id = %s AND COALESCE(size,0) >= %s RETURNING size',
                  (fee, user_id, chat_id, fee))
        row = c.fetchone()
        if row is None:
            return False
        c.execute('INSERT INTO size_log (chat_id, user_id, delta, balance_after, source, note) '
                  'VALUES (%s, %s, %s, %s, %s, %s)',
                  (chat_id, user_id, -fee, row[0], 'credit_check', 'کارمزد اعتبارسنجی'))
        tbal = _reserve_credit(c, fee)
        _bank_log(c, chat_id, user_id, 'treasury_in', fee, tbal, 'کارمزد اعتبارسنجی')
        return True


# ---------------------------------------------------------------- inter-group war
# The one mechanic that moves size ACROSS leagues rather than inside one. It is still
# strictly zero-sum - every centimetre taken off a defender lands on an attacker in the
# same transaction - but it is zero-sum *globally*, not per group: the raided group's
# money supply genuinely shrinks and the raider's grows, so tick_inflation gives the
# loser cheaper prices and the winner dearer ones the same night. That is the intended
# consequence, not a leak.

WAR_DAY_KEY = 'last_war_date'


def claim_war_day(today_str):
    """One war per day for the whole bot, claimed atomically so the scheduled job and
    the startup catch-up can never both stage one. Returns True for the caller that
    won the race."""
    with get_connection() as conn:
        c = conn.cursor()
        c.execute('INSERT INTO bot_meta (key, value) VALUES (%s, %s) ON CONFLICT (key) DO NOTHING',
                  (WAR_DAY_KEY, ''))
        c.execute('UPDATE bot_meta SET value = %s WHERE key = %s AND COALESCE(value, %s) <> %s '
                  'RETURNING key', (today_str, WAR_DAY_KEY, '', today_str))
        return c.fetchone() is not None


def release_war_day():
    """Hands the day back when a claimed war couldn't actually be staged (no eligible
    pair, an empty defender), so a later restart can try again instead of the whole day
    being silently burned."""
    with get_connection() as conn:
        c = conn.cursor()
        c.execute("UPDATE bot_meta SET value = '' WHERE key = %s", (WAR_DAY_KEY,))


def has_any_war_happened():
    with get_connection() as conn:
        c = conn.cursor()
        c.execute('SELECT 1 FROM group_wars LIMIT 1')
        return c.fetchone() is not None


def count_war_roster(chat_id, active_days):
    """How many people in a group could actually take part in a raid - played recently
    and have size worth taking. Wallets only: deposits stay out of a war exactly as they
    stay out of /dozdi, which is the whole trade the bank sells."""
    with get_connection() as conn:
        c = conn.cursor()
        c.execute('SELECT COUNT(*) FROM users '
                  'WHERE chat_id = %s AND COALESCE(size,0) > 0 '
                  "  AND last_grown >= to_char(CURRENT_DATE - %s::int, 'YYYY-MM-DD') ",
                  (chat_id, active_days))
        return int(c.fetchone()[0] or 0)


def execute_group_war(war_date, attacker_chat, defender_chat, loot_ratio, active_days,
                      exclude_user_ids=()):
    """Runs one raid in a single transaction.

    Every centimetre is taken from a named defender and handed to a named attacker in
    the same transaction, and the function refuses to commit unless the two sides
    balance exactly - a war that moved size without conserving it would be the worst
    kind of bug here, silently minting into one league and burning another.

    Returns (True, detail) where detail carries the per-player lines the announcement
    needs, or (False, reason) when there was nothing to raid."""
    excluded = set(exclude_user_ids or ())
    with get_connection() as conn:
        c = conn.cursor()

        def roster(chat_id):
            c.execute("SELECT user_id, COALESCE(first_name, %s), COALESCE(size,0) FROM users "
                      'WHERE chat_id = %s AND COALESCE(size,0) > 0 '
                      "  AND last_grown >= to_char(CURRENT_DATE - %s::int, 'YYYY-MM-DD') "
                      'ORDER BY size DESC, user_id ASC FOR UPDATE',
                      ('?', chat_id, active_days))
            return [r for r in c.fetchall() if r[0] not in excluded]

        defenders = roster(defender_chat)
        attackers = roster(attacker_chat)
        if not defenders:
            return (False, 'no_defenders')
        if not attackers:
            return (False, 'no_attackers')

        # Proportional on the way out: the biggest wallets in the losing group pay the
        # most, so a raid hits the people who can afford it rather than flattening the
        # newcomers.
        takes = []
        for uid, name, size in defenders:
            amount = int(float(size) * loot_ratio)
            if amount > 0:
                takes.append((uid, name, amount))
        loot = sum(a for _u, _n, a in takes)
        if loot <= 0:
            return (False, 'nothing_to_take')

        # Equal on the way in: the spoils are shared by everyone who was in the fight,
        # not weighted toward whoever was already winning.
        per_head = loot // len(attackers)
        remainder = loot - per_head * len(attackers)
        gains = []
        for i, (uid, name, _size) in enumerate(attackers):
            amount = per_head + (1 if i < remainder else 0)
            if amount > 0:
                gains.append((uid, name, amount))
        if sum(a for _u, _n, a in gains) != loot:
            raise AssertionError('group war would not conserve size - refusing to commit')

        for uid, _name, amount in takes:
            c.execute('UPDATE users SET size = COALESCE(size,0) - %s '
                      'WHERE user_id = %s AND chat_id = %s RETURNING size',
                      (amount, uid, defender_chat))
            c.execute('INSERT INTO size_log (chat_id, user_id, delta, balance_after, source, note) '
                      'VALUES (%s, %s, %s, %s, %s, %s)',
                      (defender_chat, uid, -amount, c.fetchone()[0], 'war_loss',
                       f'غارت توسط گروه {attacker_chat}'))
        for uid, _name, amount in gains:
            c.execute('UPDATE users SET size = COALESCE(size,0) + %s '
                      'WHERE user_id = %s AND chat_id = %s RETURNING size',
                      (amount, uid, attacker_chat))
            c.execute('INSERT INTO size_log (chat_id, user_id, delta, balance_after, source, note) '
                      'VALUES (%s, %s, %s, %s, %s, %s)',
                      (attacker_chat, uid, amount, c.fetchone()[0], 'war_loot',
                       f'غنیمت از گروه {defender_chat}'))

        c.execute('INSERT INTO group_wars (war_date, attacker_chat, defender_chat, loot, '
                  'attackers, defenders) VALUES (%s, %s, %s, %s, %s, %s) RETURNING id',
                  (war_date, attacker_chat, defender_chat, loot, len(gains), len(takes)))
        war_id = c.fetchone()[0]
        return (True, {'war_id': war_id, 'loot': loot, 'takes': takes, 'gains': gains})


def get_recent_wars(limit=10):
    with get_connection() as conn:
        c = conn.cursor()
        c.execute('SELECT war_date, attacker_chat, defender_chat, loot, attackers, defenders '
                  'FROM group_wars ORDER BY id DESC LIMIT %s', (limit,))
        return c.fetchall()


# ---------------------------------------------------------------- economy
# The inflation index is the spine of this. Every price the game quotes and every payout
# it makes is multiplied by it, so it is not a decorative number: printing money really
# does make the shop expensive, and squeezing the supply really does make savings worth
# more. It moves two ways - automatically, in response to how fast the group's money
# supply is actually growing, and deliberately, whenever the king signs a decree.

INFLATION_MIN, INFLATION_MAX = 0.40, 6.00
UNREST_MIN, UNREST_MAX = 0.0, 100.0
# How hard the index chases the money supply, and how much of that gap it closes a night.
INFLATION_SENSITIVITY = 2.5
INFLATION_SMOOTHING = 0.35
MULT_MIN, MULT_MAX = 0.25, 3.00


def get_economy(chat_id):
    """(inflation, unrest, fee_mult, interest_mult, growth_mult), creating the row."""
    with get_connection() as conn:
        c = conn.cursor()
        c.execute('INSERT INTO economy (chat_id) VALUES (%s) ON CONFLICT (chat_id) DO NOTHING',
                  (chat_id,))
        c.execute('SELECT COALESCE(inflation,1.0), COALESCE(unrest,0), COALESCE(fee_mult,1.0), '
                  'COALESCE(interest_mult,1.0), COALESCE(growth_mult,1.0) '
                  'FROM economy WHERE chat_id = %s', (chat_id,))
        return c.fetchone() or (1.0, 0.0, 1.0, 1.0, 1.0)


def get_economy_full(chat_id):
    with get_connection() as conn:
        c = conn.cursor()
        c.execute('INSERT INTO economy (chat_id) VALUES (%s) ON CONFLICT (chat_id) DO NOTHING',
                  (chat_id,))
        c.execute('SELECT COALESCE(inflation,1.0), COALESCE(unrest,0), COALESCE(fee_mult,1.0), '
                  'COALESCE(interest_mult,1.0), COALESCE(growth_mult,1.0), supply_last, '
                  "COALESCE(last_decree_date,''), COALESCE(decrees_good,0), COALESCE(decrees_bad,0) "
                  'FROM economy WHERE chat_id = %s', (chat_id,))
        return c.fetchone()


def get_money_supply(chat_id):
    """Everything in circulation in one group: wallets + deposits.

    The treasury is deliberately NOT part of this any more. It is one pot for the whole
    bot, so counting it here would count the same size once per group and make every
    group's inflation move together for reasons that have nothing to do with that group.
    Size paid into a sink has genuinely left this league's circulation, and the index
    should say so."""
    with get_connection() as conn:
        c = conn.cursor()
        c.execute('SELECT COALESCE((SELECT SUM(size) FROM users WHERE chat_id = %s), 0) '
                  '     + COALESCE((SELECT SUM(balance) FROM bank_accounts WHERE chat_id = %s), 0)',
                  (chat_id, chat_id))
        return float(c.fetchone()[0] or 0)


def tick_inflation(chat_id, today_str):
    """Once a night: move the index toward what the money supply says it should be.

    This is the automatic half of the system. If the group's total size grew 20% in a
    day, prices are chasing a 1.5 index; if the supply shrank, the index falls and
    savings gain. The king's decrees push the same number around on purpose - this only
    reacts to what actually happened. Returns (before, after, supply_growth) or None if
    it already ran today."""
    with get_connection() as conn:
        c = conn.cursor()
        c.execute('INSERT INTO economy (chat_id) VALUES (%s) ON CONFLICT (chat_id) DO NOTHING',
                  (chat_id,))
        c.execute("UPDATE economy SET last_tick_date = %s "
                  "WHERE chat_id = %s AND COALESCE(last_tick_date,'') <> %s "
                  'RETURNING COALESCE(inflation,1.0), supply_last',
                  (today_str, chat_id, today_str))
        row = c.fetchone()
        if row is None:
            return None
        inflation, supply_last = float(row[0]), row[1]

        c.execute('SELECT COALESCE((SELECT SUM(size) FROM users WHERE chat_id = %s), 0) '
                  '     + COALESCE((SELECT SUM(balance) FROM bank_accounts WHERE chat_id = %s), 0)',
                  (chat_id, chat_id))
        supply = float(c.fetchone()[0] or 0)

        if supply_last is None or float(supply_last) <= 0:
            # First night: just record where we started, nothing to compare against.
            c.execute('UPDATE economy SET supply_last = %s WHERE chat_id = %s', (supply, chat_id))
            return (inflation, inflation, 0.0)

        growth = (supply - float(supply_last)) / float(supply_last)
        target = 1.0 + growth * INFLATION_SENSITIVITY
        after = inflation + (target - inflation) * INFLATION_SMOOTHING
        after = max(INFLATION_MIN, min(INFLATION_MAX, round(after, 4)))
        c.execute('UPDATE economy SET inflation = %s, supply_last = %s WHERE chat_id = %s',
                  (after, supply, chat_id))
        return (inflation, after, growth)


def bump_inflation(chat_id, delta):
    """Nudges the index directly and immediately, independent of the nightly tick.

    Used when a shop item's stock sells all the way out - that is itself real evidence
    of scarcity, not something worth waiting for tick_inflation to infer from the money
    supply the next night."""
    with get_connection() as conn:
        c = conn.cursor()
        c.execute('INSERT INTO economy (chat_id) VALUES (%s) ON CONFLICT (chat_id) DO NOTHING',
                  (chat_id,))
        c.execute('UPDATE economy SET inflation = GREATEST(%s, LEAST(%s, COALESCE(inflation,1.0) + %s)) '
                  'WHERE chat_id = %s RETURNING inflation',
                  (INFLATION_MIN, INFLATION_MAX, delta, chat_id))
        return float(c.fetchone()[0])


# ---------------------------------------------------------------- shop supply/demand
# Every item's price and stock are shared by the whole group, not per player - buying
# four of the day's five leaves exactly one for everyone else combined, not five each.

def get_shop_item_counts(chat_id, item_name, today_str, week_str):
    """(day_count, week_count) sold so far, lazily zeroed once the stamped day/week is
    stale. Read-only - does not create a row, since /shop is rendered far more often
    than anyone actually buys."""
    with get_connection() as conn:
        c = conn.cursor()
        c.execute('SELECT day, day_count, week, week_count FROM shop_item_state '
                  'WHERE chat_id = %s AND item_name = %s', (chat_id, item_name))
        row = c.fetchone()
        if row is None:
            return 0, 0
        day, day_count, week, week_count = row
        return (day_count if day == today_str else 0,
                week_count if week == week_str else 0)


def claim_shop_purchase(chat_id, item_name, today_str, week_str, daily_limit, weekly_limit):
    """Atomically claims one global purchase slot for an item.

    Returns (True, day_count_before, week_count_before) - the counts BEFORE this sale,
    which the caller prices the purchase from, so the price shown on the button and the
    price actually charged can never disagree. Returns (False, 'day') or (False, 'week')
    when that cap is already exhausted. Call release_shop_purchase if payment then
    fails, or a declined purchase would still burn the group's one remaining slot."""
    with get_connection() as conn:
        c = conn.cursor()
        c.execute('INSERT INTO shop_item_state (chat_id, item_name, day, week) '
                  'VALUES (%s, %s, %s, %s) ON CONFLICT (chat_id, item_name) DO NOTHING',
                  (chat_id, item_name, today_str, week_str))
        c.execute('SELECT day, day_count, week, week_count FROM shop_item_state '
                  'WHERE chat_id = %s AND item_name = %s FOR UPDATE',
                  (chat_id, item_name))
        day, day_count, week, week_count = c.fetchone()
        if day != today_str:
            day_count = 0
        if week != week_str:
            week_count = 0
        if day_count >= daily_limit:
            return False, 'day', None
        if week_count >= weekly_limit:
            return False, 'week', None
        c.execute('UPDATE shop_item_state SET day = %s, day_count = %s, week = %s, week_count = %s '
                  'WHERE chat_id = %s AND item_name = %s',
                  (today_str, day_count + 1, week_str, week_count + 1, chat_id, item_name))
        return True, day_count, week_count


def release_shop_purchase(chat_id, item_name, today_str, week_str):
    """Hands back a slot claimed by claim_shop_purchase when payment then failed. A
    no-op wherever the day/week has since rolled over - there is nothing stale to give
    back, and the fresh period already started at zero."""
    with get_connection() as conn:
        c = conn.cursor()
        c.execute('UPDATE shop_item_state SET day_count = GREATEST(0, day_count - 1) '
                  'WHERE chat_id = %s AND item_name = %s AND day = %s',
                  (chat_id, item_name, today_str))
        c.execute('UPDATE shop_item_state SET week_count = GREATEST(0, week_count - 1) '
                  'WHERE chat_id = %s AND item_name = %s AND week = %s',
                  (chat_id, item_name, week_str))


def claim_decree_day(chat_id, today_str):
    """One signed decree per group per day, claimed atomically."""
    with get_connection() as conn:
        c = conn.cursor()
        c.execute('INSERT INTO economy (chat_id) VALUES (%s) ON CONFLICT (chat_id) DO NOTHING',
                  (chat_id,))
        c.execute("UPDATE economy SET last_decree_date = %s "
                  "WHERE chat_id = %s AND COALESCE(last_decree_date,'') <> %s RETURNING chat_id",
                  (today_str, chat_id, today_str))
        return c.fetchone() is not None


def release_decree_day(chat_id):
    """Hands the day back if applying the decree failed, so the king isn't locked out."""
    with get_connection() as conn:
        c = conn.cursor()
        c.execute("UPDATE economy SET last_decree_date = '' WHERE chat_id = %s", (chat_id,))


def apply_decree(chat_id, king_id, king_name, today_str, code, title, kind, eff):
    """Applies one decree's whole effect in a single transaction.

    `eff` is the effect dict from decrees.py. Everything that moves size moves it
    between real holders - the king, the treasury, the players - so a decree
    redistributes and never conjures, with one deliberate exception: 'mint', which is
    the king literally debasing the currency and is the only path in the game that
    creates size. It is what makes the worst decrees genuinely corrosive rather than
    merely unfair.

    Returns a dict describing what happened."""
    with get_connection() as conn:
        c = conn.cursor()
        # The row has to exist before the UPDATE at the bottom, or a group whose economy
        # nobody has read yet silently absorbs every decree's inflation and unrest into
        # a row that isn't there - the decree appears to work and changes nothing.
        c.execute('INSERT INTO economy (chat_id) VALUES (%s) ON CONFLICT (chat_id) DO NOTHING',
                  (chat_id,))
        c.execute('SELECT COALESCE(inflation,1.0), COALESCE(unrest,0), COALESCE(fee_mult,1.0), '
                  'COALESCE(interest_mult,1.0), COALESCE(growth_mult,1.0) '
                  'FROM economy WHERE chat_id = %s FOR UPDATE', (chat_id,))
        row = c.fetchone() or (1.0, 0.0, 1.0, 1.0, 1.0)
        inflation, unrest, fee_m, int_m, grow_m = (float(x) for x in row)
        inflation_before = inflation

        # What this group could actually draw on, not the whole bot's reserve: a
        # corrupt decree helps itself to a fraction of "the treasury", and the treasury
        # is now shared with every other group. See _group_weight.
        treasury = _group_claim(c, chat_id)

        king_delta = 0.0
        treasury_delta = 0.0
        players_delta = 0.0
        minted = 0.0
        notes = []

        # --- king takes a cut of the treasury (or pays into it) ---
        if eff.get('treasury_to_king'):
            amount = round(treasury * eff['treasury_to_king'], 2)
            amount = min(amount, treasury)
            if amount > 0:
                treasury_delta -= amount
                king_delta += amount
                notes.append(f'{int(amount)} از خزانه')
        if eff.get('king_to_treasury'):
            c.execute('SELECT COALESCE(size,0) FROM users WHERE user_id = %s AND chat_id = %s',
                      (king_id, chat_id))
            krow = c.fetchone()
            k_size = float(krow[0]) if krow else 0.0
            amount = round(max(0.0, k_size) * eff['king_to_treasury'], 2)
            if amount > 0:
                treasury_delta += amount
                king_delta -= amount
                notes.append(f'{int(amount)} از جیب پادشاه به خزانه')

        # --- levy on every other player, straight to the king ---
        if eff.get('levy'):
            c.execute('SELECT user_id, COALESCE(size,0) FROM users '
                      'WHERE chat_id = %s AND user_id <> %s AND COALESCE(size,0) > 0',
                      (chat_id, king_id))
            for uid, usize in c.fetchall():
                cut = round(float(usize) * eff['levy'], 2)
                if cut <= 0:
                    continue
                c.execute('UPDATE users SET size = COALESCE(size,0) - %s '
                          'WHERE user_id = %s AND chat_id = %s RETURNING size', (cut, uid, chat_id))
                r2 = c.fetchone()
                if r2 is None:
                    continue
                c.execute('INSERT INTO size_log (chat_id, user_id, delta, balance_after, source, note) '
                          'VALUES (%s,%s,%s,%s,%s,%s)',
                          (chat_id, uid, -cut, r2[0], 'decree', title))
                king_delta += cut
                players_delta -= cut

        # --- handout to every other player, from the king's own pocket ---
        if eff.get('handout'):
            c.execute('SELECT user_id FROM users WHERE chat_id = %s AND user_id <> %s',
                      (chat_id, king_id))
            targets = [r[0] for r in c.fetchall()]
            per = float(eff['handout'])
            for uid in targets:
                c.execute('UPDATE users SET size = COALESCE(size,0) + %s '
                          'WHERE user_id = %s AND chat_id = %s RETURNING size', (per, uid, chat_id))
                r2 = c.fetchone()
                if r2 is None:
                    continue
                c.execute('INSERT INTO size_log (chat_id, user_id, delta, balance_after, source, note) '
                          'VALUES (%s,%s,%s,%s,%s,%s)',
                          (chat_id, uid, per, r2[0], 'decree', title))
                king_delta -= per
                players_delta += per

        # --- relief aimed only at whoever is actually poor ---
        if eff.get('relief'):
            c.execute('SELECT user_id FROM users WHERE chat_id = %s AND user_id <> %s '
                      'AND COALESCE(size,0) < %s', (chat_id, king_id, eff.get('relief_below', 60)))
            for (uid,) in c.fetchall():
                per = float(eff['relief'])
                c.execute('UPDATE users SET size = COALESCE(size,0) + %s '
                          'WHERE user_id = %s AND chat_id = %s RETURNING size', (per, uid, chat_id))
                r2 = c.fetchone()
                if r2 is None:
                    continue
                c.execute('INSERT INTO size_log (chat_id, user_id, delta, balance_after, source, note) '
                          'VALUES (%s,%s,%s,%s,%s,%s)',
                          (chat_id, uid, per, r2[0], 'decree', title))
                king_delta -= per
                players_delta += per

        # --- debasement: the only thing in the game that creates size ---
        if eff.get('mint'):
            minted = float(eff['mint'])
            king_delta += minted
            notes.append(f'{int(minted)} سانت چاپ شد')

        # --- burning: size leaves the world entirely ---
        if eff.get('burn_king'):
            c.execute('SELECT COALESCE(size,0) FROM users WHERE user_id = %s AND chat_id = %s',
                      (king_id, chat_id))
            krow = c.fetchone()
            k_size = float(krow[0]) if krow else 0.0
            amount = round(max(0.0, k_size) * eff['burn_king'], 2)
            if amount > 0:
                king_delta -= amount
                notes.append(f'{int(amount)} سانت سوزانده شد')

        # --- settle the king's net movement in one ledger row ---
        if abs(king_delta) > 0.009:
            c.execute('UPDATE users SET size = COALESCE(size,0) + %s '
                      'WHERE user_id = %s AND chat_id = %s RETURNING size',
                      (king_delta, king_id, chat_id))
            krow = c.fetchone()
            if krow is not None:
                c.execute('INSERT INTO size_log (chat_id, user_id, delta, balance_after, source, note) '
                          'VALUES (%s,%s,%s,%s,%s,%s)',
                          (chat_id, king_id, king_delta, krow[0], 'decree', title))

        if abs(treasury_delta) > 0.009:
            if treasury_delta > 0:
                tbal = _reserve_credit(c, treasury_delta)
            else:
                treasury_delta, tbal = _reserve_take(c, -treasury_delta)
                treasury_delta = -treasury_delta
            _bank_log(c, chat_id, king_id, 'decree', treasury_delta, tbal, title)

        # --- the dials ---
        inflation = max(INFLATION_MIN, min(INFLATION_MAX,
                        inflation + float(eff.get('inflation', 0.0))))
        unrest = max(UNREST_MIN, min(UNREST_MAX, unrest + float(eff.get('unrest', 0.0))))
        for key, cur in (('fee_mult', fee_m), ('interest_mult', int_m), ('growth_mult', grow_m)):
            if eff.get(key):
                val = max(MULT_MIN, min(MULT_MAX, cur * float(eff[key])))
                if key == 'fee_mult': fee_m = val
                elif key == 'interest_mult': int_m = val
                else: grow_m = val

        c.execute('UPDATE economy SET inflation = %s, unrest = %s, fee_mult = %s, '
                  'interest_mult = %s, growth_mult = %s, '
                  'decrees_good = COALESCE(decrees_good,0) + %s, '
                  'decrees_bad = COALESCE(decrees_bad,0) + %s '
                  'WHERE chat_id = %s',
                  (round(inflation, 4), round(unrest, 2), round(fee_m, 3), round(int_m, 3),
                   round(grow_m, 3), 1 if kind == 'good' else 0, 1 if kind == 'bad' else 0,
                   chat_id))

        c.execute('INSERT INTO decree_log (chat_id, king_id, king_name, decree_date, code, title, '
                  'kind, inflation_before, inflation_after, king_delta, unrest_after) '
                  'VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                  (chat_id, king_id, king_name, today_str, code, title, kind,
                   inflation_before, inflation, king_delta, unrest))

        return {
            'king_delta': round(king_delta, 2), 'treasury_delta': round(treasury_delta, 2),
            'players_delta': round(players_delta, 2), 'minted': minted,
            'inflation_before': round(inflation_before, 3), 'inflation': round(inflation, 3),
            'unrest': round(unrest, 1), 'fee_mult': round(fee_m, 2),
            'interest_mult': round(int_m, 2), 'growth_mult': round(grow_m, 2),
            'notes': notes,
        }


def cool_unrest(chat_id, amount):
    with get_connection() as conn:
        c = conn.cursor()
        c.execute('UPDATE economy SET unrest = GREATEST(%s, COALESCE(unrest,0) - %s) '
                  'WHERE chat_id = %s RETURNING unrest', (UNREST_MIN, amount, chat_id))
        row = c.fetchone()
        return float(row[0]) if row else 0.0


def get_decree_history(chat_id, limit=10):
    with get_connection() as conn:
        c = conn.cursor()
        c.execute('SELECT decree_date, king_name, title, kind, inflation_before, inflation_after, '
                  'king_delta FROM decree_log WHERE chat_id = %s ORDER BY id DESC LIMIT %s',
                  (chat_id, limit))
        return c.fetchall()


# ---------------------------------------------------------------- martial law
# The crown's answer to mob rule: dissolve an open /ejma and put whoever called it in
# the motley for a day. Deliberately rationed and deliberately unpopular - it is real
# power, so the price is paid in unrest, which is the thing that eventually gets a king
# dragged out of his palace.

def try_martial_law(chat_id, cooldown_seconds):
    """Claims the group's martial-law slot atomically. (True, 0) or (False, seconds_left)."""
    with get_connection() as conn:
        c = conn.cursor()
        c.execute('INSERT INTO economy (chat_id) VALUES (%s) ON CONFLICT (chat_id) DO NOTHING',
                  (chat_id,))
        c.execute('UPDATE economy SET last_martial_at = NOW() WHERE chat_id = %s '
                  '  AND (last_martial_at IS NULL '
                  '       OR last_martial_at < NOW() - (%s || %s)::interval) '
                  'RETURNING last_martial_at', (chat_id, cooldown_seconds, ' seconds'))
        if c.fetchone() is not None:
            return (True, 0)
        c.execute('SELECT CEIL(EXTRACT(EPOCH FROM (last_martial_at + (%s || %s)::interval - NOW()))) '
                  'FROM economy WHERE chat_id = %s', (cooldown_seconds, ' seconds', chat_id))
        row = c.fetchone()
        return (False, int(row[0]) if row and row[0] and row[0] > 0 else 0)


def release_martial_law(chat_id):
    """Hands the slot back if the decree could not actually be carried out."""
    with get_connection() as conn:
        c = conn.cursor()
        c.execute('UPDATE economy SET last_martial_at = NULL WHERE chat_id = %s', (chat_id,))


def cancel_consensus(vote_id, chat_id):
    """Dissolves one open vote. Returns (target_id, target_name, initiator_id, amount)
    for the announcement, or None if it had already closed.

    Note what this deliberately does NOT do: the target gets no protection window. A
    dissolved vote was never decided, so the group is free to try again tomorrow - the
    king bought his favourite a night, not immunity."""
    with get_connection() as conn:
        c = conn.cursor()
        c.execute("UPDATE consensus_votes SET status = 'cancelled', resolved_at = now() "
                  "WHERE id = %s AND chat_id = %s AND status = 'open' "
                  'RETURNING target_id, target_name, initiator_id, amount',
                  (vote_id, chat_id))
        return c.fetchone()


def get_any_open_consensus(chat_id):
    """Every open vote in a group, oldest first - what martial law is aimed at."""
    with get_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT id, target_id, target_name, initiator_id, amount, "
                  '       EXTRACT(EPOCH FROM (now() - created_at)) '
                  "FROM consensus_votes WHERE chat_id = %s AND status = 'open' "
                  'ORDER BY created_at', (chat_id,))
        return c.fetchall()


def make_jester(user_id, chat_id, hours):
    with get_connection() as conn:
        c = conn.cursor()
        c.execute('UPDATE users SET jester_until = NOW() + make_interval(hours => %s) '
                  'WHERE user_id = %s AND chat_id = %s RETURNING jester_until',
                  (hours, user_id, chat_id))
        row = c.fetchone()
        return row[0] if row else None


def is_jester(user_id, chat_id):
    with get_connection() as conn:
        c = conn.cursor()
        c.execute('SELECT 1 FROM users WHERE user_id = %s AND chat_id = %s '
                  'AND jester_until > now()', (user_id, chat_id))
        return c.fetchone() is not None


def get_jesters(chat_id):
    """(user_id, name, seconds_left) for everyone still in the motley."""
    with get_connection() as conn:
        c = conn.cursor()
        c.execute('SELECT user_id, COALESCE(first_name, %s), '
                  '       CEIL(EXTRACT(EPOCH FROM (jester_until - now()))) '
                  'FROM users WHERE chat_id = %s AND jester_until > now()', ('?', chat_id))
        return c.fetchall()


# ---------------------------------------------------------------- decree hand

def set_pending_decrees(chat_id, day, king_id, codes):
    """Store tonight's dealt hand. One row per group, replaced each night."""
    with get_connection() as conn:
        c = conn.cursor()
        c.execute('INSERT INTO pending_decrees (chat_id, day, king_id, codes) '
                  'VALUES (%s, %s, %s, %s) ON CONFLICT (chat_id) DO UPDATE SET '
                  'day = EXCLUDED.day, king_id = EXCLUDED.king_id, codes = EXCLUDED.codes',
                  (chat_id, day, king_id, ','.join(codes)))


def get_pending_decrees(chat_id):
    """(day, codes, king_id) or None - the same shape the in-memory dict had, so the
    callers that used to read it did not have to change their unpacking."""
    with get_connection() as conn:
        c = conn.cursor()
        c.execute('SELECT day, codes, king_id FROM pending_decrees WHERE chat_id = %s',
                  (chat_id,))
        row = c.fetchone()
        if not row:
            return None
        return (row[0], [x for x in (row[1] or '').split(',') if x], row[2])


def clear_pending_decrees(chat_id):
    with get_connection() as conn:
        c = conn.cursor()
        c.execute('DELETE FROM pending_decrees WHERE chat_id = %s', (chat_id,))


# ------------------------------------------------------------ open challenges

def create_open_challenge(nonce, chat_id, challenger_id, challenger_name, bet):
    with get_connection() as conn:
        c = conn.cursor()
        c.execute('INSERT INTO open_challenges '
                  '(nonce, chat_id, challenger_id, challenger_name, bet) '
                  'VALUES (%s, %s, %s, %s, %s) ON CONFLICT (nonce) DO NOTHING',
                  (nonce, chat_id, challenger_id, challenger_name, float(bet)))


def list_open_challenges(chat_id, max_age_seconds):
    """Challenges still waiting for somebody to accept.

    Age-bounded because nothing expires one: a challenge nobody took is abandoned, not
    refused, and there is no escrow to release (the stake is taken at ACCEPT time, not
    at creation). So an old row is just clutter and is filtered on read rather than
    swept by a job.
    """
    with get_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT nonce, challenger_id, challenger_name, bet, "
                  "       EXTRACT(EPOCH FROM (now() - created_at))::bigint "
                  "FROM open_challenges "
                  "WHERE chat_id = %s AND status = 'open' "
                  "  AND created_at > now() - (%s || ' seconds')::interval "
                  "ORDER BY created_at DESC",
                  (chat_id, str(int(max_age_seconds))))
        return c.fetchall()


def get_open_challenge(nonce):
    with get_connection() as conn:
        c = conn.cursor()
        c.execute('SELECT nonce, chat_id, challenger_id, challenger_name, bet, status '
                  'FROM open_challenges WHERE nonce = %s', (nonce,))
        return c.fetchone()


def close_open_challenge(nonce, status='accepted'):
    with get_connection() as conn:
        c = conn.cursor()
        c.execute('UPDATE open_challenges SET status = %s WHERE nonce = %s',
                  (status, nonce))
        return c.rowcount


def prune_open_challenges(max_age_seconds):
    """Housekeeping only - these rows carry no money."""
    with get_connection() as conn:
        c = conn.cursor()
        c.execute("DELETE FROM open_challenges "
                  "WHERE created_at < now() - (%s || ' seconds')::interval",
                  (str(int(max_age_seconds)),))
        return c.rowcount



def get_open_consensus_list(chat_id, window_seconds):
    """Live votes in this group, for the app's ejma screen.

    Window-bounded on read rather than swept: a vote whose hour has run out is failed by
    recover_expired_consensus, but until that sweep fires it must not be offered as
    something to vote in.
    """
    with get_connection() as conn:
        c = conn.cursor()
        c.execute(
            "SELECT v.id, v.target_id, v.target_name, v.amount, v.required_votes, "
            "       v.total_players, "
            "       EXTRACT(EPOCH FROM (now() - v.created_at))::bigint, "
            "       COUNT(*) FILTER (WHERE k.choice = 'yes'), "
            "       COUNT(*) FILTER (WHERE k.choice = 'no') "
            "FROM consensus_votes v "
            "LEFT JOIN consensus_vote_casts k ON k.vote_id = v.id "
            "WHERE v.chat_id = %s AND v.status = 'open' "
            "  AND v.created_at > now() - make_interval(secs => %s) "
            "GROUP BY v.id ORDER BY v.created_at DESC",
            (chat_id, window_seconds))
        return [{'vote_id': r[0], 'target_id': r[1], 'target': r[2],
                 'amount': float(r[3] or 0), 'required': r[4], 'players': r[5],
                 'age': int(r[6] or 0), 'yes': r[7], 'no': r[8]}
                for r in c.fetchall()]


def heist_tick(attempt_id, cut_seconds, vault_seconds):
    """Advance a heist's clock from the stored timestamps, then return the row.

    This is the lazy-evaluation pattern perks already use: rather than a scheduled job
    being the only thing that can move the state, the state moves when it is READ. The
    bot still schedules its message edits - a chat player must see the cue land without
    polling - but nothing depends on those jobs having run, so the Mini App (which has
    no scheduler) plays exactly the same run, and a deploy mid-heist loses nothing.

    Two transitions happen here, both idempotent and both gated on the stage in the same
    statement that writes, for the same reason claim_heist_stage_timeout is:

      stage 1: once alarm_at has passed, the cue is live and the short cut window opens
      stage 2: vault_at is stamped so the reveal has a fixed t=0 for every viewer
    """
    with get_connection() as conn:
        c = conn.cursor()
        c.execute("UPDATE heist_attempts SET alarm_armed = TRUE, "
                  "    stage_deadline = alarm_at + make_interval(secs => %s) "
                  "WHERE id = %s AND status = 'pending' AND stage = 1 "
                  "  AND NOT COALESCE(alarm_armed, FALSE) "
                  "  AND alarm_at IS NOT NULL AND now() >= alarm_at",
                  (cut_seconds, attempt_id))
        c.execute("UPDATE heist_attempts SET vault_at = now(), "
                  "    stage_deadline = now() + make_interval(secs => %s) "
                  "WHERE id = %s AND status = 'pending' AND stage = 2 "
                  "  AND vault_at IS NULL", (vault_seconds, attempt_id))
    return get_heist_attempt(attempt_id)


def get_live_heist(chat_id, user_id):
    """The run this player is currently in, if any - the app has no message to come
    back to, so it asks. Offers count: an invitation nobody has answered is exactly the
    thing the accomplice needs to be shown."""
    with get_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT id FROM heist_attempts "
                  "WHERE chat_id = %s AND status IN ('offered', 'pending') "
                  "  AND (thief_id = %s OR partner_id = %s) "
                  "ORDER BY created_at DESC LIMIT 1", (chat_id, user_id, user_id))
        row = c.fetchone()
        return row[0] if row else None
