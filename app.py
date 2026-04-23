import csv
import hashlib
import hmac
import io
import os
import time
from difflib import SequenceMatcher
from collections import defaultdict
from datetime import date, datetime, timedelta
from functools import wraps
from zoneinfo import ZoneInfo
from urllib.parse import urlencode

from dotenv import load_dotenv
from flask import (
    Flask,
    Response,
    flash,
    jsonify,
    redirect,
    render_template_string,
    request,
    send_file,
    session,
    url_for,
)
import psycopg
from psycopg.rows import dict_row
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "change-me-secret")

DATABASE_URL = os.getenv("DATABASE_URL", "")
TIMEZONE_NAME = os.getenv("TIMEZONE_NAME", "Asia/Kolkata")
ZOOM_SECRET_TOKEN = os.getenv("ZOOM_SECRET_TOKEN", "")
HOST_NAME_HINT = os.getenv("HOST_NAME_HINT", "host").strip().lower()

DEFAULT_SETTINGS = {
    "present_percentage": os.getenv("PRESENT_PERCENTAGE", "75"),
    "late_count_as_present_percentage": os.getenv("LATE_COUNT_AS_PRESENT_PERCENTAGE", "30"),
    "late_threshold_minutes": os.getenv("LATE_THRESHOLD_MINUTES", "10"),
    "meeting_finalize_seconds": os.getenv("INACTIVITY_CONFIRM_SECONDS", "30"),
}

DB_INITIALIZED = False
LAST_STALE_CHECK_TS = 0
SETTINGS_CACHE = {}

ACTIVE_MEMBER_SQL = "CAST(active AS TEXT) IN ('1','true','t','True','TRUE')"
ACTIVE_USER_SQL = "CAST(is_active AS TEXT) IN ('1','true','t','True','TRUE')"


def now_local() -> datetime:
    return datetime.now(ZoneInfo(TIMEZONE_NAME))


def today_local() -> date:
    return now_local().date()


def parse_dt(value):
    if not value:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=ZoneInfo(TIMEZONE_NAME))
        return value.astimezone(ZoneInfo(TIMEZONE_NAME))
    text = str(value).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except Exception:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=ZoneInfo(TIMEZONE_NAME))
    return dt.astimezone(ZoneInfo(TIMEZONE_NAME))


def fmt_dt(dt):
    parsed = parse_dt(dt)
    return parsed.strftime("%d-%m-%Y %H:%M:%S") if parsed else "-"


def fmt_date(dt):
    parsed = parse_dt(dt)
    return parsed.strftime("%d-%m-%Y") if parsed else "-"


def fmt_time(dt):
    parsed = parse_dt(dt)
    return parsed.strftime("%H:%M:%S") if parsed else "-"


def fmt_time_ampm(dt):
    parsed = parse_dt(dt)
    return parsed.strftime("%I:%M:%S %p") if parsed else "-"


def mins_from_seconds(value):
    return round((value or 0) / 60, 2)


def member_display_name(row):
    if not row:
        return "-"
    return (row.get("full_name") or row.get("name") or "-").strip() or "-"


def member_name_sql(conn):
    has_full_name = column_exists(conn, "members", "full_name")
    has_name = column_exists(conn, "members", "name")
    if has_full_name and has_name:
        return "COALESCE(NULLIF(full_name, ''), NULLIF(name, ''))"
    if has_full_name:
        return "full_name"
    if has_name:
        return "name"
    return "NULL"


def insert_member_record(cur, conn, full_name, email, phone, active_value):
    has_full_name = column_exists(conn, "members", "full_name")
    has_name = column_exists(conn, "members", "name")

    if has_full_name and has_name:
        cur.execute(
            "INSERT INTO members(full_name, name, email, phone, active) VALUES (%s,%s,%s,%s,%s)",
            (full_name, full_name, email, phone, active_value),
        )
    elif has_full_name:
        cur.execute(
            "INSERT INTO members(full_name, email, phone, active) VALUES (%s,%s,%s,%s)",
            (full_name, email, phone, active_value),
        )
    elif has_name:
        cur.execute(
            "INSERT INTO members(name, email, phone, active) VALUES (%s,%s,%s,%s)",
            (full_name, email, phone, active_value),
        )


def update_member_record(cur, conn, member_id, full_name, email, phone):
    has_full_name = column_exists(conn, "members", "full_name")
    has_name = column_exists(conn, "members", "name")

    if has_full_name and has_name:
        cur.execute(
            "UPDATE members SET full_name=%s, name=%s, email=%s, phone=%s WHERE id=%s",
            (full_name, full_name, email, phone, member_id),
        )
    elif has_full_name:
        cur.execute(
            "UPDATE members SET full_name=%s, email=%s, phone=%s WHERE id=%s",
            (full_name, email, phone, member_id),
        )
    elif has_name:
        cur.execute(
            "UPDATE members SET name=%s, email=%s, phone=%s WHERE id=%s",
            (full_name, email, phone, member_id),
        )


def slugify(text: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in str(text))
    while "__" in safe:
        safe = safe.replace("__", "_")
    return safe.strip("_") or "report"


def db():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is missing")
    connect_timeout = os.getenv("DB_CONNECT_TIMEOUT", "5")
    try:
        connect_timeout = int(connect_timeout)
    except Exception:
        connect_timeout = 5
    if connect_timeout <= 0:
        connect_timeout = 5
    return psycopg.connect(DATABASE_URL, row_factory=dict_row, connect_timeout=connect_timeout)


def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated_function


def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if session.get("role") != "admin":
            flash("Admin access required.", "error")
            return redirect(url_for("home"))
        return f(*args, **kwargs)
    return decorated_function


def can_edit_users():
    return session.get("role") == "admin"


def is_truthy(value) -> bool:
    return str(value) in ("1", "True", "true", "t")


def table_exists(conn, table_name: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT EXISTS (
                SELECT 1
                FROM information_schema.tables
                WHERE table_schema='public' AND table_name=%s
            ) AS exists_flag
            """,
            (table_name,),
        )
        row = cur.fetchone()
        return bool(row and row["exists_flag"])


def column_exists(conn, table_name: str, column_name: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT EXISTS (
                SELECT 1
                FROM information_schema.columns
                WHERE table_schema='public' AND table_name=%s AND column_name=%s
            ) AS exists_flag
            """,
            (table_name, column_name),
        )
        row = cur.fetchone()
        return bool(row and row["exists_flag"])


def column_data_type(conn, table_name: str, column_name: str) -> str:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT data_type
            FROM information_schema.columns
            WHERE table_schema='public' AND table_name=%s AND column_name=%s
            """,
            (table_name, column_name),
        )
        row = cur.fetchone()
        return (row["data_type"] if row else "").lower()


def db_true_value(conn, table_name: str, column_name: str):
    dtype = column_data_type(conn, table_name, column_name)
    if dtype in ("integer", "smallint", "bigint", "numeric"):
        return 1
    return True


def db_false_value(conn, table_name: str, column_name: str):
    dtype = column_data_type(conn, table_name, column_name)
    if dtype in ("integer", "smallint", "bigint", "numeric"):
        return 0
    return False


def ensure_column(conn, table_name: str, column_name: str, definition_sql: str):
    if not column_exists(conn, table_name, column_name):
        with conn.cursor() as cur:
            cur.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition_sql}")


def ensure_index(conn, index_name: str, create_sql: str):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT EXISTS (
                SELECT 1
                FROM pg_indexes
                WHERE schemaname='public' AND indexname=%s
            ) AS exists_flag
            """,
            (index_name,),
        )
        row = cur.fetchone()
        if not row or not row["exists_flag"]:
            cur.execute(create_sql)


def fix_database_compatibility():
    with db() as conn:
        with conn.cursor() as cur:
            try:
                cur.execute(
                    """
                    DO $$
                    DECLARE
                        col_type TEXT;
                    BEGIN
                        SELECT data_type
                        INTO col_type
                        FROM information_schema.columns
                        WHERE table_schema='public'
                          AND table_name='members'
                          AND column_name='active';

                        IF col_type IN ('integer', 'smallint', 'bigint', 'numeric') THEN
                            ALTER TABLE members ALTER COLUMN active DROP DEFAULT;

                            ALTER TABLE members
                            ALTER COLUMN active TYPE BOOLEAN
                            USING (
                                CASE
                                    WHEN active IS NULL THEN FALSE
                                    WHEN active::integer = 1 THEN TRUE
                                    ELSE FALSE
                                END
                            );

                            ALTER TABLE members ALTER COLUMN active SET DEFAULT TRUE;
                        ELSIF col_type = 'text' THEN
                            ALTER TABLE members ALTER COLUMN active DROP DEFAULT;

                            ALTER TABLE members
                            ALTER COLUMN active TYPE BOOLEAN
                            USING (
                                CASE
                                    WHEN lower(trim(active)) IN ('1','true','t','yes','y') THEN TRUE
                                    ELSE FALSE
                                END
                            );

                            ALTER TABLE members ALTER COLUMN active SET DEFAULT TRUE;
                        END IF;
                    END$$;
                    """
                )

                cur.execute(
                    """
                    DO $$
                    DECLARE
                        col_type TEXT;
                    BEGIN
                        SELECT data_type
                        INTO col_type
                        FROM information_schema.columns
                        WHERE table_schema='public'
                          AND table_name='users'
                          AND column_name='is_active';

                        IF col_type IN ('integer', 'smallint', 'bigint', 'numeric') THEN
                            ALTER TABLE users ALTER COLUMN is_active DROP DEFAULT;

                            ALTER TABLE users
                            ALTER COLUMN is_active TYPE BOOLEAN
                            USING (
                                CASE
                                    WHEN is_active IS NULL THEN FALSE
                                    WHEN is_active::integer = 1 THEN TRUE
                                    ELSE FALSE
                                END
                            );

                            ALTER TABLE users ALTER COLUMN is_active SET DEFAULT TRUE;
                        ELSIF col_type = 'text' THEN
                            ALTER TABLE users ALTER COLUMN is_active DROP DEFAULT;

                            ALTER TABLE users
                            ALTER COLUMN is_active TYPE BOOLEAN
                            USING (
                                CASE
                                    WHEN lower(trim(is_active)) IN ('1','true','t','yes','y') THEN TRUE
                                    ELSE FALSE
                                END
                            );

                            ALTER TABLE users ALTER COLUMN is_active SET DEFAULT TRUE;
                        END IF;
                    END$$;
                    """
                )

                cur.execute(
                    """
                    DO $$
                    BEGIN
                        IF EXISTS (
                            SELECT 1
                            FROM information_schema.columns
                            WHERE table_schema='public'
                              AND table_name='meetings'
                              AND column_name='start_time'
                              AND data_type='text'
                        ) THEN
                            ALTER TABLE meetings ALTER COLUMN start_time DROP DEFAULT;

                            ALTER TABLE meetings
                            ALTER COLUMN start_time TYPE TIMESTAMPTZ
                            USING (
                                CASE
                                    WHEN start_time IS NULL OR btrim(start_time) = '' THEN NULL
                                    WHEN btrim(start_time) ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}' THEN start_time::timestamptz
                                    WHEN btrim(start_time) ~ '^[0-9]{1,2}/[0-9]{1,2}/[0-9]{4}' THEN to_timestamp(start_time, 'MM/DD/YYYY HH12:MI:SS AM')
                                    ELSE NULL
                                END
                            );
                        END IF;
                    END$$;
                    """
                )

                cur.execute(
                    """
                    DO $$
                    BEGIN
                        IF EXISTS (
                            SELECT 1
                            FROM information_schema.columns
                            WHERE table_schema='public'
                              AND table_name='meetings'
                              AND column_name='end_time'
                              AND data_type='text'
                        ) THEN
                            ALTER TABLE meetings ALTER COLUMN end_time DROP DEFAULT;

                            ALTER TABLE meetings
                            ALTER COLUMN end_time TYPE TIMESTAMPTZ
                            USING (
                                CASE
                                    WHEN end_time IS NULL OR btrim(end_time) = '' THEN NULL
                                    WHEN btrim(end_time) ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}' THEN end_time::timestamptz
                                    WHEN btrim(end_time) ~ '^[0-9]{1,2}/[0-9]{1,2}/[0-9]{4}' THEN to_timestamp(end_time, 'MM/DD/YYYY HH12:MI:SS AM')
                                    ELSE NULL
                                END
                            );
                        END IF;
                    END$$;
                    """
                )

                conn.commit()
            except Exception:
                conn.rollback()
                raise


def cast_setting_value(value, cast=str):
    if value is None:
        value = ""
    try:
        return cast(value)
    except Exception:
        try:
            default_value = DEFAULT_SETTINGS.get(value)
            if default_value is not None:
                return cast(default_value)
        except Exception:
            pass
        if cast is int:
            try:
                return int(float(value))
            except Exception:
                try:
                    return int(float(DEFAULT_SETTINGS.get(str(value), 0)))
                except Exception:
                    return 0
        if cast is float:
            try:
                return float(value)
            except Exception:
                try:
                    return float(DEFAULT_SETTINGS.get(str(value), 0))
                except Exception:
                    return 0.0
        if cast is bool:
            return str(value).strip().lower() in ("1", "true", "t", "yes", "y", "on")
        return str(value)


def get_setting(name, cast=str):
    cached_value = SETTINGS_CACHE.get(name, DEFAULT_SETTINGS.get(name))
    value = cached_value
    try:
        with db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT value FROM settings WHERE key=%s", (name,))
                row = cur.fetchone()
                if row and row.get("value") not in (None, ""):
                    value = row["value"]
                    SETTINGS_CACHE[name] = value
                elif name not in SETTINGS_CACHE and value is not None:
                    SETTINGS_CACHE[name] = value
    except Exception as e:
        print(f"⚠️ get_setting fallback for {name}: {e}")
        value = SETTINGS_CACHE.get(name, DEFAULT_SETTINGS.get(name))
    return cast_setting_value(value, cast)


