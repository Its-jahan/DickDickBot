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


def get_top_users(chat_id, limit=10):
    with get_connection() as conn:
        c = conn.cursor()
        c.execute('SELECT first_name, size FROM users WHERE chat_id = %s ORDER BY size DESC LIMIT %s', (chat_id, limit))
        return c.fetchall()


def get_global_top_users(limit=10):
    with get_connection() as conn:
        c = conn.cursor()
        # Need to group by user_id to sum sizes
        c.execute('SELECT MAX(first_name), SUM(size) as total_size FROM users GROUP BY user_id ORDER BY total_size DESC LIMIT %s', (limit,))
        return c.fetchall()


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
