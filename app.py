import os
import csv
import io
import json
import hmac
import base64
import hashlib
import threading
from datetime import datetime, timezone, timedelta, date
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

from werkzeug.security import generate_password_hash, check_password_hash

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.enums import TA_LEFT, TA_CENTER


# =========================================================
# CONFIG
# =========================================================
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


# =========================================================
# DB HELPERS
# =========================================================
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


def qmark(sql: str) -> str:
    if is_pg():
        return sql.replace("?", "%s")
    return sql


def now_utc():
    return datetime.now(timezone.utc)


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


def fmt_date(dt):
    dt = to_local(dt)
    return dt.strftime("%Y-%m-%d") if dt else ""


def fmt_time(dt):
    dt = to_local(dt)
    return dt.strftime("%I:%M:%S %p") if dt else ""


def fmt_dt(dt):
    dt = to_local(dt)
    return dt.strftime("%Y-%m-%d %I:%M:%S %p") if dt else ""


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


# =========================================================
# USERS / SECURITY
# =========================================================
def hash_password(password: str) -> str:
    return generate_password_hash(password)


def verify_password(stored_hash_or_plain: str, password: str) -> bool:
    if not stored_hash_or_plain:
        return False
    if stored_hash_or_plain.startswith("pbkdf2:") or stored_hash_or_plain.startswith("scrypt:"):
        return check_password_hash(stored_hash_or_plain, password)
    # legacy plaintext fallback
    return stored_hash_or_plain == password


def maybe_upgrade_password(username: str, stored_hash_or_plain: str, password: str):
    if stored_hash_or_plain and not (
        stored_hash_or_plain.startswith("pbkdf2:") or stored_hash_or_plain.startswith("scrypt:")
    ):
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(qmark("UPDATE users SET password = ? WHERE username = ?"), (hash_password(password), username))
        conn.commit()
        conn.close()


def log_activity(action, details=""):
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(qmark("""
            INSERT INTO activity_log (username, action, details, created_at)
            VALUES (?, ?, ?, ?)
        """), (
            session.get("username", "system"),
            action,
            details,
            now_utc().isoformat(),
        ))
        conn.commit()
        conn.close()
    except Exception:
        pass


def seed_default_users(conn):
    cur = conn.cursor()

    defaults = [
        (ADMIN_USERNAME, ADMIN_PASSWORD, "admin"),
        (VIEWER_USERNAME, VIEWER_PASSWORD, "viewer"),
    ]

    for username, password, role in defaults:
        if not username or not password:
            continue

        cur.execute(qmark("SELECT id, password FROM users WHERE username = ?"), (username,))
        row = cur.fetchone()

        if not row:
            cur.execute(
                qmark("INSERT INTO users (username, password, role, active) VALUES (?, ?, ?, 1)"),
                (username, hash_password(password), role),
            )
        else:
            row = dict(row)
            existing_password = row.get("password") or ""
            if not existing_password.startswith("pbkdf2:") and not existing_password.startswith("scrypt:"):
                cur.execute(
                    qmark("UPDATE users SET password = ?, role = ?, active = 1 WHERE username = ?"),
                    (hash_password(password), role, username),
                )

    conn.commit()


def authenticate_user(username, password):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(qmark("SELECT * FROM users WHERE username = ? AND active = 1"), (username,))
    row = cur.fetchone()
    conn.close()

    if not row:
        return None

    user = dict(row)
    if verify_password(user.get("password", ""), password):
        maybe_upgrade_password(username, user.get("password", ""), password)
        return user
    return None


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
    hashed = hash_password(password)

    if is_pg():
        cur.execute("""
            INSERT INTO users (username, password, role, active)
            VALUES (%s, %s, %s, 1)
            ON CONFLICT (username) DO UPDATE SET
                password = EXCLUDED.password,
                role = EXCLUDED.role
        """, (username, hashed, role))
    else:
        cur.execute("""
            INSERT INTO users (username, password, role, active)
            VALUES (?, ?, ?, 1)
            ON CONFLICT(username) DO UPDATE SET
                password = excluded.password,
                role = excluded.role
        """, (username, hashed, role))

    conn.commit()
    conn.close()


def change_current_user_password(username, old_password, new_password):
    user = authenticate_user(username, old_password)
    if not user:
        raise ValueError("Old password is incorrect.")

    conn = get_conn()
    cur = conn.cursor()
    cur.execute(qmark("UPDATE users SET password = ? WHERE username = ?"), (hash_password(new_password), username))
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


