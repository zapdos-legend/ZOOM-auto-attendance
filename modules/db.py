import os
import sqlite3

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
            name TEXT NOT NULL UNIQUE,
            email TEXT,
            whatsapp TEXT,
            active INTEGER DEFAULT 1
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS meetings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            zoom_meeting_id TEXT,
            topic TEXT,
            meeting_date TEXT,
            start_time TEXT,
            end_time TEXT,
            total_minutes REAL,
            csv_file TEXT,
            pdf_file TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS attendance (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            meeting_pk INTEGER NOT NULL,
            participant_name TEXT NOT NULL,
            participant_email TEXT,
            join_time TEXT,
            leave_time TEXT,
            duration_minutes REAL,
            rejoins INTEGER,
            status TEXT,
            is_member INTEGER DEFAULT 0,
            is_host INTEGER DEFAULT 0,
            FOREIGN KEY(meeting_pk) REFERENCES meetings(id) ON DELETE CASCADE
        )
    """)

    conn.commit()
    conn.close()


def add_member(name, email="", whatsapp="", active=1):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        INSERT OR IGNORE INTO members (name, email, whatsapp, active)
        VALUES (?, ?, ?, ?)
    """, (name.strip(), email.strip(), whatsapp.strip(), int(active)))
    conn.commit()
    conn.close()


def remove_member(member_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM members WHERE id = ?", (member_id,))
    conn.commit()
    conn.close()


def set_member_active(member_id, active):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE members SET active = ? WHERE id = ?", (int(active), member_id))
    conn.commit()
    conn.close()


def get_members(active_only=False):
    conn = get_conn()
    cur = conn.cursor()

    if active_only:
        cur.execute("SELECT id, name, email, whatsapp, active FROM members WHERE active = 1 ORDER BY name")
    else:
        cur.execute("SELECT id, name, email, whatsapp, active FROM members ORDER BY name")

    rows = cur.fetchall()
    conn.close()
    return rows


def get_active_member_lookup():
    members = get_members(active_only=True)
    lookup = {}
    for member_id, name, email, whatsapp, active in members:
        lookup[name.strip().lower()] = {
            "id": member_id,
            "name": name,
            "email": email,
            "whatsapp": whatsapp,
            "active": active,
        }
    return lookup


def save_meeting_and_attendance(meeting_meta, rows, csv_file, pdf_file):
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        INSERT INTO meetings (
            zoom_meeting_id, topic, meeting_date, start_time, end_time, total_minutes, csv_file, pdf_file
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        meeting_meta["zoom_meeting_id"],
        meeting_meta["topic"],
        meeting_meta["meeting_date"],
        meeting_meta["start_time"],
        meeting_meta["end_time"],
        meeting_meta["total_minutes"],
        os.path.basename(csv_file),
        os.path.basename(pdf_file),
    ))

    meeting_pk = cur.lastrowid

    for row in rows:
        cur.execute("""
            INSERT INTO attendance (
                meeting_pk, participant_name, participant_email, join_time, leave_time,
                duration_minutes, rejoins, status, is_member, is_host
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            meeting_pk,
            row["name"],
            row.get("email", ""),
            row["join_time_str"],
            row["leave_time_str"],
            row["duration_minutes"],
            row["rejoins"],
            row["status"],
            row["is_member"],
            row["is_host"],
        ))

    conn.commit()
    conn.close()
    return meeting_pk


def get_recent_meetings(limit=30):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT id, zoom_meeting_id, topic, meeting_date, start_time, end_time, total_minutes, csv_file, pdf_file
        FROM meetings
        ORDER BY id DESC
        LIMIT ?
    """, (limit,))
    rows = cur.fetchall()
    conn.close()
    return rows


def get_attendance_for_meeting(meeting_pk):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT participant_name, join_time, leave_time, duration_minutes, rejoins, status, is_member, is_host
        FROM attendance
        WHERE meeting_pk = ?
        ORDER BY duration_minutes DESC
    """, (meeting_pk,))
    rows = cur.fetchall()
    conn.close()
    return rows


def delete_meeting(meeting_pk):
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("SELECT csv_file, pdf_file FROM meetings WHERE id = ?", (meeting_pk,))
    files = cur.fetchone()

    cur.execute("DELETE FROM attendance WHERE meeting_pk = ?", (meeting_pk,))
    cur.execute("DELETE FROM meetings WHERE id = ?", (meeting_pk,))

    conn.commit()
    conn.close()

    return files if files else (None, None)


def get_dashboard_analytics(host_name_hint=""):
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        SELECT COUNT(*)
        FROM meetings
    """)
    total_meetings = cur.fetchone()[0]

    cur.execute("""
        SELECT COUNT(*)
        FROM members
        WHERE active = 1
    """)
    active_members = cur.fetchone()[0]

    cur.execute("""
        SELECT status, COUNT(*)
        FROM attendance
        WHERE is_member = 1
        GROUP BY status
    """)
    status_counts = {row[0]: row[1] for row in cur.fetchall()}

    cur.execute("""
        SELECT meeting_date, COUNT(*)
        FROM attendance
        WHERE is_member = 1 AND status = 'PRESENT'
        GROUP BY meeting_date
        ORDER BY id ASC
    """)
    daily_present_rows = cur.fetchall()

    if host_name_hint:
        cur.execute("""
            SELECT participant_name, ROUND(SUM(duration_minutes), 2) AS total_duration
            FROM attendance
            WHERE is_member = 1 AND is_host = 0 AND LOWER(participant_name) != LOWER(?)
            GROUP BY participant_name
            ORDER BY total_duration DESC
            LIMIT 5
        """, (host_name_hint,))
    else:
        cur.execute("""
            SELECT participant_name, ROUND(SUM(duration_minutes), 2) AS total_duration
            FROM attendance
            WHERE is_member = 1 AND is_host = 0
            GROUP BY participant_name
            ORDER BY total_duration DESC
            LIMIT 5
        """)
    top_rows = cur.fetchall()

    conn.close()

    return {
        "total_meetings": total_meetings,
        "active_members": active_members,
        "status_counts": status_counts,
        "daily_present_rows": daily_present_rows,
        "top_rows": top_rows,
    }