import os
import csv
import io
import json
import hmac
import base64
import hashlib
import threading
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from functools import wraps

from flask import (
    Flask,
    request,
    jsonify,
    redirect,
    url_for,
    send_from_directory,
    flash,
    render_template_string,
    session,
    Response,
)

import sqlite3
import psycopg
from psycopg.rows import dict_row

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import (
    SimpleDocTemplate,
    Table,
    TableStyle,
    Paragraph,
    Spacer,
)
from reportlab.lib.enums import TA_LEFT, TA_CENTER


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
REPORT_DIR = os.path.join(BASE_DIR, "attendance_reports")
LIVE_STATE_FILE = os.path.join(DATA_DIR, "live_state.json")
SQLITE_DB_FILE = os.path.join(DATA_DIR, "zoom_attendance.db")

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(REPORT_DIR, exist_ok=True)

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
USE_POSTGRES = bool(DATABASE_URL)

FLASK_SECRET_KEY = os.environ.get("FLASK_SECRET_KEY", "12345")
TIMEZONE_NAME = os.environ.get("TIMEZONE_NAME", "Asia/Kolkata")
APP_TZ = ZoneInfo(TIMEZONE_NAME)

PRESENT_PERCENTAGE = int(os.environ.get("PRESENT_PERCENTAGE", "75"))
LATE_COUNT_AS_PRESENT_PERCENTAGE = int(os.environ.get("LATE_COUNT_AS_PRESENT_PERCENTAGE", "30"))
LATE_THRESHOLD_MINUTES = int(os.environ.get("LATE_THRESHOLD_MINUTES", "10"))
INACTIVITY_CONFIRM_SECONDS = int(os.environ.get("INACTIVITY_CONFIRM_SECONDS", "120"))
ZOOM_SECRET_TOKEN = os.environ.get("ZOOM_SECRET_TOKEN", "your_zoom_secret_token")
HOST_NAME_HINT = os.environ.get("HOST_NAME_HINT", "Akshay").strip().lower()

ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")
VIEWER_USERNAME = os.environ.get("VIEWER_USERNAME", "viewer")
VIEWER_PASSWORD = os.environ.get("VIEWER_PASSWORD", "viewer123")

app = Flask(__name__)
app.secret_key = FLASK_SECRET_KEY
FINALIZE_TIMERS = {}