# =========================================================
# DB INIT
# =========================================================
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
                csv_content TEXT,
                pdf_content_b64 TEXT,
                joined_count INTEGER DEFAULT 0,
                present_member_count INTEGER DEFAULT 0,
                late_member_count INTEGER DEFAULT 0,
                absent_member_count INTEGER DEFAULT 0,
                unknown_count INTEGER DEFAULT 0,
                attendance_percentage DOUBLE PRECISION DEFAULT 0,
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
        cur.execute("""
            CREATE TABLE IF NOT EXISTS activity_log (
                id SERIAL PRIMARY KEY,
                username TEXT,
                action TEXT,
                details TEXT,
                created_at TEXT
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
                csv_content TEXT,
                pdf_content_b64 TEXT,
                joined_count INTEGER DEFAULT 0,
                present_member_count INTEGER DEFAULT 0,
                late_member_count INTEGER DEFAULT 0,
                absent_member_count INTEGER DEFAULT 0,
                unknown_count INTEGER DEFAULT 0,
                attendance_percentage REAL DEFAULT 0,
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
        cur.execute("""
            CREATE TABLE IF NOT EXISTS activity_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT,
                action TEXT,
                details TEXT,
                created_at TEXT
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
    add_column_if_missing(conn, "users", "password", "TEXT")
    add_column_if_missing(conn, "users", "role", "TEXT DEFAULT 'viewer'")
    add_column_if_missing(conn, "users", "active", "INTEGER DEFAULT 1")
    add_column_if_missing(conn, "users", "created_at", "TIMESTAMP DEFAULT CURRENT_TIMESTAMP" if is_pg() else "TEXT DEFAULT CURRENT_TIMESTAMP")

    conn.commit()
    seed_default_users(conn)
    conn.close()


# =========================================================
# AUTH / SESSION
# =========================================================
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


# =========================================================
# SETTINGS
# =========================================================
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


# =========================================================
# UI
# =========================================================
BASE_HTML = """
<!doctype html>
<html>
<head>
    <title>{{ title }}</title>
    {% if auto_refresh %}
    <meta http-equiv="refresh" content="{{ auto_refresh }}">
    {% endif %}
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        body {
            font-family: Arial, sans-serif;
            margin:0;
            padding:0;
            color:#1f2937;
            background: linear-gradient(135deg, #eef2ff 0%, #f8fafc 45%, #ecfeff 100%);
        }
        .top {
            background:#0f172a;
            color:white;
            padding:18px 24px;
            box-shadow:0 4px 20px rgba(0,0,0,0.18);
            position:sticky;
            top:0;
            z-index:10;
        }
        .top h1 { margin:0; font-size:24px; }
        .nav {
            margin-top:10px;
            display:flex;
            flex-wrap:wrap;
            gap:14px;
            align-items:center;
        }
        .nav a {
            color:#c7d2fe;
            text-decoration:none;
            font-weight:600;
        }
        .container {
            padding:24px;
            max-width:1320px;
            margin:auto;
        }
        .grid {
            display:grid;
            grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
            gap:16px;
            margin-bottom:20px;
        }
        .card {
            background:rgba(255,255,255,0.95);
            border-radius:18px;
            padding:18px;
            box-shadow:0 10px 28px rgba(15,23,42,0.08);
            margin-bottom:20px;
            border:1px solid rgba(255,255,255,0.6);
        }
        .metric .label {
            color:#64748b;
            font-size:13px;
            margin-bottom:8px;
        }
        .metric .value {
            font-size:30px;
            font-weight:700;
            color:#0f172a;
        }
        table {
            width:100%;
            border-collapse: collapse;
            background:white;
            overflow:hidden;
            border-radius:12px;
        }
        th, td {
            border-bottom:1px solid #e5e7eb;
            padding:11px 10px;
            text-align:left;
            font-size:14px;
            vertical-align:middle;
        }
        th { background:#f8fafc; }
        tr:hover td { background:#fafcff; }
        .btn {
            display:inline-block;
            text-decoration:none;
            border:none;
            background:#2563eb;
            color:white;
            padding:8px 12px;
            border-radius:12px;
            cursor:pointer;
            font-size:13px;
            font-weight:600;
            box-shadow:0 6px 16px rgba(37,99,235,0.18);
        }
        .btn:hover { transform:translateY(-1px); }
        .btn-secondary { background:#64748b; box-shadow:0 6px 16px rgba(100,116,139,0.18); }
        .btn-danger { background:#dc2626; box-shadow:0 6px 16px rgba(220,38,38,0.18); }
        .btn-green { background:#059669; box-shadow:0 6px 16px rgba(5,150,105,0.18); }
        input, select {
            width:100%;
            box-sizing:border-box;
            padding:10px;
            margin-bottom:10px;
            border:1px solid #cbd5e1;
            border-radius:12px;
            background:white;
        }
        .row {
            display:grid;
            grid-template-columns:repeat(auto-fit, minmax(180px, 1fr));
            gap:10px;
            align-items:end;
        }
        .tiny { color:#64748b; font-size:12px; }
        .flash {
            padding:12px 14px;
            border-radius:12px;
            margin-bottom:14px;
            font-weight:600;
            background:#eef2ff;
        }
        .badge {
            display:inline-block;
            padding:4px 8px;
            border-radius:999px;
            font-size:12px;
            font-weight:700;
        }
        .present { background:#dcfce7; color:#166534; }
        .late { background:#ffedd5; color:#9a3412; }
        .absent { background:#fee2e2; color:#991b1b; }
        .host { background:#dbeafe; color:#1d4ed8; }
        .muted { color:#64748b; }
        canvas { width:100% !important; height:320px !important; }
        .login-box {
            max-width:420px;
            margin:90px auto;
            background:white;
            border-radius:18px;
            padding:26px;
            box-shadow:0 10px 30px rgba(15,23,42,0.12);
        }
        .section-title {
            margin:0 0 12px 0;
            color:#0f172a;
            font-size:20px;
            font-weight:700;
        }
        .subtle {
            background:linear-gradient(135deg, #eff6ff, #f8fafc);
        }
    </style>
</head>
<body>
    {% if show_nav %}
    <div class="top">
        <h1>📊 Zoom Attendance Platform</h1>
        <div class="nav">
            <a href="{{ url_for('dashboard_home') }}">🏠 Home</a>
            <a href="{{ url_for('dashboard_live') }}">🟢 Live</a>
            <a href="{{ url_for('dashboard_members') }}">👥 Members</a>
            <a href="{{ url_for('dashboard_users') }}">🔐 Users</a>
            <a href="{{ url_for('dashboard_analytics') }}">📈 Analytics</a>
            <a href="{{ url_for('dashboard_meetings') }}">🗂 Meetings</a>
            <a href="{{ url_for('settings_page') }}">⚙ Settings</a>
            {% if user.is_logged_in %}
            <span class="muted">Logged in as {{ user.username }} ({{ user.role }})</span>
            <a href="{{ url_for('logout_page') }}">🚪 Logout</a>
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


# =========================================================
# ADVANCED FILTERED ANALYTICS ENGINE
# =========================================================
def get_all_attendance_joined():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT
            a.*,
            m.topic,
            m.meeting_date,
            m.start_time,
            m.end_time,
            m.total_minutes,
            m.zoom_meeting_id
        FROM attendance a
        JOIN meetings m ON a.meeting_pk = m.id
        ORDER BY m.id DESC, a.participant_name ASC
    """)
    rows = rows_to_dicts(cur.fetchall())
    conn.close()
    return rows


