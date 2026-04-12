import sqlite3
from config import DB_FILE


def init_db():
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS members (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE,
        email TEXT,
        whatsapp TEXT,
        active INTEGER DEFAULT 1
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS attendance_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        meeting_id TEXT,
        topic TEXT,
        date TEXT,
        start_time TEXT,
        end_time TEXT,
        participant_name TEXT,
        duration_minutes REAL,
        status TEXT,
        rejoin_count INTEGER
    )
    """)

    conn.commit()
    conn.close()


def add_member(name, email, whatsapp):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO members (name, email, whatsapp, active) VALUES (?, ?, ?, 1)",
                (name, email, whatsapp))
    conn.commit()
    conn.close()


def remove_member(name):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("DELETE FROM members WHERE name=?", (name,))
    conn.commit()
    conn.close()


def update_member_status(name, active):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("UPDATE members SET active=? WHERE name=?", (active, name))
    conn.commit()
    conn.close()


def get_members(active_only=True):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()

    if active_only:
        cur.execute("SELECT name, email, whatsapp, active FROM members WHERE active=1")
    else:
        cur.execute("SELECT name, email, whatsapp, active FROM members")

    rows = cur.fetchall()
    conn.close()
    return rows


def save_attendance_to_db(meeting_info, participants_data):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()

    for name, p in participants_data.items():
        cur.execute("""
        INSERT INTO attendance_logs 
        (meeting_id, topic, date, start_time, end_time, participant_name, duration_minutes, status, rejoin_count)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            meeting_info["meeting_id"],
            meeting_info["topic"],
            meeting_info["date"],
            meeting_info["start_time_str"],
            meeting_info["end_time_str"],
            name,
            round(p["total_seconds"] / 60, 2),
            p["status"],
            p["rejoin_count"]
        ))

    conn.commit()
    conn.close()


def fetch_attendance_logs():
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT * FROM attendance_logs ORDER BY date DESC")
    rows = cur.fetchall()
    conn.close()
    return rows