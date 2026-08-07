import datetime
import functools
import os
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

        c.execute('''
            CREATE TABLE IF NOT EXISTS football_markets (
                id SERIAL PRIMARY KEY,
                chat_id BIGINT,
                fixture_id BIGINT,
                home_team TEXT,
                away_team TEXT,
                status TEXT DEFAULT 'scheduled',
                halftime_announced BOOLEAN DEFAULT FALSE,
                result TEXT,
                message_id BIGINT,
                created_by BIGINT,
                prior_home_prob DOUBLE PRECISION DEFAULT 0.5,
                kickoff_at TIMESTAMPTZ,
                match_started_announced BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMPTZ DEFAULT now()
            )
        ''')
        c.execute("ALTER TABLE football_markets ADD COLUMN IF NOT EXISTS prior_home_prob DOUBLE PRECISION DEFAULT 0.5")
        c.execute("ALTER TABLE football_markets ADD COLUMN IF NOT EXISTS kickoff_at TIMESTAMPTZ")
        c.execute("ALTER TABLE football_markets ADD COLUMN IF NOT EXISTS match_started_announced BOOLEAN DEFAULT FALSE")
        # Last state the poller saw, so a bet can be priced from the server's own view
        # of the match instead of whatever odds the tapped button happened to carry.
        c.execute("ALTER TABLE football_markets ADD COLUMN IF NOT EXISTS last_elapsed INTEGER DEFAULT 0")
        c.execute("ALTER TABLE football_markets ADD COLUMN IF NOT EXISTS last_home_score INTEGER DEFAULT 0")
        c.execute("ALTER TABLE football_markets ADD COLUMN IF NOT EXISTS last_away_score INTEGER DEFAULT 0")

        c.execute('''
            CREATE TABLE IF NOT EXISTS football_bets (
                id SERIAL PRIMARY KEY,
                market_id INTEGER REFERENCES football_markets(id),
                user_id BIGINT,
                first_name TEXT,
                side TEXT,
                amount DOUBLE PRECISION,
                locked_odds DOUBLE PRECISION,
                placed_at TIMESTAMPTZ DEFAULT now()
            )
        ''')
        # A user can now stack multiple bets on the same market (e.g. add more at a
        # newer, tighter/looser odds as the match develops), so the old one-bet-per-user
        # constraint is dropped for anyone upgrading from before this was allowed.
        c.execute("ALTER TABLE football_bets DROP CONSTRAINT IF EXISTS football_bets_market_id_user_id_key")
        # Per-bet settlement flag: settlement pays each bet and flips its own flag, so
        # a crash part-way through the payout loop can be resumed without paying the
        # already-paid bets a second time.
        c.execute("ALTER TABLE football_bets ADD COLUMN IF NOT EXISTS settled BOOLEAN DEFAULT FALSE")

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


def update_size(user_id, chat_id, size_delta, current_date_str=None):
    # A single relative UPDATE, not read-then-write: with concurrent_updates(True)
    # two handlers settling money for the same user at once (e.g. a bet payout and
    # a donation) must both land instead of one silently overwriting the other.
    with get_connection() as conn:
        c = conn.cursor()
        if current_date_str:
            c.execute('UPDATE users SET size = COALESCE(size, 0) + %s, last_grown = %s WHERE user_id = %s AND chat_id = %s',
                      (size_delta, current_date_str, user_id, chat_id))
        else:
            c.execute('UPDATE users SET size = COALESCE(size, 0) + %s WHERE user_id = %s AND chat_id = %s',
                      (size_delta, user_id, chat_id))


def try_deduct_size(user_id, chat_id, amount):
    """Atomically escrows `amount` out of a user's size, refusing (returns False) if
    their balance is short or they have no row. The balance check and the deduction
    are one UPDATE, so two concurrent stakes can never both spend the same centimeters."""
    with get_connection() as conn:
        c = conn.cursor()
        c.execute(
            'UPDATE users SET size = COALESCE(size, 0) - %s '
            'WHERE user_id = %s AND chat_id = %s AND COALESCE(size, 0) >= %s',
            (amount, user_id, chat_id, amount)
        )
        return c.rowcount > 0


def claim_daily_growth(user_id, chat_id, today_str):
    """Atomically stamps today's growth date, returning True only for the first caller
    of the day - a rapid double-tap on the grow button can't grow (or roll a perk) twice."""
    with get_connection() as conn:
        c = conn.cursor()
        c.execute(
            'UPDATE users SET last_grown = %s '
            'WHERE user_id = %s AND chat_id = %s AND last_grown IS DISTINCT FROM %s',
            (today_str, user_id, chat_id, today_str)
        )
        return c.rowcount > 0


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


