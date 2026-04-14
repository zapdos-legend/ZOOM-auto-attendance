import sqlite3
import os
from config import DB_FILE, DATA_DIR

def get_conn():
    os.makedirs(DATA_DIR, exist_ok=True)
    return sqlite3.connect(DB_FILE)

def init_db():
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS members (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT,
        email TEXT,
        whatsapp TEXT,
        active INTEGER DEFAULT 1
    )
    """)

    conn.commit()
    conn.close()

def add_member(name, email, whatsapp):
    conn = get_conn()
    cur = conn.cursor()

    cur.execute(
        "INSERT INTO members (name, email, whatsapp) VALUES (?, ?, ?)",
        (name, email, whatsapp)
    )

    conn.commit()
    conn.close()

def get_members():
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("SELECT * FROM members WHERE active=1")
    rows = cur.fetchall()

    conn.close()

    members = []
    for r in rows:
        members.append({
            "id": r[0],
            "name": r[1],
            "email": r[2],
            "whatsapp": r[3]
        })

    return members