def set_setting(name, value):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO settings(key, value)
                VALUES (%s, %s)
                ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=NOW()
                """,
                (name, str(value)),
            )
        conn.commit()
    SETTINGS_CACHE[name] = str(value)


def sync_special_user(conn, username: str, password: str, role: str):
    if not username or not password:
        return

    username = username.strip()
    password_hash = hash_password(password)
    true_val = db_true_value(conn, "users", "is_active")

    with conn.cursor() as cur:
        cur.execute("SELECT * FROM users WHERE username=%s", (username,))
        existing = cur.fetchone()

        if existing:
            needs_update = (
                existing["password_hash"] != password_hash
                or existing["role"] != role
                or not is_truthy(existing["is_active"])
            )
            if needs_update:
                cur.execute(
                    "UPDATE users SET password_hash=%s, role=%s, is_active=%s WHERE username=%s",
                    (password_hash, role, true_val, username),
                )
        else:
            cur.execute(
                "INSERT INTO users(username, password_hash, role, is_active) VALUES (%s, %s, %s, %s)",
                (username, password_hash, role, true_val),
            )


def maybe_finalize_stale_live_meetings(force=False):
    global LAST_STALE_CHECK_TS
    now_ts = time.time()
    if not force and now_ts - LAST_STALE_CHECK_TS < 12:
        return
    LAST_STALE_CHECK_TS = now_ts
    try:
        finalize_stale_live_meetings()
    except Exception as e:
        print(f"⚠️ finalize_stale_live_meetings skipped: {e}")


def finalize_stale_live_meetings():
    finalize_seconds = get_setting("meeting_finalize_seconds", int)
    if finalize_seconds <= 0:
        finalize_seconds = cast_setting_value(DEFAULT_SETTINGS.get("meeting_finalize_seconds", "30"), int)
        if finalize_seconds <= 0:
            finalize_seconds = 30
    threshold_time = now_local() - timedelta(seconds=finalize_seconds)

    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM meetings WHERE status='live' ORDER BY id DESC")
            meetings = cur.fetchall()

            for meeting in meetings:
                meeting_uuid = meeting.get("meeting_uuid")
                if not meeting_uuid:
                    continue

                cur.execute(
                    "SELECT * FROM attendance WHERE meeting_uuid=%s ORDER BY updated_at DESC NULLS LAST, id DESC",
                    (meeting_uuid,),
                )
                rows = cur.fetchall()
                if not rows:
                    continue

                anybody_live = any(r.get("current_join") is not None for r in rows)
                if anybody_live:
                    continue

                last_activity = get_meeting_rows_last_activity(rows)

                if last_activity and last_activity <= threshold_time:
                    try:
                        finalize_meeting(meeting_uuid, last_activity)
                    except Exception as e:
                        print(f"⚠️ finalize_meeting skipped for {meeting_uuid}: {e}")


def init_db():
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id SERIAL PRIMARY KEY,
                    username TEXT UNIQUE NOT NULL,
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL DEFAULT 'viewer',
                    is_active BOOLEAN NOT NULL DEFAULT TRUE,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS members (
                    id SERIAL PRIMARY KEY,
                    full_name TEXT NOT NULL,
                    email TEXT,
                    phone TEXT,
                    tags TEXT,
                    active BOOLEAN NOT NULL DEFAULT TRUE,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS meetings (
                    id SERIAL PRIMARY KEY,
                    meeting_uuid TEXT UNIQUE,
                    meeting_id TEXT,
                    topic TEXT,
                    host_name TEXT,
                    start_time TIMESTAMPTZ,
                    end_time TIMESTAMPTZ,
                    status TEXT NOT NULL DEFAULT 'live',
                    source TEXT NOT NULL DEFAULT 'webhook',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    finalized_at TIMESTAMPTZ,
                    unique_participants INTEGER NOT NULL DEFAULT 0,
                    member_participants INTEGER NOT NULL DEFAULT 0,
                    unknown_participants INTEGER NOT NULL DEFAULT 0,
                    present_count INTEGER NOT NULL DEFAULT 0,
                    late_count INTEGER NOT NULL DEFAULT 0,
                    absent_count INTEGER NOT NULL DEFAULT 0,
                    host_present BOOLEAN NOT NULL DEFAULT FALSE,
                    notes TEXT
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS attendance (
                    id SERIAL PRIMARY KEY,
                    meeting_uuid TEXT NOT NULL,
                    participant_name TEXT NOT NULL,
                    participant_email TEXT,
                    participant_key TEXT NOT NULL,
                    first_join TIMESTAMPTZ,
                    last_leave TIMESTAMPTZ,
                    total_seconds INTEGER NOT NULL DEFAULT 0,
                    rejoin_count INTEGER NOT NULL DEFAULT 0,
                    current_join TIMESTAMPTZ,
                    is_member BOOLEAN NOT NULL DEFAULT FALSE,
                    member_id INTEGER REFERENCES members(id) ON DELETE SET NULL,
                    is_host BOOLEAN NOT NULL DEFAULT FALSE,
                    status TEXT DEFAULT 'JOINED',
                    final_status TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE(meeting_uuid, participant_key)
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS activity_log (
                    id SERIAL PRIMARY KEY,
                    username TEXT,
                    action TEXT NOT NULL,
                    details TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )

        if table_exists(conn, "users"):
            ensure_column(conn, "users", "role", "TEXT NOT NULL DEFAULT 'viewer'")
            ensure_column(conn, "users", "is_active", "BOOLEAN NOT NULL DEFAULT TRUE")
            ensure_column(conn, "users", "created_at", "TIMESTAMPTZ NOT NULL DEFAULT NOW()")

        if table_exists(conn, "settings"):
            ensure_column(conn, "settings", "updated_at", "TIMESTAMPTZ NOT NULL DEFAULT NOW()")

        if table_exists(conn, "members"):
            ensure_column(conn, "members", "email", "TEXT")
            ensure_column(conn, "members", "phone", "TEXT")
            ensure_column(conn, "members", "tags", "TEXT")
            ensure_column(conn, "members", "active", "BOOLEAN NOT NULL DEFAULT TRUE")
            ensure_column(conn, "members", "created_at", "TIMESTAMPTZ NOT NULL DEFAULT NOW()")

        if table_exists(conn, "meetings"):
            ensure_column(conn, "meetings", "meeting_uuid", "TEXT")
            ensure_column(conn, "meetings", "meeting_id", "TEXT")
            ensure_column(conn, "meetings", "topic", "TEXT")
            ensure_column(conn, "meetings", "host_name", "TEXT")
            ensure_column(conn, "meetings", "start_time", "TIMESTAMPTZ")
            ensure_column(conn, "meetings", "end_time", "TIMESTAMPTZ")
            ensure_column(conn, "meetings", "status", "TEXT NOT NULL DEFAULT 'live'")
            ensure_column(conn, "meetings", "source", "TEXT NOT NULL DEFAULT 'webhook'")
            ensure_column(conn, "meetings", "created_at", "TIMESTAMPTZ NOT NULL DEFAULT NOW()")
            ensure_column(conn, "meetings", "finalized_at", "TIMESTAMPTZ")
            ensure_column(conn, "meetings", "unique_participants", "INTEGER NOT NULL DEFAULT 0")
            ensure_column(conn, "meetings", "member_participants", "INTEGER NOT NULL DEFAULT 0")
            ensure_column(conn, "meetings", "unknown_participants", "INTEGER NOT NULL DEFAULT 0")
            ensure_column(conn, "meetings", "present_count", "INTEGER NOT NULL DEFAULT 0")
            ensure_column(conn, "meetings", "late_count", "INTEGER NOT NULL DEFAULT 0")
            ensure_column(conn, "meetings", "absent_count", "INTEGER NOT NULL DEFAULT 0")
            ensure_column(conn, "meetings", "host_present", "BOOLEAN NOT NULL DEFAULT FALSE")
            ensure_column(conn, "meetings", "notes", "TEXT")

        if table_exists(conn, "attendance"):
            ensure_column(conn, "attendance", "meeting_uuid", "TEXT")
            ensure_column(conn, "attendance", "participant_name", "TEXT")
            ensure_column(conn, "attendance", "participant_email", "TEXT")
            ensure_column(conn, "attendance", "participant_key", "TEXT")
            ensure_column(conn, "attendance", "first_join", "TIMESTAMPTZ")
            ensure_column(conn, "attendance", "last_leave", "TIMESTAMPTZ")
            ensure_column(conn, "attendance", "total_seconds", "INTEGER NOT NULL DEFAULT 0")
            ensure_column(conn, "attendance", "rejoin_count", "INTEGER NOT NULL DEFAULT 0")
            ensure_column(conn, "attendance", "current_join", "TIMESTAMPTZ")
            ensure_column(conn, "attendance", "is_member", "BOOLEAN NOT NULL DEFAULT FALSE")
            ensure_column(conn, "attendance", "member_id", "INTEGER")
            ensure_column(conn, "attendance", "is_host", "BOOLEAN NOT NULL DEFAULT FALSE")
            ensure_column(conn, "attendance", "status", "TEXT DEFAULT 'JOINED'")
            ensure_column(conn, "attendance", "final_status", "TEXT")
            ensure_column(conn, "attendance", "created_at", "TIMESTAMPTZ NOT NULL DEFAULT NOW()")
            ensure_column(conn, "attendance", "updated_at", "TIMESTAMPTZ NOT NULL DEFAULT NOW()")

        if table_exists(conn, "activity_log"):
            ensure_column(conn, "activity_log", "username", "TEXT")
            ensure_column(conn, "activity_log", "action", "TEXT")
            ensure_column(conn, "activity_log", "details", "TEXT")
            ensure_column(conn, "activity_log", "created_at", "TIMESTAMPTZ NOT NULL DEFAULT NOW()")

        ensure_index(conn, "idx_attendance_meeting_uuid", "CREATE INDEX idx_attendance_meeting_uuid ON attendance(meeting_uuid)")
        ensure_index(conn, "idx_attendance_member_id", "CREATE INDEX idx_attendance_member_id ON attendance(member_id)")
        ensure_index(conn, "idx_meetings_status", "CREATE INDEX idx_meetings_status ON meetings(status)")

        with conn.cursor() as cur:
            for key, value in DEFAULT_SETTINGS.items():
                cur.execute(
                    "INSERT INTO settings(key, value) VALUES (%s, %s) ON CONFLICT (key) DO NOTHING",
                    (key, value),
                )

        admin_username = os.getenv("ADMIN_USERNAME", "admin").strip()
        admin_password = os.getenv("ADMIN_PASSWORD", "admin123")
        viewer_username = os.getenv("VIEWER_USERNAME", "viewer").strip()
        viewer_password = os.getenv("VIEWER_PASSWORD", "viewer123")

        sync_special_user(conn, admin_username, admin_password, "admin")
        sync_special_user(conn, viewer_username, viewer_password, "viewer")

        conn.commit()


def log_activity(action, details=""):
    username = session.get("username") if session else None
    try:
        with db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO activity_log(username, action, details) VALUES (%s, %s, %s)",
                    (username, action, details[:2000]),
                )
            conn.commit()
    except Exception:
        pass


def find_member(name: str, email: str | None = None):
    norm_name = (name or "").strip().lower()
    norm_email = (email or "").strip().lower()
    with db() as conn:
        with conn.cursor() as cur:
            name_sql = member_name_sql(conn)
            if norm_email:
                cur.execute(
                    f"SELECT * FROM members WHERE {ACTIVE_MEMBER_SQL} AND lower(email)=%s LIMIT 1",
                    (norm_email,),
                )
                row = cur.fetchone()
                if row:
                    return row
            cur.execute(
                f"SELECT * FROM members WHERE {ACTIVE_MEMBER_SQL} AND lower(COALESCE({name_sql}, ''))=%s LIMIT 1",
                (norm_name,),
            )
            row = cur.fetchone()
            if row:
                return row
    return None


def participant_key(name, email=None):
    if email:
        return f"email::{email.strip().lower()}"
    return f"name::{(name or '').strip().lower()}"


def ensure_meeting(payload_object):
    meeting_uuid = str(payload_object.get("uuid") or "").strip()
    meeting_id = str(payload_object.get("id") or payload_object.get("meeting_id") or "").strip()
    topic = (payload_object.get("topic") or payload_object.get("meeting_topic") or "Zoom Meeting").strip()
    host_name = (payload_object.get("host_name") or payload_object.get("host_email") or "").strip()
    start_time = parse_dt(payload_object.get("start_time")) or now_local()

    lookup_uuid = meeting_uuid or meeting_id
    if not lookup_uuid and not meeting_id:
        return None

    with db() as conn:
        with conn.cursor() as cur:
            if lookup_uuid:
                cur.execute("SELECT * FROM meetings WHERE meeting_uuid=%s", (lookup_uuid,))
                row = cur.fetchone()
                if row:
                    cur.execute(
                        """
                        UPDATE meetings
                        SET meeting_id=COALESCE(NULLIF(%s, ''), meeting_id),
                            topic=CASE WHEN %s <> '' THEN %s ELSE topic END,
                            host_name=CASE WHEN %s <> '' THEN %s ELSE host_name END,
                            start_time=COALESCE(start_time, %s)
                        WHERE id=%s
                        RETURNING *
                        """,
                        (meeting_id, topic, topic, host_name, host_name, start_time, row["id"]),
                    )
                    row = cur.fetchone()
                    conn.commit()
                    return row

            if meeting_id:
                cur.execute(
                    "SELECT * FROM meetings WHERE meeting_id=%s AND status='live' ORDER BY id DESC LIMIT 1",
                    (meeting_id,),
                )
                row = cur.fetchone()
                if row:
                    cur.execute(
                        """
                        UPDATE meetings
                        SET topic=CASE WHEN %s <> '' THEN %s ELSE topic END,
                            host_name=CASE WHEN %s <> '' THEN %s ELSE host_name END,
                            start_time=COALESCE(start_time, %s)
                        WHERE id=%s
                        RETURNING *
                        """,
                        (topic, topic, host_name, host_name, start_time, row["id"]),
                    )
                    row = cur.fetchone()
                    conn.commit()
                    return row

            cur.execute(
                """
                INSERT INTO meetings(meeting_uuid, meeting_id, topic, host_name, start_time, status)
                VALUES (%s, %s, %s, %s, %s, 'live') RETURNING *
                """,
                (lookup_uuid or meeting_id, meeting_id, topic, host_name, start_time),
            )
            row = cur.fetchone()
        conn.commit()

    return row


def get_row_visible_span_seconds(row, end_time=None):
    first_join_dt = parse_dt(row.get("first_join"))
    if not first_join_dt:
        return None
    last_point_dt = parse_dt(row.get("last_leave")) or parse_dt(row.get("current_join")) or parse_dt(end_time)
    if not last_point_dt:
        return None
    if last_point_dt < first_join_dt:
        return 0
    return max(int((last_point_dt - first_join_dt).total_seconds()), 0)


def get_row_effective_total_seconds(row, end_time=None):
    total_seconds = cast_setting_value(row.get("total_seconds") or 0, int)
    current_join_dt = parse_dt(row.get("current_join"))
    end_dt = parse_dt(end_time)
    if current_join_dt:
        if end_dt and current_join_dt > end_dt:
            current_join_dt = end_dt
        if end_dt and current_join_dt:
            total_seconds += max(int((end_dt - current_join_dt).total_seconds()), 0)

    visible_span_seconds = get_row_visible_span_seconds(row, end_time)
    if visible_span_seconds is not None and total_seconds > visible_span_seconds:
        total_seconds = visible_span_seconds

    return max(total_seconds, 0)


def get_meeting_rows_last_activity(attendance_rows):
    last_activity = None
    for row in attendance_rows or []:
        candidate = parse_dt(row.get("last_leave")) or parse_dt(row.get("current_join")) or parse_dt(row.get("first_join"))
        if candidate and (last_activity is None or candidate > last_activity):
            last_activity = candidate
    return last_activity


def update_participant(meeting_uuid, participant_name, participant_email, event_time, event_type):
    is_host = bool(HOST_NAME_HINT and HOST_NAME_HINT in (participant_name or "").strip().lower())
    member = find_member(participant_name, participant_email)
    key = participant_key(participant_name, participant_email)

    with db() as conn:
        with conn.cursor() as cur:
            is_member_db_value = db_true_value(conn, "attendance", "is_member") if member else db_false_value(conn, "attendance", "is_member")
            is_host_db_value = db_true_value(conn, "attendance", "is_host") if is_host else db_false_value(conn, "attendance", "is_host")
            has_meeting_pk = column_exists(conn, "attendance", "meeting_pk")
            meeting_pk = None

            if has_meeting_pk:
                cur.execute("SELECT * FROM meetings WHERE meeting_uuid=%s ORDER BY id DESC LIMIT 1", (meeting_uuid,))
                meeting = cur.fetchone()
                if not meeting:
                    print("❌ Meeting not found, skipping participant")
                    return
                meeting_pk = meeting.get("id")
                if not meeting_pk:
                    print("⚠️ Skipping attendance insert: meeting_pk missing")
                    return

            cur.execute(
                "SELECT * FROM attendance WHERE meeting_uuid=%s AND participant_key=%s",
                (meeting_uuid, key),
            )
            row = cur.fetchone()

            if not row:
                first_join = event_time if event_type == "join" else None
                current_join = event_time if event_type == "join" else None
                last_leave = event_time if event_type == "leave" else None

                try:
                    if has_meeting_pk:
                        cur.execute(
                            """
                            INSERT INTO attendance(
                                meeting_pk, meeting_uuid, participant_name, participant_email, participant_key,
                                first_join, last_leave, current_join, total_seconds, rejoin_count,
                                is_member, member_id, is_host, status, updated_at
                            )
                            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,0,0,%s,%s,%s,%s,NOW())
                            RETURNING *
                            """,
                            (
                                meeting_pk,
                                meeting_uuid,
                                participant_name,
                                participant_email,
                                key,
                                first_join,
                                last_leave,
                                current_join,
                                is_member_db_value,
                                member["id"] if member else None,
                                is_host_db_value,
                                "JOINED" if event_type == "join" else "LEFT",
                            ),
                        )
                    else:
                        cur.execute(
                            """
                            INSERT INTO attendance(
                                meeting_uuid, participant_name, participant_email, participant_key,
                                first_join, last_leave, current_join, total_seconds, rejoin_count,
                                is_member, member_id, is_host, status, updated_at
                            )
                            VALUES (%s,%s,%s,%s,%s,%s,%s,0,0,%s,%s,%s,%s,NOW())
                            RETURNING *
                            """,
                            (
                                meeting_uuid,
                                participant_name,
                                participant_email,
                                key,
                                first_join,
                                last_leave,
                                current_join,
                                is_member_db_value,
                                member["id"] if member else None,
                                is_host_db_value,
                                "JOINED" if event_type == "join" else "LEFT",
                            ),
                        )
                    row = cur.fetchone()
                except Exception as e:
                    print("❌ ATTENDANCE INSERT FAILED:", str(e))
                    raise

            if event_type == "join":
                rejoin_count = row["rejoin_count"] or 0
                had_previous_session = (
                    row.get("first_join") is not None
                    and row.get("current_join") is None
                    and (
                        row.get("last_leave") is not None
                        or (row.get("total_seconds") or 0) > 0
                    )
                )
                if had_previous_session:
                    rejoin_count += 1

                if has_meeting_pk:
                    cur.execute(
                        """
                        UPDATE attendance
                        SET participant_name=%s,
                            participant_email=%s,
                            first_join=COALESCE(first_join, %s),
                            current_join=%s,
                            rejoin_count=%s,
                            is_member=%s,
                            member_id=%s,
                            is_host=%s,
                            meeting_pk=%s,
                            status='JOINED',
                            updated_at=NOW()
                        WHERE id=%s
                        """,
                        (
                            participant_name,
                            participant_email,
                            event_time,
                            event_time,
                            rejoin_count,
                            is_member_db_value,
                            member["id"] if member else None,
                            is_host_db_value,
                            meeting_pk,
                            row["id"],
                        ),
                    )
                else:
                    cur.execute(
                        """
                        UPDATE attendance
                        SET participant_name=%s,
                            participant_email=%s,
                            first_join=COALESCE(first_join, %s),
                            current_join=%s,
                            rejoin_count=%s,
                            is_member=%s,
                            member_id=%s,
                            is_host=%s,
                            status='JOINED',
                            updated_at=NOW()
                        WHERE id=%s
                        """,
                        (
                            participant_name,
                            participant_email,
                            event_time,
                            event_time,
                            rejoin_count,
                            is_member_db_value,
                            member["id"] if member else None,
                            is_host_db_value,
                            row["id"],
                        ),
                    )
            else:
                total_seconds = cast_setting_value(row.get("total_seconds") or 0, int)
                current_join_dt = parse_dt(row.get("current_join"))
                if current_join_dt:
                    if event_time < current_join_dt:
                        delta = 0
                    else:
                        delta = int((event_time - current_join_dt).total_seconds())
                    total_seconds += max(delta, 0)

                visible_span_seconds = get_row_visible_span_seconds(
                    {
                        "first_join": row.get("first_join"),
                        "last_leave": event_time,
                        "current_join": None,
                    },
                    event_time,
                )
                if visible_span_seconds is not None and total_seconds > visible_span_seconds:
                    total_seconds = visible_span_seconds

                if has_meeting_pk:
                    cur.execute(
                        """
                        UPDATE attendance
                        SET participant_name=%s,
                            participant_email=%s,
                            last_leave=%s,
                            current_join=NULL,
                            total_seconds=%s,
                            is_member=%s,
                            member_id=%s,
                            is_host=%s,
                            meeting_pk=%s,
                            status='LEFT',
                            updated_at=NOW()
                        WHERE id=%s
                        """,
                        (
                            participant_name,
                            participant_email,
                            event_time,
                            total_seconds,
                            is_member_db_value,
                            member["id"] if member else None,
                            is_host_db_value,
                            meeting_pk,
                            row["id"],
                        ),
                    )
                else:
                    cur.execute(
                        """
                        UPDATE attendance
                        SET participant_name=%s,
                            participant_email=%s,
                            last_leave=%s,
                            current_join=NULL,
                            total_seconds=%s,
                            is_member=%s,
                            member_id=%s,
                            is_host=%s,
                            status='LEFT',
                            updated_at=NOW()
                        WHERE id=%s
                        """,
                        (
                            participant_name,
                            participant_email,
                            event_time,
                            total_seconds,
                            is_member_db_value,
                            member["id"] if member else None,
                            is_host_db_value,
                            row["id"],
                        ),
                    )
        conn.commit()

    refresh_live_meeting_summary(meeting_uuid)


def classify_row_for_meeting(row, start_time, end_time, present_percentage=None, late_pct=None):
    if present_percentage is None:
        present_percentage = get_setting("present_percentage", int)
    if late_pct is None:
        late_pct = get_setting("late_count_as_present_percentage", int)

    start_dt = parse_dt(start_time) or now_local()
    end_dt = parse_dt(end_time) or start_dt
    if end_dt < start_dt:
        end_dt = start_dt

    total = get_row_effective_total_seconds(row, end_dt)

    meeting_seconds = max(int((end_dt - start_dt).total_seconds()), 0)
    required_present = meeting_seconds * present_percentage / 100.0
    required_late = meeting_seconds * late_pct / 100.0

    if row.get("is_host"):
        return "HOST", total
    if total >= required_present:
        return "PRESENT", total
    if total >= required_late:
        return "LATE", total
    return "ABSENT", total


def finalize_meeting(meeting_uuid, ended_at=None):
    ended_at = parse_dt(ended_at) or now_local()
    present_percentage = get_setting("present_percentage", int)
    late_pct = get_setting("late_count_as_present_percentage", int)

    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM meetings WHERE meeting_uuid=%s", (meeting_uuid,))
            meeting = cur.fetchone()
            if not meeting:
                return None

            start_time = parse_dt(meeting["start_time"]) or ended_at

            cur.execute("SELECT * FROM attendance WHERE meeting_uuid=%s ORDER BY participant_name", (meeting_uuid,))
            rows = cur.fetchall()

            derived_last_activity = get_meeting_rows_last_activity(rows)
            if derived_last_activity and derived_last_activity >= start_time:
                if ended_at > derived_last_activity:
                    end_time = derived_last_activity
                else:
                    end_time = ended_at
            else:
                end_time = ended_at

            if end_time < start_time:
                end_time = start_time

            present_count = 0
            late_count = 0
            absent_count = 0
            member_participants = 0
            unknown_participants = 0
            host_present = False

            for row in rows:
                final_status, total = classify_row_for_meeting(row, start_time, end_time, present_percentage, late_pct)

                if final_status == "PRESENT":
                    present_count += 1
                elif final_status == "LATE":
                    late_count += 1
                elif final_status == "ABSENT":
                    absent_count += 1

                if row.get("is_member"):
                    member_participants += 1
                else:
                    unknown_participants += 1

                if row.get("is_host"):
                    host_present = True

                cur.execute(
                    """
                    UPDATE attendance
                    SET total_seconds=%s,
                        last_leave=COALESCE(last_leave, %s),
                        current_join=NULL,
                        final_status=%s,
                        updated_at=NOW()
                    WHERE id=%s
                    """,
                    (total, end_time, final_status, row["id"]),
                )

            cur.execute(
                """
                UPDATE meetings
                SET end_time=%s,
                    status='ended',
                    finalized_at=NOW(),
                    unique_participants=%s,
                    member_participants=%s,
                    unknown_participants=%s,
                    present_count=%s,
                    late_count=%s,
                    absent_count=%s,
                    host_present=%s
                WHERE meeting_uuid=%s
                RETURNING *
                """,
                (
                    end_time,
                    len(rows),
                    member_participants,
                    unknown_participants,
                    present_count,
                    late_count,
                    absent_count,
                    host_present,
                    meeting_uuid,
                ),
            )
            updated = cur.fetchone()
        conn.commit()
    return updated


def refresh_live_meeting_summary(meeting_uuid):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM attendance WHERE meeting_uuid=%s", (meeting_uuid,))
            rows = cur.fetchall()

            member_participants = sum(1 for r in rows if r["is_member"])
            unknown_participants = sum(1 for r in rows if not r["is_member"])
            host_present = any(r["is_host"] and r["current_join"] is not None for r in rows)

            cur.execute(
                """
                UPDATE meetings
                SET unique_participants=%s,
                    member_participants=%s,
                    unknown_participants=%s,
                    host_present=%s
                WHERE meeting_uuid=%s
                """,
                (len(rows), member_participants, unknown_participants, host_present, meeting_uuid),
            )
        conn.commit()


def get_live_status_for_row(row, meeting_start):
    now_dt = now_local()
    start_dt = parse_dt(meeting_start) or now_dt
    status, total = classify_row_for_meeting(row, start_dt, now_dt)
    return status, total


def read_live_snapshot():
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT *
                FROM meetings
                WHERE status='live' AND meeting_uuid IS NOT NULL AND meeting_uuid <> ''
                ORDER BY id DESC
                LIMIT 1
                """
            )
            meeting = cur.fetchone()

            if not meeting:
                return None

            meeting_uuid = meeting.get("meeting_uuid")
            cur.execute("SELECT * FROM attendance WHERE meeting_uuid=%s ORDER BY participant_name", (meeting_uuid,))
            participants = cur.fetchall()

            cur.execute(f"SELECT * FROM members WHERE {ACTIVE_MEMBER_SQL} ORDER BY full_name")
            members = cur.fetchall()

    joined_member_ids = {p["member_id"] for p in participants if p.get("member_id") and p.get("first_join")}
    not_joined_members = [m for m in members if m["id"] not in joined_member_ids]
    active_now = [p for p in participants if p.get("current_join") is not None]

    return {
        "meeting": meeting,
        "participants": participants,
        "active_now": active_now,
        "not_joined_members": not_joined_members,
    }


