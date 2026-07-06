import os
from contextlib import contextmanager

import psycopg2

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
                PRIMARY KEY (user_id, chat_id)
            )
        ''')

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

        # Remembers the last group each user interacted in, so inline results
        # (like the leaderboard) can be rendered for that group directly.
        c.execute('''
            CREATE TABLE IF NOT EXISTS user_last_chat (
                user_id BIGINT PRIMARY KEY,
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


def set_last_chat(user_id, chat_id):
    with get_connection() as conn:
        c = conn.cursor()
        c.execute(
            'INSERT INTO user_last_chat (user_id, chat_id) VALUES (%s, %s) '
            'ON CONFLICT (user_id) DO UPDATE SET chat_id = EXCLUDED.chat_id',
            (user_id, chat_id)
        )


def get_last_chat(user_id):
    with get_connection() as conn:
        c = conn.cursor()
        c.execute('SELECT chat_id FROM user_last_chat WHERE user_id = %s', (user_id,))
        row = c.fetchone()
        return row[0] if row else None


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
        # Remember this group as the user's last active chat (for inline results).
        if chat_id < 0:
            c.execute(
                'INSERT INTO user_last_chat (user_id, chat_id) VALUES (%s, %s) '
                'ON CONFLICT (user_id) DO UPDATE SET chat_id = EXCLUDED.chat_id',
                (user_id, chat_id)
            )
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
        return row


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
        return c.fetchone()


def get_user_info(user_id, chat_id):
    with get_connection() as conn:
        c = conn.cursor()
        c.execute('SELECT first_name, size FROM users WHERE user_id = %s AND chat_id = %s', (user_id, chat_id))
        return c.fetchone()


def get_top_users(chat_id, limit=None):
    """Returns every player in the group ordered by size, unless limit caps it."""
    with get_connection() as conn:
        c = conn.cursor()
        if limit:
            c.execute('SELECT first_name, size FROM users WHERE chat_id = %s ORDER BY size DESC LIMIT %s', (chat_id, limit))
        else:
            c.execute('SELECT first_name, size FROM users WHERE chat_id = %s ORDER BY size DESC', (chat_id,))
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


def get_consensus_protection_remaining(chat_id, target_id):
    """Returns a timedelta if the target is still protected, else None.
    A failed consensus grants 3 days of protection; a succeeded one grants 6 days
    (so a target can't be hit by consensus over and over)."""
    with get_connection() as conn:
        c = conn.cursor()
        c.execute(
            """
            SELECT MAX(protect_until) - now() FROM (
                SELECT resolved_at + interval '3 days' AS protect_until FROM consensus_votes
                WHERE chat_id = %s AND target_id = %s AND status = 'failed'
                UNION ALL
                SELECT resolved_at + interval '6 days' AS protect_until FROM consensus_votes
                WHERE chat_id = %s AND target_id = %s AND status = 'succeeded'
            ) t
            WHERE protect_until > now()
            """,
            (chat_id, target_id, chat_id, target_id)
        )
        row = c.fetchone()
        return row[0] if row and row[0] else None


def get_open_consensus(chat_id, target_id):
    with get_connection() as conn:
        c = conn.cursor()
        c.execute(
            "SELECT id FROM consensus_votes WHERE chat_id = %s AND target_id = %s AND status = 'open'",
            (chat_id, target_id)
        )
        return c.fetchone()


def fail_open_consensus(chat_id, target_id):
    with get_connection() as conn:
        c = conn.cursor()
        c.execute(
            "UPDATE consensus_votes SET status = 'failed', resolved_at = now() "
            "WHERE chat_id = %s AND target_id = %s AND status = 'open'",
            (chat_id, target_id)
        )


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
    with get_connection() as conn:
        c = conn.cursor()
        c.execute(
            'SELECT chat_id, target_id, target_name, initiator_id, amount, required_votes, total_players, status '
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


def resolve_consensus_success(vote_id):
    """Atomically flips an open consensus to succeeded. Returns True only for the caller that won the race."""
    with get_connection() as conn:
        c = conn.cursor()
        c.execute(
            "UPDATE consensus_votes SET status = 'succeeded', resolved_at = now() WHERE id = %s AND status = 'open'",
            (vote_id,)
        )
        return c.rowcount > 0