def get_period_range(period_mode, date_value="", month_value="", year_value="", start_date="", end_date=""):
    today = datetime.now(APP_TZ).date()

    if period_mode == "daily":
        if date_value:
            d = datetime.strptime(date_value, "%Y-%m-%d").date()
        else:
            d = today
        return d, d

    if period_mode == "weekly":
        if date_value:
            d = datetime.strptime(date_value, "%Y-%m-%d").date()
        else:
            d = today
        start = d - timedelta(days=d.weekday())
        end = start + timedelta(days=6)
        return start, end

    if period_mode == "monthly":
        if month_value:
            year_int, month_int = map(int, month_value.split("-"))
            start = date(year_int, month_int, 1)
        else:
            start = date(today.year, today.month, 1)
        if start.month == 12:
            next_month = date(start.year + 1, 1, 1)
        else:
            next_month = date(start.year, start.month + 1, 1)
        end = next_month - timedelta(days=1)
        return start, end

    if period_mode == "yearly":
        year_int = int(year_value) if year_value else today.year
        start = date(year_int, 1, 1)
        end = date(year_int, 12, 31)
        return start, end

    if period_mode == "custom":
        if start_date and end_date:
            s = datetime.strptime(start_date, "%Y-%m-%d").date()
            e = datetime.strptime(end_date, "%Y-%m-%d").date()
            return s, e
        return date(2000, 1, 1), date(2100, 12, 31)

    return date(2000, 1, 1), date(2100, 12, 31)


def filter_attendance_records(member="ALL", period_mode="custom", date_value="", month_value="", year_value="", start_date="", end_date=""):
    rows = get_all_attendance_joined()
    range_start, range_end = get_period_range(period_mode, date_value, month_value, year_value, start_date, end_date)

    filtered = []
    for r in rows:
        d = None
        try:
            d = datetime.strptime(r.get("meeting_date") or "", "%Y-%m-%d").date()
        except Exception:
            continue

        if not (range_start <= d <= range_end):
            continue

        if member != "ALL" and (r.get("participant_name") or "").strip().lower() != member.strip().lower():
            continue

        filtered.append(r)

    return filtered, range_start, range_end


def build_filtered_analytics(member="ALL", period_mode="custom", date_value="", month_value="", year_value="", start_date="", end_date=""):
    rows, range_start, range_end = filter_attendance_records(
        member=member,
        period_mode=period_mode,
        date_value=date_value,
        month_value=month_value,
        year_value=year_value,
        start_date=start_date,
        end_date=end_date,
    )

    total_rows = len(rows)
    present_count = sum(1 for r in rows if (r.get("status") or "").upper() == "PRESENT")
    late_count = sum(1 for r in rows if (r.get("status") or "").upper() == "LATE")
    absent_count = sum(1 for r in rows if (r.get("status") or "").upper() == "ABSENT")
    unknown_count = sum(1 for r in rows if int(r.get("is_unknown") or 0) == 1)
    host_count = sum(1 for r in rows if int(r.get("is_host") or 0) == 1)
    total_duration = round(sum(float(r.get("duration_minutes") or 0) for r in rows), 2)
    avg_duration = round(total_duration / total_rows, 2) if total_rows else 0
    total_rejoins = sum(int(r.get("rejoins") or 0) for r in rows)

    attendance_rate = round(
        ((present_count + late_count) / total_rows) * 100, 2
    ) if total_rows else 0

    meeting_keys = {(r.get("meeting_pk"), r.get("topic"), r.get("meeting_date")) for r in rows}
    total_meetings = len(meeting_keys)

    member_map = {}
    for r in rows:
        name = r.get("participant_name") or "Unknown"
        if name not in member_map:
            member_map[name] = {
                "participant_name": name,
                "present_count": 0,
                "late_count": 0,
                "absent_count": 0,
                "total_duration": 0.0,
                "total_rejoins": 0,
                "records": 0,
            }

        status = (r.get("status") or "").upper()
        if status == "PRESENT":
            member_map[name]["present_count"] += 1
        elif status == "LATE":
            member_map[name]["late_count"] += 1
        elif status == "ABSENT":
            member_map[name]["absent_count"] += 1

        member_map[name]["total_duration"] += float(r.get("duration_minutes") or 0)
        member_map[name]["total_rejoins"] += int(r.get("rejoins") or 0)
        member_map[name]["records"] += 1

    member_stats = []
    for _, data in member_map.items():
        records = data["records"]
        attendance_percentage = round(
            ((data["present_count"] + data["late_count"]) / records) * 100, 2
        ) if records else 0
        member_stats.append({
            "participant_name": data["participant_name"],
            "present_count": data["present_count"],
            "late_count": data["late_count"],
            "absent_count": data["absent_count"],
            "total_duration": round(data["total_duration"], 2),
            "avg_duration": round(data["total_duration"] / records, 2) if records else 0,
            "total_rejoins": data["total_rejoins"],
            "attendance_percentage": attendance_percentage,
        })

    member_stats.sort(key=lambda x: (-x["attendance_percentage"], -x["total_duration"], x["participant_name"].lower()))

    meeting_map = {}
    for r in rows:
        key = (r.get("meeting_pk"), r.get("topic"), r.get("meeting_date"))
        if key not in meeting_map:
            meeting_map[key] = {
                "id": r.get("meeting_pk"),
                "topic": r.get("topic"),
                "meeting_date": r.get("meeting_date"),
                "present_count": 0,
                "late_count": 0,
                "absent_count": 0,
                "unknown_count": 0,
                "joined_count": 0,
                "total_minutes": float(r.get("total_minutes") or 0),
            }

        status = (r.get("status") or "").upper()
        if status == "PRESENT":
            meeting_map[key]["present_count"] += 1
        elif status == "LATE":
            meeting_map[key]["late_count"] += 1
        elif status == "ABSENT":
            meeting_map[key]["absent_count"] += 1

        if int(r.get("is_unknown") or 0) == 1:
            meeting_map[key]["unknown_count"] += 1
        if (r.get("join_time") or "-") != "-":
            meeting_map[key]["joined_count"] += 1

    recent_stats = []
    for _, data in meeting_map.items():
        total = data["present_count"] + data["late_count"] + data["absent_count"]
        pct = round(((data["present_count"] + data["late_count"]) / total) * 100, 2) if total else 0
        data["attendance_percentage"] = pct
        recent_stats.append(data)

    recent_stats.sort(key=lambda x: (x["meeting_date"], x["topic"]), reverse=True)

    top_attendees = member_stats[:5]
    low_attendance = sorted(member_stats, key=lambda x: (x["attendance_percentage"], x["total_duration"]))[:5]

    return {
        "rows": rows,
        "range_start": str(range_start),
        "range_end": str(range_end),
        "member": member,
        "period_mode": period_mode,
        "total_rows": total_rows,
        "total_meetings": total_meetings,
        "present_count": present_count,
        "late_count": late_count,
        "absent_count": absent_count,
        "unknown_count": unknown_count,
        "host_count": host_count,
        "total_duration": total_duration,
        "avg_duration": avg_duration,
        "total_rejoins": total_rejoins,
        "attendance_rate": attendance_rate,
        "member_stats": member_stats,
        "recent_stats": recent_stats,
        "top_attendees": top_attendees,
        "low_attendance": low_attendance,
    }