def normalize_period_dates(filters):
    period_mode = filters.get("period_mode", "custom")
    from_date = filters.get("from_date", "")
    to_date = filters.get("to_date", "")

    today = today_local()

    if period_mode == "day":
        from_date = today.isoformat()
        to_date = today.isoformat()
    elif period_mode == "week":
        start = today - timedelta(days=today.weekday())
        from_date = start.isoformat()
        to_date = today.isoformat()
    elif period_mode == "month":
        start = today.replace(day=1)
        from_date = start.isoformat()
        to_date = today.isoformat()
    elif period_mode == "year":
        start = today.replace(month=1, day=1)
        from_date = start.isoformat()
        to_date = today.isoformat()

    return from_date, to_date


def compute_trend(rows, period_mode="custom"):
    buckets = defaultdict(lambda: {"present": 0, "late": 0, "absent": 0, "unknown": 0})

    for r in rows:
        dt = parse_dt(r.get("start_time"))
        if not dt:
            continue

        if period_mode == "year":
            label = dt.strftime("%b")
        elif period_mode == "month":
            label = f"W{((dt.day - 1) // 7) + 1}"
        elif period_mode == "week":
            label = dt.strftime("%a")
        else:
            label = dt.strftime("%d-%m")

        status = r.get("final_status")
        if status == "PRESENT":
            buckets[label]["present"] += 1
        elif status == "LATE":
            buckets[label]["late"] += 1
        elif status == "ABSENT":
            buckets[label]["absent"] += 1

        if not r.get("is_member"):
            buckets[label]["unknown"] += 1

    labels = list(buckets.keys())
    return {
        "labels": labels,
        "present": [buckets[k]["present"] for k in labels],
        "late": [buckets[k]["late"] for k in labels],
        "absent": [buckets[k]["absent"] for k in labels],
        "unknown": [buckets[k]["unknown"] for k in labels],
    }


def predict_next_attendance(meeting_compare):
    if not meeting_compare:
        return 0
    recent = meeting_compare[:5]
    totals = [m.get("present", 0) + m.get("late", 0) + m.get("absent", 0) for m in recent]
    return round(sum(totals) / len(totals)) if totals else 0




def clamp_score(value, minimum=0, maximum=100):
    try:
        value = float(value)
    except Exception:
        value = 0.0
    return round(max(minimum, min(maximum, value)), 2)


def calculate_attendance_score(present_count, late_count, absent_count):
    total = max(int(present_count or 0) + int(late_count or 0) + int(absent_count or 0), 0)
    if total <= 0:
        return 0.0
    raw = (int(present_count or 0) * 10) + (int(late_count or 0) * 5) - (int(absent_count or 0) * 10)
    raw_min = total * -10
    raw_max = total * 10
    if raw_max == raw_min:
        return 0.0
    normalized = ((raw - raw_min) / (raw_max - raw_min)) * 100.0
    return clamp_score(normalized)


def calculate_engagement_score(minutes_attended, rejoins, meetings_count, present_count, late_count, absent_count, avg_minutes_reference):
    meetings_count = max(int(meetings_count or 0), 0)
    if meetings_count <= 0:
        return 0.0
    avg_minutes_reference = max(float(avg_minutes_reference or 0), 1.0)
    minutes_attended = max(float(minutes_attended or 0), 0.0)
    rejoins = max(float(rejoins or 0), 0.0)
    attended_ratio = min(minutes_attended / (avg_minutes_reference * meetings_count), 1.25)
    attended_component = min(attended_ratio / 1.25, 1.0) * 55.0
    consistency_ratio = ((int(present_count or 0) * 1.0) + (int(late_count or 0) * 0.6)) / meetings_count
    consistency_component = min(max(consistency_ratio, 0.0), 1.0) * 30.0
    rejoins_per_meeting = rejoins / meetings_count
    rejoin_component = max(0.0, 15.0 - min(rejoins_per_meeting * 7.5, 15.0))
    return clamp_score(attended_component + consistency_component + rejoin_component)


def get_risk_level(score):
    score = clamp_score(score)
    if score >= 80:
        return {"label": "Safe", "emoji": "🟢", "css": "ok", "short": "SAFE"}
    if score >= 50:
        return {"label": "Warning", "emoji": "🟡", "css": "warn", "short": "WARNING"}
    return {"label": "Critical", "emoji": "🔴", "css": "danger", "short": "CRITICAL"}


def normalize_name_for_match(value):
    return " ".join("".join(ch.lower() if ch.isalnum() else " " for ch in str(value or "")).split())


def suggest_unknown_matches(unknown_board, members, limit=8):
    suggestions = []
    member_pool = []
    for m in members or []:
        member_name = member_display_name(m)
        member_norm = normalize_name_for_match(member_name)
        if member_norm:
            member_pool.append((member_name, member_norm, m.get("id")))

    for item in unknown_board or []:
        unknown_name = item.get("name") or ""
        unknown_norm = normalize_name_for_match(unknown_name)
        if not unknown_norm:
            continue
        best = None
        best_score = 0.0
        for member_name, member_norm, member_id in member_pool:
            ratio = SequenceMatcher(None, unknown_norm, member_norm).ratio()
            if unknown_norm in member_norm or member_norm in unknown_norm:
                ratio = max(ratio, 0.86)
            if ratio > best_score:
                best_score = ratio
                best = {"unknown": unknown_name, "member": member_name, "member_id": member_id, "score": round(ratio * 100, 1)}
        if best and best_score >= 0.65:
            suggestions.append(best)
        if len(suggestions) >= limit:
            break
    return suggestions


def build_heatmap_data(rows, member_ids=None):
    member_ids = {int(x) for x in (member_ids or []) if str(x).isdigit()}
    status_priority = {"ABSENT": 0, "LATE": 1, "PRESENT": 2, "HOST": 2}
    day_map = {}
    filtered_rows = []
    for r in rows or []:
        if member_ids and int(r.get("member_id") or 0) not in member_ids:
            continue
        filtered_rows.append(r)

    for r in filtered_rows:
        dt = parse_dt(r.get("start_time")) or parse_dt(r.get("first_join"))
        if not dt:
            continue
        day_key = dt.date().isoformat()
        status = r.get("final_status") or ("HOST" if r.get("is_host") else None) or "ABSENT"
        current = day_map.get(day_key)
        if current is None or status_priority.get(status, 0) >= status_priority.get(current, 0):
            day_map[day_key] = status

    if day_map:
        end_day = max(datetime.fromisoformat(k).date() for k in day_map.keys())
    else:
        end_day = today_local()
    start_day = end_day - timedelta(days=83)
    cells = []
    current = start_day
    while current <= end_day:
        day_key = current.isoformat()
        status = day_map.get(day_key)
        if status == "PRESENT" or status == "HOST":
            css = "heat-good"
            title = f"{day_key}: Present"
        elif status == "LATE":
            css = "heat-warn"
            title = f"{day_key}: Late / weak participation"
        elif status == "ABSENT":
            css = "heat-bad"
            title = f"{day_key}: Absent"
        else:
            css = "heat-none"
            title = f"{day_key}: No record"
        cells.append({"date": day_key, "css": css, "title": title, "day": current.strftime("%d")})
        current += timedelta(days=1)
    return cells


def build_insight_lines(summary, meeting_compare, leaderboard, risk_table):
    lines = []
    if summary.get("attendance_health") is not None:
        lines.append(f"Attendance health is {summary['attendance_health']}% across the filtered dataset.")
    if summary.get("risk_members_count", 0) > 0:
        lines.append(f"{summary['risk_members_count']} member(s) currently fall into Warning or Critical risk level.")
    if leaderboard:
        top = leaderboard[0]
        lines.append(f"Top ranked member is {top['name']} with attendance score {top['attendance_score']} and engagement score {top['engagement_score']}.")
    if len(meeting_compare) >= 2:
        latest = meeting_compare[0]
        previous = meeting_compare[1]
        delta = round((latest.get('health') or 0) - (previous.get('health') or 0), 2)
        direction = 'improved' if delta >= 0 else 'dropped'
        lines.append(f"Latest meeting health {direction} by {abs(delta)} points compared with the previous meeting.")
    if risk_table:
        critical = [r for r in risk_table if r['risk']['short'] == 'CRITICAL']
        if critical:
            lines.append(f"Critical attention is needed for {', '.join(r['name'] for r in critical[:3])}.")
    return lines[:6]


def build_filter_query(filters):
    query_items = []
    for key, value in filters.items():
        if key == "member_ids":
            for item in value or []:
                if str(item).strip():
                    query_items.append(("member_ids", str(item).strip()))
        else:
            if value not in (None, ""):
                query_items.append((key, str(value)))
    return urlencode(query_items, doseq=True)


