import datetime
import os
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
    conn = psycopg2.connect(DB_URL)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


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
    username = username.replace('@', '')
    with get_connection() as conn:
        c = conn.cursor()
        c.execute('SELECT user_id, first_name, size FROM users WHERE username = %s AND chat_id = %s', (username, chat_id))
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
    with get_connection() as conn:
        c = conn.cursor()
        c.execute('SELECT size FROM users WHERE user_id = %s AND chat_id = %s', (user_id, chat_id))
        row = c.fetchone()
        if row:
            new_size = row[0] + size_delta
            if current_date_str:
                c.execute('UPDATE users SET size = %s, last_grown = %s WHERE user_id = %s AND chat_id = %s',
                          (new_size, current_date_str, user_id, chat_id))
            else:
                c.execute('UPDATE users SET size = %s WHERE user_id = %s AND chat_id = %s',
                          (new_size, user_id, chat_id))


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
    with get_connection() as conn:
        c = conn.cursor()
        c.execute('SELECT quantity FROM inventory WHERE user_id = %s AND chat_id = %s AND item_name = %s AND quantity > 0', (user_id, chat_id, item_name))
        row = c.fetchone()
        if row:
            c.execute('UPDATE inventory SET quantity = quantity - 1 WHERE user_id = %s AND chat_id = %s AND item_name = %s', (user_id, chat_id, item_name))
            return True
        return False


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
    result, message_id, created_by, prior_home_prob, kickoff_at, match_started_announced) or None."""
    with get_connection() as conn:
        c = conn.cursor()
        c.execute(
            'SELECT chat_id, fixture_id, home_team, away_team, status, halftime_announced, '
            'result, message_id, created_by, prior_home_prob, kickoff_at, match_started_announced '
            'FROM football_markets WHERE id = %s',
            (market_id,)
        )
        return c.fetchone()


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
            'SELECT user_id, first_name, side, amount, locked_odds FROM football_bets WHERE market_id = %s',
            (market_id,)
        )
        return c.fetchall()


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
    with get_connection() as conn:
        c = conn.cursor()
        c.execute(
            'INSERT INTO pvp_match_bets (match_id, user_id, first_name, side, amount) VALUES (%s, %s, %s, %s, %s)',
            (match_id, user_id, first_name, side, amount)
        )


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