def generate_filtered_analytics_csv_bytes(analytics):
    output = io.StringIO()
    writer = csv.writer(output)

    writer.writerow(["Filtered Analytics Report"])
    writer.writerow(["Member", analytics["member"]])
    writer.writerow(["Period", analytics["period_mode"]])
    writer.writerow(["Start", analytics["range_start"]])
    writer.writerow(["End", analytics["range_end"]])
    writer.writerow(["Total Meetings", analytics["total_meetings"]])
    writer.writerow(["Present", analytics["present_count"]])
    writer.writerow(["Late", analytics["late_count"]])
    writer.writerow(["Absent", analytics["absent_count"]])
    writer.writerow(["Unknown", analytics["unknown_count"]])
    writer.writerow(["Total Duration", analytics["total_duration"]])
    writer.writerow(["Average Duration", analytics["avg_duration"]])
    writer.writerow(["Attendance Rate", analytics["attendance_rate"]])
    writer.writerow([])

    writer.writerow(["Name", "Present", "Late", "Absent", "Total Duration", "Avg Duration", "Rejoins", "Attendance %"])
    for row in analytics["member_stats"]:
        writer.writerow([
            row["participant_name"],
            row["present_count"],
            row["late_count"],
            row["absent_count"],
            row["total_duration"],
            row["avg_duration"],
            row["total_rejoins"],
            row["attendance_percentage"],
        ])

    return output.getvalue().encode("utf-8")


def generate_filtered_analytics_pdf_bytes(analytics):
    pdf_path = os.path.join(DATA_DIR, "filtered_analytics_export.pdf")
    doc = SimpleDocTemplate(pdf_path, pagesize=landscape(A4), leftMargin=20, rightMargin=20, topMargin=20, bottomMargin=20)
    styles = getSampleStyleSheet()
    story = []

    story.append(Paragraph("Filtered Analytics Report", styles["Title"]))
    story.append(Spacer(1, 12))
    story.append(Paragraph(f"Member: {analytics['member']}", styles["Normal"]))
    story.append(Paragraph(f"Period: {analytics['period_mode']}", styles["Normal"]))
    story.append(Paragraph(f"Date Range: {analytics['range_start']} to {analytics['range_end']}", styles["Normal"]))
    story.append(Paragraph(f"Total Meetings: {analytics['total_meetings']}", styles["Normal"]))
    story.append(Paragraph(f"Present: {analytics['present_count']}", styles["Normal"]))
    story.append(Paragraph(f"Late: {analytics['late_count']}", styles["Normal"]))
    story.append(Paragraph(f"Absent: {analytics['absent_count']}", styles["Normal"]))
    story.append(Paragraph(f"Unknown: {analytics['unknown_count']}", styles["Normal"]))
    story.append(Paragraph(f"Attendance Rate: {analytics['attendance_rate']}%", styles["Normal"]))
    story.append(Spacer(1, 12))

    table_data = [["Name", "Present", "Late", "Absent", "Total Duration", "Avg Duration", "Rejoins", "Attendance %"]]
    for row in analytics["member_stats"]:
        table_data.append([
            row["participant_name"],
            row["present_count"],
            row["late_count"],
            row["absent_count"],
            row["total_duration"],
            row["avg_duration"],
            row["total_rejoins"],
            f"{row['attendance_percentage']}%",
        ])

    table = Table(table_data, repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.black),
        ("PADDING", (0, 0), (-1, -1), 5),
    ]))
    story.append(table)

    doc.build(story)

    with open(pdf_path, "rb") as f:
        return f.read()