def analytics_data(filters):
    period_mode = filters.get("period_mode", "custom")
    from_date, to_date = normalize_period_dates(filters)
    filters["from_date"] = from_date
    filters["to_date"] = to_date

    raw_member_ids = filters.get("member_ids") or []
    if isinstance(raw_member_ids, str):
        raw_member_ids = [raw_member_ids]
    member_ids = []
    for item in raw_member_ids:
        item_text = str(item).strip()
        if item_text.isdigit():
            member_ids.append(int(item_text))
    filters["member_ids"] = [str(item) for item in member_ids]

    where = ["1=1"]
    params = []

    if from_date:
        where.append("CAST(m.start_time AS TEXT)::date >= %s")
        params.append(from_date)
    if to_date:
        where.append("CAST(m.start_time AS TEXT)::date <= %s")
        params.append(to_date)
    if filters.get("meeting_uuid"):
        where.append("a.meeting_uuid = %s")
        params.append(filters["meeting_uuid"])
    if member_ids:
        where.append("a.member_id = ANY(%s)")
        params.append(member_ids)
    if filters.get("person_name"):
        where.append("lower(a.participant_name) LIKE %s")
        params.append(f"%{filters['person_name'].strip().lower()}%")
    if filters.get("participant_type") == "member":
        where.append("CAST(a.is_member AS TEXT) IN ('1','true','t','True','TRUE')")
    elif filters.get("participant_type") == "unknown":
        where.append("COALESCE(CAST(a.is_member AS TEXT), '0') NOT IN ('1','true','t','True','TRUE')")
    elif filters.get("participant_type") == "host":
        where.append("CAST(a.is_host AS TEXT) IN ('1','true','t','True','TRUE')")

    sql = f"""
        SELECT
            a.*, m.topic, m.start_time, m.end_time, m.meeting_id, m.id AS meeting_row_id
        FROM attendance a
        JOIN meetings m ON m.meeting_uuid = a.meeting_uuid
        WHERE {' AND '.join(where)}
        ORDER BY m.id DESC, a.participant_name ASC
    """

    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()

            member_name_expr = member_name_sql(conn)
            cur.execute(f"SELECT *, {member_name_expr} AS display_name FROM members ORDER BY COALESCE({member_name_expr}, '')")
            all_members = cur.fetchall()

            cur.execute(f"SELECT *, {member_name_expr} AS display_name FROM members WHERE {ACTIVE_MEMBER_SQL} ORDER BY COALESCE({member_name_expr}, '')")
            members = cur.fetchall()

            cur.execute(
                """
                SELECT id, meeting_uuid, topic, start_time
                FROM meetings
                WHERE meeting_uuid IS NOT NULL AND meeting_uuid <> ''
                ORDER BY id DESC
                LIMIT 300
                """
            )
            meetings = cur.fetchall()

    total_rows = len(rows)
    present_rows = sum(1 for r in rows if r.get("final_status") == "PRESENT")
    late_rows = sum(1 for r in rows if r.get("final_status") == "LATE")
    absent_rows = sum(1 for r in rows if r.get("final_status") == "ABSENT")
    unknown_rows = sum(1 for r in rows if not r.get("is_member"))
    member_rows = total_rows - unknown_rows
    avg_minutes = round(sum((r.get("total_seconds") or 0) for r in rows) / 60 / total_rows, 2) if total_rows else 0
    avg_rejoins = round(sum((r.get("rejoin_count") or 0) for r in rows) / total_rows, 2) if total_rows else 0
    attendance_health = round(((present_rows + late_rows) / total_rows) * 100, 2) if total_rows else 0

    by_person = {}
    by_meeting = {}

    for r in rows:
        key = r.get("participant_name") or "Unknown Participant"
        by_person.setdefault(
            key,
            {
                "name": key,
                "meetings": 0,
                "minutes": 0.0,
                "present": 0,
                "late": 0,
                "absent": 0,
                "rejoins": 0,
                "is_member": bool(r.get("is_member")),
                "member_id": r.get("member_id"),
                "is_host": bool(r.get("is_host")),
            },
        )
        by_person[key]["meetings"] += 1
        by_person[key]["minutes"] += (r.get("total_seconds") or 0) / 60
        by_person[key]["rejoins"] += (r.get("rejoin_count") or 0)
        if r.get("final_status") == "PRESENT":
            by_person[key]["present"] += 1
        elif r.get("final_status") == "LATE":
            by_person[key]["late"] += 1
        elif r.get("final_status") == "ABSENT":
            by_person[key]["absent"] += 1

        mk = r.get("meeting_uuid") or f"meeting_{r.get('meeting_row_id')}"
        by_meeting.setdefault(
            mk,
            {
                "meeting_uuid": r.get("meeting_uuid"),
                "topic": r.get("topic") or "Untitled Meeting",
                "start_time": r.get("start_time"),
                "present": 0,
                "late": 0,
                "absent": 0,
                "unknown": 0,
                "total": 0,
                "health": 0,
            },
        )
        by_meeting[mk]["total"] += 1
        if r.get("final_status") == "PRESENT":
            by_meeting[mk]["present"] += 1
        elif r.get("final_status") == "LATE":
            by_meeting[mk]["late"] += 1
        elif r.get("final_status") == "ABSENT":
            by_meeting[mk]["absent"] += 1
        if not r.get("is_member"):
            by_meeting[mk]["unknown"] += 1

    meeting_compare = list(by_meeting.values())[:30]
    for m in meeting_compare:
        total = m["total"] or 1
        m["health"] = round(((m["present"] + m["late"]) / total) * 100, 2)

    trend = compute_trend(rows, period_mode)
    prediction = predict_next_attendance(meeting_compare)

    selected_member_map = {int(m["id"]): member_display_name(m) for m in members if m.get("id") is not None}

    chart_rows = []
    chart_where = ["CAST(a.is_member AS TEXT) IN ('1','true','t','True','TRUE')", "a.member_id IS NOT NULL"]
    chart_params = []
    if from_date:
        chart_where.append("CAST(m.start_time AS TEXT)::date >= %s")
        chart_params.append(from_date)
    if to_date:
        chart_where.append("CAST(m.start_time AS TEXT)::date <= %s")
        chart_params.append(to_date)
    if filters.get("meeting_uuid"):
        chart_where.append("a.meeting_uuid = %s")
        chart_params.append(filters["meeting_uuid"])
    if member_ids:
        chart_where.append("a.member_id = ANY(%s)")
        chart_params.append(member_ids)
    if filters.get("person_name"):
        chart_where.append("lower(a.participant_name) LIKE %s")
        chart_params.append(f"%{filters['person_name'].strip().lower()}%")
    if filters.get("participant_type") == "host":
        chart_where.append("CAST(a.is_host AS TEXT) IN ('1','true','t','True','TRUE')")

    chart_sql = f"""
        SELECT a.member_id, a.participant_name, a.total_seconds, a.current_join, a.first_join, a.last_leave,
               a.rejoin_count, a.final_status, a.is_member, m.meeting_uuid, m.start_time, m.topic, m.id AS meeting_row_id
        FROM attendance a
        JOIN meetings m ON m.meeting_uuid = a.meeting_uuid
        WHERE {' AND '.join(chart_where)}
        ORDER BY m.id DESC, a.participant_name ASC
    """

    latest_meeting_label = None
    if not member_ids and not filters.get("meeting_uuid") and not from_date and not to_date and not filters.get("person_name") and filters.get("participant_type", "all") in ("all", "member"):
        latest_meeting_uuid = meetings[0]["meeting_uuid"] if meetings else None
        if latest_meeting_uuid:
            chart_rows = [r for r in rows if r.get("meeting_uuid") == latest_meeting_uuid and r.get("is_member")]
            if not chart_rows:
                with db() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            """
                            SELECT a.member_id, a.participant_name, a.total_seconds, a.current_join, a.first_join, a.last_leave,
                                   a.rejoin_count, a.final_status, a.is_member, m.meeting_uuid, m.start_time, m.topic, m.id AS meeting_row_id
                            FROM attendance a
                            JOIN meetings m ON m.meeting_uuid = a.meeting_uuid
                            WHERE CAST(a.is_member AS TEXT) IN ('1','true','t','True','TRUE') AND a.member_id IS NOT NULL AND a.meeting_uuid = %s
                            ORDER BY a.participant_name ASC
                            """,
                            (latest_meeting_uuid,),
                        )
                        chart_rows = cur.fetchall()
            latest_meeting = meetings[0] if meetings else None
            if latest_meeting:
                latest_meeting_label = f"{latest_meeting.get('topic') or 'Latest Meeting'} - {fmt_dt(latest_meeting.get('start_time'))}"
        chart_mode = "latest_meeting_all_members"
    else:
        with db() as conn:
            with conn.cursor() as cur:
                cur.execute(chart_sql, chart_params)
                chart_rows = cur.fetchall()
        chart_mode = "filtered_selection"

    member_duration_map = defaultdict(float)
    member_label_map = {}
    for r in chart_rows:
        member_id = r.get("member_id")
        if not member_id:
            continue
        duration_seconds = r.get("total_seconds") or 0
        if r.get("current_join"):
            chart_end = now_local()
            current_join = parse_dt(r.get("current_join"))
            if current_join:
                duration_seconds += max(int((chart_end - current_join).total_seconds()), 0)
        member_duration_map[int(member_id)] += duration_seconds / 60.0
        member_label_map[int(member_id)] = selected_member_map.get(int(member_id), r.get("participant_name") or f"Member {member_id}")

    if member_ids:
        ordered_member_ids = [mid for mid in member_ids if mid in member_label_map or mid in selected_member_map]
    else:
        ordered_member_ids = sorted(member_duration_map.keys(), key=lambda mid: member_label_map.get(mid, "").lower())

    member_duration_labels = []
    member_duration_values = []
    for mid in ordered_member_ids:
        label = member_label_map.get(mid) or selected_member_map.get(mid)
        if not label:
            continue
        member_duration_labels.append(label)
        member_duration_values.append(round(member_duration_map.get(mid, 0), 2))

    member_duration_chart = {
        "labels": member_duration_labels,
        "chart_values": member_duration_values,
        "empty": len(member_duration_labels) == 0,
        "subtitle": (
            f"Showing all members for latest meeting: {latest_meeting_label}" if chart_mode == "latest_meeting_all_members" and latest_meeting_label else
            "Showing selected members based on your current filters." if member_ids else
            "Showing members based on your current filters."
        ),
    }

    avg_minutes_reference = avg_minutes if avg_minutes > 0 else max((sum(member_duration_values) / len(member_duration_values)) if member_duration_values else 0, 1)

    enriched_people = []
    for person in by_person.values():
        attendance_score = calculate_attendance_score(person["present"], person["late"], person["absent"])
        engagement_score = calculate_engagement_score(
            person["minutes"],
            person["rejoins"],
            person["meetings"],
            person["present"],
            person["late"],
            person["absent"],
            avg_minutes_reference,
        )
        overall_score = clamp_score((attendance_score * 0.6) + (engagement_score * 0.4))
        risk = get_risk_level(overall_score)
        person["attendance_score"] = attendance_score
        person["engagement_score"] = engagement_score
        person["overall_score"] = overall_score
        person["risk"] = risk
        enriched_people.append(person)

    leaderboard = sorted(
        [p for p in enriched_people if p.get("is_member")],
        key=lambda x: (x["overall_score"], x["attendance_score"], x["engagement_score"], x["minutes"]),
        reverse=True,
    )
    risk_table = sorted(
        [p for p in leaderboard if p["risk"]["short"] in ("CRITICAL", "WARNING")],
        key=lambda x: (x["risk"]["short"] != "CRITICAL", x["overall_score"], x["minutes"]),
    )
    top_people = leaderboard[:5]
    low_people = sorted(
        [p for p in leaderboard],
        key=lambda x: (x["overall_score"], x["attendance_score"], -x["absent"], x["minutes"]),
    )[:5]
    unknown_board = sorted([v for v in enriched_people if not v["is_member"]], key=lambda x: x["meetings"], reverse=True)[:10]

    avg_attendance_score = round(sum(p["attendance_score"] for p in leaderboard) / len(leaderboard), 2) if leaderboard else 0
    avg_engagement_score = round(sum(p["engagement_score"] for p in leaderboard) / len(leaderboard), 2) if leaderboard else 0
    risk_members_count = sum(1 for p in leaderboard if p["risk"]["short"] in ("CRITICAL", "WARNING"))
    critical_members = [p for p in leaderboard if p["risk"]["short"] == "CRITICAL"]
    warning_members = [p for p in leaderboard if p["risk"]["short"] == "WARNING"]

    latest_meeting_summary = meeting_compare[0] if meeting_compare else None
    previous_meeting_summary = meeting_compare[1] if len(meeting_compare) > 1 else None
    comparison_delta = None
    if latest_meeting_summary and previous_meeting_summary:
        comparison_delta = round((latest_meeting_summary.get("health") or 0) - (previous_meeting_summary.get("health") or 0), 2)

    heatmap = build_heatmap_data(rows, member_ids=member_ids)
    unknown_match_suggestions = suggest_unknown_matches(unknown_board, members)
    reminder_suggestion = {
        "count": len(critical_members) + len(warning_members),
        "message": f"⚠️ {len(critical_members) + len(warning_members)} members missed or underperformed in the latest filtered view." if (critical_members or warning_members) else "No urgent reminder suggestion right now.",
        "names": [p["name"] for p in (critical_members + warning_members)[:6]],
    }
    insight_lines = build_insight_lines(
        {
            "attendance_health": attendance_health,
            "risk_members_count": risk_members_count,
        },
        meeting_compare,
        leaderboard,
        risk_table,
    )

    summary = {
        "total_rows": total_rows,
        "present_rows": present_rows,
        "late_rows": late_rows,
        "absent_rows": absent_rows,
        "unknown_rows": unknown_rows,
        "member_rows": member_rows,
        "avg_minutes": avg_minutes,
        "avg_rejoins": avg_rejoins,
        "predicted_next_attendance": prediction,
        "attendance_health": attendance_health,
        "avg_attendance_score": avg_attendance_score,
        "avg_engagement_score": avg_engagement_score,
        "risk_members_count": risk_members_count,
        "critical_members_count": len(critical_members),
        "warning_members_count": len(warning_members),
        "safe_members_count": sum(1 for p in leaderboard if p["risk"]["short"] == "SAFE"),
        "insight_lines": insight_lines,
    }

    return {
        "filters": filters,
        "rows": rows,
        "members": members,
        "all_members": all_members,
        "meetings": meetings,
        "summary": summary,
        "top_people": top_people,
        "low_people": low_people,
        "unknown_board": unknown_board,
        "meeting_compare": meeting_compare,
        "trend": trend,
        "member_duration_chart": member_duration_chart,
        "leaderboard": leaderboard[:10],
        "risk_table": risk_table[:12],
        "heatmap": heatmap,
        "unknown_match_suggestions": unknown_match_suggestions,
        "reminder_suggestion": reminder_suggestion,
        "latest_meeting_summary": latest_meeting_summary,
        "previous_meeting_summary": previous_meeting_summary,
        "comparison_delta": comparison_delta,
    }



def build_meeting_report_data(meeting_uuid):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM meetings WHERE meeting_uuid=%s", (meeting_uuid,))
            meeting = cur.fetchone()
            if not meeting:
                return None

            cur.execute("SELECT * FROM attendance WHERE meeting_uuid=%s ORDER BY participant_name", (meeting_uuid,))
            attendance_rows = cur.fetchall()

            cur.execute(f"SELECT * FROM members WHERE {ACTIVE_MEMBER_SQL} ORDER BY id")
            active_members = cur.fetchall()

    start_time = parse_dt(meeting.get("start_time")) or now_local()
    end_time = parse_dt(meeting.get("end_time"))
    derived_last_activity = get_meeting_rows_last_activity(attendance_rows)
    if not end_time:
        end_time = derived_last_activity or now_local()
    elif derived_last_activity and derived_last_activity >= start_time and end_time > derived_last_activity:
        end_time = derived_last_activity
    if end_time < start_time:
        end_time = start_time

    meeting_total_seconds = max(int((end_time - start_time).total_seconds()), 0)
    present_threshold_minutes = round(meeting_total_seconds / 60 * get_setting("present_percentage", int) / 100.0, 2)
    late_summary_threshold_minutes = round(max((end_time - start_time).total_seconds(), 0) / 60 * get_setting("late_count_as_present_percentage", int) / 100.0, 2)

    joined_member_ids = set()
    joined_actual_count = 0
    report_rows = []
    present_members_count = 0
    absent_members_count = 0
    unknown_participants_count = 0

    for row in attendance_rows:
        final_status = row.get("final_status")
        if not final_status:
            final_status, total_seconds = classify_row_for_meeting(row, start_time, end_time)
        else:
            total_seconds = get_row_effective_total_seconds(row, end_time)
        total_seconds = max(0, min(int(total_seconds or 0), meeting_total_seconds))

        join_seen = bool(row.get("first_join") or row.get("last_leave") or row.get("current_join") or total_seconds > 0)
        if join_seen:
            joined_actual_count += 1

        if row.get("member_id"):
            joined_member_ids.add(row.get("member_id"))

        display_status = "HOST" if row.get("is_host") else final_status

        if row.get("is_member") and final_status == "PRESENT":
            present_members_count += 1
        if (not row.get("is_member")) and join_seen:
            unknown_participants_count += 1

        report_rows.append(
            {
                "participant_name": row.get("participant_name") or "-",
                "join_display": fmt_time_ampm(row.get("first_join")) if row.get("first_join") else "-",
                "leave_display": fmt_time_ampm(row.get("last_leave")) if row.get("last_leave") else "-",
                "duration_minutes": mins_from_seconds(total_seconds),
                "rejoin_count": row.get("rejoin_count") or 0,
                "status": display_status,
                "is_unknown_joined": (not row.get("is_member")) and join_seen,
            }
        )

    for member in active_members:
        if member.get("id") not in joined_member_ids:
            absent_members_count += 1
            report_rows.append(
                {
                    "participant_name": member_display_name(member),
                    "join_display": "-",
                    "leave_display": "-",
                    "duration_minutes": 0.0,
                    "rejoin_count": 0,
                    "status": "ABSENT",
                    "is_unknown_joined": False,
                }
            )

    def status_order(item):
        order = {"HOST": 0, "PRESENT": 1, "LATE": 2, "ABSENT": 3}
        return (order.get(item["status"], 99), item["participant_name"].lower())

    report_rows = sorted(report_rows, key=status_order)

    summary = {
        "topic": meeting.get("topic") or "Zoom Meeting",
        "meeting_id": meeting.get("meeting_id") or "-",
        "date": fmt_date(start_time),
        "start_time": fmt_time_ampm(start_time),
        "end_time": fmt_time_ampm(end_time),
        "meeting_duration_minutes": mins_from_seconds(int((end_time - start_time).total_seconds())),
        "total_participants": joined_actual_count,
        "total_members": len(active_members),
        "total_present_members": present_members_count,
        "total_absent_members": absent_members_count,
        "total_unknown_participants": unknown_participants_count,
        "present_threshold_minutes": present_threshold_minutes,
        "late_summary_threshold_minutes": late_summary_threshold_minutes,
    }
    return {"meeting": meeting, "rows": report_rows, "summary": summary}


def build_meeting_pdf_filename(report_data):
    summary = report_data["summary"]
    date_part = summary.get("date") or fmt_date(now_local())
    start_part = (summary.get("start_time") or "-").replace(":", "-").replace(" ", "_")
    end_part = (summary.get("end_time") or "-").replace(":", "-").replace(" ", "_")
    return f"{date_part}_from_{start_part}_to_{end_part}.pdf"


def export_meeting_pdf_bytes(title, report_data):
    meeting = report_data["meeting"]
    rows = report_data["rows"]
    summary = report_data["summary"]

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter, leftMargin=28, rightMargin=28, topMargin=28, bottomMargin=28)
    styles = getSampleStyleSheet()
    elements = []

    title_style = styles["Title"]
    title_style.alignment = 1
    elements.append(Paragraph(f"<b>{title}</b>", title_style))
    elements.append(Spacer(1, 12))

    info_lines = [
        f"Topic: {summary['topic']}",
        f"Meeting ID: {summary['meeting_id']}",
        f"Date: {summary['date']}",
        f"Start Time: {summary['start_time']}",
        f"End Time: {summary['end_time']}",
        f"Total Meeting Duration: {summary['meeting_duration_minutes']} minutes",
    ]
    for line in info_lines:
        elements.append(Paragraph(line, styles["Normal"]))

    elements.append(Spacer(1, 10))

    table_data = [["Name", "Join", "Leave", "Duration", "Rejoins", "Status"]]
    for row in rows:
        name_value = row["participant_name"]
        if row["is_unknown_joined"]:
            name_value = f'<font color="red">{name_value}</font>'
        table_data.append(
            [
                Paragraph(name_value, styles["Normal"]),
                row["join_display"],
                row["leave_display"],
                str(row["duration_minutes"]),
                str(row["rejoin_count"]),
                row["status"],
            ]
        )

    table = Table(table_data, repeatRows=1, colWidths=[190, 75, 75, 60, 55, 65])
    table_style = TableStyle(
        [
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0f172a")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.black),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("ALIGN", (1, 0), (-1, -1), "CENTER"),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
            ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ]
    )

    for i in range(1, len(table_data)):
        status = rows[i - 1]["status"]
        if status == "PRESENT":
            table_style.add("TEXTCOLOR", (5, i), (5, i), colors.green)
        elif status == "LATE":
            table_style.add("TEXTCOLOR", (5, i), (5, i), colors.orange)
        elif status == "ABSENT":
            table_style.add("TEXTCOLOR", (5, i), (5, i), colors.red)
        elif status == "HOST":
            table_style.add("TEXTCOLOR", (5, i), (5, i), colors.HexColor("#1d4ed8"))

    table.setStyle(table_style)
    elements.append(table)
    elements.append(Spacer(1, 12))

    summary_lines = [
        f"Total Participants: {summary['total_participants']}",
        f"Total Members: {summary['total_members']}",
        f"Total Present Members: {summary['total_present_members']}",
        f"Total Absent Members: {summary['total_absent_members']}",
        f"Total Unknown Participants: {summary['total_unknown_participants']}",
    ]
    for line in summary_lines:
        elements.append(Paragraph(line, styles["Normal"]))

    elements.append(Spacer(1, 10))

    criteria_data = [[
        Paragraph(
            "<b>■ Attendance Criteria</b><br/>"
            "■ Present = Duration ≥ 75% of total meeting duration<br/>"
            "■ Late = Duration &lt; 75% of total meeting duration<br/>"
            "■ Absent = Did not join the meeting (for added members only)<br/><br/>"
            f"<b>■ Present Threshold For This Meeting: {summary['present_threshold_minutes']} minutes</b><br/>"
            f"<b>■ Late counted as present in summary if Duration &gt; {summary['late_summary_threshold_minutes']} minutes</b>",
            styles["Normal"],
        )
    ]]
    criteria_table = Table(criteria_data, colWidths=[540])
    criteria_table.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 1, colors.black),
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
                ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                ("TOPPADDING", (0, 0), (-1, -1), 8),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
            ]
        )
    )
    elements.append(criteria_table)

    doc.build(elements)
    buffer.seek(0)
    return buffer.getvalue()


def export_csv_bytes(rows):
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(
        [
            "Meeting Topic",
            "Meeting ID",
            "Meeting Start",
            "Participant",
            "Email",
            "Member",
            "Host",
            "First Join",
            "Last Leave",
            "Duration (Min)",
            "Rejoins",
            "Final Status",
        ]
    )
    for r in rows:
        writer.writerow(
            [
                r.get("topic") or "",
                r.get("meeting_id") or "",
                fmt_dt(r.get("start_time")),
                r.get("participant_name") or "",
                r.get("participant_email") or "",
                "Yes" if r.get("is_member") else "No",
                "Yes" if r.get("is_host") else "No",
                fmt_dt(r.get("first_join")),
                fmt_dt(r.get("last_leave")),
                mins_from_seconds(r.get("total_seconds")),
                r.get("rejoin_count") or 0,
                r.get("final_status") or "-",
            ]
        )
    return out.getvalue().encode("utf-8")