def create_football_market(chat_id, fixture_id, home_team, away_team, created_by, prior_home_prob=0.5, kickoff_at=None):
    with get_connection() as conn:
        c = conn.cursor()
        c.execute(
            'INSERT INTO football_markets (chat_id, fixture_id, home_team, away_team, created_by, prior_home_prob, kickoff_at) '
            'VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id',
            (chat_id, fixture_id, home_team, away_team, created_by, prior_home_prob, kickoff_at)
        )
        return c.fetchone()[0]


def set_football_market_message(market_id, message_id):
    with get_connection() as conn:
        c = conn.cursor()
        c.execute('UPDATE football_markets SET message_id = %s WHERE id = %s', (message_id, market_id))


def get_football_market(market_id):
    """Returns (chat_id, fixture_id, home_team, away_team, status, halftime_announced,
    result, message_id, created_by, prior_home_prob, kickoff_at, match_started_announced,
    last_elapsed, last_home_score, last_away_score) or None."""
    with get_connection() as conn:
        c = conn.cursor()
        c.execute(
            'SELECT chat_id, fixture_id, home_team, away_team, status, halftime_announced, '
            'result, message_id, created_by, prior_home_prob, kickoff_at, match_started_announced, '
            'COALESCE(last_elapsed, 0), COALESCE(last_home_score, 0), COALESCE(last_away_score, 0) '
            'FROM football_markets WHERE id = %s',
            (market_id,)
        )
        return c.fetchone()


def set_football_market_state(market_id, elapsed, home_score, away_score):
    """Records the latest polled match state, which is what bets are priced from."""
    with get_connection() as conn:
        c = conn.cursor()
        c.execute(
            'UPDATE football_markets SET last_elapsed = %s, last_home_score = %s, last_away_score = %s WHERE id = %s',
            (elapsed, home_score, away_score, market_id)
        )


def get_active_football_markets():
    """Returns (id, chat_id, fixture_id, home_team, away_team, status, halftime_announced,
    message_id, prior_home_prob, kickoff_at, match_started_announced) for every market that
    hasn't finished yet, for the polling job to check."""
    with get_connection() as conn:
        c = conn.cursor()
        c.execute(
            "SELECT id, chat_id, fixture_id, home_team, away_team, status, halftime_announced, message_id, "
            "prior_home_prob, kickoff_at, match_started_announced "
            "FROM football_markets WHERE status != 'finished'"
        )
        return c.fetchall()


def set_football_market_status(market_id, status):
    with get_connection() as conn:
        c = conn.cursor()
        c.execute('UPDATE football_markets SET status = %s WHERE id = %s', (status, market_id))


def mark_football_halftime_announced(market_id):
    with get_connection() as conn:
        c = conn.cursor()
        c.execute('UPDATE football_markets SET halftime_announced = TRUE WHERE id = %s', (market_id,))


def mark_football_match_started(market_id):
    with get_connection() as conn:
        c = conn.cursor()
        c.execute('UPDATE football_markets SET match_started_announced = TRUE WHERE id = %s', (market_id,))


def finish_football_market(market_id, result):
    """result is 'home', 'away', or 'draw'."""
    with get_connection() as conn:
        c = conn.cursor()
        c.execute(
            "UPDATE football_markets SET status = 'finished', result = %s WHERE id = %s",
            (result, market_id)
        )


def place_football_bet(market_id, user_id, first_name, side, amount, odds):
    """Records a new bet. A user can place any number of bets on the same market (e.g.
    adding more later at whatever odds are current then) - each is its own independent
    stake, locked at its own odds, settled independently."""
    with get_connection() as conn:
        c = conn.cursor()
        c.execute(
            'INSERT INTO football_bets (market_id, user_id, first_name, side, amount, locked_odds) '
            'VALUES (%s, %s, %s, %s, %s, %s)',
            (market_id, user_id, first_name, side, amount, odds)
        )


def get_football_bets(market_id):
    """Returns (user_id, first_name, side, amount, locked_odds) for every bet on this market."""
    with get_connection() as conn:
        c = conn.cursor()
        c.execute(
            'SELECT user_id, first_name, side, amount, locked_odds FROM football_bets WHERE market_id = %s ORDER BY id',
            (market_id,)
        )
        return c.fetchall()


def claim_unsettled_football_bets(market_id):
    """Atomically flips every not-yet-settled bet on this market to settled and returns
    them as (id, user_id, first_name, side, amount, locked_odds). Settlement pays only
    what this returns, so a poll that crashed part-way through (or two polls overlapping)
    can never pay the same bet twice - and the market is only marked finished afterwards,
    so a crash before that point simply retries the unpaid remainder next poll."""
    with get_connection() as conn:
        c = conn.cursor()
        c.execute(
            'UPDATE football_bets SET settled = TRUE '
            'WHERE market_id = %s AND COALESCE(settled, FALSE) = FALSE '
            'RETURNING id, user_id, first_name, side, amount, locked_odds',
            (market_id,)
        )
        return sorted(c.fetchall(), key=lambda r: r[0])


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