# =========================================================
# ROUTES
# =========================================================
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
            log_activity("login", f"{username} logged in")
            flash(f"✅ Logged in as {user['role']}.", "ok")
            return redirect(url_for("dashboard_home"))
        flash("❌ Invalid username or password.", "bad")
        return redirect(url_for("login_page"))

    content = """
    <div class="login-box">
        <h2>🔐 Login</h2>
        <form method="post">
            <input name="username" placeholder="Username" required>
            <input name="password" type="password" placeholder="Password" required>
            <button class="btn" type="submit">Login</button>
        </form>
        <p class="tiny">Use your admin/viewer account to access the dashboard.</p>
    </div>
    """
    return render_page("Login", content, show_nav=False)


@app.route("/logout")
def logout_page():
    log_activity("logout", f"{session.get('username','')} logged out")
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

    latest_meeting = meetings[0] if meetings else None

    activity_rows = []
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT * FROM activity_log ORDER BY id DESC LIMIT 5")
        activities = rows_to_dicts(cur.fetchall())
        conn.close()
        for act in activities:
            activity_rows.append(f"<tr><td>{act.get('username') or 'system'}</td><td>{act.get('action') or ''}</td><td>{act.get('details') or ''}</td><td>{act.get('created_at') or ''}</td></tr>")
    except Exception:
        pass

    meeting_rows = []
    for i, m in enumerate(meetings, start=1):
        meeting_rows.append(f"""
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

    spotlight = ""
    if latest_meeting:
        spotlight = f"""
        <div class="card subtle">
            <h2 class="section-title">⭐ Latest Meeting Spotlight</h2>
            <div class="grid">
                <div class="card metric"><div class="label">Topic</div><div class="value" style="font-size:20px;">{latest_meeting['topic']}</div></div>
                <div class="card metric"><div class="label">Date</div><div class="value" style="font-size:20px;">{latest_meeting['meeting_date']}</div></div>
                <div class="card metric"><div class="label">Joined</div><div class="value">{latest_meeting.get('joined_count') or 0}</div></div>
                <div class="card metric"><div class="label">Attendance %</div><div class="value">{latest_meeting.get('attendance_percentage') or 0}%</div></div>
            </div>
            <a class="btn" href="{url_for('meeting_detail', meeting_id=latest_meeting['id'])}">Open Latest Meeting</a>
        </div>
        """

    content = f"""
    <div class="grid">
        <div class="card metric"><div class="label">Total Meetings</div><div class="value">{a['total_meetings']}</div></div>
        <div class="card metric"><div class="label">Total Members</div><div class="value">{a['total_members']}</div></div>
        <div class="card metric"><div class="label">Active Members</div><div class="value">{a['active_members']}</div></div>
        <div class="card metric"><div class="label">Present Count</div><div class="value">{a['present_count']}</div></div>
        <div class="card metric"><div class="label">Late Count</div><div class="value">{a['late_count']}</div></div>
        <div class="card metric"><div class="label">Unknown Participants</div><div class="value">{a['unknown_count']}</div></div>
    </div>

    {spotlight}

    <div class="card">
        <h2 class="section-title">🚀 Quick Actions</h2>
        <div style="display:flex;gap:10px;flex-wrap:wrap;">
            <a class="btn" href="{url_for('dashboard_live')}">Open Live Dashboard</a>
            <a class="btn btn-green" href="{url_for('dashboard_members')}">Manage Members</a>
            <a class="btn btn-secondary" href="{url_for('dashboard_analytics')}">View Analytics</a>
            <a class="btn" href="{url_for('analytics_pdf')}">Export Analytics PDF</a>
        </div>
    </div>

    <div class="card">
        <h2 class="section-title">🗂 Recent Meetings</h2>
        <table>
            <tr><th>#</th><th>Topic</th><th>Date</th><th>Start</th><th>End</th><th>Total Minutes</th><th>Joined</th><th>Attendance %</th><th>Action</th></tr>
            {''.join(meeting_rows) if meeting_rows else '<tr><td colspan="9">No meetings saved yet.</td></tr>'}
        </table>
    </div>

    <div class="card">
        <h2 class="section-title">🕒 Recent Activity</h2>
        <table>
            <tr><th>User</th><th>Action</th><th>Details</th><th>Time</th></tr>
            {''.join(activity_rows) if activity_rows else '<tr><td colspan="4">No recent activity.</td></tr>'}
        </table>
    </div>
    """
    return render_page("Home Dashboard", content)


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
    <div style="display:flex;gap:10px;flex-wrap:wrap;margin-bottom:14px;">
        <a class="btn" href="{url_for('dashboard_live')}">🔄 Refresh Live</a>
        <a class="btn btn-secondary" href="{url_for('dashboard_home')}">🏠 Home</a>
    </div>

    <div class="grid">
        <div class="card metric"><div class="label">Live Topic</div><div class="value" style="font-size:20px;">{meeting.get("topic","No live meeting")}</div></div>
        <div class="card metric"><div class="label">Meeting ID</div><div class="value" style="font-size:20px;">{meeting.get("zoom_meeting_id","-")}</div></div>
        <div class="card metric"><div class="label">Live Count</div><div class="value">{live_count}</div></div>
        <div class="card metric"><div class="label">Left Count</div><div class="value">{left_count}</div></div>
        <div class="card metric"><div class="label">Detected Host</div><div class="value" style="font-size:20px;">{host_name}</div></div>
        <div class="card metric"><div class="label">Top Duration So Far</div><div class="value">{max_minutes} min</div></div>
    </div>

    <div class="card">
        <h2 class="section-title">🟢 Live Participants</h2>
        <table>
            <tr><th>Name</th><th>Status</th><th>Duration (Min)</th><th>Rejoins</th><th>Host</th></tr>
            {''.join(rows) if rows else '<tr><td colspan="5">No participant data yet.</td></tr>'}
        </table>
    </div>

    <div class="card">
        <h2 class="section-title">⏳ Active Members Not Joined Yet</h2>
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
        <h2 class="section-title">➕ Add / Update Member</h2>
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
        <h2 class="section-title">📥 Import Members CSV</h2>
        <form method="post" action="/members/import" enctype="multipart/form-data">
            <input type="file" name="file" accept=".csv" required>
            <button class="btn btn-green" type="submit">Import CSV</button>
        </form>
    </div>

    <div class="card">
        <h2 class="section-title">👥 Members</h2>
        <form method="get">
            <div class="row">
                <input name="search" placeholder="Search member by name" value="{search}">
                <button class="btn" type="submit">Search</button>
                <a class="btn btn-secondary" href="{url_for('dashboard_members')}">Reset</a>
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
        log_activity("member_save", f"Member saved: {request.form.get('name','')}")
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
        log_activity("member_import", f"Imported {count} members")
        flash(f"✅ Imported/updated {count} members successfully.", "ok")
    except Exception as e:
        flash(f"❌ CSV import failed: {e}", "bad")
    return redirect(url_for("dashboard_members"))


@app.route("/members/toggle/<int:member_id>")
@admin_required
def member_toggle(member_id):
    toggle_member(member_id)
    log_activity("member_toggle", f"Toggled member id {member_id}")
    flash("✅ Member status updated.", "ok")
    return redirect(url_for("dashboard_members"))


@app.route("/members/delete/<int:member_id>")
@admin_required
def member_delete(member_id):
    delete_member(member_id)
    log_activity("member_delete", f"Deleted member id {member_id}")
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
        <h2 class="section-title">🔐 Add / Update User</h2>
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
        <h2 class="section-title">👤 Users</h2>
        <form method="get">
            <div class="row">
                <input name="search" placeholder="Search by username" value="{search}">
                <button class="btn" type="submit">Search</button>
                <a class="btn btn-secondary" href="{url_for('dashboard_users')}">Reset</a>
            </div>
        </form>
        <table>
            <tr><th>ID</th><th>Username</th><th>Role</th><th>Status</th><th>Actions</th></tr>
            {''.join(rows) if rows else '<tr><td colspan="5">No users found.</td></tr>'}
        </table>
    </div>

    <div class="card">
        <h2 class="section-title">🔑 Change My Password</h2>
        <form method="post" action="{url_for('change_my_password')}">
            <div class="row">
                <input name="old_password" type="password" placeholder="Old password" required>
                <input name="new_password" type="password" placeholder="New password" required>
                <button class="btn btn-green" type="submit">Update Password</button>
            </div>
        </form>
    </div>
    """
    return render_page("Users Dashboard", content)


@app.route("/users/add", methods=["POST"])
@admin_required
def user_add():
    try:
        create_or_update_user(request.form.get("username", ""), request.form.get("password", ""), request.form.get("role", "viewer"))
        log_activity("user_save", f"Saved user {request.form.get('username','')}")
        flash("✅ User saved successfully.", "ok")
    except Exception as e:
        flash(f"❌ {e}", "bad")
    return redirect(url_for("dashboard_users"))


@app.route("/users/toggle/<int:user_id>")
@admin_required
def user_toggle(user_id):
    toggle_user_active(user_id)
    log_activity("user_toggle", f"Toggled user id {user_id}")
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
    log_activity("user_delete", f"Deleted user {username}")
    flash("✅ User deleted successfully.", "ok")
    return redirect(url_for("dashboard_users"))


@app.route("/users/change-password", methods=["POST"])
@login_required
def change_my_password():
    try:
        change_current_user_password(
            session.get("username"),
            request.form.get("old_password", ""),
            request.form.get("new_password", ""),
        )
        log_activity("password_change", f"Changed password for {session.get('username')}")
        flash("✅ Password updated successfully.", "ok")
    except Exception as e:
        flash(f"❌ {e}", "bad")
    return redirect(url_for("dashboard_users"))


@app.route("/dashboard/analytics")
@login_required
def dashboard_analytics():
    member = request.args.get("member", "ALL")
    period_mode = request.args.get("period_mode", "monthly")
    date_value = request.args.get("date_value", "")
    month_value = request.args.get("month_value", "")
    year_value = request.args.get("year_value", "")
    start_date = request.args.get("start_date", "")
    end_date = request.args.get("end_date", "")

    analytics = build_filtered_analytics(
        member=member,
        period_mode=period_mode,
        date_value=date_value,
        month_value=month_value,
        year_value=year_value,
        start_date=start_date,
        end_date=end_date,
    )

    members = get_members(active_only=False)
    member_options = ['<option value="ALL">All Members</option>']
    for m in members:
        selected = "selected" if member == m["name"] else ""
        member_options.append(f'<option value="{m["name"]}" {selected}>{m["name"]}</option>')

    top_rows = []
    for row in analytics["top_attendees"]:
        top_rows.append(f"<tr><td>{row['participant_name']}</td><td>{row['attendance_percentage']}%</td><td>{row['total_duration']}</td></tr>")

    low_rows = []
    for row in analytics["low_attendance"]:
        low_rows.append(f"<tr><td>{row['participant_name']}</td><td>{row['attendance_percentage']}%</td><td>{row['absent_count']}</td></tr>")

    detail_rows = []
    for r in analytics["rows"][:200]:
        detail_rows.append(f"""
            <tr>
                <td>{r.get('meeting_date') or ''}</td>
                <td>{r.get('topic') or ''}</td>
                <td>{r.get('participant_name') or ''}</td>
                <td>{r.get('join_time') or '-'}</td>
                <td>{r.get('leave_time') or '-'}</td>
                <td>{r.get('duration_minutes') or 0}</td>
                <td>{r.get('rejoins') or 0}</td>
                <td>{status_badge(r.get('status') or '')}</td>
            </tr>
        """)

    chart_labels = json.dumps([row["participant_name"] for row in analytics["member_stats"][:10]])
    chart_values = json.dumps([float(row["attendance_percentage"] or 0) for row in analytics["member_stats"][:10]])
    meeting_labels = json.dumps([f"{row['topic']} ({row['meeting_date']})" for row in analytics["recent_stats"][:12]])
    meeting_values = json.dumps([float(row["attendance_percentage"] or 0) for row in analytics["recent_stats"][:12]])
    summary_labels = json.dumps(["Present", "Late", "Absent", "Unknown"])
    summary_values = json.dumps([
        analytics["present_count"],
        analytics["late_count"],
        analytics["absent_count"],
        analytics["unknown_count"],
    ])

    export_query = (
        f"member={member}&period_mode={period_mode}&date_value={date_value}"
        f"&month_value={month_value}&year_value={year_value}&start_date={start_date}&end_date={end_date}"
    )

    content = f"""
    <div class="card">
        <h2 class="section-title">🔎 Smart Analytics Filters</h2>
        <form method="get">
            <div class="row">
                <select name="member">{''.join(member_options)}</select>
                <select name="period_mode">
                    <option value="daily" {"selected" if period_mode=="daily" else ""}>Daily</option>
                    <option value="weekly" {"selected" if period_mode=="weekly" else ""}>Weekly</option>
                    <option value="monthly" {"selected" if period_mode=="monthly" else ""}>Monthly</option>
                    <option value="yearly" {"selected" if period_mode=="yearly" else ""}>Yearly</option>
                    <option value="custom" {"selected" if period_mode=="custom" else ""}>Custom Range</option>
                </select>
                <input type="date" name="date_value" value="{date_value}">
                <input type="month" name="month_value" value="{month_value}">
                <input type="number" name="year_value" placeholder="Year" value="{year_value}">
                <input type="date" name="start_date" value="{start_date}">
                <input type="date" name="end_date" value="{end_date}">
            </div>
            <div style="display:flex;gap:10px;flex-wrap:wrap;">
                <button class="btn" type="submit">Apply Filters</button>
                <a class="btn btn-secondary" href="{url_for('dashboard_analytics')}">Reset</a>
                <a class="btn btn-green" href="{url_for('analytics_pdf_filtered')}?{export_query}">Export PDF</a>
                <a class="btn" href="{url_for('analytics_csv_filtered')}?{export_query}">Export CSV</a>
            </div>
            <p class="tiny">Current range: {analytics['range_start']} to {analytics['range_end']}</p>
        </form>
    </div>

    <div class="grid">
        <div class="card metric"><div class="label">Selected Member</div><div class="value" style="font-size:20px;">{analytics['member']}</div></div>
        <div class="card metric"><div class="label">Total Meetings</div><div class="value">{analytics['total_meetings']}</div></div>
        <div class="card metric"><div class="label">Present</div><div class="value">{analytics['present_count']}</div></div>
        <div class="card metric"><div class="label">Late</div><div class="value">{analytics['late_count']}</div></div>
        <div class="card metric"><div class="label">Absent</div><div class="value">{analytics['absent_count']}</div></div>
        <div class="card metric"><div class="label">Unknown</div><div class="value">{analytics['unknown_count']}</div></div>
        <div class="card metric"><div class="label">Attendance Rate</div><div class="value">{analytics['attendance_rate']}%</div></div>
        <div class="card metric"><div class="label">Total Rejoins</div><div class="value">{analytics['total_rejoins']}</div></div>
    </div>

    <div class="grid">
        <div class="card"><h2 class="section-title">📊 Summary</h2><canvas id="summaryPie"></canvas></div>
        <div class="card"><h2 class="section-title">📈 Member Attendance %</h2><canvas id="memberBar"></canvas></div>
    </div>

    <div class="card">
        <h2 class="section-title">📉 Meeting-wise Attendance %</h2>
        <canvas id="meetingBar"></canvas>
    </div>

    <div class="grid">
        <div class="card">
            <h2 class="section-title">🏆 Top Performers</h2>
            <table>
                <tr><th>Name</th><th>Attendance %</th><th>Total Duration</th></tr>
                {''.join(top_rows) if top_rows else '<tr><td colspan="3">No data.</td></tr>'}
            </table>
        </div>
        <div class="card">
            <h2 class="section-title">⚠ Low Attendance Alerts</h2>
            <table>
                <tr><th>Name</th><th>Attendance %</th><th>Absent Count</th></tr>
                {''.join(low_rows) if low_rows else '<tr><td colspan="3">No data.</td></tr>'}
            </table>
        </div>
    </div>

    <div class="card">
        <h2 class="section-title">🧾 Detailed Filtered Report</h2>
        <table>
            <tr><th>Date</th><th>Topic</th><th>Name</th><th>Join</th><th>Leave</th><th>Duration</th><th>Rejoins</th><th>Status</th></tr>
            {''.join(detail_rows) if detail_rows else '<tr><td colspan="8">No records for selected filters.</td></tr>'}
        </table>
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
    return render_page("Advanced Analytics Dashboard", content)


@app.route("/dashboard/analytics/pdf")
@login_required
def analytics_pdf():
    pdf_bytes = generate_analytics_pdf_bytes(get_analytics())
    return Response(pdf_bytes, mimetype="application/pdf", headers={"Content-Disposition": "inline; filename=analytics_summary.pdf"})


@app.route("/dashboard/analytics/pdf-filtered")
@login_required
def analytics_pdf_filtered():
    analytics = build_filtered_analytics(
        member=request.args.get("member", "ALL"),
        period_mode=request.args.get("period_mode", "monthly"),
        date_value=request.args.get("date_value", ""),
        month_value=request.args.get("month_value", ""),
        year_value=request.args.get("year_value", ""),
        start_date=request.args.get("start_date", ""),
        end_date=request.args.get("end_date", ""),
    )
    pdf_bytes = generate_filtered_analytics_pdf_bytes(analytics)
    return Response(pdf_bytes, mimetype="application/pdf", headers={"Content-Disposition": "inline; filename=filtered_analytics.pdf"})


@app.route("/dashboard/analytics/csv-filtered")
@login_required
def analytics_csv_filtered():
    analytics = build_filtered_analytics(
        member=request.args.get("member", "ALL"),
        period_mode=request.args.get("period_mode", "monthly"),
        date_value=request.args.get("date_value", ""),
        month_value=request.args.get("month_value", ""),
        year_value=request.args.get("year_value", ""),
        start_date=request.args.get("start_date", ""),
        end_date=request.args.get("end_date", ""),
    )
    csv_bytes = generate_filtered_analytics_csv_bytes(analytics)
    return Response(csv_bytes, mimetype="text/csv", headers={"Content-Disposition": "attachment; filename=filtered_analytics.csv"})


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
        <h2 class="section-title">🗂 Recent Meetings</h2>
        <form method="get">
            <div class="row">
                <input type="date" name="date_from" value="{date_from}">
                <input type="date" name="date_to" value="{date_to}">
                <input name="topic" placeholder="Search by topic" value="{topic_search}">
                <button class="btn" type="submit">Apply Filters</button>
                <a class="btn btn-secondary" href="{url_for('dashboard_meetings')}">Reset</a>
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

    member_name = request.args.get("member", "").strip()
    status_filter = request.args.get("status", "").strip().upper()
    topic_filter = request.args.get("topic", "").strip()
    date_filter = request.args.get("date", "").strip()

    rows = get_attendance_rows(meeting_id)

    filtered = []
    for r in rows:
        if member_name and member_name.lower() not in (r.get("participant_name") or "").lower():
            continue
        if status_filter and status_filter != (r.get("status") or "").upper():
            continue
        if topic_filter and topic_filter.lower() not in (r.get("topic") or "").lower():
            continue
        if date_filter and date_filter != (r.get("meeting_date") or ""):
            continue
        filtered.append(r)

    joined_only_count = len({r["participant_name"].strip().lower() for r in filtered if (r.get("join_time") or "-") != "-"})
    unknown_count = sum(1 for r in filtered if int(r.get("is_unknown") or 0) == 1)
    present_count = sum(1 for r in filtered if (r.get("status") or "").upper() == "PRESENT")
    late_count = sum(1 for r in filtered if (r.get("status") or "").upper() == "LATE")
    absent_count = sum(1 for r in filtered if (r.get("status") or "").upper() == "ABSENT")

    tr = []
    for r in filtered:
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
    <div class="grid">
        <div class="card metric"><div class="label">Topic</div><div class="value" style="font-size:20px;">{meeting['topic']}</div></div>
        <div class="card metric"><div class="label">Date</div><div class="value" style="font-size:20px;">{meeting['meeting_date']}</div></div>
        <div class="card metric"><div class="label">Joined Only</div><div class="value">{joined_only_count}</div></div>
        <div class="card metric"><div class="label">Present</div><div class="value">{present_count}</div></div>
        <div class="card metric"><div class="label">Late</div><div class="value">{late_count}</div></div>
        <div class="card metric"><div class="label">Absent</div><div class="value">{absent_count}</div></div>
        <div class="card metric"><div class="label">Unknown Participants</div><div class="value">{unknown_count}</div></div>
    </div>

    <div class="card">
        <h2 class="section-title">🔎 Report Filters</h2>
        <form method="get">
            <div class="row">
                <input name="member" placeholder="Filter by member name" value="{member_name}">
                <select name="status">
                    <option value="">All Status</option>
                    <option value="PRESENT" {"selected" if status_filter=="PRESENT" else ""}>PRESENT</option>
                    <option value="LATE" {"selected" if status_filter=="LATE" else ""}>LATE</option>
                    <option value="ABSENT" {"selected" if status_filter=="ABSENT" else ""}>ABSENT</option>
                    <option value="HOST" {"selected" if status_filter=="HOST" else ""}>HOST</option>
                </select>
                <input name="topic" placeholder="Topic" value="{topic_filter}">
                <input type="date" name="date" value="{date_filter}">
                <button class="btn" type="submit">Apply Filters</button>
                <a class="btn btn-secondary" href="{url_for('meeting_detail', meeting_id=meeting_id)}">Reset</a>
            </div>
        </form>
        <div style="display:flex;gap:10px;flex-wrap:wrap;margin-top:10px;">
            <a class="btn" href="{url_for('download_report_by_meeting', meeting_id=meeting_id, file_type='pdf')}">PDF</a>
            <a class="btn btn-secondary" href="{url_for('download_report_by_meeting', meeting_id=meeting_id, file_type='csv')}">CSV</a>
        </div>
    </div>

    <div class="card">
        <h2 class="section-title">📋 Attendance Detail</h2>
        <table>
            <tr><th>Name</th><th>Join Time</th><th>Leave Time</th><th>Minutes</th><th>Rejoins</th><th>Status</th><th>Member</th><th>Host</th></tr>
            {''.join(tr) if tr else '<tr><td colspan="8">No attendance rows for selected filters.</td></tr>'}
        </table>
    </div>
    """
    return render_page("Meeting Detail", content)


@app.route("/meeting/<int:meeting_id>/delete")
@admin_required
def meeting_delete(meeting_id):
    delete_meeting(meeting_id)
    log_activity("meeting_delete", f"Deleted meeting id {meeting_id}")
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
            log_activity("settings_update", "Updated attendance settings")
            flash("✅ Settings updated successfully.", "ok")
        except Exception as e:
            flash(f"❌ Failed to update settings: {e}", "bad")
        return redirect(url_for("settings_page"))

    content = f"""
    <div class="card">
        <h2 class="section-title">⚙ Attendance Settings</h2>
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
        <p class="tiny">These values control attendance classification and meeting finalization timing.</p>
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


# =========================================================
# STARTUP
# =========================================================
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