def export_pdf_bytes(title, rows, summary):
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter, leftMargin=28, rightMargin=28, topMargin=28, bottomMargin=28)
    styles = getSampleStyleSheet()
    elements = [Paragraph(f"<b>{title}</b>", styles["Title"]), Spacer(1, 12)]

    elements.append(Paragraph(f"Generated: {fmt_dt(now_local())}", styles["Normal"]))
    elements.append(
        Paragraph(
            f"Total: {summary.get('total_rows', 0)} | Present: {summary.get('present_rows', 0)} | Late: {summary.get('late_rows', 0)} | "
            f"Absent: {summary.get('absent_rows', 0)} | Unknown: {summary.get('unknown_rows', 0)} | Avg Minutes: {summary.get('avg_minutes', 0)}",
            styles["Normal"],
        )
    )
    elements.append(
        Paragraph(
            f"Attendance Health: {summary.get('attendance_health', 0)}% | Avg Attendance Score: {summary.get('avg_attendance_score', 0)} | "
            f"Avg Engagement Score: {summary.get('avg_engagement_score', 0)} | Risk Members: {summary.get('risk_members_count', 0)}",
            styles["Normal"],
        )
    )
    elements.append(Spacer(1, 12))

    for insight in summary.get("insight_lines", [])[:4]:
        elements.append(Paragraph(f"• {insight}", styles["Normal"]))
    if summary.get("insight_lines"):
        elements.append(Spacer(1, 10))

    data = [["Topic", "Participant", "Member", "Duration", "Rejoins", "Status"]]
    for r in rows[:140]:
        data.append(
            [
                (r.get("topic") or "")[:20],
                (r.get("participant_name") or "")[:18],
                "Yes" if r.get("is_member") else "No",
                str(mins_from_seconds(r.get("total_seconds"))),
                str(r.get("rejoin_count") or 0),
                r.get("final_status") or ("HOST" if r.get("is_host") else "-"),
            ]
        )

    table = Table(data, repeatRows=1, colWidths=[120, 110, 55, 65, 55, 70])
    style = TableStyle(
        [
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0f172a")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
            ("ALIGN", (0, 0), (-1, -1), "CENTER"),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ]
    )

    for i in range(1, len(data)):
        status = data[i][5]
        if status == "PRESENT":
            style.add("TEXTCOLOR", (5, i), (5, i), colors.green)
        elif status == "LATE":
            style.add("TEXTCOLOR", (5, i), (5, i), colors.orange)
        elif status == "ABSENT":
            style.add("TEXTCOLOR", (5, i), (5, i), colors.red)
        elif status == "HOST":
            style.add("TEXTCOLOR", (5, i), (5, i), colors.HexColor("#1d4ed8"))

    table.setStyle(style)
    elements.append(table)
    elements.append(Spacer(1, 12))

    criteria_data = [[
        Paragraph(
            "<b>Attendance intelligence notes</b><br/>"
            "• Attendance Score model: Present = +10, Late = +5, Absent = -10, normalized to 0–100<br/>"
            "• Engagement Score model: duration quality + lower rejoins + consistency, normalized to 0–100<br/>"
            "• Risk Level: 80–100 Safe, 50–79 Warning, below 50 Critical<br/>"
            "• Green = healthy, Yellow = caution, Red = poor / critical, Blue = informational.",
            styles["Normal"],
        )
    ]]
    criteria_table = Table(criteria_data, colWidths=[540])
    criteria_table.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 1, colors.black),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
    ]))
    elements.append(criteria_table)

    doc.build(elements)
    buffer.seek(0)
    return buffer.getvalue()



def verify_zoom_signature(req):
    if not ZOOM_SECRET_TOKEN:
        return True
    timestamp = req.headers.get("x-zm-request-timestamp", "")
    signature = req.headers.get("x-zm-signature", "")
    body = req.get_data(as_text=True)
    message = f"v0:{timestamp}:{body}".encode("utf-8")
    secret = ZOOM_SECRET_TOKEN.encode("utf-8")
    computed = "v0=" + hmac.new(secret, message, hashlib.sha256).hexdigest()
    return hmac.compare_digest(computed, signature)


BASE_HTML = """
<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{{ title }}</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <style>
        :root{
            --nav:#081226;
            --nav2:#0f172a;
            --bg1:#eef4ff;
            --bg2:#f8fbff;
            --card:#ffffff;
            --card2:rgba(255,255,255,.94);
            --text:#0f172a;
            --muted:#64748b;
            --primary:#2563eb;
            --primary2:#1d4ed8;
            --success:#16a34a;
            --warn:#f59e0b;
            --danger:#dc2626;
            --line:#e5e7eb;
            --soft:#eff6ff;
            --shadow:0 14px 32px rgba(15,23,42,.10);
            --radius:22px;
        }
        body.dark{
            --nav:#020617;
            --nav2:#0f172a;
            --bg1:#0b1220;
            --bg2:#121a2c;
            --card:#0f172a;
            --card2:rgba(15,23,42,.92);
            --text:#e5eefc;
            --muted:#9fb2d3;
            --primary:#3b82f6;
            --primary2:#1d4ed8;
            --success:#22c55e;
            --warn:#fbbf24;
            --danger:#ef4444;
            --line:#23314a;
            --soft:#16233b;
            --shadow:0 14px 32px rgba(2,6,23,.35);
        }
        *{box-sizing:border-box}
        body{
            margin:0;
            font-family:Arial,sans-serif;
            color:var(--text);
            background:linear-gradient(135deg,var(--bg1),var(--bg2));
            transition:background .25s ease,color .25s ease;
        }
        .fade-in{animation:fadeIn .35s ease}
        @keyframes fadeIn{
            from{opacity:.0;transform:translateY(6px)}
            to{opacity:1;transform:none}
        }
        .topbar{
            background:linear-gradient(90deg,var(--nav),var(--nav2));
            color:#fff;
            padding:14px 20px;
            display:flex;
            justify-content:space-between;
            align-items:center;
            position:sticky;
            top:0;
            z-index:30;
            box-shadow:0 4px 18px rgba(2,6,23,.22);
        }
        .brand{font-size:17px;font-weight:800;letter-spacing:.2px}
        .wrap{display:flex;min-height:calc(100vh - 56px)}
        .sidebar{
            width:210px;
            background:rgba(255,255,255,.72);
            backdrop-filter:blur(10px);
            padding:16px 14px;
            border-right:1px solid rgba(148,163,184,.25);
        }
        body.dark .sidebar{background:rgba(8,15,30,.72)}
        .sidebar a{
            display:flex;
            align-items:center;
            gap:8px;
            padding:12px 12px;
            color:var(--text);
            text-decoration:none;
            border-radius:14px;
            margin-bottom:8px;
            font-weight:700;
            transition:.18s ease;
        }
        .sidebar a:hover,.sidebar a.active{
            background:#dbeafe;
            color:#1d4ed8;
            transform:translateX(2px);
        }
        body.dark .sidebar a:hover, body.dark .sidebar a.active{
            background:#18365f;
            color:#bfdbfe;
        }
        .content{flex:1;padding:20px}
        .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:16px}
        .card{
            background:var(--card2);
            border:1px solid rgba(148,163,184,.16);
            border-radius:var(--radius);
            padding:18px;
            box-shadow:var(--shadow);
            color:var(--text);
        }
        .hero{
            background:linear-gradient(135deg,#0f172a,#1d4ed8);
            color:#fff;
            border-radius:26px;
            padding:20px;
            box-shadow:var(--shadow);
            margin-bottom:18px;
        }
        body.dark .hero{background:linear-gradient(135deg,#0b1220,#1d4ed8)}
        .hero h2,.card h3,.card h4{margin:0 0 10px 0}
        .metric{font-size:30px;font-weight:800;margin-top:8px}
        .muted{color:var(--muted);font-size:13px}
        .row{display:flex;gap:10px;flex-wrap:wrap}
        .toolbar{display:flex;gap:10px;flex-wrap:wrap;margin-top:12px}
        .badge{
            display:inline-block;
            padding:6px 10px;
            border-radius:999px;
            font-size:12px;
            font-weight:800;
        }
        .ok{background:#dcfce7;color:#166534}
        .warn{background:#fef3c7;color:#92400e}
        .danger{background:#fee2e2;color:#991b1b}
        .info{background:#dbeafe;color:#1d4ed8}
        .gray{background:#e2e8f0;color:#334155}
        body.dark .ok{background:#123524;color:#86efac}
        body.dark .warn{background:#4a3414;color:#fde68a}
        body.dark .danger{background:#4a1d1d;color:#fecaca}
        body.dark .info{background:#18365f;color:#bfdbfe}
        body.dark .gray{background:#1f2937;color:#cbd5e1}
        .table-wrap{
            width:100%;
            overflow-x:auto;
            border-radius:16px;
            border:1px solid var(--line);
            background:var(--card);
        }
        table{
            width:100%;
            border-collapse:collapse;
            min-width:720px;
            background:transparent;
        }
        th,td{
            padding:11px 12px;
            border-bottom:1px solid var(--line);
            text-align:left;
            font-size:14px;
            vertical-align:top;
        }
        th{
            background:var(--soft);
            font-weight:800;
            color:var(--text);
            position:static;
        }
        td.long{
            word-break:break-word;
            white-space:normal;
            min-width:240px;
        }
        input,select,textarea{
            width:100%;
            padding:11px 12px;
            border-radius:12px;
            border:1px solid #cbd5e1;
            margin-top:6px;
            margin-bottom:10px;
            background:#fff;
            color:#0f172a;
        }
        body.dark input, body.dark select, body.dark textarea{
            background:#0b1220;
            color:#e5eefc;
            border-color:#334155;
        }
        textarea{min-height:90px;resize:vertical}
        button,.btn{
            background:linear-gradient(180deg,var(--primary),var(--primary2));
            color:#fff;
            border:none;
            padding:10px 14px;
            border-radius:12px;
            cursor:pointer;
            font-weight:800;
            text-decoration:none;
            display:inline-flex;
            align-items:center;
            justify-content:center;
            white-space:nowrap;
            transition:transform .15s ease, box-shadow .15s ease;
            box-shadow:0 8px 18px rgba(37,99,235,.18);
        }
        button:hover,.btn:hover{transform:translateY(-1px)}
        .btn.secondary{background:linear-gradient(180deg,#334155,#1f2937)}
        .btn.success{background:linear-gradient(180deg,#22c55e,#15803d)}
        .btn.warn{background:linear-gradient(180deg,#fbbf24,#d97706);color:#111827}
        .btn.danger{background:linear-gradient(180deg,#ef4444,#b91c1c)}
        .btn.purple{background:linear-gradient(180deg,#8b5cf6,#6d28d9)}
        .btn.small{padding:8px 10px;font-size:12px}
        .flash{
            padding:12px 14px;
            border-radius:14px;
            margin-bottom:12px;
            font-weight:700;
            border:1px solid transparent;
        }
        .flash.success{background:#dcfce7;color:#166534;border-color:#86efac}
        .flash.error{background:#fee2e2;color:#991b1b;border-color:#fca5a5}
        .login-wrap{
            min-height:100vh;
            display:grid;
            place-items:center;
            padding:20px;
        }
        .login-box{
            width:100%;
            max-width:980px;
            display:grid;
            grid-template-columns:1.1fr .9fr;
            gap:22px;
            align-items:stretch;
        }
        .login-side{
            background:linear-gradient(135deg,#0f172a,#1d4ed8);
            color:#fff;
            border-radius:28px;
            padding:30px;
            box-shadow:var(--shadow);
        }
        .login-card{
            background:var(--card2);
            border:1px solid rgba(148,163,184,.18);
            border-radius:28px;
            padding:28px;
            box-shadow:var(--shadow);
        }
        .login-error{
            background:#fee2e2;
            color:#991b1b;
            border:1px solid #fca5a5;
            border-radius:12px;
            padding:10px 12px;
            margin:10px 0 14px 0;
            font-weight:700;
        }
        .debug-box{
            background:#fff7ed;
            color:#9a3412;
            border:1px solid #fdba74;
            border-radius:12px;
            padding:14px;
            white-space:pre-wrap;
            word-break:break-word;
            font-family:monospace;
        }
        .stat-card{position:relative;overflow:hidden}
        .stat-card::after{
            content:"";
            position:absolute;
            right:-18px;top:-18px;
            width:78px;height:78px;border-radius:50%;
            background:rgba(37,99,235,.08);
        }
        .top-actions{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
        .kpi-note{font-size:12px;color:var(--muted)}
        .toggle-form{display:inline-flex;align-items:center}
        .toggle-switch{position:relative;width:98px;height:42px;border:none;border-radius:999px;padding:0;cursor:pointer;box-shadow:inset 0 2px 6px rgba(255,255,255,.14),0 10px 22px rgba(15,23,42,.22);overflow:hidden;background:linear-gradient(135deg,#ef4444,#dc2626);transition:transform .16s ease, box-shadow .16s ease}
        .toggle-switch.on{background:linear-gradient(135deg,#22c55e,#16a34a)}
        .toggle-switch.off{background:linear-gradient(135deg,#ef4444,#dc2626)}
        .toggle-switch .toggle-knob{position:absolute;top:4px;left:4px;width:34px;height:34px;border-radius:50%;background:linear-gradient(180deg,#ffffff,#e2e8f0);box-shadow:0 4px 12px rgba(2,6,23,.25);transition:left .2s ease}
        .toggle-switch.on .toggle-knob{left:60px}
        .toggle-switch .toggle-icon{position:absolute;top:50%;transform:translateY(-50%);font-size:19px;font-weight:900;line-height:1;color:rgba(255,255,255,.92);text-shadow:0 1px 2px rgba(0,0,0,.22)}
        .toggle-switch .toggle-on{left:16px}
        .toggle-switch .toggle-off{right:16px}

        .purple{background:#ede9fe;color:#6d28d9}
        body.dark .purple{background:#312e81;color:#ddd6fe}
        .glass-card{background:rgba(255,255,255,.76);backdrop-filter:blur(16px);border:1px solid rgba(255,255,255,.35);box-shadow:0 18px 40px rgba(37,99,235,.10)}
        body.dark .glass-card{background:rgba(15,23,42,.78);border-color:rgba(148,163,184,.18);box-shadow:0 20px 42px rgba(0,0,0,.35)}
        .premium-hero{display:flex;justify-content:space-between;gap:16px;align-items:flex-start;flex-wrap:wrap}
        .hero-subtext{max-width:760px}
        .hero-pills{display:flex;gap:8px;flex-wrap:wrap}
        .section-title{font-size:18px;font-weight:800;margin:6px 0 12px 0;color:var(--text)}
        .analytics-filter-grid{grid-template-columns:repeat(auto-fit,minmax(200px,1fr));align-items:start}
        .member-multiselect-wrap{grid-column:span 2}
        .multi-member-box{border:1px solid var(--line);border-radius:16px;background:var(--card);padding:10px 12px;max-height:220px}
        .premium-scroll{overflow:auto}
        .multi-member-title{font-weight:700;margin-bottom:10px;color:var(--muted);font-size:13px}
        .multi-member-list{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:8px}
        .member-check-item{display:flex;align-items:center;gap:10px;padding:8px 10px;border-radius:12px;cursor:pointer;transition:.18s ease;background:transparent}
        .member-check-item:hover{background:rgba(37,99,235,.08)}
        .member-check-item input{display:none}
        .member-check-circle{width:18px;height:18px;border-radius:999px;border:2px solid #94a3b8;display:inline-block;position:relative;flex:0 0 auto}
        .member-check-item input:checked + .member-check-circle{border-color:var(--primary);background:var(--primary)}
        .member-check-item input:checked + .member-check-circle::after{content:'';position:absolute;inset:4px;border-radius:999px;background:#fff}
        .member-check-text{font-weight:600;font-size:13px}
        .kpi-grid{grid-template-columns:repeat(auto-fit,minmax(190px,1fr))}
        .score-good{box-shadow:0 14px 34px rgba(34,197,94,.12)}
        .score-warn{box-shadow:0 14px 34px rgba(245,158,11,.12)}
        .score-bad{box-shadow:0 14px 34px rgba(239,68,68,.12)}
        .score-neutral{box-shadow:0 14px 34px rgba(59,130,246,.12)}
        .score-purple{box-shadow:0 14px 34px rgba(139,92,246,.12)}
        .insight-card{min-height:160px}
        .insight-list{margin:10px 0 0 18px;padding:0}
        .insight-list li{margin-bottom:8px}
        .reminder-callout{padding:12px 14px;border-radius:14px;background:rgba(245,158,11,.12);border:1px solid rgba(245,158,11,.25);font-weight:700;margin-top:8px}
        .heatmap-grid{display:grid;grid-template-columns:repeat(14,1fr);gap:6px;margin-top:14px}
        .heat-cell{width:100%;aspect-ratio:1;border-radius:6px;border:1px solid rgba(148,163,184,.18)}
        .heat-good{background:rgba(34,197,94,.9)}
        .heat-warn{background:rgba(245,158,11,.9)}
        .heat-bad{background:rgba(239,68,68,.9)}
        .heat-none{background:rgba(148,163,184,.18)}
        .empty-state{padding:28px 16px;text-align:center;border:1px dashed var(--line);border-radius:16px;color:var(--muted);margin-top:12px}
        .hover-row{transition:background .16s ease, transform .16s ease}
        .hover-row:hover{background:rgba(37,99,235,.06)}
        .tooltip{display:inline-flex;align-items:center;justify-content:center;width:18px;height:18px;border-radius:999px;background:var(--soft);color:var(--primary);font-size:11px;font-weight:800;cursor:help;position:relative}
        .tooltip:hover::after{content:attr(data-tip);position:absolute;left:50%;top:-8px;transform:translate(-50%,-100%);min-width:220px;max-width:260px;background:#0f172a;color:#fff;padding:8px 10px;border-radius:10px;font-size:11px;line-height:1.4;z-index:50;box-shadow:var(--shadow)}

        @media (max-width: 920px){
            .wrap{display:block}
            .sidebar{width:100%;border-right:none;border-bottom:1px solid rgba(148,163,184,.25)}
            .login-box{grid-template-columns:1fr}
        }
    </style>
</head>
<body class="{{ 'dark' if session.get('theme') == 'dark' else 'light' }}">
{% if session.get('user_id') %}
<div class="topbar">
    <div class="brand">Zoom Attendance Platform</div>
    <div class="top-actions">
        <span class="badge info">{{ session.get('username') }} ({{ session.get('role') }})</span>
        <a class="btn secondary small" href="{{ url_for('toggle_theme') }}">🌓 {{ 'Light' if session.get('theme') == 'dark' else 'Dark' }} Mode</a>
        <a class="btn secondary small" href="{{ url_for('profile') }}">Profile</a>
        <a class="btn secondary small" href="{{ url_for('logout') }}">Logout</a>
    </div>
</div>
<div class="wrap">
    <div class="sidebar fade-in">
        {% for item in nav %}
            <a href="{{ item.href }}" class="{% if item.key == active %}active{% endif %}">{{ item.label }}</a>
        {% endfor %}
    </div>
    <div class="content fade-in">
        {% with messages = get_flashed_messages(with_categories=true) %}
            {% for category, message in messages %}
                <div class="flash {{ category }}">{{ message }}</div>
            {% endfor %}
        {% endwith %}
        {{ body|safe }}
    </div>
</div>
{% else %}
<div class="login-wrap fade-in">
    {{ body|safe }}
</div>
{% endif %}
</body>
</html>
"""