def get_sqlite_conn():
    conn = sqlite3.connect(SQLITE_DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def get_pg_conn():
    if not DATABASE_URL:
        raise ValueError("DATABASE_URL is missing")
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


def get_conn():
    return get_pg_conn() if USE_POSTGRES else get_sqlite_conn()


def is_pg():
    return USE_POSTGRES


def qmark(sql):
    if is_pg():
        return sql.replace("?", "%s")
    return sql


def now_utc():
    return datetime.now(timezone.utc)


def to_local(dt):
    if not dt:
        return None
    if isinstance(dt, str):
        dt = parse_zoom_time(dt)
    if not dt:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(APP_TZ)


def parse_zoom_time(value):
    if not value:
        return None
    try:
        if isinstance(value, datetime):
            if value.tzinfo is None:
                return value.replace(tzinfo=timezone.utc)
            return value.astimezone(timezone.utc)
        value = str(value)
        if value.endswith("Z"):
            return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def fmt_date(dt):
    dt = to_local(dt)
    return dt.strftime("%Y-%m-%d") if dt else ""


def fmt_time(dt):
    dt = to_local(dt)
    return dt.strftime("%I:%M:%S %p") if dt else ""


def rows_to_dicts(rows):
    out = []
    for r in rows:
        if isinstance(r, dict):
            out.append(r)
        else:
            out.append(dict(r))
    return out


def column_exists(conn, table_name, column_name):
    cur = conn.cursor()
    if is_pg():
        cur.execute("""
            SELECT EXISTS (
                SELECT 1
                FROM information_schema.columns
                WHERE table_schema='public'
                  AND table_name=%s
                  AND column_name=%s
            ) AS ok
        """, (table_name, column_name))
        return bool(cur.fetchone()["ok"])
    cur.execute(f"PRAGMA table_info({table_name})")
    cols = cur.fetchall()
    return any((c["name"] if isinstance(c, sqlite3.Row) else c[1]) == column_name for c in cols)


def add_column_if_missing(conn, table_name, column_name, sql_type):
    if column_exists(conn, table_name, column_name):
        return
    cur = conn.cursor()
    cur.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {sql_type}")
    conn.commit()


def seed_default_users(conn):
    cur = conn.cursor()
    defaults = [
        (ADMIN_USERNAME, ADMIN_PASSWORD, "admin"),
        (VIEWER_USERNAME, VIEWER_PASSWORD, "viewer"),
    ]

    for username, password, role in defaults:
        if not username or not password:
            continue
        cur.execute(qmark("SELECT id FROM users WHERE username = ?"), (username,))
        exists = cur.fetchone()
        if not exists:
            cur.execute(
                qmark("INSERT INTO users (username, password, role, active) VALUES (?, ?, ?, 1)"),
                (username, password, role),
            )
    conn.commit()


def init_db():
    conn = get_conn()
    cur = conn.cursor()

    if is_pg():
        cur.execute("""
            CREATE TABLE IF NOT EXISTS members (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                email TEXT,
                whatsapp TEXT,
                active INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS meetings (
                id SERIAL PRIMARY KEY,
                zoom_meeting_id TEXT,
                topic TEXT,
                meeting_date TEXT,
                start_time TEXT,
                end_time TEXT,
                total_minutes DOUBLE PRECISION,
                csv_file TEXT,
                pdf_file TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS attendance (
                id SERIAL PRIMARY KEY,
                meeting_pk INTEGER NOT NULL,
                participant_name TEXT NOT NULL,
                participant_email TEXT,
                join_time TEXT,
                leave_time TEXT,
                duration_minutes DOUBLE PRECISION,
                rejoins INTEGER,
                status TEXT,
                is_member INTEGER DEFAULT 0,
                is_host INTEGER DEFAULT 0,
                is_unknown INTEGER DEFAULT 0
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                username TEXT NOT NULL UNIQUE,
                password TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'viewer',
                active INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
    else:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS members (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                email TEXT,
                whatsapp TEXT,
                active INTEGER DEFAULT 1,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
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
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
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
                is_unknown INTEGER DEFAULT 0
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                password TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'viewer',
                active INTEGER DEFAULT 1,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)

    conn.commit()

    add_column_if_missing(conn, "attendance", "is_unknown", "INTEGER DEFAULT 0")
    add_column_if_missing(conn, "meetings", "csv_content", "TEXT")
    add_column_if_missing(conn, "meetings", "pdf_content_b64", "TEXT")
    add_column_if_missing(conn, "meetings", "joined_count", "INTEGER DEFAULT 0")
    add_column_if_missing(conn, "meetings", "present_member_count", "INTEGER DEFAULT 0")
    add_column_if_missing(conn, "meetings", "late_member_count", "INTEGER DEFAULT 0")
    add_column_if_missing(conn, "meetings", "absent_member_count", "INTEGER DEFAULT 0")
    add_column_if_missing(conn, "meetings", "unknown_count", "INTEGER DEFAULT 0")
    add_column_if_missing(conn, "meetings", "attendance_percentage", "DOUBLE PRECISION" if is_pg() else "REAL")

    conn.commit()
    seed_default_users(conn)
    conn.close()


def authenticate_user(username, password):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        qmark("SELECT * FROM users WHERE username = ? AND password = ? AND active = 1"),
        (username, password),
    )
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


def get_users(search=""):
    conn = get_conn()
    cur = conn.cursor()
    sql = "SELECT * FROM users"
    params = []
    if search.strip():
        sql += " WHERE LOWER(username) LIKE LOWER(?)" if not is_pg() else " WHERE LOWER(username) LIKE LOWER(%s)"
        params.append(f"%{search.strip()}%")
    sql += " ORDER BY username"
    cur.execute(sql, tuple(params))
    rows = rows_to_dicts(cur.fetchall())
    conn.close()
    return rows


def create_or_update_user(username, password, role):
    username = (username or "").strip()
    password = (password or "").strip()
    role = (role or "viewer").strip().lower()

    if not username or not password:
        raise ValueError("Username and password are required.")
    if role not in ("admin", "viewer"):
        raise ValueError("Role must be admin or viewer.")

    conn = get_conn()
    cur = conn.cursor()
    if is_pg():
        cur.execute("""
            INSERT INTO users (username, password, role, active)
            VALUES (%s, %s, %s, 1)
            ON CONFLICT (username) DO UPDATE SET
                password = EXCLUDED.password,
                role = EXCLUDED.role
        """, (username, password, role))
    else:
        cur.execute("""
            INSERT INTO users (username, password, role, active)
            VALUES (?, ?, ?, 1)
            ON CONFLICT(username) DO UPDATE SET
                password = excluded.password,
                role = excluded.role
        """, (username, password, role))
    conn.commit()
    conn.close()


def toggle_user_active(user_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(qmark("UPDATE users SET active = CASE WHEN active = 1 THEN 0 ELSE 1 END WHERE id = ?"), (user_id,))
    conn.commit()
    conn.close()


def delete_user_account(user_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(qmark("DELETE FROM users WHERE id = ?"), (user_id,))
    conn.commit()
    conn.close()


def current_user():
    return {
        "username": session.get("username", ""),
        "role": session.get("role", ""),
        "is_logged_in": bool(session.get("username")),
    }


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("username"):
            flash("Please login first.", "bad")
            return redirect(url_for("login_page"))
        return fn(*args, **kwargs)
    return wrapper


def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("username"):
            flash("Please login first.", "bad")
            return redirect(url_for("login_page"))
        if session.get("role") != "admin":
            flash("Admin access required.", "bad")
            return redirect(url_for("dashboard_home"))
        return fn(*args, **kwargs)
    return wrapper


def get_setting(key, default=""):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(qmark("SELECT value FROM app_settings WHERE key = ?"), (key,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return default
    return row["value"] if isinstance(row, dict) else row["value"]


def set_setting(key, value):
    conn = get_conn()
    cur = conn.cursor()
    if is_pg():
        cur.execute("""
            INSERT INTO app_settings (key, value)
            VALUES (%s, %s)
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
        """, (key, str(value)))
    else:
        cur.execute("""
            INSERT INTO app_settings (key, value)
            VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """, (key, str(value)))
    conn.commit()
    conn.close()


def current_present_percentage():
    return int(get_setting("present_percentage", str(PRESENT_PERCENTAGE)))


def current_late_count_as_present_percentage():
    return int(get_setting("late_count_as_present_percentage", str(LATE_COUNT_AS_PRESENT_PERCENTAGE)))


def current_late_threshold_minutes():
    return int(get_setting("late_threshold_minutes", str(LATE_THRESHOLD_MINUTES)))


def current_host_name_hint():
    return (get_setting("host_name_hint", HOST_NAME_HINT) or "").strip().lower()


def current_inactivity_confirm_seconds():
    return int(get_setting("inactivity_confirm_seconds", str(INACTIVITY_CONFIRM_SECONDS)))


def get_members(active_only=False, search=""):
    conn = get_conn()
    cur = conn.cursor()
    sql = "SELECT * FROM members"
    params = []
    where = []
    if active_only:
        where.append("active = 1")
    if search.strip():
        where.append("LOWER(name) LIKE LOWER(?)" if not is_pg() else "LOWER(name) LIKE LOWER(%s)")
        params.append(f"%{search.strip()}%")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY name"
    cur.execute(sql, tuple(params))
    rows = rows_to_dicts(cur.fetchall())
    conn.close()
    return rows


def add_or_update_member(name, email, whatsapp):
    name = (name or "").strip()
    email = (email or "").strip()
    whatsapp = (whatsapp or "").strip()
    if not name:
        raise ValueError("Name is required.")

    conn = get_conn()
    cur = conn.cursor()

    if is_pg():
        cur.execute("""
            INSERT INTO members (name, email, whatsapp, active)
            VALUES (%s, %s, %s, 1)
            ON CONFLICT(name) DO UPDATE SET
                email = EXCLUDED.email,
                whatsapp = EXCLUDED.whatsapp
        """, (name, email, whatsapp))
    else:
        cur.execute("""
            INSERT INTO members (name, email, whatsapp, active)
            VALUES (?, ?, ?, 1)
            ON CONFLICT(name) DO UPDATE SET
                email = excluded.email,
                whatsapp = excluded.whatsapp
        """, (name, email, whatsapp))
    conn.commit()
    conn.close()


def toggle_member(member_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(qmark("UPDATE members SET active = CASE WHEN active = 1 THEN 0 ELSE 1 END WHERE id = ?"), (member_id,))
    conn.commit()
    conn.close()


def delete_member(member_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(qmark("DELETE FROM members WHERE id = ?"), (member_id,))
    conn.commit()
    conn.close()


def import_members_from_csv(file_storage):
    content = file_storage.read().decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(content))
    imported = 0
    for row in reader:
        name = (row.get("name") or row.get("Name") or "").strip()
        email = (row.get("email") or row.get("Email") or "").strip()
        whatsapp = (row.get("whatsapp") or row.get("WhatsApp") or row.get("phone") or "").strip()
        if name:
            add_or_update_member(name, email, whatsapp)
            imported += 1
    return imported


def save_meeting_and_attendance(meeting_meta, rows, csv_file, pdf_file):
    with open(csv_file, "r", encoding="utf-8") as f:
        csv_content = f.read()
    with open(pdf_file, "rb") as f:
        pdf_b64 = base64.b64encode(f.read()).decode("utf-8")

    conn = get_conn()
    cur = conn.cursor()

    if is_pg():
        cur.execute("""
            INSERT INTO meetings (
                zoom_meeting_id, topic, meeting_date, start_time, end_time,
                total_minutes, csv_file, pdf_file,
                csv_content, pdf_content_b64,
                joined_count, present_member_count, late_member_count,
                absent_member_count, unknown_count, attendance_percentage
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
        """, (
            meeting_meta["zoom_meeting_id"],
            meeting_meta["topic"],
            meeting_meta["meeting_date"],
            meeting_meta["start_time"],
            meeting_meta["end_time"],
            meeting_meta["total_minutes"],
            os.path.basename(csv_file),
            os.path.basename(pdf_file),
            csv_content,
            pdf_b64,
            meeting_meta["joined_count"],
            meeting_meta["total_present_members"],
            meeting_meta["late_member_count"],
            meeting_meta["total_absent_members"],
            meeting_meta["total_unknown_participants"],
            meeting_meta["member_attendance_percentage"],
        ))
        meeting_pk = cur.fetchone()["id"]
    else:
        cur.execute("""
            INSERT INTO meetings (
                zoom_meeting_id, topic, meeting_date, start_time, end_time,
                total_minutes, csv_file, pdf_file,
                csv_content, pdf_content_b64,
                joined_count, present_member_count, late_member_count,
                absent_member_count, unknown_count, attendance_percentage
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            meeting_meta["zoom_meeting_id"],
            meeting_meta["topic"],
            meeting_meta["meeting_date"],
            meeting_meta["start_time"],
            meeting_meta["end_time"],
            meeting_meta["total_minutes"],
            os.path.basename(csv_file),
            os.path.basename(pdf_file),
            csv_content,
            pdf_b64,
            meeting_meta["joined_count"],
            meeting_meta["total_present_members"],
            meeting_meta["late_member_count"],
            meeting_meta["total_absent_members"],
            meeting_meta["total_unknown_participants"],
            meeting_meta["member_attendance_percentage"],
        ))
        meeting_pk = cur.lastrowid

    for row in rows:
        cur.execute(qmark("""
            INSERT INTO attendance (
                meeting_pk, participant_name, participant_email, join_time, leave_time,
                duration_minutes, rejoins, status, is_member, is_host, is_unknown
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """), (
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
            row["is_unknown"],
        ))

    conn.commit()
    conn.close()


def get_recent_meetings(limit=50, date_from="", date_to="", topic_search=""):
    conn = get_conn()
    cur = conn.cursor()

    sql = "SELECT * FROM meetings"
    params = []
    where = []

    if date_from.strip():
        where.append("meeting_date >= ?" if not is_pg() else "meeting_date >= %s")
        params.append(date_from.strip())
    if date_to.strip():
        where.append("meeting_date <= ?" if not is_pg() else "meeting_date <= %s")
        params.append(date_to.strip())
    if topic_search.strip():
        where.append("LOWER(topic) LIKE LOWER(?)" if not is_pg() else "LOWER(topic) LIKE LOWER(%s)")
        params.append(f"%{topic_search.strip()}%")

    if where:
        sql += " WHERE " + " AND ".join(where)

    sql += " ORDER BY id DESC LIMIT ?" if not is_pg() else " ORDER BY id DESC LIMIT %s"
    params.append(limit)

    cur.execute(sql, tuple(params))
    rows = rows_to_dicts(cur.fetchall())
    conn.close()
    return rows


def get_meeting(meeting_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(qmark("SELECT * FROM meetings WHERE id = ?"), (meeting_id,))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


def get_attendance_rows(meeting_id, member_name="", status="", topic="", date_str=""):
    conn = get_conn()
    cur = conn.cursor()

    sql = """
        SELECT a.*, m.topic, m.meeting_date
        FROM attendance a
        JOIN meetings m ON a.meeting_pk = m.id
        WHERE a.meeting_pk = ?
    """
    params = [meeting_id]

    if member_name.strip():
        sql += " AND LOWER(a.participant_name) LIKE LOWER(?)" if not is_pg() else " AND LOWER(a.participant_name) LIKE LOWER(%s)"
        params.append(f"%{member_name.strip()}%")
    if status.strip():
        sql += " AND a.status = ?" if not is_pg() else " AND a.status = %s"
        params.append(status.strip().upper())
    if topic.strip():
        sql += " AND LOWER(m.topic) LIKE LOWER(?)" if not is_pg() else " AND LOWER(m.topic) LIKE LOWER(%s)"
        params.append(f"%{topic.strip()}%")
    if date_str.strip():
        sql += " AND m.meeting_date = ?" if not is_pg() else " AND m.meeting_date = %s"
        params.append(date_str.strip())

    sql += " ORDER BY a.is_host DESC, a.duration_minutes DESC, a.participant_name ASC"
    cur.execute(sql, tuple(params))
    rows = rows_to_dicts(cur.fetchall())
    conn.close()
    return rows


def delete_meeting(meeting_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(qmark("DELETE FROM attendance WHERE meeting_pk = ?"), (meeting_id,))
    cur.execute(qmark("DELETE FROM meetings WHERE id = ?"), (meeting_id,))
    conn.commit()
    conn.close()


def load_report_from_db(meeting_id, file_type):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(qmark("SELECT csv_file, pdf_file, csv_content, pdf_content_b64 FROM meetings WHERE id = ?"), (meeting_id,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    row = dict(row)
    if file_type == "csv":
        return {
            "filename": row.get("csv_file") or f"meeting_{meeting_id}.csv",
            "content": (row.get("csv_content") or "").encode("utf-8"),
            "mimetype": "text/csv",
        }
    b64 = row.get("pdf_content_b64") or ""
    if not b64:
        return None
    return {
        "filename": row.get("pdf_file") or f"meeting_{meeting_id}.pdf",
        "content": base64.b64decode(b64),
        "mimetype": "application/pdf",
    }


def get_analytics():
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("SELECT COUNT(*) AS c FROM meetings")
    total_meetings = cur.fetchone()["c"]

    cur.execute("SELECT COUNT(*) AS c FROM members")
    total_members = cur.fetchone()["c"]

    cur.execute("SELECT COUNT(*) AS c FROM members WHERE active = 1")
    active_members = cur.fetchone()["c"]

    cur.execute("SELECT COUNT(*) AS c FROM attendance WHERE status = 'PRESENT' AND is_member = 1 AND is_host = 0")
    present_count = cur.fetchone()["c"]

    cur.execute("SELECT COUNT(*) AS c FROM attendance WHERE status = 'LATE' AND is_member = 1 AND is_host = 0")
    late_count = cur.fetchone()["c"]

    cur.execute("SELECT COUNT(*) AS c FROM attendance WHERE status = 'ABSENT' AND is_member = 1 AND is_host = 0")
    absent_count = cur.fetchone()["c"]

    cur.execute("SELECT COUNT(*) AS c FROM attendance WHERE is_unknown = 1 AND is_host = 0")
    unknown_count = cur.fetchone()["c"]

    total_member_records = present_count + late_count + absent_count
    attendance_rate = round(((present_count + late_count) / total_member_records) * 100, 2) if total_member_records else 0

    cur.execute("""
        SELECT participant_name, ROUND(SUM(duration_minutes)::numeric, 2) AS total_duration
        FROM attendance
        WHERE is_host = 0
        GROUP BY participant_name
        ORDER BY total_duration DESC
        LIMIT 5
    """ if is_pg() else """
        SELECT participant_name, ROUND(SUM(duration_minutes), 2) AS total_duration
        FROM attendance
        WHERE is_host = 0
        GROUP BY participant_name
        ORDER BY total_duration DESC
        LIMIT 5
    """)
    top_attendees = rows_to_dicts(cur.fetchall())

    cur.execute("""
        SELECT
            participant_name,
            SUM(CASE WHEN status = 'PRESENT' THEN 1 ELSE 0 END) AS present_count,
            SUM(CASE WHEN status = 'LATE' THEN 1 ELSE 0 END) AS late_count,
            SUM(CASE WHEN status = 'ABSENT' THEN 1 ELSE 0 END) AS absent_count,
            ROUND(SUM(duration_minutes)::numeric, 2) AS total_duration,
            ROUND(AVG(duration_minutes)::numeric, 2) AS avg_duration,
            ROUND(
                100.0 * SUM(CASE WHEN status IN ('PRESENT','LATE') THEN 1 ELSE 0 END) / NULLIF(COUNT(*),0),
                2
            ) AS attendance_percentage
        FROM attendance
        WHERE is_member = 1 AND is_host = 0
        GROUP BY participant_name
        ORDER BY attendance_percentage DESC, total_duration DESC
    """ if is_pg() else """
        SELECT
            participant_name,
            SUM(CASE WHEN status = 'PRESENT' THEN 1 ELSE 0 END) AS present_count,
            SUM(CASE WHEN status = 'LATE' THEN 1 ELSE 0 END) AS late_count,
            SUM(CASE WHEN status = 'ABSENT' THEN 1 ELSE 0 END) AS absent_count,
            ROUND(SUM(duration_minutes), 2) AS total_duration,
            ROUND(AVG(duration_minutes), 2) AS avg_duration,
            ROUND(
                100.0 * SUM(CASE WHEN status IN ('PRESENT','LATE') THEN 1 ELSE 0 END) / COUNT(*),
                2
            ) AS attendance_percentage
        FROM attendance
        WHERE is_member = 1 AND is_host = 0
        GROUP BY participant_name
        ORDER BY attendance_percentage DESC, total_duration DESC
    """)
    member_stats = rows_to_dicts(cur.fetchall())

    cur.execute("""
        SELECT
            m.id,
            m.topic,
            m.meeting_date,
            ROUND(COALESCE(m.total_minutes, 0)::numeric, 2) AS total_minutes,
            COALESCE(m.present_member_count, 0) AS present_count,
            COALESCE(m.late_member_count, 0) AS late_count,
            COALESCE(m.absent_member_count, 0) AS absent_count,
            COALESCE(m.unknown_count, 0) AS unknown_count,
            COALESCE(m.attendance_percentage, 0) AS attendance_percentage,
            COALESCE(m.joined_count, 0) AS joined_count
        FROM meetings m
        ORDER BY m.id DESC
        LIMIT 20
    """ if is_pg() else """
        SELECT
            m.id,
            m.topic,
            m.meeting_date,
            ROUND(COALESCE(m.total_minutes, 0), 2) AS total_minutes,
            COALESCE(m.present_member_count, 0) AS present_count,
            COALESCE(m.late_member_count, 0) AS late_count,
            COALESCE(m.absent_member_count, 0) AS absent_count,
            COALESCE(m.unknown_count, 0) AS unknown_count,
            COALESCE(m.attendance_percentage, 0) AS attendance_percentage,
            COALESCE(m.joined_count, 0) AS joined_count
        FROM meetings m
        ORDER BY m.id DESC
        LIMIT 20
    """)
    recent_stats = rows_to_dicts(cur.fetchall())

    conn.close()
    return {
        "total_meetings": total_meetings,
        "total_members": total_members,
        "active_members": active_members,
        "present_count": present_count,
        "late_count": late_count,
        "absent_count": absent_count,
        "unknown_count": unknown_count,
        "attendance_rate": attendance_rate,
        "top_attendees": top_attendees,
        "member_stats": member_stats,
        "recent_stats": recent_stats,
    }


def default_live_state():
    return {
        "meeting": {
            "zoom_meeting_id": "",
            "topic": "No live meeting",
            "started_at": "",
            "ended_at": "",
            "finalized": False,
        },
        "participants": {}
    }


def load_live_state():
    if not os.path.exists(LIVE_STATE_FILE):
        return default_live_state()
    try:
        with open(LIVE_STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default_live_state()


def save_live_state(state):
    with open(LIVE_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def reset_live_state():
    state = default_live_state()
    save_live_state(state)
    return state


def member_lookup():
    rows = get_members(active_only=True)
    return {row["name"].strip().lower(): row for row in rows}


def verify_zoom_signature():
    if not ZOOM_SECRET_TOKEN:
        return True

    signature = request.headers.get("x-zm-signature", "")
    timestamp = request.headers.get("x-zm-request-timestamp", "")

    if not signature or not timestamp:
        return True

    body = request.get_data(as_text=True)
    message = f"v0:{timestamp}:{body}"
    digest = hmac.new(
        ZOOM_SECRET_TOKEN.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    expected = f"v0={digest}"
    return hmac.compare_digest(expected, signature)


def zoom_url_validation(payload):
    plain_token = payload.get("payload", {}).get("plainToken", "")
    encrypted_token = hmac.new(
        ZOOM_SECRET_TOKEN.encode("utf-8"),
        plain_token.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return {"plainToken": plain_token, "encryptedToken": encrypted_token}


def ensure_live_meeting(zoom_meeting_id, topic, event_time):
    state = load_live_state()
    current_id = state["meeting"].get("zoom_meeting_id", "")

    if not current_id or current_id != str(zoom_meeting_id):
        state = default_live_state()
        state["meeting"]["zoom_meeting_id"] = str(zoom_meeting_id)
        state["meeting"]["topic"] = topic or "Untitled Meeting"
        state["meeting"]["started_at"] = (event_time or now_utc()).isoformat()
        state["meeting"]["ended_at"] = ""
        state["meeting"]["finalized"] = False
        save_live_state(state)

    return state


def update_participant_join(name, email, event_time):
    state = load_live_state()
    participants = state["participants"]
    key = (name or "Unknown").strip()
    host_hint = current_host_name_hint()

    if key not in participants:
        participants[key] = {
            "name": key,
            "email": email or "",
            "first_join": (event_time or now_utc()).isoformat(),
            "last_leave": "",
            "current_join": (event_time or now_utc()).isoformat(),
            "total_seconds": 0,
            "rejoins": 0,
            "status": "LIVE",
            "is_host": host_hint in key.lower() if host_hint else False,
        }
    else:
        p = participants[key]
        if not p.get("current_join"):
            p["current_join"] = (event_time or now_utc()).isoformat()
            p["rejoins"] = int(p.get("rejoins", 0)) + 1
        p["status"] = "LIVE"
        if email and not p.get("email"):
            p["email"] = email

    save_live_state(state)


def update_participant_leave(name, event_time):
    state = load_live_state()
    participants = state["participants"]
    key = (name or "Unknown").strip()

    if key in participants:
        p = participants[key]
        current_join = parse_zoom_time(p.get("current_join", "")) if p.get("current_join") else None
        if current_join and event_time:
            session_seconds = max(0, int((event_time - current_join).total_seconds()))
            p["total_seconds"] = int(p.get("total_seconds", 0)) + session_seconds
        p["current_join"] = ""
        p["last_leave"] = (event_time or now_utc()).isoformat()
        p["status"] = "LEFT"

    save_live_state(state)


def schedule_finalize(meeting_id):
    if meeting_id in FINALIZE_TIMERS:
        try:
            FINALIZE_TIMERS[meeting_id].cancel()
        except Exception:
            pass

    timer = threading.Timer(current_inactivity_confirm_seconds(), finalize_meeting, args=[meeting_id])
    timer.daemon = True
    FINALIZE_TIMERS[meeting_id] = timer
    timer.start()


def finalize_meeting(zoom_meeting_id):
    state = load_live_state()
    meeting = state["meeting"]

    if str(meeting.get("zoom_meeting_id", "")) != str(zoom_meeting_id):
        return
    if meeting.get("finalized"):
        return

    started_at = parse_zoom_time(meeting.get("started_at", "")) or now_utc()
    ended_at = parse_zoom_time(meeting.get("ended_at", "")) or now_utc()

    lookup = member_lookup()
    rows = []
    max_participant_minutes = 0.0

    for _, p in state["participants"].items():
        p = dict(p)
        current_join = parse_zoom_time(p.get("current_join", "")) if p.get("current_join") else None
        total_seconds = int(p.get("total_seconds", 0))

        if current_join:
            total_seconds += max(0, int((ended_at - current_join).total_seconds()))
            p["last_leave"] = ended_at.isoformat()
            p["current_join"] = ""

        duration_minutes = round(total_seconds / 60.0, 2)
        max_participant_minutes = max(max_participant_minutes, duration_minutes)

        key = p["name"].strip().lower()
        member = lookup.get(key)

        rows.append({
            "name": p["name"],
            "email": p.get("email", "") or (member["email"] if member else ""),
            "join_time_str": fmt_time(parse_zoom_time(p.get("first_join", ""))) if p.get("first_join") else "-",
            "leave_time_str": fmt_time(parse_zoom_time(p.get("last_leave", ""))) if p.get("last_leave") else "-",
            "duration_minutes": duration_minutes,
            "rejoins": int(p.get("rejoins", 0)),
            "status": "PENDING",
            "is_member": 1 if member else 0,
            "is_host": 1 if p.get("is_host") else 0,
            "is_unknown": 0 if member else 1,
        })

    actual_minutes = round(max(0, (ended_at - started_at).total_seconds()) / 60.0, 2)
    total_meeting_minutes = round(max(actual_minutes, max_participant_minutes), 2)
    present_percentage = current_present_percentage()
    late_count_as_present_percentage = current_late_count_as_present_percentage()

    threshold_minutes = round((present_percentage / 100.0) * total_meeting_minutes, 2)
    late_count_as_present_threshold = round((late_count_as_present_percentage / 100.0) * total_meeting_minutes, 2)

    active_members = get_members(active_only=True)
    existing_names = {r["name"].strip().lower() for r in rows}

    for m in active_members:
        if m["name"].strip().lower() not in existing_names:
            rows.append({
                "name": m["name"],
                "email": m["email"] or "",
                "join_time_str": "-",
                "leave_time_str": "-",
                "duration_minutes": 0.0,
                "rejoins": 0,
                "status": "ABSENT",
                "is_member": 1,
                "is_host": 0,
                "is_unknown": 0,
            })

    late_member_count = 0
    for row in rows:
        if row["is_host"] == 1:
            row["status"] = "HOST"
        elif row["is_member"] == 1:
            if row["duration_minutes"] <= 0:
                row["status"] = "ABSENT"
            elif row["duration_minutes"] >= threshold_minutes:
                row["status"] = "PRESENT"
            else:
                row["status"] = "LATE"
                late_member_count += 1
        else:
            row["status"] = "PRESENT" if row["duration_minutes"] >= threshold_minutes else "LATE"

    rows.sort(key=lambda x: (x["is_host"] == 0, -x["duration_minutes"], x["name"].lower()))

    stamp = datetime.now(APP_TZ).strftime("%Y%m%d_%H%M%S")
    safe_topic = "".join(c if c.isalnum() or c in ("_", "-") else "_" for c in meeting.get("topic", "meeting"))[:40]
    csv_file = os.path.join(REPORT_DIR, f"{safe_topic}_{stamp}.csv")
    pdf_file = os.path.join(REPORT_DIR, f"{safe_topic}_{stamp}.pdf")

    joined_count = sum(1 for r in rows if r["join_time_str"] != "-")
    total_members = sum(1 for r in rows if r["is_member"] == 1 and r["is_host"] == 0)
    total_present_members = sum(
        1 for r in rows
        if r["is_member"] == 1 and r["is_host"] == 0 and (
            r["status"] == "PRESENT" or (r["status"] == "LATE" and r["duration_minutes"] > late_count_as_present_threshold)
        )
    )
    total_absent_members = sum(
        1 for r in rows if r["is_member"] == 1 and r["is_host"] == 0 and r["status"] == "ABSENT"
    )
    total_unknown_participants = sum(1 for r in rows if r["is_unknown"] == 1 and r["join_time_str"] != "-" and r["is_host"] == 0)

    member_attendance_percentage = round((100.0 * total_present_members / total_members), 2) if total_members else 0.0

    meeting_meta = {
        "zoom_meeting_id": str(meeting.get("zoom_meeting_id", "")),
        "topic": meeting.get("topic", "Untitled Meeting"),
        "meeting_date": fmt_date(started_at),
        "start_time": fmt_time(started_at),
        "end_time": fmt_time(ended_at),
        "total_minutes": total_meeting_minutes,
        "threshold_minutes": threshold_minutes,
        "late_count_as_present_threshold": late_count_as_present_threshold,
        "joined_count": joined_count,
        "total_members": total_members,
        "total_present_members": total_present_members,
        "late_member_count": late_member_count,
        "total_absent_members": total_absent_members,
        "total_unknown_participants": total_unknown_participants,
        "member_attendance_percentage": member_attendance_percentage,
    }

    generate_csv_report(csv_file, meeting_meta, rows)
    generate_pdf_report(pdf_file, meeting_meta, rows)
    save_meeting_and_attendance(meeting_meta, rows, csv_file, pdf_file)

    state["meeting"]["finalized"] = True
    save_live_state(state)
    reset_live_state()


def generate_csv_report(file_path, meeting_meta, rows):
    with open(file_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["Topic", meeting_meta["topic"]])
        writer.writerow(["Meeting ID", meeting_meta["zoom_meeting_id"]])
        writer.writerow(["Date", meeting_meta["meeting_date"]])
        writer.writerow(["Start Time", meeting_meta["start_time"]])
        writer.writerow(["End Time", meeting_meta["end_time"]])
        writer.writerow(["Total Meeting Duration", meeting_meta["total_minutes"]])
        writer.writerow(["Present Threshold", meeting_meta["threshold_minutes"]])
        writer.writerow(["Late Count As Present Threshold", meeting_meta["late_count_as_present_threshold"]])
        writer.writerow(["Total Participants", meeting_meta["joined_count"]])
        writer.writerow(["Total Members", meeting_meta["total_members"]])
        writer.writerow(["Total Present Members", meeting_meta["total_present_members"]])
        writer.writerow(["Late Member Count", meeting_meta["late_member_count"]])
        writer.writerow(["Total Absent Members", meeting_meta["total_absent_members"]])
        if meeting_meta["total_unknown_participants"] > 0:
            writer.writerow(["Total Unknown Participants", meeting_meta["total_unknown_participants"]])
        writer.writerow(["Member Attendance Percentage", meeting_meta["member_attendance_percentage"]])
        writer.writerow([])
        writer.writerow(["Name", "Join", "Leave", "Duration", "Rejoins", "Status"])

        for row in rows:
            writer.writerow([
                row["name"],
                row["join_time_str"],
                row["leave_time_str"],
                row["duration_minutes"],
                row["rejoins"],
                row["status"],
            ])


def generate_pdf_report(file_path, meeting_meta, rows):
    doc = SimpleDocTemplate(
        file_path,
        pagesize=landscape(A4),
        leftMargin=20,
        rightMargin=20,
        topMargin=20,
        bottomMargin=20
    )
    styles = getSampleStyleSheet()

    normal = ParagraphStyle("normal_custom", parent=styles["Normal"], fontSize=10, leading=13, alignment=TA_LEFT)
    small = ParagraphStyle("small_custom", parent=styles["Normal"], fontSize=9, leading=11)
    center = ParagraphStyle("center_custom", parent=styles["Normal"], fontSize=9, leading=11, alignment=TA_CENTER)
    title_style = ParagraphStyle("title_custom", parent=styles["Title"], alignment=TA_CENTER)

    story = []
    story.append(Paragraph("Attendance Report", title_style))
    story.append(Spacer(1, 12))
    story.append(Paragraph(f"<b>Topic:</b> {meeting_meta['topic']}", normal))
    story.append(Paragraph(f"<b>Meeting ID:</b> {meeting_meta['zoom_meeting_id']}", normal))
    story.append(Paragraph(f"<b>Date:</b> {meeting_meta['meeting_date']}", normal))
    story.append(Paragraph(f"<b>Start Time:</b> {meeting_meta['start_time']}", normal))
    story.append(Paragraph(f"<b>End Time:</b> {meeting_meta['end_time']}", normal))
    story.append(Spacer(1, 6))
    story.append(Paragraph(f"<b>Total Meeting Duration:</b> {meeting_meta['total_minutes']} minutes", normal))
    story.append(Spacer(1, 10))

    def colored_name(row):
        if row["is_unknown"] == 1 and row["is_host"] == 0:
            return Paragraph(f'<font color="red">{row["name"]}</font>', center)
        return Paragraph(row["name"], center)

    def colored_status(status):
        if status == "PRESENT":
            return Paragraph('<font color="green"><b>PRESENT</b></font>', center)
        if status == "LATE":
            return Paragraph('<font color="orange"><b>LATE</b></font>', center)
        if status == "ABSENT":
            return Paragraph('<font color="red"><b>ABSENT</b></font>', center)
        if status == "HOST":
            return Paragraph('<b>HOST</b>', center)
        return Paragraph(status, center)

    table_data = [[
        Paragraph("<b>Name</b>", center),
        Paragraph("<b>Join</b>", center),
        Paragraph("<b>Leave</b>", center),
        Paragraph("<b>Duration</b>", center),
        Paragraph("<b>Rejoins</b>", center),
        Paragraph("<b>Status</b>", center),
    ]]

    for row in rows:
        table_data.append([
            colored_name(row),
            Paragraph(row["join_time_str"], center),
            Paragraph(row["leave_time_str"], center),
            Paragraph(str(row["duration_minutes"]), center),
            Paragraph(str(row["rejoins"]), center),
            colored_status(row["status"]),
        ])

    table = Table(table_data, repeatRows=1, colWidths=[135, 95, 95, 70, 60, 85])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
        ("GRID", (0, 0), (-1, -1), 0.7, colors.black),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("PADDING", (0, 0), (-1, -1), 6),
    ]))
    story.append(table)
    story.append(Spacer(1, 12))

    summary_lines = [
        f"<b>Total Participants (Joined Only):</b> {meeting_meta['joined_count']}",
        f"<b>Total Members:</b> {meeting_meta['total_members']}",
        f"<b>Total Present Members:</b> {meeting_meta['total_present_members']}",
        f"<b>Late Member Count:</b> {meeting_meta['late_member_count']}",
        f"<b>Total Absent Members:</b> {meeting_meta['total_absent_members']}",
        f"<b>Member Attendance Percentage:</b> {meeting_meta['member_attendance_percentage']}%",
    ]
    if meeting_meta["total_unknown_participants"] > 0:
        summary_lines.append(f"<b>Total Unknown Participants:</b> {meeting_meta['total_unknown_participants']}")

    for line in summary_lines:
        story.append(Paragraph(line, normal))

    doc.build(story)


def generate_analytics_pdf_bytes(analytics):
    buffer_path = os.path.join(DATA_DIR, "analytics_export.pdf")
    doc = SimpleDocTemplate(buffer_path, pagesize=landscape(A4), leftMargin=20, rightMargin=20, topMargin=20, bottomMargin=20)
    styles = getSampleStyleSheet()
    story = []

    story.append(Paragraph("Analytics Summary", styles["Title"]))
    story.append(Spacer(1, 12))
    story.append(Paragraph(f"Total Meetings: {analytics['total_meetings']}", styles["Normal"]))
    story.append(Paragraph(f"Total Members: {analytics['total_members']}", styles["Normal"]))
    story.append(Paragraph(f"Active Members: {analytics['active_members']}", styles["Normal"]))
    story.append(Paragraph(f"Present Count: {analytics['present_count']}", styles["Normal"]))
    story.append(Paragraph(f"Late Count: {analytics['late_count']}", styles["Normal"]))
    story.append(Paragraph(f"Absent Count: {analytics['absent_count']}", styles["Normal"]))
    story.append(Paragraph(f"Unknown Participant Count: {analytics['unknown_count']}", styles["Normal"]))
    story.append(Paragraph(f"Attendance Rate: {analytics['attendance_rate']}%", styles["Normal"]))
    doc.build(story)

    with open(buffer_path, "rb") as f:
        return f.read()


BASE_HTML = """
<!doctype html>
<html>
<head>
    <title>{{ title }}</title>
    {% if auto_refresh %}
    <meta http-equiv="refresh" content="{{ auto_refresh }}">
    {% endif %}
    <style>
        body { font-family: Arial, sans-serif; background:#f5f7fb; margin:0; padding:0; color:#1f2937; }
        .top { background:#111827; color:white; padding:18px 24px; }
        .top h1 { margin:0; font-size:26px; }
        .nav { margin-top:10px; display:flex; flex-wrap:wrap; gap:14px; align-items:center; }
        .nav a { color:#c7d2fe; text-decoration:none; font-weight:600; }
        .container { padding:20px; max-width:1280px; margin:auto; }
        .grid { display:grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap:16px; margin-bottom:20px; }
        .card { background:white; border-radius:16px; padding:18px; box-shadow:0 4px 18px rgba(0,0,0,0.08); margin-bottom:20px; }
        .metric .label { color:#6b7280; font-size:13px; margin-bottom:8px; }
        .metric .value { font-size:28px; font-weight:700; color:#111827; }
        table { width:100%; border-collapse: collapse; }
        th, td { border-bottom:1px solid #e5e7eb; padding:11px 10px; text-align:left; font-size:14px; vertical-align:middle; }
        th { background:#f9fafb; }
        .btn { display:inline-block; text-decoration:none; border:none; background:#2563eb; color:white; padding:8px 12px; border-radius:10px; cursor:pointer; font-size:13px; }
        .btn-secondary { background:#6b7280; }
        .btn-danger { background:#dc2626; }
        .btn-green { background:#059669; }
        input, select { width:100%; box-sizing:border-box; padding:10px; margin-bottom:10px; border:1px solid #d1d5db; border-radius:10px; }
        .row { display:grid; grid-template-columns:repeat(auto-fit, minmax(180px, 1fr)); gap:10px; align-items:end; }
        .tiny { color:#6b7280; font-size:12px; }
        .flash { background:#eef2ff; padding:12px; border-radius:10px; margin-bottom:14px; }
        .badge { display:inline-block; padding:4px 8px; border-radius:999px; font-size:12px; font-weight:700; }
        .present { background:#dcfce7; color:#166534; }
        .late { background:#ffedd5; color:#9a3412; }
        .absent { background:#fee2e2; color:#991b1b; }
        .host { background:#dbeafe; color:#1d4ed8; }
        .muted { color:#6b7280; }
        canvas { width:100% !important; height:320px !important; }
        .login-box { max-width:420px; margin:80px auto; background:white; border-radius:16px; padding:24px; box-shadow:0 4px 18px rgba(0,0,0,0.08);}
    </style>
</head>
<body>
    {% if show_nav %}
    <div class="top">
        <h1>Zoom Attendance Platform</h1>
        <div class="nav">
            <a href="{{ url_for('dashboard_home') }}">Home</a>
            <a href="{{ url_for('dashboard_live') }}">Live</a>
            <a href="{{ url_for('dashboard_members') }}">Members</a>
            <a href="{{ url_for('dashboard_users') }}">Users</a>
            <a href="{{ url_for('dashboard_analytics') }}">Analytics</a>
            <a href="{{ url_for('dashboard_meetings') }}">Recent Meetings</a>
            <a href="{{ url_for('settings_page') }}">Settings</a>
            {% if user.is_logged_in %}
            <span class="muted">Logged in as {{ user.username }} ({{ user.role }})</span>
            <a href="{{ url_for('logout_page') }}">Logout</a>
            {% endif %}
        </div>
    </div>
    {% endif %}
    <div class="container">
        {% with messages = get_flashed_messages(with_categories=true) %}
            {% if messages %}
                {% for category, message in messages %}
                    <div class="flash">{{ message|safe }}</div>
                {% endfor %}
            {% endif %}
        {% endwith %}
        {{ content|safe }}
    </div>
</body>
</html>
"""


def render_page(title, content, auto_refresh=None, show_nav=True):
    return render_template_string(
        BASE_HTML,
        title=title,
        content=content,
        auto_refresh=auto_refresh,
        show_nav=show_nav,
        user=current_user(),
    )


def status_badge(status):
    s = (status or "").upper()
    if s == "PRESENT":
        return '<span class="badge present">PRESENT</span>'
    if s == "LATE":
        return '<span class="badge late">LATE</span>'
    if s == "ABSENT":
        return '<span class="badge absent">ABSENT</span>'
    if s == "HOST":
        return '<span class="badge host">HOST</span>'
    if s == "LIVE":
        return '<span class="badge present">LIVE</span>'
    if s == "LEFT":
        return '<span class="badge late">LEFT</span>'
    return f'<span class="badge host">{status}</span>'


@app.route("/")
def root():
    if session.get("username"):
        return redirect(url_for("dashboard_home"))
    return redirect(url_for("login_page"))


@app.route("/login", methods=["GET", "POST"])
def login_page():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()
        user = authenticate_user(username, password)
        if user:
            session["username"] = user["username"]
            session["role"] = user["role"]
            flash(f"✅ Logged in as {user['role']}.", "ok")
            return redirect(url_for("dashboard_home"))
        flash("❌ Invalid username or password.", "bad")
        return redirect(url_for("login_page"))

    content = """
    <div class="login-box">
        <h2>Login</h2>
        <form method="post">
            <input name="username" placeholder="Username" required>
            <input name="password" type="password" placeholder="Password" required>
            <button class="btn" type="submit">Login</button>
        </form>
    </div>
    """
    return render_page("Login", content, show_nav=False)


@app.route("/logout")
def logout_page():
    session.clear()
    flash("✅ Logged out.", "ok")
    return redirect(url_for("login_page"))


@app.route("/test-db")
@login_required
def test_db():
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT NOW() AS now" if is_pg() else "SELECT datetime('now') AS now")
        result = cur.fetchone()
        conn.close()
        return f"DB Connected Successfully: {result['now']}"
    except Exception as e:
        return f"DB Connection Error: {e}", 500


@app.route("/dashboard")
@login_required
def dashboard_home():
    a = get_analytics()
    meetings = get_recent_meetings(limit=5)

    rows = []
    for i, m in enumerate(meetings, start=1):
        rows.append(f"""
        <tr>
            <td>{i}</td>
            <td>{m['topic']}</td>
            <td>{m['meeting_date']}</td>
            <td>{m['start_time']}</td>
            <td>{m['end_time']}</td>
            <td>{round(float(m.get('total_minutes') or 0), 2)}</td>
            <td>{m.get('joined_count') or 0}</td>
            <td>{m.get('attendance_percentage') or 0}%</td>
            <td><a class="btn" href="{url_for('meeting_detail', meeting_id=m['id'])}">Open</a></td>
        </tr>
        """)

    content = f"""
    <div class="grid">
        <div class="card metric"><div class="label">Total Meetings</div><div class="value">{a['total_meetings']}</div></div>
        <div class="card metric"><div class="label">Total Members</div><div class="value">{a['total_members']}</div></div>
        <div class="card metric"><div class="label">Active Members</div><div class="value">{a['active_members']}</div></div>
        <div class="card metric"><div class="label">Present Count</div><div class="value">{a['present_count']}</div></div>
        <div class="card metric"><div class="label">Late Count</div><div class="value">{a['late_count']}</div></div>
        <div class="card metric"><div class="label">Unknown Participants</div><div class="value">{a['unknown_count']}</div></div>
    </div>

    <div class="card">
        <h2>Meeting Summary</h2>
        <table>
            <tr><th>#</th><th>Topic</th><th>Date</th><th>Start</th><th>End</th><th>Total Minutes</th><th>Joined</th><th>Attendance %</th><th>Action</th></tr>
            {''.join(rows) if rows else '<tr><td colspan="9">No meetings saved yet.</td></tr>'}
        </table>
    </div>
    """
    return render_page("Home", content)


@app.route("/dashboard/live")
@login_required
def dashboard_live():
    state = load_live_state()
    meeting = state["meeting"]
    participants = list(state["participants"].values())

    processed = []
    live_count = 0
    left_count = 0
    max_minutes = 0.0
    host_name = "-"

    for p in participants:
        total_seconds = int(p.get("total_seconds", 0))
        if p.get("current_join"):
            cj = parse_zoom_time(p["current_join"])
            if cj:
                total_seconds += max(0, int((now_utc() - cj).total_seconds()))
        minutes = round(total_seconds / 60.0, 2)

        processed.append({
            "name": p.get("name", ""),
            "status": p.get("status", ""),
            "minutes": minutes,
            "rejoins": p.get("rejoins", 0),
            "is_host": p.get("is_host", False),
        })

    processed.sort(key=lambda x: (-x["minutes"], x["name"].lower()))
    active_members = get_members(active_only=True)
    joined_now = {p["name"].strip().lower() for p in processed if p["status"] in ("LIVE", "LEFT")}
    not_joined_active = [m for m in active_members if m["name"].strip().lower() not in joined_now]

    rows = []
    for p in processed:
        max_minutes = max(max_minutes, p["minutes"])
        if p["status"] == "LIVE":
            live_count += 1
        else:
            left_count += 1
        if p["is_host"]:
            host_name = p["name"]

        rows.append(f"""
        <tr>
            <td>{p['name']}</td>
            <td>{status_badge(p['status'])}</td>
            <td>{p['minutes']}</td>
            <td>{p['rejoins']}</td>
            <td>{"Yes" if p['is_host'] else "No"}</td>
        </tr>
        """)

    not_joined_rows = []
    for idx, m in enumerate(not_joined_active, start=1):
        not_joined_rows.append(f"<tr><td>{idx}</td><td>{m['name']}</td><td>{m.get('email') or '-'}</td></tr>")

    content = f"""
    <div class="grid">
        <div class="card metric"><div class="label">Live Topic</div><div class="value">{meeting.get("topic","No live meeting")}</div></div>
        <div class="card metric"><div class="label">Meeting ID</div><div class="value">{meeting.get("zoom_meeting_id","-")}</div></div>
        <div class="card metric"><div class="label">Live Count</div><div class="value">{live_count}</div></div>
        <div class="card metric"><div class="label">Left Count</div><div class="value">{left_count}</div></div>
        <div class="card metric"><div class="label">Detected Host</div><div class="value">{host_name}</div></div>
        <div class="card metric"><div class="label">Top Duration So Far</div><div class="value">{max_minutes} min</div></div>
    </div>

    <div class="card">
        <h2>Live Participants</h2>
        <table>
            <tr><th>Name</th><th>Status</th><th>Duration (Min)</th><th>Rejoins</th><th>Host</th></tr>
            {''.join(rows) if rows else '<tr><td colspan="5">No participant data yet.</td></tr>'}
        </table>
    </div>

    <div class="card">
        <h2>Active Members Not Joined Yet</h2>
        <table>
            <tr><th>#</th><th>Name</th><th>Email</th></tr>
            {''.join(not_joined_rows) if not_joined_rows else '<tr><td colspan="3">All active members have joined or no active members found.</td></tr>'}
        </table>
    </div>
    """
    return render_page("Live Dashboard", content, auto_refresh=2)


@app.route("/dashboard/members")
@login_required
def dashboard_members():
    search = request.args.get("search", "").strip()
    members = get_members(active_only=False, search=search)

    member_rows = []
    for m in members:
        admin_actions = ""
        if current_user()["role"] == "admin":
            admin_actions = f"""
                <a class="btn btn-secondary" href="{url_for('member_toggle', member_id=m['id'])}">{"Deactivate" if m["active"] == 1 else "Activate"}</a>
                <a class="btn btn-danger" href="{url_for('member_delete', member_id=m['id'])}" onclick="return confirm('Delete this member?')">Delete</a>
            """
        member_rows.append(f"""
            <tr>
                <td>{m['id']}</td>
                <td>{m['name']}</td>
                <td>{m.get('email') or ''}</td>
                <td>{m.get('whatsapp') or ''}</td>
                <td>{status_badge('PRESENT') if m['active'] == 1 else status_badge('ABSENT')}</td>
                <td>{admin_actions or '<span class="tiny">Viewer mode</span>'}</td>
            </tr>
        """)

    content = f"""
    <div class="card">
        <h2>Add / Update Member</h2>
        <form method="post" action="/members/add">
            <div class="row">
                <input name="name" placeholder="Name" required>
                <input name="email" placeholder="Email">
                <input name="whatsapp" placeholder="WhatsApp">
                <button class="btn" type="submit">Save Member</button>
            </div>
        </form>
    </div>

    <div class="card">
        <h2>Import Members CSV</h2>
        <form method="post" action="/members/import" enctype="multipart/form-data">
            <input type="file" name="file" accept=".csv" required>
            <button class="btn btn-green" type="submit">Import CSV</button>
        </form>
    </div>

    <div class="card">
        <h2>Members</h2>
        <form method="get">
            <div class="row">
                <input name="search" placeholder="Search member by name" value="{search}">
                <button class="btn" type="submit">Search</button>
            </div>
        </form>
        <table>
            <tr><th>ID</th><th>Name</th><th>Email</th><th>WhatsApp</th><th>Status</th><th>Actions</th></tr>
            {''.join(member_rows) if member_rows else '<tr><td colspan="6">No members added yet.</td></tr>'}
        </table>
    </div>
    """
    return render_page("Members Dashboard", content)


@app.route("/members/add", methods=["POST"])
@admin_required
def member_add():
    try:
        add_or_update_member(request.form.get("name", ""), request.form.get("email", ""), request.form.get("whatsapp", ""))
        flash("✅ Member added/updated successfully.", "ok")
    except Exception as e:
        flash(f"❌ {e}", "bad")
    return redirect(url_for("dashboard_members"))


@app.route("/members/import", methods=["POST"])
@admin_required
def members_import():
    file = request.files.get("file")
    if not file or not file.filename.lower().endswith(".csv"):
        flash("❌ Please upload a valid CSV file.", "bad")
        return redirect(url_for("dashboard_members"))
    try:
        count = import_members_from_csv(file)
        flash(f"✅ Imported/updated {count} members successfully.", "ok")
    except Exception as e:
        flash(f"❌ CSV import failed: {e}", "bad")
    return redirect(url_for("dashboard_members"))


@app.route("/members/toggle/<int:member_id>")
@admin_required
def member_toggle(member_id):
    toggle_member(member_id)
    flash("✅ Member status updated.", "ok")
    return redirect(url_for("dashboard_members"))


@app.route("/members/delete/<int:member_id>")
@admin_required
def member_delete(member_id):
    delete_member(member_id)
    flash("✅ Member deleted.", "ok")
    return redirect(url_for("dashboard_members"))


@app.route("/dashboard/users")
@admin_required
def dashboard_users():
    search = request.args.get("search", "").strip()
    users = get_users(search=search)

    rows = []
    for u in users:
        disable_me = session.get("username") == u["username"]
        actions = "<span class='tiny'>Current session</span>" if disable_me else f"""
            <a class="btn btn-secondary" href="{url_for('user_toggle', user_id=u['id'])}">{'Deactivate' if u['active'] == 1 else 'Activate'}</a>
            <a class="btn btn-danger" href="{url_for('user_delete', user_id=u['id'])}" onclick="return confirm('Delete this user?')">Delete</a>
        """
        rows.append(f"""
            <tr>
                <td>{u['id']}</td>
                <td>{u['username']}</td>
                <td>{u['role']}</td>
                <td>{status_badge('PRESENT') if u['active'] == 1 else status_badge('ABSENT')}</td>
                <td>{actions}</td>
            </tr>
        """)

    content = f"""
    <div class="card">
        <h2>Add / Update User</h2>
        <form method="post" action="{url_for('user_add')}">
            <div class="row">
                <input name="username" placeholder="Username" required>
                <input name="password" placeholder="Password" required>
                <select name="role">
                    <option value="admin">admin</option>
                    <option value="viewer">viewer</option>
                </select>
                <button class="btn" type="submit">Save User</button>
            </div>
        </form>
    </div>

    <div class="card">
        <h2>Users</h2>
        <form method="get">
            <div class="row">
                <input name="search" placeholder="Search by username" value="{search}">
                <button class="btn" type="submit">Search</button>
            </div>
        </form>
        <table>
            <tr><th>ID</th><th>Username</th><th>Role</th><th>Status</th><th>Actions</th></tr>
            {''.join(rows) if rows else '<tr><td colspan="5">No users found.</td></tr>'}
        </table>
    </div>
    """
    return render_page("Users Dashboard", content)


@app.route("/users/add", methods=["POST"])
@admin_required
def user_add():
    try:
        create_or_update_user(request.form.get("username", ""), request.form.get("password", ""), request.form.get("role", "viewer"))
        flash("✅ User saved successfully.", "ok")
    except Exception as e:
        flash(f"❌ {e}", "bad")
    return redirect(url_for("dashboard_users"))


@app.route("/users/toggle/<int:user_id>")
@admin_required
def user_toggle(user_id):
    toggle_user_active(user_id)
    flash("✅ User status updated.", "ok")
    return redirect(url_for("dashboard_users"))


@app.route("/users/delete/<int:user_id>")
@admin_required
def user_delete(user_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(qmark("SELECT username FROM users WHERE id = ?"), (user_id,))
    row = cur.fetchone()
    conn.close()
    username = dict(row)["username"] if row and not isinstance(row, dict) else (row["username"] if row else "")
    if username == session.get("username"):
        flash("❌ You cannot delete the currently logged in user.", "bad")
        return redirect(url_for("dashboard_users"))
    delete_user_account(user_id)
    flash("✅ User deleted successfully.", "ok")
    return redirect(url_for("dashboard_users"))


@app.route("/dashboard/analytics")
@login_required
def dashboard_analytics():
    a = get_analytics()

    chart_labels = json.dumps([row["participant_name"] for row in a["member_stats"][:10]])
    chart_values = json.dumps([float(row["attendance_percentage"] or 0) for row in a["member_stats"][:10]])
    meeting_labels = json.dumps([f"{row['topic']} ({row['meeting_date']})" for row in a["recent_stats"]])
    meeting_values = json.dumps([float(row["attendance_percentage"] or 0) for row in a["recent_stats"]])
    summary_labels = json.dumps(["Present", "Late", "Absent", "Unknown"])
    summary_values = json.dumps([a["present_count"], a["late_count"], a["absent_count"], a["unknown_count"]])

    content = f"""
    <div class="grid">
        <div class="card metric"><div class="label">Total Meetings</div><div class="value">{a['total_meetings']}</div></div>
        <div class="card metric"><div class="label">Total Members</div><div class="value">{a['total_members']}</div></div>
        <div class="card metric"><div class="label">Active Members</div><div class="value">{a['active_members']}</div></div>
        <div class="card metric"><div class="label">Attendance Rate</div><div class="value">{a['attendance_rate']}%</div></div>
    </div>

    <div class="grid">
        <div class="card"><h2>Attendance Summary</h2><canvas id="summaryPie"></canvas></div>
        <div class="card"><h2>Member Attendance %</h2><canvas id="memberBar"></canvas></div>
    </div>

    <div class="card">
        <h2>Meeting-wise Attendance %</h2>
        <canvas id="meetingBar"></canvas>
    </div>

    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <script>
    new Chart(document.getElementById('summaryPie'), {{
        type: 'pie',
        data: {{
            labels: {summary_labels},
            datasets: [{{ data: {summary_values}, backgroundColor: ['#16a34a', '#ea580c', '#dc2626', '#2563eb'] }}]
        }},
        options: {{ responsive: true, maintainAspectRatio: false }}
    }});

    new Chart(document.getElementById('memberBar'), {{
        type: 'bar',
        data: {{
            labels: {chart_labels},
            datasets: [{{ label: 'Attendance %', data: {chart_values}, backgroundColor: '#2563eb' }}]
        }},
        options: {{ scales: {{ y: {{ beginAtZero: true, max: 100 }} }}, responsive: true, maintainAspectRatio: false }}
    }});

    new Chart(document.getElementById('meetingBar'), {{
        type: 'bar',
        data: {{
            labels: {meeting_labels},
            datasets: [{{ label: 'Meeting Attendance %', data: {meeting_values}, backgroundColor: '#059669' }}]
        }},
        options: {{ scales: {{ y: {{ beginAtZero: true, max: 100 }} }}, responsive: true, maintainAspectRatio: false }}
    }});
    </script>
    """
    return render_page("Analytics Dashboard", content)


@app.route("/dashboard/analytics/pdf")
@login_required
def analytics_pdf():
    pdf_bytes = generate_analytics_pdf_bytes(get_analytics())
    return Response(pdf_bytes, mimetype="application/pdf", headers={"Content-Disposition": "inline; filename=analytics_summary.pdf"})


@app.route("/dashboard/meetings")
@login_required
def dashboard_meetings():
    date_from = request.args.get("date_from", "").strip()
    date_to = request.args.get("date_to", "").strip()
    topic_search = request.args.get("topic", "").strip()

    meetings = get_recent_meetings(limit=200, date_from=date_from, date_to=date_to, topic_search=topic_search)
    rows = []

    for idx, m in enumerate(meetings, start=1):
        csv_btn = f'<a class="btn" href="{url_for("download_report_by_meeting", meeting_id=m["id"], file_type="csv")}">CSV</a>' if (m.get("csv_file") or m.get("csv_content")) else ""
        pdf_btn = f'<a class="btn btn-secondary" href="{url_for("download_report_by_meeting", meeting_id=m["id"], file_type="pdf")}">PDF</a>' if (m.get("pdf_file") or m.get("pdf_content_b64")) else ""
        open_btn = f'<a class="btn" href="{url_for("meeting_detail", meeting_id=m["id"])}">Open</a>'
        delete_btn = f'<a class="btn btn-danger" href="{url_for("meeting_delete", meeting_id=m["id"])}" onclick="return confirm(\'Delete this meeting?\')">Delete</a>' if current_user()["role"] == "admin" else ""

        rows.append(f"""
            <tr>
                <td>{idx}</td>
                <td>{m['topic']}</td>
                <td>{m['meeting_date']}</td>
                <td>{m['start_time']}</td>
                <td>{m['end_time']}</td>
                <td>{round(float(m['total_minutes'] or 0), 2)}</td>
                <td>{m.get('joined_count') or 0}</td>
                <td>{m.get('attendance_percentage') or 0}%</td>
                <td>{csv_btn} {pdf_btn}</td>
                <td>{open_btn} {delete_btn}</td>
            </tr>
        """)

    content = f"""
    <div class="card">
        <h2>Recent Meetings</h2>
        <form method="get">
            <div class="row">
                <input type="date" name="date_from" value="{date_from}">
                <input type="date" name="date_to" value="{date_to}">
                <input name="topic" placeholder="Search by topic" value="{topic_search}">
                <button class="btn" type="submit">Apply Filters</button>
            </div>
        </form>
        <table>
            <tr><th>#</th><th>Topic</th><th>Date</th><th>Start</th><th>End</th><th>Total Minutes</th><th>Joined</th><th>Attendance %</th><th>Reports</th><th>Action</th></tr>
            {''.join(rows) if rows else '<tr><td colspan="10">No meetings saved yet.</td></tr>'}
        </table>
    </div>
    """
    return render_page("Meetings Dashboard", content)


@app.route("/meeting/<int:meeting_id>")
@login_required
def meeting_detail(meeting_id):
    meeting = get_meeting(meeting_id)
    if not meeting:
        return "Meeting not found", 404

    rows = get_attendance_rows(meeting_id=meeting_id)
    joined_only_count = len({r["participant_name"].strip().lower() for r in rows if (r.get("join_time") or "-") != "-"})

    tr = []
    for r in rows:
        tr.append(f"""
            <tr>
                <td>{r['participant_name']}</td>
                <td>{r['join_time'] or '-'}</td>
                <td>{r['leave_time'] or '-'}</td>
                <td>{r['duration_minutes']}</td>
                <td>{r['rejoins']}</td>
                <td>{status_badge(r['status'])}</td>
                <td>{"Yes" if r['is_member'] else "No"}</td>
                <td>{"Yes" if r['is_host'] else "No"}</td>
            </tr>
        """)

    content = f"""
    <div class="card">
        <h2>Meeting Detail</h2>
        <p><b>Topic:</b> {meeting['topic']}</p>
        <p><b>Meeting ID:</b> {meeting['zoom_meeting_id']}</p>
        <p><b>Date:</b> {meeting['meeting_date']}</p>
        <p><b>Total Participants (Joined Only):</b> {joined_only_count}</p>
        <a class="btn" href="{url_for('download_report_by_meeting', meeting_id=meeting_id, file_type='pdf')}">PDF</a>
        <a class="btn btn-secondary" href="{url_for('download_report_by_meeting', meeting_id=meeting_id, file_type='csv')}">CSV</a>
    </div>

    <div class="card">
        <table>
            <tr><th>Name</th><th>Join Time</th><th>Leave Time</th><th>Minutes</th><th>Rejoins</th><th>Status</th><th>Member</th><th>Host</th></tr>
            {''.join(tr) if tr else '<tr><td colspan="8">No attendance rows.</td></tr>'}
        </table>
    </div>
    """
    return render_page("Meeting Detail", content)


@app.route("/meeting/<int:meeting_id>/delete")
@admin_required
def meeting_delete(meeting_id):
    delete_meeting(meeting_id)
    flash("✅ Meeting deleted successfully.", "ok")
    return redirect(url_for("dashboard_meetings"))


@app.route("/download/<string:file_type>/<int:meeting_id>")
@login_required
def download_report_by_meeting(file_type, meeting_id):
    meeting = get_meeting(meeting_id)
    if not meeting:
        return "Meeting not found", 404

    filename = meeting.get("csv_file") if file_type == "csv" else meeting.get("pdf_file")
    local_path = os.path.join(REPORT_DIR, filename) if filename else ""

    if local_path and os.path.exists(local_path):
        return send_from_directory(REPORT_DIR, filename, as_attachment=False if file_type == "pdf" else True)

    db_file = load_report_from_db(meeting_id, file_type)
    if not db_file or not db_file["content"]:
        return "Saved report not found for this meeting.", 404

    return Response(
        db_file["content"],
        mimetype=db_file["mimetype"],
        headers={"Content-Disposition": f'{"inline" if file_type=="pdf" else "attachment"}; filename={db_file["filename"]}'},
    )


@app.route("/settings", methods=["GET", "POST"])
@admin_required
def settings_page():
    if request.method == "POST":
        try:
            set_setting("present_percentage", request.form.get("present_percentage", PRESENT_PERCENTAGE))
            set_setting("late_count_as_present_percentage", request.form.get("late_count_as_present_percentage", LATE_COUNT_AS_PRESENT_PERCENTAGE))
            set_setting("late_threshold_minutes", request.form.get("late_threshold_minutes", LATE_THRESHOLD_MINUTES))
            set_setting("host_name_hint", request.form.get("host_name_hint", HOST_NAME_HINT))
            set_setting("inactivity_confirm_seconds", request.form.get("inactivity_confirm_seconds", INACTIVITY_CONFIRM_SECONDS))
            flash("✅ Settings updated successfully.", "ok")
        except Exception as e:
            flash(f"❌ Failed to update settings: {e}", "bad")
        return redirect(url_for("settings_page"))

    content = f"""
    <div class="card">
        <h2>Attendance Settings</h2>
        <form method="post">
            <div class="row">
                <input type="number" name="present_percentage" value="{current_present_percentage()}" min="1" max="100">
                <input type="number" name="late_count_as_present_percentage" value="{current_late_count_as_present_percentage()}" min="0" max="100">
                <input type="number" name="late_threshold_minutes" value="{current_late_threshold_minutes()}" min="0">
                <input name="host_name_hint" value="{current_host_name_hint()}">
                <input type="number" name="inactivity_confirm_seconds" value="{current_inactivity_confirm_seconds()}" min="30">
                <button class="btn" type="submit">Save Settings</button>
            </div>
        </form>
    </div>
    """
    return render_page("Settings", content)


@app.route("/zoom/webhook", methods=["POST"])
def zoom_webhook():
    if not verify_zoom_signature():
        return jsonify({"message": "Invalid Zoom signature"}), 401

    payload = request.get_json(silent=True) or {}
    event = payload.get("event", "")

    if event == "endpoint.url_validation":
        return jsonify(zoom_url_validation(payload))

    obj = payload.get("payload", {}).get("object", {})
    participant = obj.get("participant", {})

    zoom_meeting_id = obj.get("id") or obj.get("uuid") or ""
    topic = obj.get("topic", "Untitled Meeting")
    event_time = now_utc()

    if event == "meeting.participant_joined":
        ensure_live_meeting(zoom_meeting_id, topic, event_time)
        pname = participant.get("user_name") or participant.get("participant_user_name") or "Unknown"
        pemail = participant.get("email", "")
        update_participant_join(pname, pemail, event_time)
        return jsonify({"message": "join processed"})

    if event == "meeting.participant_left":
        ensure_live_meeting(zoom_meeting_id, topic, event_time)
        pname = participant.get("user_name") or participant.get("participant_user_name") or "Unknown"
        update_participant_leave(pname, event_time)
        return jsonify({"message": "leave processed"})

    if event == "meeting.ended":
        state = ensure_live_meeting(zoom_meeting_id, topic, event_time)
        state["meeting"]["ended_at"] = event_time.isoformat()
        save_live_state(state)
        schedule_finalize(str(zoom_meeting_id))
        return jsonify({"message": "meeting end received, finalization scheduled"})

    return jsonify({"message": "event ignored", "event": event})


def safe_startup():
    try:
        init_db()
        print("✅ Database initialized successfully")
    except Exception as e:
        print(f"❌ Startup DB init failed: {e}")


safe_startup()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port, debug=False)
    