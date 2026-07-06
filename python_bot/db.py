import sqlite3
import datetime

DB_NAME = 'dickbot.db'

def get_connection():
    return sqlite3.connect(DB_NAME)

def init_db():
    conn = get_connection()
    c = conn.cursor()
    # Drop old tables to migrate to new schema
    c.execute('DROP TABLE IF EXISTS users')
    c.execute('DROP TABLE IF EXISTS inventory')
    
    c.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER,
            chat_id INTEGER,
            username TEXT,
            first_name TEXT,
            size REAL DEFAULT 0,
            last_grown TEXT DEFAULT '',
            perk TEXT DEFAULT 'عادی',
            active_item TEXT DEFAULT '',
            PRIMARY KEY (user_id, chat_id)
        )
    ''')
    
    c.execute('''
        CREATE TABLE IF NOT EXISTS chats (
            chat_id INTEGER PRIMARY KEY
        )
    ''')
    
    c.execute('''
        CREATE TABLE IF NOT EXISTS inventory (
            user_id INTEGER,
            chat_id INTEGER,
            item_name TEXT,
            quantity INTEGER DEFAULT 0,
            PRIMARY KEY (user_id, chat_id, item_name)
        )
    ''')
    
    c.execute('''
        CREATE TABLE IF NOT EXISTS chat_instances (
            chat_instance TEXT PRIMARY KEY,
            chat_id INTEGER
        )
    ''')
    conn.commit()

def track_chat(chat_id):
    if chat_id < 0:
        conn = get_connection()
        c = conn.cursor()
        c.execute('INSERT OR IGNORE INTO chats (chat_id) VALUES (?)', (chat_id,))
        conn.commit()

def track_chat_instance(chat_instance, chat_id):
    conn = get_connection()
    c = conn.cursor()
    c.execute('INSERT OR REPLACE INTO chat_instances (chat_instance, chat_id) VALUES (?, ?)', (chat_instance, chat_id))
    conn.commit()
    conn.close()

def get_chat_id_from_instance(chat_instance):
    conn = get_connection()
    c = conn.cursor()
    c.execute('SELECT chat_id FROM chat_instances WHERE chat_instance = ?', (chat_instance,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else None

def get_all_chats():
    conn = get_connection()
    c = conn.cursor()
    c.execute('SELECT chat_id FROM chats')
    rows = [r[0] for r in c.fetchall()]
    conn.close()
    return rows

def get_user(user_id, chat_id, username, first_name):
    conn = get_connection()
    c = conn.cursor()
    c.execute('SELECT size, last_grown, perk FROM users WHERE user_id = ? AND chat_id = ?', (user_id, chat_id))
    row = c.fetchone()
    if row is None:
        c.execute('INSERT INTO users (user_id, chat_id, username, first_name) VALUES (?, ?, ?, ?)', (user_id, chat_id, username, first_name))
        conn.commit()
        conn.close()
        return (0.0, '', 'عادی')
    
    # Update username/first_name if they changed (only if not None)
    if username is not None or first_name is not None:
        updates = []
        params = []
        if username is not None:
            updates.append('username = ?')
            params.append(username)
        if first_name is not None:
            updates.append('first_name = ?')
            params.append(first_name)
        params.extend([user_id, chat_id])
        c.execute(f'UPDATE users SET {", ".join(updates)} WHERE user_id = ? AND chat_id = ?', params)
        conn.commit()
    conn.close()
    return row

def get_global_user(user_id, username, first_name):
    conn = get_connection()
    c = conn.cursor()
    c.execute('SELECT SUM(size) FROM users WHERE user_id = ?', (user_id,))
    total_size = c.fetchone()[0]
    conn.close()
    if total_size is None:
        return 0.0
    return total_size

def set_user_perk(user_id, chat_id, perk):
    conn = get_connection()
    c = conn.cursor()
    c.execute('UPDATE users SET perk = ? WHERE user_id = ? AND chat_id = ?', (perk, user_id, chat_id))
    conn.commit()
    conn.close()

def find_user_by_username(username, chat_id):
    username = username.replace('@', '')
    conn = get_connection()
    c = conn.cursor()
    c.execute('SELECT user_id, first_name, size FROM users WHERE username = ? AND chat_id = ?', (username, chat_id))
    row = c.fetchone()
    conn.close()
    return row

def get_user_info(user_id, chat_id):
    conn = get_connection()
    c = conn.cursor()
    c.execute('SELECT first_name, size FROM users WHERE user_id = ? AND chat_id = ?', (user_id, chat_id))
    row = c.fetchone()
    conn.close()
    return row

def get_top_users(chat_id, limit=10):
    conn = get_connection()
    c = conn.cursor()
    c.execute('SELECT first_name, size FROM users WHERE chat_id = ? ORDER BY size DESC LIMIT ?', (chat_id, limit))
    rows = c.fetchall()
    conn.close()
    return rows

def get_global_top_users(limit=10):
    conn = get_connection()
    c = conn.cursor()
    # Need to group by user_id to sum sizes
    c.execute('SELECT first_name, SUM(size) as total_size FROM users GROUP BY user_id ORDER BY total_size DESC LIMIT ?', (limit,))
    rows = c.fetchall()
    conn.close()
    return rows

def update_size(user_id, chat_id, size_delta, current_date_str=None):
    conn = get_connection()
    c = conn.cursor()
    c.execute('SELECT size FROM users WHERE user_id = ? AND chat_id = ?', (user_id, chat_id))
    row = c.fetchone()
    if row:
        new_size = row[0] + size_delta
        if current_date_str:
            c.execute('UPDATE users SET size = ?, last_grown = ? WHERE user_id = ? AND chat_id = ?', 
                      (new_size, current_date_str, user_id, chat_id))
        else:
            c.execute('UPDATE users SET size = ? WHERE user_id = ? AND chat_id = ?', 
                      (new_size, user_id, chat_id))
        conn.commit()
    conn.close()

def get_user_active_item(user_id, chat_id):
    conn = get_connection()
    c = conn.cursor()
    c.execute('SELECT active_item FROM users WHERE user_id = ? AND chat_id = ?', (user_id, chat_id))
    row = c.fetchone()
    conn.close()
    return row[0] if row else ""

def set_user_active_item(user_id, chat_id, item_name):
    conn = get_connection()
    c = conn.cursor()
    c.execute('UPDATE users SET active_item = ? WHERE user_id = ? AND chat_id = ?', (item_name, user_id, chat_id))
    conn.commit()
    conn.close()

def add_inventory(user_id, chat_id, item_name, amount=1):
    conn = get_connection()
    c = conn.cursor()
    c.execute('SELECT quantity FROM inventory WHERE user_id = ? AND chat_id = ? AND item_name = ?', (user_id, chat_id, item_name))
    row = c.fetchone()
    if row:
        c.execute('UPDATE inventory SET quantity = quantity + ? WHERE user_id = ? AND chat_id = ? AND item_name = ?', (amount, user_id, chat_id, item_name))
    else:
        c.execute('INSERT INTO inventory (user_id, chat_id, item_name, quantity) VALUES (?, ?, ?, ?)', (user_id, chat_id, item_name, amount))
    conn.commit()
    conn.close()

def get_inventory(user_id, chat_id):
    conn = get_connection()
    c = conn.cursor()
    c.execute('SELECT item_name, quantity FROM inventory WHERE user_id = ? AND chat_id = ? AND quantity > 0', (user_id, chat_id))
    rows = c.fetchall()
    conn.close()
    return rows

def use_inventory(user_id, chat_id, item_name):
    conn = get_connection()
    c = conn.cursor()
    c.execute('SELECT quantity FROM inventory WHERE user_id = ? AND chat_id = ? AND item_name = ? AND quantity > 0', (user_id, chat_id, item_name))
    row = c.fetchone()
    if row:
        c.execute('UPDATE inventory SET quantity = quantity - 1 WHERE user_id = ? AND chat_id = ? AND item_name = ?', (user_id, chat_id, item_name))
        conn.commit()
        conn.close()
        return True
    conn.close()
    return False

def clear_user_active_item(user_id, chat_id):
    conn = get_connection()
    c = conn.cursor()
    c.execute('UPDATE users SET active_item = "" WHERE user_id = ? AND chat_id = ?', (user_id, chat_id))
    conn.commit()
    conn.close()