def page(title, body, active="home"):
    nav = [
        {"key": "home", "label": "🏠 Home", "href": url_for("home")},
        {"key": "live", "label": "🟢 Live", "href": url_for("live")},
        {"key": "members", "label": "👥 Members", "href": url_for("members")},
        {"key": "users", "label": "🔐 Users", "href": url_for("users")},
        {"key": "analytics", "label": "📊 Analytics", "href": url_for("analytics")},
        {"key": "meetings", "label": "📂 Meetings", "href": url_for("meetings")},
        {"key": "settings", "label": "⚙️ Settings", "href": url_for("settings")},
        {"key": "activity", "label": "📝 Activity", "href": url_for("activity")},
        {"key": "profile", "label": "🙍 Profile", "href": url_for("profile")},
    ]
    return render_template_string(BASE_HTML, title=title, body=body, nav=nav, active=active)


@app.before_request
def startup_once():
    global DB_INITIALIZED
    if not DB_INITIALIZED:
        init_db()
        fix_database_compatibility()
        DB_INITIALIZED = True


@app.errorhandler(Exception)
def handle_any_error(e):
    body = render_template_string(
        """
        <div class="card" style="max-width:1100px;margin:10px auto">
            <h2>Something went wrong</h2>
            <p class="muted">Copy this error and send it to me.</p>
            <div class="debug-box">{{ error_text }}</div>
            <br>
            <a class="btn" href="{{ url_for('login') }}">Back to Login</a>
        </div>
        """,
        error_text=str(e),
    )
    return render_template_string(BASE_HTML, title="Error", body=body, nav=[], active=""), 500


@app.route("/toggle-theme")
@login_required
def toggle_theme():
    session["theme"] = "light" if session.get("theme") == "dark" else "dark"
    return redirect(request.referrer or url_for("home"))


@app.route("/", methods=["GET", "HEAD"])
def index():
    if session.get("user_id"):
        return redirect(url_for("home"))
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    login_error = None

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        with db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT * FROM users WHERE username=%s AND {ACTIVE_USER_SQL}",
                    (username,),
                )
                user = cur.fetchone()

        if user and user["password_hash"] == hash_password(password):
            session["user_id"] = user["id"]
            session["username"] = user["username"]
            session["role"] = user["role"]
            if "theme" not in session:
                session["theme"] = "light"
            log_activity("login", f"{username} logged in")
            return redirect(url_for("home"))

        login_error = "Invalid username or password"
        flash("Invalid username or password", "error")

    body = render_template_string(
        """
        <div class="login-box">
            <div class="login-side">
                <h1 style="margin:0 0 14px 0">Zoom Attendance Platform</h1>
                <p style="color:#dbeafe;line-height:1.7">
                    Track Zoom meeting attendance with live monitoring, member vs non-member distinction,
                    strong analytics, exportable reports, role-based login, and professional dashboard UI.
                </p>
                <div class="row" style="margin-top:20px">
                    <span class="badge info">Live Tracking</span>
                    <span class="badge ok">Reports</span>
                    <span class="badge warn">Analytics</span>
                    <span class="badge gray">Role Based Access</span>
                </div>
            </div>
            <div class="login-card">
                <h2 style="margin-top:0">Welcome Back</h2>
                <p class="muted">Login to continue to your attendance dashboard.</p>

                {% if login_error %}
                    <div class='login-error'>{{ login_error }}</div>
                {% endif %}

                <form method='post'>
                    <label>Username</label>
                    <input name='username' required value='{{ request.form.get("username", "") if request.method == "POST" else "" }}'>
                    <label>Password</label>
                    <input type='password' name='password' required>
                    <button type='submit' style="width:100%">Login</button>
                </form>
            </div>
        </div>
        """,
        login_error=login_error,
        request=request,
    )
    return render_template_string(BASE_HTML, title="Login", body=body, nav=[], active="")


@app.route("/logout")
def logout():
    log_activity("logout", f"{session.get('username')} logged out")
    session.clear()
    return redirect(url_for("login"))


@app.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    if request.method == "POST":
        current_password = request.form.get("current_password", "")
        new_password = request.form.get("new_password", "")
        confirm_password = request.form.get("confirm_password", "")

        with db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM users WHERE id=%s", (session["user_id"],))
                user = cur.fetchone()

                if not user or user["password_hash"] != hash_password(current_password):
                    flash("Current password is incorrect.", "error")
                    return redirect(url_for("profile"))

                if not new_password or len(new_password) < 4:
                    flash("New password must be at least 4 characters.", "error")
                    return redirect(url_for("profile"))

                if new_password != confirm_password:
                    flash("New password and confirm password do not match.", "error")
                    return redirect(url_for("profile"))

                cur.execute(
                    "UPDATE users SET password_hash=%s WHERE id=%s",
                    (hash_password(new_password), session["user_id"]),
                )
            conn.commit()

        log_activity("profile_password_change", session.get("username"))
        flash("Password updated successfully.", "success")
        return redirect(url_for("profile"))

    body = render_template_string(
        """
        <div class="hero">
            <h2>My Profile</h2>
            <div class="muted" style="color:#cbd5e1">Manage your account and password safely.</div>
        </div>
        <div class="grid">
            <div class="card">
                <h3>Account Details</h3>
                <p><b>Username:</b> {{ session.get('username') }}</p>
                <p><b>Role:</b> {{ session.get('role') }}</p>
            </div>
            <div class="card">
                <h3>Change Password</h3>
                <form method="post">
                    <label>Current Password</label>
                    <input type="password" name="current_password" required>
                    <label>New Password</label>
                    <input type="password" name="new_password" required>
                    <label>Confirm New Password</label>
                    <input type="password" name="confirm_password" required>
                    <button type="submit">Update Password</button>
                </form>
            </div>
        </div>
        """
    )
    return page("Profile", body, "profile")


@app.route("/home")
@login_required
def home():
    try:
        maybe_finalize_stale_live_meetings()
    except Exception as e:
        print(f"⚠️ home stale finalization skipped: {e}")

    live_info = read_live_snapshot()

    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS c FROM meetings")
            total_meetings = cur.fetchone()["c"]

            cur.execute("SELECT COUNT(*) AS c FROM members")
            total_members = cur.fetchone()["c"]

            cur.execute(f"SELECT COUNT(*) AS c FROM members WHERE {ACTIVE_MEMBER_SQL}")
            active_members = cur.fetchone()["c"]

            cur.execute("SELECT COUNT(*) AS c FROM attendance WHERE final_status='PRESENT'")
            present = cur.fetchone()["c"]

            cur.execute("SELECT COUNT(*) AS c FROM attendance WHERE final_status='LATE'")
            late = cur.fetchone()["c"]

            cur.execute("SELECT COUNT(*) AS c FROM attendance WHERE final_status='ABSENT'")
            absent = cur.fetchone()["c"]

            cur.execute("SELECT * FROM meetings ORDER BY id DESC LIMIT 6")
            recent_meetings = cur.fetchall()

            cur.execute("SELECT * FROM activity_log ORDER BY id DESC LIMIT 10")
            recent_activity = cur.fetchall()

    total_classified = present + late + absent
    health = round(((present + late) / total_classified) * 100, 2) if total_classified else 0

    body = render_template_string(
        """
        <div class="hero">
            <h2>Attendance Platform Overview</h2>
            <div class="muted" style="color:#cbd5e1">
                Accurate tracking, member distinction, reports, analytics, and multi-page dashboard working together.
            </div>
        </div>

        <div class='grid'>
            <div class='card stat-card'><h4>Total Meetings</h4><div class='metric'>{{ total_meetings }}</div></div>
            <div class='card stat-card'><h4>Active Members</h4><div class='metric'>{{ active_members }}</div><div class='kpi-note'>Total members: {{ total_members }}</div></div>
            <div class='card stat-card'><h4>Attendance Health</h4><div class='metric'>{{ health }}%</div><div class='kpi-note'>Present + Late across finalized records</div></div>
            <div class='card stat-card'><h4>Live Status</h4><div class='metric'>{{ 'LIVE' if live_info else 'IDLE' }}</div><div class='kpi-note'>Current monitoring status</div></div>
        </div>

        <br>

        <div class='grid'>
            <div class='card'>
                <h3>Latest Meeting Spotlight</h3>
                {% if recent_meetings %}
                    <div><b>{{ recent_meetings[0].topic or 'Untitled Meeting' }}</b></div>
                    <div class='muted'>{{ fmt_dt(recent_meetings[0].start_time) }}</div>
                    <div class='row' style='margin-top:12px'>
                        <span class='badge ok'>Present {{ recent_meetings[0].present_count or 0 }}</span>
                        <span class='badge warn'>Late {{ recent_meetings[0].late_count or 0 }}</span>
                        <span class='badge danger'>Absent {{ recent_meetings[0].absent_count or 0 }}</span>
                        <span class='badge info'>Unknown {{ recent_meetings[0].unknown_participants or 0 }}</span>
                    </div>
                {% else %}
                    <div class='muted'>No meetings yet.</div>
                {% endif %}
            </div>

            <div class='card'>
                <h3>Quick Actions</h3>
                <div class='toolbar'>
                    <a class='btn' href='{{ url_for("live") }}'>Open Live</a>
                    <a class='btn success' href='{{ url_for("analytics") }}'>Open Analytics</a>
                    <a class='btn secondary' href='{{ url_for("meetings") }}'>Open Meetings</a>
                    {% if session.get("role") == "admin" %}
                    <a class='btn warn' href='{{ url_for("settings") }}'>Settings</a>
                    {% endif %}
                </div>
            </div>
        </div>

        <br>

        <div class='grid'>
            <div class='card'>
                <h3>Recent Meetings</h3>
                <div class="table-wrap">
                    <table>
                        <tr><th>Date</th><th>Topic</th><th>Status</th><th>Participants</th></tr>
                        {% for m in recent_meetings %}
                        <tr>
                            <td>{{ fmt_dt(m.start_time) }}</td>
                            <td>{{ m.topic or 'Untitled Meeting' }}</td>
                            <td>{{ m.status or '-' }}</td>
                            <td>{{ m.unique_participants or 0 }}</td>
                        </tr>
                        {% endfor %}
                    </table>
                </div>
            </div>

            <div class='card'>
                <h3>Recent Activity</h3>
                <div class="table-wrap">
                    <table>
                        <tr><th>When</th><th>Action</th><th>Details</th></tr>
                        {% for a in recent_activity %}
                        <tr>
                            <td>{{ fmt_dt(a.created_at) }}</td>
                            <td>{{ a.action }}</td>
                            <td class="long">{{ a.details }}</td>
                        </tr>
                        {% endfor %}
                    </table>
                </div>
            </div>
        </div>
        """,
        total_meetings=total_meetings,
        total_members=total_members,
        active_members=active_members,
        health=health,
        live_info=live_info,
        recent_meetings=recent_meetings,
        recent_activity=recent_activity,
        fmt_dt=fmt_dt,
        fmt_time_ampm=fmt_time_ampm,
        member_display_name=member_display_name,
        session=session,
    )
    return page("Home", body, "home")


@app.route("/live")
@login_required
def live():
    maybe_finalize_stale_live_meetings()
    info = read_live_snapshot()

    if not info:
        body = """
        <meta http-equiv='refresh' content='2'>
        <div class='hero'>
            <h2>Live Dashboard</h2>
            <div class='muted' style='color:#cbd5e1'>No live meeting is active right now.</div>
        </div>
        <div class='card'><div class='muted'>Start a Zoom meeting and send webhook events to see live tracking here.</div></div>
        """
        return page("Live", body, "live")

    meeting = info["meeting"]
    participants = info["participants"]
    not_joined = info["not_joined_members"]

    rows_for_live = []
    start_dt = parse_dt(meeting.get("start_time")) or now_local()
    for p in participants:
        live_status, live_total = get_live_status_for_row(p, start_dt)
        rows_for_live.append(
            {
                "participant_name": p.get("participant_name"),
                "first_join": p.get("first_join"),
                "last_leave": p.get("last_leave"),
                "duration_min": mins_from_seconds(live_total),
                "rejoin_count": p.get("rejoin_count") or 0,
                "status": live_status,
            }
        )

    body = render_template_string(
        """
        <meta http-equiv='refresh' content='2'>
        <div class="hero">
            <h2>Live Dashboard</h2>
            <div class="muted" style="color:#cbd5e1">Auto refresh every 2 seconds.</div>
        </div>

        <div class='grid'>
            <div class='card stat-card'><h4>Topic</h4><div class='metric' style='font-size:22px'>{{ meeting.topic or 'Untitled Meeting' }}</div></div>
            <div class='card stat-card'><h4>Meeting ID</h4><div class='metric'>{{ meeting.meeting_id or '-' }}</div></div>
            <div class='card stat-card'><h4>Joined Only Count</h4><div class='metric'>{{ live_rows|length }}</div></div>
            <div class='card stat-card'><h4>Active Now</h4><div class='metric'>{{ active_now }}</div><div class='kpi-note'>Host present: {{ host_now }}</div></div>
        </div>

        <br>

        <div class='grid' style='grid-template-columns:minmax(0,1.72fr) minmax(260px,0.68fr);gap:12px;align-items:start;'>
            <div class='card'>
                <h3>Live Participants</h3>
                <div class="table-wrap">
                    <table>
                        <tr>
                            <th>Name</th>
                            <th>Join</th>
                            <th>Leave</th>
                            <th>Duration (Min)</th>
                            <th>Rejoins</th>
                            <th>Status</th>
                        </tr>
                        {% for p in live_rows %}
                        <tr>
                            <td>{{ p.participant_name }}</td>
                            <td>{{ fmt_time_ampm(p.first_join) }}</td>
                            <td>{{ fmt_time_ampm(p.last_leave) if p.last_leave else '-' }}</td>
                            <td>{{ p.duration_min }}</td>
                            <td>{{ p.rejoin_count }}</td>
                            <td>
                                {% if p.status == 'HOST' %}
                                    <span class='badge info'>Host</span>
                                {% elif p.status == 'PRESENT' %}
                                    <span class='badge ok'>Present</span>
                                {% elif p.status == 'LATE' %}
                                    <span class='badge warn'>Late</span>
                                {% else %}
                                    <span class='badge danger'>Absent</span>
                                {% endif %}
                            </td>
                        </tr>
                        {% endfor %}
                    </table>
                </div>
            </div>

            <div class='card'>
                <h3>Active Members Who Didn't Joined Yet</h3>
                <div class="table-wrap">
                    <table>
                        <tr><th>Name</th><th>Phone</th></tr>
                        {% for m in not_joined %}
                        <tr>
                            <td>{{ member_display_name(m) }}</td>
                            <td>{{ m.phone or '-' }}</td>
                        </tr>
                        {% endfor %}
                    </table>
                </div>
            </div>
        </div>

        {% if session.get('role') == 'admin' and meeting.meeting_uuid %}
        <br>
        <div class='card'>
            <h3>Manual Finalize</h3>
            <div class='muted'>Use this if Zoom ends but webhook is delayed.</div>
            <br>
            <a class='btn danger' href='{{ url_for("manual_finalize_meeting", meeting_uuid=meeting.meeting_uuid) }}'>Finalize Current Live Meeting</a>
        </div>
        {% endif %}
        """,
        meeting=meeting,
        live_rows=rows_for_live,
        active_now=sum(1 for p in participants if p.get("current_join")),
        host_now="Yes" if any(p.get("is_host") and p.get("current_join") is not None for p in participants) else "No",
        not_joined=not_joined,
        fmt_dt=fmt_dt,
        fmt_time_ampm=fmt_time_ampm,
        member_display_name=member_display_name,
        session=session,
    )
    return page("Live", body, "live")


@app.route("/meetings/<path:meeting_uuid>/finalize")
@login_required
@admin_required
def manual_finalize_meeting(meeting_uuid):
    finalize_meeting(meeting_uuid, now_local())
    log_activity("manual_finalize_meeting", meeting_uuid)
    flash("Meeting finalized successfully.", "success")
    return redirect(url_for("meetings"))


@app.route("/meetings/<path:meeting_uuid>/delete", methods=["POST"])
@login_required
@admin_required
def delete_meeting(meeting_uuid):
    if not meeting_uuid:
        flash("Meeting UUID missing for this record.", "error")
        return redirect(url_for("meetings"))

    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM attendance WHERE meeting_uuid=%s", (meeting_uuid,))
            cur.execute("DELETE FROM meetings WHERE meeting_uuid=%s", (meeting_uuid,))
        conn.commit()

    log_activity("meeting_delete", meeting_uuid)
    flash("Meeting deleted successfully.", "success")
    return redirect(url_for("meetings"))


