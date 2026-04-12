import sqlite3
import os

DB_FILE = "data/attendance.db"

def init_db():
    # ✅ VERY IMPORTANT (this fixes your error)
    os.makedirs("data", exist_ok=True)

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