import sqlite3
import os

DB_FILE = "attendance.db"   # ✅ remove folder dependency

def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS participants (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT,
        join_time TEXT,
        leave_time TEXT,
        duration REAL
    )
    """)

    conn.commit()
    conn.close()


def save_attendance_to_db(name, join_time, leave_time, duration):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    cursor.execute("""
    INSERT INTO participants (name, join_time, leave_time, duration)
    VALUES (?, ?, ?, ?)
    """, (name, join_time, leave_time, duration))

    conn.commit()
    conn.close()


def get_members():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    cursor.execute("SELECT * FROM participants")
    data = cursor.fetchall()

    conn.close()
    return data