@app.route("/members", methods=["GET", "POST"])
@login_required
def members():
    if request.method == "POST":
        action = request.form.get("action")

        if action == "add" and can_edit_users():
            full_name = request.form.get("full_name", "").strip()
            email = None
            phone = request.form.get("phone", "").strip() or None

            if full_name:
                with db() as conn:
                    true_val = db_true_value(conn, "members", "active")
                    with conn.cursor() as cur:
                        insert_member_record(cur, conn, full_name, email, phone, true_val)
                    conn.commit()
                log_activity("member_add", full_name)
                flash("Member added successfully.", "success")

        elif action == "edit" and can_edit_users():
            member_id = int(request.form.get("member_id"))
            full_name = request.form.get("full_name", "").strip()
            email = request.form.get("email", "").strip() or None
            phone = request.form.get("phone", "").strip() or None
            with db() as conn:
                with conn.cursor() as cur:
                    update_member_record(cur, conn, member_id, full_name, email, phone)
                conn.commit()
            log_activity("member_edit", str(member_id))
            flash("Member updated successfully.", "success")

        elif action == "toggle" and can_edit_users():
            member_id = int(request.form.get("member_id"))
            with db() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT active FROM members WHERE id=%s", (member_id,))
                    row = cur.fetchone()
                    if row:
                        next_val = db_false_value(conn, "members", "active") if is_truthy(row["active"]) else db_true_value(conn, "members", "active")
                        cur.execute("UPDATE members SET active=%s WHERE id=%s", (next_val, member_id))
                conn.commit()
            log_activity("member_toggle", str(member_id))
            flash("Member status updated.", "success")

        elif action == "delete" and can_edit_users():
            member_id = int(request.form.get("member_id"))
            with db() as conn:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM members WHERE id=%s", (member_id,))
                conn.commit()
            log_activity("member_delete", str(member_id))
            flash("Member deleted successfully.", "success")

        elif action == "import_csv" and can_edit_users():
            file = request.files.get("csv_file")
            imported = 0
            if file:
                stream = io.StringIO(file.stream.read().decode("utf-8"))
                reader = csv.DictReader(stream)
                with db() as conn:
                    true_val = db_true_value(conn, "members", "active")
                    with conn.cursor() as cur:
                        for row in reader:
                            name = (row.get("full_name") or row.get("name") or "").strip()
                            if not name:
                                continue
                            email = (row.get("email") or "").strip() or None
                            phone = (row.get("phone") or "").strip() or None
                            insert_member_record(cur, conn, name, email, phone, true_val)
                            imported += 1
                    conn.commit()
                log_activity("member_import", f"Imported {imported} members")
                flash(f"Imported {imported} members.", "success")

        return redirect(url_for("members"))

    q = request.args.get("q", "").strip().lower()
    edit_id = request.args.get("edit_id", "").strip()

    with db() as conn:
        with conn.cursor() as cur:
            member_name_field = member_name_sql(conn)
            cur.execute("SELECT COUNT(*) AS c FROM members")
            total_members_count = cur.fetchone()["c"]
            cur.execute(f"SELECT COUNT(*) AS c FROM members WHERE {ACTIVE_MEMBER_SQL}")
            active_members_count = cur.fetchone()["c"]
            inactive_members_count = total_members_count - active_members_count

            if q:
                cur.execute(
                    f"SELECT * FROM members WHERE (lower(COALESCE({member_name_field}, '')) LIKE %s OR lower(COALESCE(email,'')) LIKE %s OR lower(COALESCE(phone,'')) LIKE %s) ORDER BY id DESC",
                    (f"%{q}%", f"%{q}%", f"%{q}%"),
                )
            else:
                cur.execute("SELECT * FROM members ORDER BY id DESC")
            rows = cur.fetchall()

            edit_member = None
            if edit_id:
                cur.execute("SELECT * FROM members WHERE id=%s", (int(edit_id),))
                edit_member = cur.fetchone()

    body = render_template_string(
        """
        <div class="hero">
            <h2>Members</h2>
            <div class="muted" style="color:#cbd5e1">Manage members, import CSV, and maintain clean member vs non-member distinction.</div>
        </div>

        <div class='grid'>
            <div class='card stat-card'><h4>Total Members</h4><div class='metric'>{{ total_members_count }}</div></div>
            <div class='card stat-card'><h4>Active Members</h4><div class='metric'>{{ active_members_count }}</div></div>
            <div class='card stat-card'><h4>Inactive Members</h4><div class='metric'>{{ inactive_members_count }}</div></div>
        </div>

        <br>

        <div class='grid'>
            <div class='card'>
                <h3>{{ 'Edit Member' if edit_member else 'Add Member' }}</h3>
                {% if session.get('role') == 'admin' %}
                <form method='post'>
                    <input type='hidden' name='action' value='{{ "edit" if edit_member else "add" }}'>
                    {% if edit_member %}<input type='hidden' name='member_id' value='{{ edit_member.id }}'>{% endif %}
                    <label>Full Name</label>
                    <input name='full_name' required value='{{ member_display_name(edit_member) if edit_member else "" }}'>
                    {% if edit_member %}
                    <label>Email</label>
                    <input name='email' value='{{ edit_member.email if edit_member else "" }}'>
                    {% endif %}
                    <label>Phone</label>
                    <input name='phone' value='{{ edit_member.phone if edit_member else "" }}'>
                    <button type='submit'>{{ 'Update Member' if edit_member else 'Save Member' }}</button>
                    {% if edit_member %}
                        <a class='btn secondary' href='{{ url_for("members") }}'>Cancel</a>
                    {% endif %}
                </form>
                {% else %}
                <div class='muted'>Viewer can only view members.</div>
                {% endif %}
            </div>

            <div class='card'>
                <h3>CSV Import</h3>
                <div class='muted'>Expected columns: full_name, email, phone</div>
                {% if session.get('role') == 'admin' %}
                <form method='post' enctype='multipart/form-data'>
                    <input type='hidden' name='action' value='import_csv'>
                    <input type='file' name='csv_file' accept='.csv' required>
                    <button type='submit' class='btn success'>Import CSV</button>
                </form>
                {% endif %}
            </div>
        </div>

        <br>

        <div class='card'>
            <h3>Search Members</h3>
            <form method='get'>
                <input name='q' value='{{ q }}' placeholder='Search by name, email or phone'>
                <button type='submit'>Search</button>
            </form>

            <br>

            <div class="table-wrap">
                <table>
                    <tr>
                        <th>Name</th><th>Email</th><th>Phone</th><th>Status</th>
                        {% if session.get('role') == 'admin' %}<th>Actions</th>{% endif %}
                    </tr>
                    {% for m in rows %}
                        <tr>
                            <td>{{ member_display_name(m) }}</td>
                            <td>{{ m.email or '-' }}</td>
                            <td>{{ m.phone or '-' }}</td>
                            <td>
                                {% if m.active|string in ['1', 'True', 'true', 't'] %}
                                    <span class='badge ok'>Active</span>
                                {% else %}
                                    <span class='badge danger'>Inactive</span>
                                {% endif %}
                            </td>
                            {% if session.get('role') == 'admin' %}
                            <td>
                                <div class='row'>
                                    <a class='btn secondary small' href='{{ url_for("members", edit_id=m.id) }}'>Edit</a>
                                    <form method='post' class='toggle-form'>
                                        <input type='hidden' name='action' value='toggle'>
                                        <input type='hidden' name='member_id' value='{{ m.id }}'>
                                        <button type='submit' class='toggle-switch {% if m.active|string in ['1', 'True', 'true', 't'] %}on{% else %}off{% endif %}' aria-label='Toggle member status'>
                                            <span class='toggle-knob'></span>
                                            <span class='toggle-icon toggle-on'>✓</span>
                                            <span class='toggle-icon toggle-off'>✕</span>
                                        </button>
                                    </form>
                                    <form method='post' onsubmit='return confirm("Delete this member?")'>
                                        <input type='hidden' name='action' value='delete'>
                                        <input type='hidden' name='member_id' value='{{ m.id }}'>
                                        <button type='submit' class='btn danger small'>Delete</button>
                                    </form>
                                </div>
                            </td>
                            {% endif %}
                        </tr>
                    {% endfor %}
                </table>
            </div>
        </div>
        """,
        rows=rows,
        q=q,
        edit_member=edit_member,
        member_display_name=member_display_name,
        total_members_count=total_members_count,
        active_members_count=active_members_count,
        inactive_members_count=inactive_members_count,
        session=session,
    )
    return page("Members", body, "members")


@app.route("/users", methods=["GET", "POST"])
@login_required
@admin_required
def users():
    if request.method == "POST":
        action = request.form.get("action")

        if action == "add":
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            role = request.form.get("role", "viewer")
            if username and password:
                with db() as conn:
                    true_val = db_true_value(conn, "users", "is_active")
                    with conn.cursor() as cur:
                        cur.execute(
                            "INSERT INTO users(username, password_hash, role, is_active) VALUES (%s,%s,%s,%s)",
                            (username, hash_password(password), role, true_val),
                        )
                    conn.commit()
                log_activity("user_add", username)
                flash("User created.", "success")

        elif action == "edit":
            user_id = int(request.form.get("user_id"))
            username = request.form.get("username", "").strip()
            role = request.form.get("role", "viewer")
            with db() as conn:
                with conn.cursor() as cur:
                    cur.execute("UPDATE users SET username=%s, role=%s WHERE id=%s", (username, role, user_id))
                conn.commit()
            log_activity("user_edit", str(user_id))
            flash("User updated.", "success")

        elif action == "toggle":
            user_id = int(request.form.get("user_id"))
            with db() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT is_active, username FROM users WHERE id=%s", (user_id,))
                    row = cur.fetchone()
                    if row:
                        if row["username"] == session.get("username"):
                            flash("You cannot disable your own active session.", "error")
                            return redirect(url_for("users"))
                        next_val = db_false_value(conn, "users", "is_active") if is_truthy(row["is_active"]) else db_true_value(conn, "users", "is_active")
                        cur.execute("UPDATE users SET is_active=%s WHERE id=%s", (next_val, user_id))
                conn.commit()
            log_activity("user_toggle", str(user_id))
            flash("User status updated.", "success")

        elif action == "password":
            user_id = int(request.form.get("user_id"))
            new_password = request.form.get("new_password", "")
            if new_password:
                with db() as conn:
                    with conn.cursor() as cur:
                        cur.execute("UPDATE users SET password_hash=%s WHERE id=%s", (hash_password(new_password), user_id))
                    conn.commit()
                log_activity("user_password", str(user_id))
                flash("Password changed.", "success")

        elif action == "delete":
            user_id = int(request.form.get("user_id"))
            with db() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT username FROM users WHERE id=%s", (user_id,))
                    row = cur.fetchone()
                    if row:
                        if row["username"] == session.get("username"):
                            flash("You cannot delete your own account while logged in.", "error")
                            return redirect(url_for("users"))
                        cur.execute("DELETE FROM users WHERE id=%s", (user_id,))
                conn.commit()
            log_activity("user_delete", str(user_id))
            flash("User deleted.", "success")

        return redirect(url_for("users"))

    edit_id = request.args.get("edit_id", "").strip()
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM users ORDER BY id DESC")
            rows = cur.fetchall()

            edit_user = None
            if edit_id:
                cur.execute("SELECT * FROM users WHERE id=%s", (int(edit_id),))
                edit_user = cur.fetchone()

    body = render_template_string(
        """
        <div class="hero">
            <h2>Users & Roles</h2>
            <div class="muted" style="color:#cbd5e1">Manage admin/viewer access, reset passwords, delete users, and control activity safely.</div>
        </div>

        <div class='grid'>
            <div class='card'>
                <h3>{{ 'Edit User' if edit_user else 'Create User' }}</h3>
                <form method='post'>
                    <input type='hidden' name='action' value='{{ "edit" if edit_user else "add" }}'>
                    {% if edit_user %}<input type='hidden' name='user_id' value='{{ edit_user.id }}'>{% endif %}
                    <label>Username</label>
                    <input name='username' required value='{{ edit_user.username if edit_user else "" }}'>
                    {% if not edit_user %}
                    <label>Password</label>
                    <input name='password' required>
                    {% endif %}
                    <label>Role</label>
                    <select name='role'>
                        <option value='viewer' {% if edit_user and edit_user.role == 'viewer' %}selected{% endif %}>viewer</option>
                        <option value='admin' {% if edit_user and edit_user.role == 'admin' %}selected{% endif %}>admin</option>
                    </select>
                    <button type='submit'>{{ 'Update User' if edit_user else 'Create' }}</button>
                    {% if edit_user %}
                        <a class='btn secondary' href='{{ url_for("users") }}'>Cancel</a>
                    {% endif %}
                </form>
            </div>

            <div class='card'>
                <h3>Role Guide</h3>
                <div class='muted'>
                    Admin can manage members, users, settings, imports, and finalization.
                    Viewer can safely view live dashboard, meetings, analytics, and reports.
                </div>
            </div>
        </div>

        <br>

        <div class='card'>
            <div class="table-wrap">
                <table>
                    <tr><th>Username</th><th>Role</th><th>Status</th><th>Created</th><th>Actions</th></tr>
                    {% for u in rows %}
                    <tr>
                        <td>{{ u.username }}</td>
                        <td>{{ u.role }}</td>
                        <td>
                            {% if u.is_active|string in ['1', 'True', 'true', 't'] %}
                                <span class='badge ok'>Active</span>
                            {% else %}
                                <span class='badge danger'>Disabled</span>
                            {% endif %}
                        </td>
                        <td>{{ fmt_dt(u.created_at) }}</td>
                        <td>
                            <div class='row'>
                                <a class='btn secondary small' href='{{ url_for("users", edit_id=u.id) }}'>Edit</a>
                                <form method='post'>
                                    <input type='hidden' name='action' value='toggle'>
                                    <input type='hidden' name='user_id' value='{{ u.id }}'>
                                    <button class='btn warn small' type='submit'>Toggle</button>
                                </form>
                                <form method='post'>
                                    <input type='hidden' name='action' value='password'>
                                    <input type='hidden' name='user_id' value='{{ u.id }}'>
                                    <input name='new_password' placeholder='new password' required>
                                    <button class='btn secondary small' type='submit'>Reset Password</button>
                                </form>
                                <form method='post' onsubmit='return confirm("Delete this user?")'>
                                    <input type='hidden' name='action' value='delete'>
                                    <input type='hidden' name='user_id' value='{{ u.id }}'>
                                    <button class='btn danger small' type='submit'>Delete</button>
                                </form>
                            </div>
                        </td>
                    </tr>
                    {% endfor %}
                </table>
            </div>
        </div>
        """,
        rows=rows,
        fmt_dt=fmt_dt,
        edit_user=edit_user,
    )
    return page("Users", body, "users")


