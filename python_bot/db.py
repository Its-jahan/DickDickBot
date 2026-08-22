import datetime
import functools
import os
import sys
import time
import types
from contextlib import contextmanager
from zoneinfo import ZoneInfo

import psycopg2

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


@contextmanager
def get_connection():
    """Open a short-lived connection to Supabase, commit on success and always close."""
    if not DB_URL:
        raise RuntimeError(
            "Supabase connection string is not configured. "
            "Set the SUPABASE_DB_URL (or DATABASE_URL) environment variable to your "
            "Supabase Postgres connection string."
        )
    # TCP keepalives so a connection silently dropped by the Supabase pooler (the
    # recurring "SSL connection has been closed unexpectedly" in production) is
    # detected instead of hanging, and a connect_timeout so a network blip can't
    # freeze a handler forever.
    conn = psycopg2.connect(
        DB_URL, connect_timeout=10,
        keepalives=1, keepalives_idle=30, keepalives_interval=10, keepalives_count=3,
    )
    try:
        yield conn
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass  # connection already dead - let the original error propagate, not this one
        raise
    finally:
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
        # One treasury per group. Interest is paid strictly out of this, so the bank can
        # never mint size: what the sinks put in is the ceiling on what interest pays out.
        c.execute('''
            CREATE TABLE IF NOT EXISTS bank_treasury (
                chat_id BIGINT PRIMARY KEY,
                balance DOUBLE PRECISION DEFAULT 0,
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
        # How many times this player has been force-collected. Worn publicly as بدهکار
        # and used to price future loans.
        c.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS loan_defaults INTEGER DEFAULT 0")
        c.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS last_xfer_at TIMESTAMPTZ")

        # One-time: charge the deposit fee on money that was banked before the fee
        # existed. Everyone who deposited in that window got in free, which is both
        # unfair to whoever deposits next and the reason the treasury is empty while
        # the vault is full. Guarded by bot_meta so it can only ever run once, and it
        # moves the fee into the treasury rather than deleting it - the same
        # conservation rule every other fee follows.
        c.execute("SELECT value FROM bot_meta WHERE key = 'deposit_fee_backfilled'")
        if not c.fetchone():
            c.execute('INSERT INTO bank_treasury (chat_id) '
                      'SELECT DISTINCT chat_id FROM bank_accounts WHERE COALESCE(balance,0) > 0 '
                      'ON CONFLICT (chat_id) DO NOTHING')
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
                agg AS (SELECT chat_id, SUM(fee) AS tot FROM deb GROUP BY chat_id)
                UPDATE bank_treasury t SET balance = COALESCE(t.balance,0) + a.tot
                FROM agg a WHERE t.chat_id = a.chat_id
            """, (BACKFILL_DEPOSIT_FEE_RATIO,))
            c.execute("INSERT INTO bot_meta (key, value) VALUES ('deposit_fee_backfilled', '1') "
                      "ON CONFLICT (key) DO NOTHING")
        c.execute("CREATE INDEX IF NOT EXISTS size_log_chat_user_idx ON size_log (chat_id, user_id, created_at DESC)")
        c.execute("CREATE INDEX IF NOT EXISTS size_log_created_idx ON size_log (created_at DESC)")

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


def track_chat(chat_id):
    if chat_id < 0:
        with get_connection() as conn:
            c = conn.cursor()
            c.execute('INSERT INTO chats (chat_id) VALUES (%s) ON CONFLICT (chat_id) DO NOTHING', (chat_id,))


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


def get_all_chats():
    with get_connection() as conn:
        c = conn.cursor()
        c.execute('SELECT chat_id FROM chats')
        return [r[0] for r in c.fetchall()]


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
    from usury gets throttled by the handicap exactly like one getting rich from dice."""
    with get_connection() as conn:
        c = conn.cursor()
        c.execute(
            'SELECT l.user_id, MAX(COALESCE(u.first_name, %s)), SUM(l.delta), COUNT(*) '
            'FROM size_log l LEFT JOIN users u '
            '  ON u.user_id = l.user_id AND u.chat_id = l.chat_id '
            'WHERE l.chat_id = %s AND l.created_at >= NOW() - (%s || %s)::interval '
            '  AND COALESCE(l.source, %s) NOT IN (%s, %s, %s, %s) '
            'GROUP BY l.user_id',
            ('', chat_id, days, ' days', '', 'bank_deposit', 'bank_withdraw',
             'loan_principal', 'xfer_principal')
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
            'traitor_until, last_dosed_at, COALESCE(theft_luck,1.0), COALESCE(growth_mult,1.0) '
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
#   2. The bank cannot mint. Interest is paid only out of bank_treasury, which is
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
            c.execute('INSERT INTO bank_treasury (chat_id, balance) VALUES (%s, %s) '
                      'ON CONFLICT (chat_id) DO UPDATE SET '
                      'balance = COALESCE(bank_treasury.balance,0) + %s RETURNING balance',
                      (chat_id, fee, fee))
            _bank_log(c, chat_id, user_id, 'treasury_in', fee, c.fetchone()[0], 'کارمزد واریز')
        return (True, float(brow[0]), float(brow[1]), fee)


def bank_withdraw(user_id, chat_id, amount, fee_ratio=0.0):
    """Moves `amount` out of the bank, atomically, minus the vault's cut. Returns
    (True, new_bank_balance, paid_out, fee) or (False, None, 0, 0) if the account is
    short. `amount` is what leaves the bank; `paid_out` is what reaches the wallet."""
    with get_connection() as conn:
        c = conn.cursor()
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
            c.execute('INSERT INTO bank_treasury (chat_id, balance) VALUES (%s, %s) '
                      'ON CONFLICT (chat_id) DO UPDATE SET '
                      'balance = COALESCE(bank_treasury.balance,0) + %s RETURNING balance',
                      (chat_id, fee, fee))
            _bank_log(c, chat_id, user_id, 'treasury_in', fee, c.fetchone()[0], 'کارمزد برداشت')
        return (True, float(brow[0]), paid_out, fee)


def treasury_add(chat_id, amount, note=None):
    """The only way size enters the treasury: a sink hands over what it just destroyed.
    Called from the spots that used to simply delete size."""
    if amount <= 0:
        return
    with get_connection() as conn:
        c = conn.cursor()
        c.execute('INSERT INTO bank_treasury (chat_id, balance) VALUES (%s, %s) '
                  'ON CONFLICT (chat_id) DO UPDATE SET balance = COALESCE(bank_treasury.balance,0) + %s '
                  'RETURNING balance', (chat_id, amount, amount))
        row = c.fetchone()
        _bank_log(c, chat_id, None, 'treasury_in', amount, row[0], note)


def get_treasury(chat_id):
    with get_connection() as conn:
        c = conn.cursor()
        c.execute('SELECT COALESCE(balance,0), COALESCE(last_interest_date,%s), last_heist_at '
                  'FROM bank_treasury WHERE chat_id = %s', ('', chat_id))
        return c.fetchone() or (0.0, '', None)


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
    """Pays one day's interest out of the treasury and returns
    (rows_paid, total_paid, treasury_left).

    The treasury is the hard ceiling. If what everyone is owed exceeds what the
    treasury can afford (capped further by `max_share` of it, so one day never drains
    the whole thing), every depositor is scaled down by the same factor rather than
    the early rows being paid in full and the late ones getting nothing."""
    with get_connection() as conn:
        c = conn.cursor()
        c.execute('SELECT COALESCE(balance,0) FROM bank_treasury WHERE chat_id = %s FOR UPDATE', (chat_id,))
        row = c.fetchone()
        treasury = float(row[0]) if row else 0.0
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
            c.execute('UPDATE bank_treasury SET balance = COALESCE(balance,0) - %s '
                      'WHERE chat_id = %s RETURNING balance', (paid_total, chat_id))
            trow = c.fetchone()
            _bank_log(c, chat_id, None, 'interest_out', -paid_total, trow[0])
            treasury = float(trow[0])
        return (paid_rows, paid_total, treasury)


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


def heist_take(chat_id, thief_id, treasury_ratio, deposit_ratio):
    """Drains the vault for a successful heist: `treasury_ratio` of the treasury plus
    `deposit_ratio` of every depositor's balance, all in one transaction.

    Strictly zero-sum - every centimetre handed to the thief is one taken from the
    treasury or from a named depositor, and the per-victim amounts are returned so the
    group can be told exactly who paid for it.

    Returns (total_loot, treasury_part, [(user_id, name, amount), ...])."""
    with get_connection() as conn:
        c = conn.cursor()
        c.execute('SELECT COALESCE(balance,0) FROM bank_treasury WHERE chat_id = %s FOR UPDATE', (chat_id,))
        row = c.fetchone()
        treasury = float(row[0]) if row else 0.0
        treasury_part = round(max(0.0, treasury) * treasury_ratio, 2)
        if treasury_part > 0:
            c.execute('UPDATE bank_treasury SET balance = COALESCE(balance,0) - %s '
                      'WHERE chat_id = %s RETURNING balance', (treasury_part, chat_id))
            trow = c.fetchone()
            _bank_log(c, chat_id, thief_id, 'heist_treasury', -treasury_part, trow[0])

        c.execute('SELECT b.user_id, COALESCE(u.first_name, %s), COALESCE(b.balance,0) '
                  'FROM bank_accounts b LEFT JOIN users u '
                  '  ON u.user_id = b.user_id AND u.chat_id = b.chat_id '
                  'WHERE b.chat_id = %s AND COALESCE(b.balance,0) > 0 '
                  'ORDER BY b.balance DESC FOR UPDATE OF b', ('?', chat_id))
        victims = []
        deposit_part = 0.0
        for uid, name, bal in c.fetchall():
            if uid == thief_id:
                continue  # you don't rob your own deposit
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
        if total > 0:
            c.execute('UPDATE users SET size = COALESCE(size,0) + %s '
                      'WHERE user_id = %s AND chat_id = %s RETURNING size',
                      (total, thief_id, chat_id))
            wrow = c.fetchone()
            if wrow is None:
                raise RuntimeError('thief has no users row')
            c.execute('INSERT INTO size_log (chat_id, user_id, delta, balance_after, source, note) '
                      'VALUES (%s, %s, %s, %s, %s, %s)',
                      (chat_id, thief_id, total, wrow[0], 'bank_heist', 'سرقت از بانک'))
        return (total, treasury_part, victims)


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


def accept_loan(loan_id, borrower_id, term_days):
    """Atomically turns an offer into an active loan and hands over the principal.

    Claims the row with a conditional UPDATE first, so two taps on the same button
    cannot disburse twice. Returns (True, principal, due_amount) or (False, reason)."""
    with get_connection() as conn:
        c = conn.cursor()
        c.execute("UPDATE loans SET status = 'active', accepted_at = NOW(), "
                  "due_at = NOW() + (%s || %s)::interval "
                  "WHERE id = %s AND status = 'offered' AND borrower_id = %s "
                  'RETURNING chat_id, lender_id, principal, due_amount',
                  (term_days, ' days', loan_id, borrower_id))
        row = c.fetchone()
        if row is None:
            return (False, 'gone', 0)
        chat_id, lender_id, principal, due_amount = row

        if lender_id is None:
            # Treasury loan: the vault funds it, and must actually have the money.
            c.execute('SELECT COALESCE(balance,0) FROM bank_treasury WHERE chat_id = %s FOR UPDATE',
                      (chat_id,))
            trow = c.fetchone()
            if not trow or float(trow[0]) < principal:
                c.execute("UPDATE loans SET status = 'offered', accepted_at = NULL, due_at = NULL "
                          'WHERE id = %s', (loan_id,))
                return (False, 'treasury', 0)
            c.execute('UPDATE bank_treasury SET balance = COALESCE(balance,0) - %s '
                      'WHERE chat_id = %s RETURNING balance', (principal, chat_id))
            _bank_log(c, chat_id, borrower_id, 'loan_out', -principal, c.fetchone()[0],
                      f'وام #{loan_id}')
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
                return (False, 'lender_broke', 0)
            c.execute('INSERT INTO size_log (chat_id, user_id, delta, balance_after, source, note) '
                      'VALUES (%s, %s, %s, %s, %s, %s)',
                      (chat_id, lender_id, -principal, lrow[0], 'loan_principal', f'نزول #{loan_id}'))

        if _size_move(c, chat_id, borrower_id, principal, 'loan_principal', f'وام #{loan_id}') is None:
            raise RuntimeError('borrower has no users row')
        return (True, float(principal), float(due_amount))


def _collect(c, chat_id, borrower_id, principal, interest, loan_id):
    """Pulls a whole debt out of a borrower: wallet first, then their bank deposit, and
    if they are still short the remainder is driven negative on the wallet.

    Reaching into the bank is deliberate. The bank is safe from *theft*, but if it were
    safe from *debt* too then borrowing and immediately hiding the money in it would be
    a free money printer.

    The wallet-borne part of the debt is logged as two rows, not one: the interest under
    'loan_interest' (real cost the handicap counts) and the rest under 'loan_principal'
    (a transfer it ignores). Interest is charged against the wallet first, so the two
    rows always sum to exactly the change the wallet actually saw - the ledger has to
    reconstruct the balance, so it cannot book money the wallet never paid.

    Returns (from_wallet, from_bank, shortfall)."""
    total = round(principal + interest, 2)

    c.execute('SELECT COALESCE(size,0) FROM users WHERE user_id = %s AND chat_id = %s FOR UPDATE',
              (borrower_id, chat_id))
    row = c.fetchone()
    wallet = float(row[0]) if row else 0.0

    from_wallet = round(min(max(wallet, 0.0), total), 2)
    remaining = round(total - from_wallet, 2)

    from_bank = 0.0
    if remaining > 0.009:
        c.execute('SELECT COALESCE(balance,0) FROM bank_accounts '
                  'WHERE user_id = %s AND chat_id = %s FOR UPDATE', (borrower_id, chat_id))
        brow = c.fetchone()
        bank_bal = float(brow[0]) if brow else 0.0
        from_bank = round(min(max(bank_bal, 0.0), remaining), 2)
        if from_bank > 0:
            c.execute('UPDATE bank_accounts SET balance = COALESCE(balance,0) - %s '
                      'WHERE user_id = %s AND chat_id = %s RETURNING balance',
                      (from_bank, borrower_id, chat_id))
            _bank_log(c, chat_id, borrower_id, 'debt_seized', -from_bank, c.fetchone()[0],
                      f'بدهی #{loan_id}')
            remaining = round(remaining - from_bank, 2)

    # Nothing left to take: the debt is still owed in full, so the wallet goes negative
    # for the rest. The lender is made whole either way - that is what the borrower
    # agreed to - and the hole is the borrower's problem to dig out of.
    shortfall = remaining if remaining > 0.009 else 0.0
    wallet_total = round(from_wallet + shortfall, 2)

    interest_w = round(min(interest, wallet_total), 2)
    principal_w = round(wallet_total - interest_w, 2)
    if principal_w > 0.009:
        _size_move(c, chat_id, borrower_id, -principal_w, 'loan_principal',
                   f'بازپرداخت #{loan_id}')
    if interest_w > 0.009:
        _size_move(c, chat_id, borrower_id, -interest_w, 'loan_interest',
                   f'سود بدهی #{loan_id}')

    return (from_wallet, from_bank, shortfall)


def settle_loan(loan_id, forced):
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

        from_wallet, from_bank, shortfall = _collect(c, chat_id, borrower_id,
                                                     principal, interest, loan_id)

        if lender_id is None:
            c.execute('INSERT INTO bank_treasury (chat_id, balance) VALUES (%s, %s) '
                      'ON CONFLICT (chat_id) DO UPDATE SET '
                      'balance = COALESCE(bank_treasury.balance,0) + %s RETURNING balance',
                      (chat_id, due_amount, due_amount))
            _bank_log(c, chat_id, borrower_id, 'loan_repaid', due_amount, c.fetchone()[0],
                      f'وام #{loan_id}')
        else:
            _size_move(c, chat_id, lender_id, principal, 'loan_principal', f'اصل نزول #{loan_id}')
            if interest > 0:
                _size_move(c, chat_id, lender_id, interest, 'loan_interest', f'سود نزول #{loan_id}')

        if forced:
            c.execute('UPDATE users SET loan_defaults = COALESCE(loan_defaults,0) + 1 '
                      'WHERE user_id = %s AND chat_id = %s', (borrower_id, chat_id))

        return {
            'chat_id': chat_id, 'lender_id': lender_id, 'lender_name': lender_name,
            'borrower_id': borrower_id, 'borrower_name': borrower_name,
            'principal': principal, 'due_amount': due_amount, 'interest': interest,
            'from_wallet': from_wallet, 'from_bank': from_bank, 'shortfall': shortfall,
            'forced': forced,
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


def get_loan_defaults(chat_id, user_id):
    with get_connection() as conn:
        c = conn.cursor()
        c.execute('SELECT COALESCE(loan_defaults,0) FROM users WHERE user_id = %s AND chat_id = %s',
                  (user_id, chat_id))
        row = c.fetchone()
        return int(row[0]) if row else 0


# ---------------------------------------------------------------- cross-group transfer
# Every group is otherwise a completely separate league - the same player has an
# independent size in each. This is the one seam between them, and it is priced steeply
# on purpose: without a heavy fee, a player who is rich in one group could simply import
# that lead into another and skip the game entirely.

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


def cross_group_transfer(user_id, from_chat, to_chat, amount, fee_ratio):
    """Moves one player's own size from one group to another, minus a heavy fee.

    One transaction across both groups, so the size can never exist in both at once or
    in neither. The fee stays in the *source* group's treasury: that group is the one
    losing the wealth, so it is the one that keeps a cut of it.

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
            c.execute('INSERT INTO bank_treasury (chat_id, balance) VALUES (%s, %s) '
                      'ON CONFLICT (chat_id) DO UPDATE SET '
                      'balance = COALESCE(bank_treasury.balance,0) + %s RETURNING balance',
                      (from_chat, fee, fee))
            _bank_log(c, from_chat, user_id, 'treasury_in', fee, c.fetchone()[0], 'کارمزد انتقال')

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