@app.route("/analytics")
@login_required
def analytics():
    maybe_finalize_stale_live_meetings()

    filters = {
        "period_mode": request.args.get("period_mode", "custom"),
        "from_date": request.args.get("from_date", ""),
        "to_date": request.args.get("to_date", ""),
        "meeting_uuid": request.args.get("meeting_uuid", ""),
        "member_ids": request.args.getlist("member_ids"),
        "person_name": request.args.get("person_name", ""),
        "participant_type": request.args.get("participant_type", "all"),
    }

    data = analytics_data(filters)
    trend = data["trend"]
    member_chart = data["member_duration_chart"]
    export_query = build_filter_query(data["filters"])
    export_csv_url = url_for("export_analytics_csv") + (f"?{export_query}" if export_query else "")
    export_pdf_url = url_for("export_analytics_pdf") + (f"?{export_query}" if export_query else "")

    body = render_template_string(
        """
        <div class="hero">
            <h2>Advanced Analytics</h2>
            <div class="muted" style="color:#cbd5e1">Filter by day, week, month, year, or custom range and export matching reports.</div>
        </div>

        <div class='card'>
            <form method='get'>
                <div class='grid'>
                    <div>
                        <label>Period Mode</label>
                        <select name='period_mode'>
                            <option value='day' {% if filters.period_mode == 'day' %}selected{% endif %}>Day</option>
                            <option value='week' {% if filters.period_mode == 'week' %}selected{% endif %}>Week</option>
                            <option value='month' {% if filters.period_mode == 'month' %}selected{% endif %}>Month</option>
                            <option value='year' {% if filters.period_mode == 'year' %}selected{% endif %}>Year</option>
                            <option value='custom' {% if filters.period_mode == 'custom' %}selected{% endif %}>Custom</option>
                        </select>
                    </div>
                    <div><label>From Date</label><input type='date' name='from_date' value='{{ filters.from_date }}'></div>
                    <div><label>To Date</label><input type='date' name='to_date' value='{{ filters.to_date }}'></div>
                    <div>
                        <label>Meeting</label>
                        <select name='meeting_uuid'>
                            <option value=''>All meetings</option>
                            {% for m in data.meetings %}
                            <option value='{{ m.meeting_uuid }}' {% if filters.meeting_uuid == m.meeting_uuid %}selected{% endif %}>
                                {{ m.topic or 'Untitled Meeting' }} - {{ fmt_dt(m.start_time) }}
                            </option>
                            {% endfor %}
                        </select>
                    </div>
                    <div>
                        <label>Members</label>
                        <div class='multi-member-box'>
                            <div class='multi-member-title'>Select one or more members</div>
                            <div class='multi-member-list'>
                                {% for m in data.members %}
                                <label class='member-check-item'>
                                    <input type='checkbox' name='member_ids' value='{{ m.id }}' {% if (m.id|string) in filters.member_ids %}checked{% endif %}>
                                    <span class='member-check-circle'></span>
                                    <span class='member-check-text'>{{ member_display_name(m) }}</span>
                                </label>
                                {% endfor %}
                            </div>
                        </div>
                    </div>
                    <div><label>Person Search</label><input name='person_name' value='{{ filters.person_name }}' placeholder='type participant name'></div>
                    <div>
                        <label>Participant Type</label>
                        <select name='participant_type'>
                            <option value='all' {% if filters.participant_type == 'all' %}selected{% endif %}>All</option>
                            <option value='member' {% if filters.participant_type == 'member' %}selected{% endif %}>Members</option>
                            <option value='unknown' {% if filters.participant_type == 'unknown' %}selected{% endif %}>Unknown / non-member</option>
                            <option value='host' {% if filters.participant_type == 'host' %}selected{% endif %}>Host</option>
                        </select>
                    </div>
                </div>
                <div class='toolbar'>
                    <button type='submit'>Apply Filters</button>
                    <a class='btn success' href='{{ export_csv_url }}'>Export CSV</a>
                    <a class='btn secondary' href='{{ export_pdf_url }}'>Export PDF</a>
                </div>
            </form>
        </div>

        <br>

        <div class='grid'>
            <div class='card stat-card'><h4>Total Rows</h4><div class='metric'>{{ data.summary.total_rows }}</div></div>
            <div class='card stat-card'><h4>Present</h4><div class='metric'>{{ data.summary.present_rows }}</div></div>
            <div class='card stat-card'><h4>Late</h4><div class='metric'>{{ data.summary.late_rows }}</div></div>
            <div class='card stat-card'><h4>Absent</h4><div class='metric'>{{ data.summary.absent_rows }}</div></div>
            <div class='card stat-card'><h4>Unknown</h4><div class='metric'>{{ data.summary.unknown_rows }}</div></div>
            <div class='card stat-card'><h4>Predicted Next</h4><div class='metric'>{{ data.summary.predicted_next_attendance }}</div><div class='kpi-note'>simple recent average</div></div>
        </div>

        <br>

        <div class='grid'>
            <div class='card'>
                <h3>Attendance Trend</h3>
                <canvas id='trendChart'></canvas>
            </div>
            <div class='card'>
                <h3>Status Mix</h3>
                <canvas id='statusChart'></canvas>
            </div>
        </div>

        <br>

        <div class='card'>
            <h3>Member Duration Comparison</h3>
            <div class='muted'>{{ member_chart.subtitle }}</div>
            <br>
            {% if member_chart.empty %}
                <div class='empty-analytics-state'>No member duration data found for the selected filters.</div>
            {% else %}
                <canvas id='memberDurationChart' height='120'></canvas>
            {% endif %}
        </div>

        <br>

        <div class='grid'>
            <div class='card'>
                <h3>Top Performers</h3>
                <div class="table-wrap">
                    <table>
                        <tr><th>Name</th><th>Meetings</th><th>Minutes</th><th>Present</th><th>Rejoins</th></tr>
                        {% for p in data.top_people %}
                        <tr><td>{{ p.name }}</td><td>{{ p.meetings }}</td><td>{{ p.minutes|round(2) }}</td><td>{{ p.present }}</td><td>{{ p.rejoins }}</td></tr>
                        {% endfor %}
                    </table>
                </div>
            </div>
            <div class='card'>
                <h3>Low Performers</h3>
                <div class="table-wrap">
                    <table>
                        <tr><th>Name</th><th>Present</th><th>Late</th><th>Absent</th></tr>
                        {% for p in data.low_people %}
                        <tr><td>{{ p.name }}</td><td>{{ p.present }}</td><td>{{ p.late }}</td><td>{{ p.absent }}</td></tr>
                        {% endfor %}
                    </table>
                </div>
            </div>
        </div>

        <br>

        <div class='grid'>
            <div class='card'>
                <h3>Unknown Participant Leaderboard</h3>
                <div class="table-wrap">
                    <table>
                        <tr><th>Name</th><th>Meetings</th><th>Minutes</th><th>Rejoins</th></tr>
                        {% for p in data.unknown_board %}
                        <tr><td>{{ p.name }}</td><td>{{ p.meetings }}</td><td>{{ p.minutes|round(2) }}</td><td>{{ p.rejoins }}</td></tr>
                        {% endfor %}
                    </table>
                </div>
            </div>
            <div class='card'>
                <h3>Meeting Comparison</h3>
                <div class="table-wrap">
                    <table>
                        <tr><th>Meeting</th><th>Date</th><th>Present</th><th>Late</th><th>Absent</th><th>Unknown</th><th>Health</th></tr>
                        {% for m in data.meeting_compare %}
                        <tr>
                            <td>{{ m.topic }}</td>
                            <td>{{ fmt_dt(m.start_time) }}</td>
                            <td>{{ m.present }}</td>
                            <td>{{ m.late }}</td>
                            <td>{{ m.absent }}</td>
                            <td>{{ m.unknown }}</td>
                            <td>{{ m.health }}%</td>
                        </tr>
                        {% endfor %}
                    </table>
                </div>
            </div>
        </div>

        <br>

        <div class='card'>
            <h3>Filtered Attendance Rows</h3>
            <div class="table-wrap">
                <table>
                    <tr><th>Topic</th><th>Participant</th><th>Member</th><th>Duration</th><th>Rejoins</th><th>Status</th></tr>
                    {% for r in data.rows[:180] %}
                    <tr>
                        <td>{{ r.topic }}</td>
                        <td>{{ r.participant_name }}</td>
                        <td>{% if r.is_member %}Yes{% else %}No{% endif %}</td>
                        <td>{{ mins_from_seconds(r.total_seconds) }}</td>
                        <td>{{ r.rejoin_count or 0 }}</td>
                        <td>{{ r.final_status }}</td>
                    </tr>
                    {% endfor %}
                </table>
            </div>
        </div>

        <style>
            .multi-member-box{border:1px solid var(--line);border-radius:16px;padding:10px 12px;background:var(--card)}
            .multi-member-title{font-size:12px;color:var(--muted);margin-bottom:8px}
            .multi-member-list{max-height:160px;overflow:auto;display:flex;flex-direction:column;gap:8px;padding-right:4px}
            .member-check-item{display:flex;align-items:center;gap:10px;cursor:pointer;padding:6px 4px;border-radius:10px}
            .member-check-item:hover{background:var(--soft)}
            .member-check-item input{display:none}
            .member-check-circle{width:18px;height:18px;border-radius:50%;border:2px solid #93c5fd;display:inline-flex;align-items:center;justify-content:center;position:relative;flex-shrink:0}
            .member-check-circle::after{content:'';width:8px;height:8px;border-radius:50%;background:var(--primary);transform:scale(0);transition:.15s ease}
            .member-check-item input:checked + .member-check-circle::after{transform:scale(1)}
            .member-check-text{font-size:13px}
            .empty-analytics-state{border:1px dashed var(--line);border-radius:16px;padding:24px;text-align:center;color:var(--muted);background:var(--soft)}
        </style>

        <script>
        new Chart(document.getElementById('trendChart'), {
            type:'line',
            data:{
                labels: {{ trend.labels|tojson }},
                datasets:[
                    {label:'Present', data: {{ trend.present|tojson }}, tension:0.25},
                    {label:'Late', data: {{ trend.late|tojson }}, tension:0.25},
                    {label:'Absent', data: {{ trend.absent|tojson }}, tension:0.25}
                ]
            },
            options:{responsive:true}
        });

        new Chart(document.getElementById('statusChart'), {
            type:'doughnut',
            data:{
                labels:['Present','Late','Absent','Unknown'],
                datasets:[{
                    data:[
                        {{ data.summary.present_rows }},
                        {{ data.summary.late_rows }},
                        {{ data.summary.absent_rows }},
                        {{ data.summary.unknown_rows }}
                    ]
                }]
            },
            options:{responsive:true}
        });

        {% if not member_chart.empty %}
        new Chart(document.getElementById('memberDurationChart'), {
            type:'bar',
            data:{
                labels: {{ member_chart.labels|tojson }},
                datasets:[{
                    label:'Minutes',
                    data: {{ member_chart.chart_values|tojson }},
                    borderWidth:1,
                    borderRadius:8
                }]
            },
            options:{
                responsive:true,
                scales:{
                    y:{beginAtZero:true,title:{display:true,text:'Minutes'}},
                    x:{title:{display:true,text:'Members'}}
                }
            }
        });
        {% endif %}
        </script>
        """,
        data=data,
        filters=data["filters"],
        fmt_dt=fmt_dt,
        mins_from_seconds=mins_from_seconds,
        trend=trend,
        member_chart=member_chart,
        member_display_name=member_display_name,
        export_csv_url=export_csv_url,
        export_pdf_url=export_pdf_url,
    )
    return page("Analytics", body, "analytics")



@app.route("/analytics/reminder")
@login_required
def analytics_reminder():
    filters = {
        "period_mode": request.args.get("period_mode", "custom"),
        "from_date": request.args.get("from_date", ""),
        "to_date": request.args.get("to_date", ""),
        "meeting_uuid": request.args.get("meeting_uuid", ""),
        "member_ids": request.args.getlist("member_ids"),
        "person_name": request.args.get("person_name", ""),
        "participant_type": request.args.get("participant_type", "all"),
    }
    data = analytics_data(filters)
    names = data["reminder_suggestion"].get("names") or []
    if names:
        flash("Reminder suggestion prepared for: " + ", ".join(names), "success")
    else:
        flash("No urgent reminder targets found in the current filtered view.", "success")
    query = build_filter_query(data["filters"])
    return redirect(url_for("analytics") + (f"?{query}" if query else ""))

@app.route("/analytics/export.csv")
@login_required
def export_analytics_csv():
    filters = dict(request.args)
    filters["member_ids"] = request.args.getlist("member_ids")
    data = analytics_data(filters)
    content = export_csv_bytes(data["rows"])
    filename = f"analytics_{slugify(now_local().strftime('%Y%m%d_%H%M%S'))}.csv"
    return Response(content, mimetype="text/csv", headers={"Content-Disposition": f"attachment; filename={filename}"})


@app.route("/analytics/export.pdf")
@login_required
def export_analytics_pdf():
    filters = dict(request.args)
    filters["member_ids"] = request.args.getlist("member_ids")
    data = analytics_data(filters)
    pdf = export_pdf_bytes("Filtered Analytics Report", data["rows"], data["summary"])
    return send_file(io.BytesIO(pdf), download_name="analytics_report.pdf", mimetype="application/pdf", as_attachment=True)


@app.route("/meetings")
@login_required
def meetings():
    maybe_finalize_stale_live_meetings()

    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM meetings ORDER BY id DESC LIMIT 250")
            rows = cur.fetchall()

    body = render_template_string(
        """
        <div class="hero">
            <h2>Meetings</h2>
            <div class="muted" style="color:#cbd5e1">View meeting summaries and download meeting-level reports.</div>
        </div>

        <div class='card'>
            <div class="table-wrap">
                <table>
                    <tr>
                        <th>Date</th>
                        <th>Topic</th>
                        <th>Status</th>
                        <th>Participants</th>
                        <th>Members</th>
                        <th>Unknown</th>
                        <th>Reports</th>
                    </tr>
                    {% for m in rows %}
                    <tr>
                        <td>{{ fmt_dt(m.start_time) }}</td>
                        <td>{{ m.topic or 'Untitled Meeting' }}</td>
                        <td>{{ m.status or '-' }}</td>
                        <td>{{ m.unique_participants or 0 }}</td>
                        <td>{{ m.member_participants or 0 }}</td>
                        <td>{{ m.unknown_participants or 0 }}</td>
                        <td>
                            {% if m.meeting_uuid %}
                                <div class="row">
                                    <a class='btn success small' href='{{ url_for("meeting_csv", meeting_uuid=m.meeting_uuid) }}'>CSV</a>
                                    <a class='btn secondary small' href='{{ url_for("meeting_pdf", meeting_uuid=m.meeting_uuid) }}'>PDF</a>
                                    {% if session.get('role') == 'admin' %}
                                        <form method='post' action='{{ url_for("delete_meeting", meeting_uuid=m.meeting_uuid) }}' onsubmit='return confirm("Delete this meeting and its attendance records?")'>
                                            <button type='submit' class='btn danger small'>Delete</button>
                                        </form>
                                    {% endif %}
                                    {% if session.get('role') == 'admin' and m.status == 'live' %}
                                        <a class='btn danger small' href='{{ url_for("manual_finalize_meeting", meeting_uuid=m.meeting_uuid) }}'>Finalize</a>
                                    {% endif %}
                                </div>
                            {% else %}
                                <span class='badge danger'>No UUID / old record</span>
                            {% endif %}
                        </td>
                    </tr>
                    {% endfor %}
                </table>
            </div>
        </div>
        """,
        rows=rows,
        fmt_dt=fmt_dt,
        fmt_time_ampm=fmt_time_ampm,
        member_display_name=member_display_name,
        session=session,
    )
    return page("Meetings", body, "meetings")


@app.route("/meetings/<path:meeting_uuid>/report.csv")
@login_required
def meeting_csv(meeting_uuid):
    if not meeting_uuid:
        flash("Meeting UUID missing for this record.", "error")
        return redirect(url_for("meetings"))

    data = analytics_data({"meeting_uuid": meeting_uuid, "period_mode": "custom"})
    content = export_csv_bytes(data["rows"])
    return Response(
        content,
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={slugify(meeting_uuid)}.csv"},
    )


@app.route("/meetings/<path:meeting_uuid>/report.pdf")
@login_required
def meeting_pdf(meeting_uuid):
    if not meeting_uuid:
        flash("Meeting UUID missing for this record.", "error")
        return redirect(url_for("meetings"))

    report_data = build_meeting_report_data(meeting_uuid)
    if not report_data:
        flash("Meeting report data not found.", "error")
        return redirect(url_for("meetings"))

    pdf = export_meeting_pdf_bytes("Attendance Report", report_data)
    pdf_filename = build_meeting_pdf_filename(report_data)
    return send_file(
        io.BytesIO(pdf),
        download_name=pdf_filename,
        mimetype="application/pdf",
        as_attachment=True,
    )


@app.route("/settings", methods=["GET", "POST"])
@login_required
@admin_required
def settings():
    if request.method == "POST":
        for key in ["present_percentage", "late_count_as_present_percentage", "late_threshold_minutes", "meeting_finalize_seconds"]:
            set_setting(key, request.form.get(key, DEFAULT_SETTINGS[key]))
        log_activity("settings_update", "Attendance rules changed")
        flash("Settings updated.", "success")
        return redirect(url_for("settings"))

    settings_map = {k: get_setting(k, str) for k in DEFAULT_SETTINGS.keys()}

    body = render_template_string(
        """
        <div class="hero">
            <h2>Settings</h2>
            <div class="muted" style="color:#cbd5e1">Control attendance rules and meeting finalization timing.</div>
        </div>

        <div class='card'>
            <form method='post'>
                <label>Present Percentage</label>
                <input name='present_percentage' value='{{ s.present_percentage }}'>
                <label>Late Count As Present Percentage</label>
                <input name='late_count_as_present_percentage' value='{{ s.late_count_as_present_percentage }}'>
                <label>Late Threshold Minutes</label>
                <input name='late_threshold_minutes' value='{{ s.late_threshold_minutes }}'>
                <label>Meeting Finalize Seconds</label>
                <input name='meeting_finalize_seconds' value='{{ s.meeting_finalize_seconds }}'>
                <button type='submit'>Save Settings</button>
            </form>
        </div>
        """,
        s=settings_map,
    )
    return page("Settings", body, "settings")


@app.route("/activity")
@login_required
def activity():
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM activity_log ORDER BY id DESC LIMIT 250")
            rows = cur.fetchall()

    body = render_template_string(
        """
        <div class="hero">
            <h2>Activity Log</h2>
            <div class="muted" style="color:#cbd5e1">Track user actions and important admin events.</div>
        </div>

        <div class='card'>
            <div class="table-wrap">
                <table>
                    <tr><th>Time</th><th>User</th><th>Action</th><th>Details</th></tr>
                    {% for a in rows %}
                    <tr>
                        <td>{{ fmt_dt(a.created_at) }}</td>
                        <td>{{ a.username or '-' }}</td>
                        <td>{{ a.action }}</td>
                        <td class="long">{{ a.details }}</td>
                    </tr>
                    {% endfor %}
                </table>
            </div>
        </div>
        """,
        rows=rows,
        fmt_dt=fmt_dt,
    )
    return page("Activity", body, "activity")


@app.route("/health")
def health():
    maybe_finalize_stale_live_meetings(force=True)
    return jsonify({"ok": True, "time": fmt_dt(now_local())})


@app.route("/zoom/webhook", methods=["POST"])
def zoom_webhook():
    try:
        payload = request.get_json(force=True, silent=True) or {}
        print("🔥 FULL ZOOM DATA:", payload)

        if payload.get("event") == "endpoint.url_validation":
            plain = payload.get("payload", {}).get("plainToken", "")
            encrypted = hmac.new(
                ZOOM_SECRET_TOKEN.encode("utf-8"),
                plain.encode("utf-8"),
                hashlib.sha256,
            ).hexdigest() if ZOOM_SECRET_TOKEN else ""
            print("✅ URL VALIDATION:", {"plainToken": plain, "encryptedToken": encrypted})
            return jsonify({"plainToken": plain, "encryptedToken": encrypted})

        if not verify_zoom_signature(request):
            print("❌ INVALID SIGNATURE")
            return jsonify({"message": "invalid signature"}), 401

        event = (payload.get("event") or "").strip()
        payload_root = payload.get("payload", {}) or {}
        obj = payload_root.get("object", {}) or {}

        print("📌 EVENT:", event)
        print("📌 OBJECT:", obj)

        participant = obj.get("participant") or payload_root.get("participant") or {}
        if not participant and isinstance(obj.get("participants"), list) and obj.get("participants"):
            participant = obj.get("participants")[0] or {}
        if not participant and any(k in obj for k in ("user_name", "participant_user_name", "name", "email", "user_email")):
            participant = obj

        print("📌 PARTICIPANT:", participant)

        if event == "meeting.started":
            meeting = ensure_meeting(obj)
            print("✅ MEETING STARTED RESOLVED:", meeting)
            log_activity("zoom_started", meeting["meeting_uuid"] if meeting else "unknown")
            return jsonify({"ok": True})

        if "participant_joined" in event or "participant_left" in event:
            meeting = ensure_meeting(obj)
            print("✅ PARTICIPANT EVENT MEETING:", meeting)

            if not meeting:
                print("❌ meeting not resolved")
                return jsonify({"ok": False, "reason": "meeting not resolved"}), 200

            meeting_uuid = meeting["meeting_uuid"]
            event_type = "join" if "participant_joined" in event else "leave"

            event_raw = (
                participant.get("join_time")
                or participant.get("leave_time")
                or obj.get("join_time")
                or obj.get("leave_time")
                or (
                    datetime.fromtimestamp(payload.get("event_ts") / 1000, tz=ZoneInfo(TIMEZONE_NAME)).isoformat()
                    if isinstance(payload.get("event_ts"), (int, float))
                    else None
                )
            )
            event_time = parse_dt(event_raw) or now_local()

            participant_name = (
                participant.get("user_name")
                or participant.get("participant_user_name")
                or participant.get("display_name")
                or participant.get("name")
                or participant.get("participant_name")
                or participant.get("screen_name")
                or "Unknown Participant"
            )
            participant_email = (
                participant.get("email")
                or participant.get("user_email")
                or participant.get("participant_email")
                or None
            )

            print("📌 PARSED PARTICIPANT:", {
                "meeting_uuid": meeting_uuid,
                "event_type": event_type,
                "participant_name": participant_name,
                "participant_email": participant_email,
                "event_raw": event_raw,
                "event_time": str(event_time),
            })

            update_participant(
                meeting_uuid,
                participant_name,
                participant_email,
                event_time,
                event_type,
            )
            log_activity("zoom_participant_event", f"{event} :: {meeting_uuid} :: {participant_name}")
            return jsonify({"ok": True})

        if event in ("meeting.ended", "meeting.end"):
            meeting = ensure_meeting(obj)
            print("✅ MEETING ENDED RESOLVED:", meeting)

            if not meeting:
                print("❌ meeting not resolved")
                return jsonify({"ok": False, "reason": "meeting not resolved"}), 200

            finalized = finalize_meeting(meeting["meeting_uuid"], parse_dt(obj.get("end_time")) or now_local())
            print("✅ FINALIZED:", finalized)
            log_activity("zoom_meeting_ended", meeting["meeting_uuid"])
            return jsonify({"ok": True, "finalized": bool(finalized)})

        print("ℹ️ IGNORED EVENT:", event)
        return jsonify({"ok": True, "ignored": event})

    except Exception as e:
        print("❌ WEBHOOK ERROR:", str(e))
        return jsonify({"ok": False, "error": str(e)}), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=True)