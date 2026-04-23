
# ===== DARK SAAS THEME INJECTION (SAFE) =====
DARK_THEME_CSS = '''
<style>
body { background: linear-gradient(135deg,#0b0f1a,#111827); color:#e5e7eb; font-family: Inter, sans-serif;}
.card { background: rgba(255,255,255,0.05); backdrop-filter: blur(10px); border-radius:12px; padding:16px; box-shadow:0 8px 30px rgba(0,0,0,0.4);}
button { background: linear-gradient(90deg,#6366f1,#8b5cf6); color:white; border:none; padding:10px 16px; border-radius:8px;}
button:hover { opacity:0.9; }
table { background: rgba(255,255,255,0.03); border-radius:10px;}
th { position: sticky; top:0; background:#111827;}
</style>
'''
# ===== END THEME =====

import csv
import hashlib
import hmac
import io
import json
import os
import smtplib
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
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
from pywebpush import WebPushException, webpush
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
WEB_PUSH_ENABLED = os.getenv("WEB_PUSH_ENABLED", "false").strip().lower() in ("1", "true", "yes", "on")
VAPID_SUBJECT = os.getenv("VAPID_SUBJECT", "mailto:test@example.com").strip()
VAPID_PUBLIC_KEY = os.getenv("VAPID_PUBLIC_KEY", "").strip()
VAPID_PRIVATE_KEY = os.getenv("VAPID_PRIVATE_KEY", "")


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

def send_email(to_email, subject, body, html_body=None):
    if str(os.getenv("EMAIL_ENABLED", "true")).strip().lower() not in ("1", "true", "yes", "on"):
        print("⚠️ Email sending is disabled")
        return False, "Email disabled"

    smtp_host = os.getenv("SMTP_HOST", "").strip()
    smtp_port = os.getenv("SMTP_PORT", "465").strip()
    smtp_user = os.getenv("SMTP_USER", "").strip()
    smtp_pass = os.getenv("SMTP_PASS", "").strip()
    from_name = os.getenv("SMTP_FROM_NAME", "Zoom Attendance Platform").strip()

    if not smtp_host or not smtp_port or not smtp_user or not smtp_pass:
        print("⚠️ SMTP config missing")
        return False, "SMTP config missing"

    try:
        smtp_port = int(smtp_port)
    except Exception:
        smtp_port = 465

    try:
        if html_body:
            msg = MIMEMultipart("alternative")
            msg["Subject"] = subject
            msg["From"] = f"{from_name} <{smtp_user}>"
            msg["To"] = to_email
            msg.attach(MIMEText(body or "", "plain", "utf-8"))
            msg.attach(MIMEText(html_body, "html", "utf-8"))
        else:
            msg = MIMEText(body or "", "plain", "utf-8")
            msg["Subject"] = subject
            msg["From"] = f"{from_name} <{smtp_user}>"
            msg["To"] = to_email

        if smtp_port == 465:
            with smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=20) as server:
                server.login(smtp_user, smtp_pass)
                server.sendmail(smtp_user, [to_email], msg.as_string())
        else:
            with smtplib.SMTP(smtp_host, smtp_port, timeout=20) as server:
                server.starttls()
                server.login(smtp_user, smtp_pass)
                server.sendmail(smtp_user, [to_email], msg.as_string())

        print(f"✅ Email sent to {to_email}")
        return True, "Email sent successfully"

    except Exception as e:
        print(f"❌ Email failed: {e}")
        return False, str(e)


def get_vapid_private_key_value():
    raw = VAPID_PRIVATE_KEY or ""
    if not raw:
        return ""
    return raw.replace("\n", "\n").replace("\r", "").strip()


def is_web_push_configured() -> bool:
    return bool(WEB_PUSH_ENABLED and VAPID_PUBLIC_KEY and get_vapid_private_key_value())


def save_push_subscription(subscription_data, username=None):
    endpoint = (subscription_data or {}).get("endpoint") or ""
    keys = (subscription_data or {}).get("keys") or {}
    p256dh = keys.get("p256dh") or ""
    auth = keys.get("auth") or ""
    if not endpoint or not p256dh or not auth:
        return False, "Invalid subscription payload"

    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO push_subscriptions(endpoint, p256dh, auth, username, updated_at)
                VALUES (%s, %s, %s, %s, NOW())
                ON CONFLICT (endpoint)
                DO UPDATE SET
                    p256dh = EXCLUDED.p256dh,
                    auth = EXCLUDED.auth,
                    username = COALESCE(EXCLUDED.username, push_subscriptions.username),
                    updated_at = NOW()
                """,
                (endpoint, p256dh, auth, username),
            )
        conn.commit()
    return True, "Subscription saved"


def send_push_notification(title, body, target_username=None, click_url=None):
    if not is_web_push_configured():
        print("⚠️ Web Push not configured")
        return {"sent": 0, "failed": 0, "errors": ["Web Push not configured"]}

    payload = json.dumps({
        "title": title,
        "body": body,
        "icon": "/static/icon.png",
        "badge": "/static/icon.png",
        "url": click_url or url_for("home", _external=True),
    })

    results = {"sent": 0, "failed": 0, "errors": []}

    with db() as conn:
        with conn.cursor() as cur:
            if target_username:
                cur.execute(
                    "SELECT * FROM push_subscriptions WHERE username=%s ORDER BY id DESC",
                    (target_username,),
                )
            else:
                cur.execute("SELECT * FROM push_subscriptions ORDER BY id DESC")
            rows = cur.fetchall()

            for row in rows:
                subscription_info = {
                    "endpoint": row.get("endpoint"),
                    "keys": {
                        "p256dh": row.get("p256dh"),
                        "auth": row.get("auth"),
                    },
                }
                try:
                    webpush(
                        subscription_info=subscription_info,
                        data=payload,
                        vapid_private_key=get_vapid_private_key_value(),
                        vapid_claims={"sub": VAPID_SUBJECT},
                        ttl=60,
                    )
                    results["sent"] += 1
                except WebPushException as exc:
                    results["failed"] += 1
                    err_text = str(exc)
                    results["errors"].append(err_text)
                    status_code = getattr(getattr(exc, "response", None), "status_code", None)
                    if status_code in (404, 410):
                        try:
                            cur.execute("DELETE FROM push_subscriptions WHERE endpoint=%s", (row.get("endpoint"),))
                        except Exception:
                            pass
                except Exception as exc:
                    results["failed"] += 1
                    results["errors"].append(str(exc))
        conn.commit()

    return results


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
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS push_subscriptions (
                    id SERIAL PRIMARY KEY,
                    endpoint TEXT UNIQUE NOT NULL,
                    p256dh TEXT NOT NULL,
                    auth TEXT NOT NULL,
                    username TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
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

        if table_exists(conn, "push_subscriptions"):
            ensure_column(conn, "push_subscriptions", "endpoint", "TEXT")
            ensure_column(conn, "push_subscriptions", "p256dh", "TEXT")
            ensure_column(conn, "push_subscriptions", "auth", "TEXT")
            ensure_column(conn, "push_subscriptions", "username", "TEXT")
            ensure_column(conn, "push_subscriptions", "created_at", "TIMESTAMPTZ NOT NULL DEFAULT NOW()")
            ensure_column(conn, "push_subscriptions", "updated_at", "TIMESTAMPTZ NOT NULL DEFAULT NOW()")

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
    attendance_ratio = ((int(present_count or 0) * 1.0) + (int(late_count or 0) * 0.6)) / total
    return clamp_score(attendance_ratio * 100.0)


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


def safe_percent(part, whole):
    try:
        part = float(part or 0)
        whole = float(whole or 0)
    except Exception:
        return 0.0
    if whole <= 0:
        return 0.0
    return clamp_score((part / whole) * 100.0)


def calculate_weighted_member_score(attendance_pct, consistency_pct, duration_pct):
    return clamp_score((attendance_pct * 0.5) + (consistency_pct * 0.3) + (duration_pct * 0.2))


def derive_trend_label(score_points):
    points = [float(x) for x in (score_points or []) if x is not None]
    if len(points) < 2:
        return {"label": "Stable", "emoji": "➖", "short": "STABLE", "delta": 0.0}
    recent = points[-3:]
    previous = points[:-3] if len(points) > 3 else points[:-1]
    if not previous:
        previous = [points[0]]
    recent_avg = sum(recent) / len(recent)
    previous_avg = sum(previous) / len(previous)
    delta = round(recent_avg - previous_avg, 2)
    if delta >= 5:
        return {"label": "Improving", "emoji": "📈", "short": "IMPROVING", "delta": delta}
    if delta <= -5:
        return {"label": "Declining", "emoji": "📉", "short": "DECLINING", "delta": delta}
    return {"label": "Stable", "emoji": "➖", "short": "STABLE", "delta": delta}


def build_member_intelligence(person, avg_minutes_reference):
    meetings = max(int(person.get("meetings") or 0), 0)
    present = int(person.get("present") or 0)
    late = int(person.get("late") or 0)
    absent = int(person.get("absent") or 0)
    minutes = max(float(person.get("minutes") or 0), 0.0)
    rejoins = max(float(person.get("rejoins") or 0), 0.0)
    attendance_pct = calculate_attendance_score(present, late, absent)
    if meetings > 0:
        stability_penalty = min((rejoins / meetings) * 12.0, 24.0)
        attendance_consistency_pct = clamp_score((((present * 1.0) + (late * 0.65)) / meetings) * 100.0 - stability_penalty)
    else:
        attendance_consistency_pct = 0.0
    duration_pct = clamp_score(min(minutes / max(avg_minutes_reference * max(meetings, 1), 1.0), 1.15) / 1.15 * 100.0)
    weighted_score = calculate_weighted_member_score(attendance_pct, attendance_consistency_pct, duration_pct)
    engagement_score = calculate_engagement_score(minutes, rejoins, meetings, present, late, absent, avg_minutes_reference)
    overall_score = clamp_score((weighted_score * 0.72) + (engagement_score * 0.28))
    last_seen = parse_dt(person.get("last_seen"))
    days_since_seen = None
    recency_penalty = 0.0
    if last_seen:
        days_since_seen = max((today_local() - last_seen.date()).days, 0)
        if days_since_seen >= 30:
            recency_penalty = 22.0
        elif days_since_seen >= 14:
            recency_penalty = 12.0
        elif days_since_seen >= 7:
            recency_penalty = 6.0
    else:
        recency_penalty = 18.0 if meetings > 0 else 0.0
    risk_driver = clamp_score(overall_score - recency_penalty)
    risk = get_risk_level(risk_driver)
    trend = derive_trend_label(person.get("score_points") or [])
    return {
        "attendance_pct": attendance_pct,
        "consistency_pct": attendance_consistency_pct,
        "duration_pct": duration_pct,
        "attendance_score": weighted_score,
        "engagement_score": engagement_score,
        "overall_score": overall_score,
        "risk_driver": risk_driver,
        "risk": risk,
        "trend": trend,
        "last_seen": last_seen,
        "days_since_seen": days_since_seen,
    }


def calculate_meeting_health_score(present_count, late_count, absent_count, avg_duration_minutes, reference_duration_minutes, unknown_count=0, host_present=False):
    total = max(int(present_count or 0) + int(late_count or 0) + int(absent_count or 0), 0)
    attendance_component = safe_percent((int(present_count or 0) + (int(late_count or 0) * 0.6)), total) * 0.5
    duration_component = clamp_score(min(float(avg_duration_minutes or 0) / max(float(reference_duration_minutes or 0), 1.0), 1.15) / 1.15 * 100.0) * 0.3
    unknown_penalty = min(int(unknown_count or 0) * 4.0, 18.0)
    host_bonus = 6.0 if host_present else -6.0
    participation_component = clamp_score(100.0 - unknown_penalty + host_bonus) * 0.2
    return clamp_score(attendance_component + duration_component + participation_component)


def build_smart_actions(summary, latest_meeting_summary, risk_table):
    actions = []
    target_names = [item["name"] for item in (risk_table or [])[:6]]
    if target_names:
        actions.append(f"Send reminder to these users: {', '.join(target_names)}")
    if latest_meeting_summary and float(latest_meeting_summary.get("health") or 0) < 60:
        actions.append("Mark meeting low quality")
    if summary.get("critical_members_count", 0) > 0 or summary.get("host_absent_flag"):
        actions.append("Follow-up required")
    if summary.get("unknown_spike_flag"):
        actions.append("Review unknown participants and member mapping")
    if not actions:
        actions.append("No automatic action required right now")
    return actions[:5]


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


def build_phase3_alerts(summary, latest_meeting_summary, previous_meeting_summary, reminder_suggestion):
    alerts = []
    if summary.get("critical_members_count", 0) > 0:
        alerts.append({"level": "danger", "title": "Critical member risk detected", "text": f"{summary.get('critical_members_count', 0)} member(s) need immediate follow-up."})
    elif summary.get("warning_members_count", 0) > 0:
        alerts.append({"level": "warn", "title": "Warning members found", "text": f"{summary.get('warning_members_count', 0)} member(s) are slipping below healthy attendance."})

    if summary.get("unknown_rows", 0) >= 3:
        alerts.append({"level": "info", "title": "Unknown participants trend", "text": f"{summary.get('unknown_rows', 0)} unknown participant records appeared in the current filtered view."})

    if latest_meeting_summary and not latest_meeting_summary.get("present"):
        alerts.append({"level": "warn", "title": "Latest meeting has weak turnout", "text": "The latest meeting has no present classifications in the filtered dataset."})

    if latest_meeting_summary and latest_meeting_summary.get("unknown", 0) >= 2:
        alerts.append({"level": "info", "title": "Unknown participant watch", "text": f"{latest_meeting_summary.get('unknown', 0)} unknown attendees appeared in the latest meeting snapshot."})

    if latest_meeting_summary and previous_meeting_summary:
        latest_health = float(latest_meeting_summary.get("health") or 0)
        previous_health = float(previous_meeting_summary.get("health") or 0)
        if previous_health and latest_health + 10 < previous_health:
            alerts.append({"level": "danger", "title": "Meeting health drop detected", "text": f"Latest meeting health dropped from {previous_health}% to {latest_health}%."})

    if reminder_suggestion and reminder_suggestion.get("count"):
        alerts.append({"level": "warn", "title": "Reminder suggested", "text": reminder_suggestion.get("message")})

    if not alerts:
        alerts.append({"level": "ok", "title": "No urgent alerts", "text": "Current filters do not show any critical operational issue."})
    return alerts[:5]


def export_meeting_excel_bytes(report_data):
    out = io.StringIO()
    writer = csv.writer(out)
    summary = report_data["summary"]
    rows = report_data["rows"]

    writer.writerow(["Attendance Report"])
    writer.writerow(["Topic", summary.get("topic")])
    writer.writerow(["Meeting ID", summary.get("meeting_id")])
    writer.writerow(["Date", summary.get("date")])
    writer.writerow(["Start Time", summary.get("start_time")])
    writer.writerow(["End Time", summary.get("end_time")])
    writer.writerow(["Meeting Duration (Minutes)", summary.get("meeting_duration_minutes")])
    writer.writerow(["Meeting Health Score", summary.get("meeting_health_score")])
    writer.writerow([])
    writer.writerow(["Total Participants", summary.get("total_participants")])
    writer.writerow(["Total Members", summary.get("total_members")])
    writer.writerow(["Present Members", summary.get("total_present_members")])
    writer.writerow(["Absent Members", summary.get("total_absent_members")])
    writer.writerow(["Unknown Participants", summary.get("total_unknown_participants")])
    writer.writerow([])
    writer.writerow(["Name", "Join", "Leave", "Duration", "Rejoins", "Status", "Unknown"])
    for row in rows:
        writer.writerow([
            row.get("participant_name") or "",
            row.get("join_display") or "",
            row.get("leave_display") or "",
            row.get("duration_minutes") or 0,
            row.get("rejoin_count") or 0,
            row.get("status") or "",
            "Yes" if row.get("is_unknown_joined") else "No",
        ])
    return out.getvalue().encode("utf-8")


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
                "last_seen": None,
                "score_points": [],
            },
        )
        by_person[key]["meetings"] += 1
        row_minutes = (r.get("total_seconds") or 0) / 60
        by_person[key]["minutes"] += row_minutes
        by_person[key]["rejoins"] += (r.get("rejoin_count") or 0)
        if r.get("final_status") == "PRESENT":
            by_person[key]["present"] += 1
            score_point = 100.0
        elif r.get("final_status") == "LATE":
            by_person[key]["late"] += 1
            score_point = 62.0
        elif r.get("final_status") == "ABSENT":
            by_person[key]["absent"] += 1
            score_point = 20.0
        else:
            score_point = 50.0
        if row_minutes > 0:
            score_point = min(100.0, score_point + min(row_minutes / 3.0, 16.0))
        by_person[key]["score_points"].append(round(score_point, 2))
        last_seen_candidate = parse_dt(r.get("last_leave")) or parse_dt(r.get("current_join")) or parse_dt(r.get("first_join")) or parse_dt(r.get("start_time"))
        if last_seen_candidate and (by_person[key]["last_seen"] is None or last_seen_candidate > by_person[key]["last_seen"]):
            by_person[key]["last_seen"] = last_seen_candidate

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
    meeting_duration_minutes_reference = avg_minutes if avg_minutes > 0 else 1
    for m in meeting_compare:
        total = m["total"] or 1
        meeting_rows = [row for row in rows if row.get("meeting_uuid") == m.get("meeting_uuid")]
        average_duration_for_meeting = round(sum((row.get("total_seconds") or 0) for row in meeting_rows) / 60 / len(meeting_rows), 2) if meeting_rows else 0
        host_present_for_meeting = any(bool(row.get("is_host")) for row in meeting_rows)
        m["avg_duration_minutes"] = average_duration_for_meeting
        m["health"] = calculate_meeting_health_score(
            m.get("present", 0),
            m.get("late", 0),
            m.get("absent", 0),
            average_duration_for_meeting,
            meeting_duration_minutes_reference,
            m.get("unknown", 0),
            host_present_for_meeting,
        )

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
        intelligence = build_member_intelligence(person, avg_minutes_reference)
        person["attendance_pct"] = intelligence["attendance_pct"]
        person["consistency_pct"] = intelligence["consistency_pct"]
        person["duration_pct"] = intelligence["duration_pct"]
        person["attendance_score"] = intelligence["attendance_score"]
        person["engagement_score"] = intelligence["engagement_score"]
        person["overall_score"] = intelligence["overall_score"]
        person["risk_driver"] = intelligence["risk_driver"]
        person["risk"] = intelligence["risk"]
        person["trend"] = intelligence["trend"]
        person["last_seen"] = intelligence["last_seen"]
        person["days_since_seen"] = intelligence["days_since_seen"]
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

    duration_distribution = {
        "0-15": 0,
        "15-30": 0,
        "30-60": 0,
        "60+": 0,
    }
    for r in rows:
        mins = mins_from_seconds(r.get("total_seconds") or 0)
        if mins < 15:
            duration_distribution["0-15"] += 1
        elif mins < 30:
            duration_distribution["15-30"] += 1
        elif mins < 60:
            duration_distribution["30-60"] += 1
        else:
            duration_distribution["60+"] += 1

    current_meeting_health = latest_meeting_summary.get("health") if latest_meeting_summary else 0
    latest_unknown_ratio = safe_percent((latest_meeting_summary or {}).get("unknown", 0), (latest_meeting_summary or {}).get("total", 0)) if latest_meeting_summary else 0
    unknown_spike_flag = bool(latest_meeting_summary and latest_meeting_summary.get("unknown", 0) >= 3 and latest_unknown_ratio >= 25)
    host_absent_flag = bool(latest_meeting_summary and not any((row.get("meeting_uuid") == latest_meeting_summary.get("meeting_uuid")) and bool(row.get("is_host")) for row in rows))
    ended_early_flag = False
    if latest_meeting_summary and previous_meeting_summary:
        ended_early_flag = float(latest_meeting_summary.get("avg_duration_minutes") or 0) < max(float(previous_meeting_summary.get("avg_duration_minutes") or 0) * 0.65, 10.0)

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
        "current_meeting_health": round(current_meeting_health, 2) if current_meeting_health is not None else 0,
        "avg_attendance_score": avg_attendance_score,
        "avg_engagement_score": avg_engagement_score,
        "risk_members_count": risk_members_count,
        "critical_members_count": len(critical_members),
        "warning_members_count": len(warning_members),
        "safe_members_count": sum(1 for p in leaderboard if p["risk"]["short"] == "SAFE"),
        "insight_lines": insight_lines,
        "duration_distribution": duration_distribution,
        "unknown_spike_flag": unknown_spike_flag,
        "host_absent_flag": host_absent_flag,
        "ended_early_flag": ended_early_flag,
    }
    alerts = build_phase3_alerts(summary, latest_meeting_summary, previous_meeting_summary, reminder_suggestion)
    if any(item.get("trend", {}).get("short") == "DECLINING" for item in leaderboard[:8]):
        alerts.insert(0, {"level": "warn", "title": "Low attendance trend", "text": "At least one high-visibility member is showing a declining attendance trend."})
    if unknown_spike_flag:
        alerts.insert(0, {"level": "danger", "title": "Too many unknown participants", "text": f"Unknown participant ratio reached {round(latest_unknown_ratio, 2)}% in the latest meeting."})
    if host_absent_flag:
        alerts.insert(0, {"level": "warn", "title": "Host absent", "text": "The latest tracked meeting snapshot does not show the host as present."})
    if ended_early_flag:
        alerts.insert(0, {"level": "warn", "title": "Meeting ended early", "text": "Latest meeting duration looks lower than the recent meeting baseline."})
    alerts = alerts[:6]

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
        "alerts": alerts,
        "auto_actions": build_smart_actions(summary, latest_meeting_summary, risk_table),
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

    avg_duration_minutes = round(sum(row["duration_minutes"] for row in report_rows) / len(report_rows), 2) if report_rows else 0
    critical_count = sum(1 for row in report_rows if row.get("status") == "ABSENT")
    warning_count = sum(1 for row in report_rows if row.get("status") == "LATE")
    healthy_count = sum(1 for row in report_rows if row.get("status") in ("PRESENT", "HOST"))
    meeting_health_score = calculate_meeting_health_score(
        present_members_count,
        sum(1 for row in report_rows if row.get("status") == "LATE"),
        absent_members_count,
        avg_duration_minutes,
        max(avg_duration_minutes, 1),
        unknown_participants_count,
        bool(meeting.get("host_present")),
    )
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
        "meeting_health_score": meeting_health_score,
        "healthy_count": healthy_count,
        "warning_count": warning_count,
        "critical_count": critical_count,
        "avg_duration_minutes": avg_duration_minutes,
        "notes": meeting.get("notes") or "No meeting notes were stored for this meeting.",
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
    doc = SimpleDocTemplate(buffer, pagesize=letter, leftMargin=24, rightMargin=24, topMargin=22, bottomMargin=22)
    styles = getSampleStyleSheet()
    elements = []

    title_style = styles["Title"]
    title_style.alignment = 1
    elements.append(Paragraph("<b>Zoom Attendance Platform</b>", title_style))
    elements.append(Paragraph(f"<b>{title}</b>", styles["Heading2"]))
    elements.append(Spacer(1, 10))

    header_table = Table([
        ["Topic", summary["topic"], "Meeting ID", summary["meeting_id"]],
        ["Date", summary["date"], "Start", summary["start_time"]],
        ["End", summary["end_time"], "Duration", f"{summary['meeting_duration_minutes']} min"],
    ], colWidths=[70, 190, 70, 180])
    header_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.whitesmoke),
        ("GRID", (0, 0), (-1, -1), 0.6, colors.HexColor("#cbd5e1")),
        ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
        ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
        ("FONTNAME", (2, 0), (2, -1), "Helvetica-Bold"),
        ("PADDING", (0, 0), (-1, -1), 6),
    ]))
    elements.append(header_table)
    elements.append(Spacer(1, 10))

    summary_table = Table([
        ["Participants", summary["total_participants"], "Members", summary["total_members"], "Health", f"{summary['meeting_health_score']} / 100"],
        ["Present", summary["total_present_members"], "Absent", summary["total_absent_members"], "Unknown", summary["total_unknown_participants"]],
        ["Healthy", summary.get("healthy_count", 0), "Warning", summary.get("warning_count", 0), "Critical", summary.get("critical_count", 0)],
    ], colWidths=[75, 70, 75, 70, 75, 75])
    summary_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#eff6ff")),
        ("GRID", (0, 0), (-1, -1), 0.6, colors.HexColor("#bfdbfe")),
        ("FONTNAME", (0, 0), (-1, -1), "Helvetica-Bold"),
        ("TEXTCOLOR", (0, 0), (-1, -1), colors.HexColor("#0f172a")),
        ("ALIGN", (1, 0), (-1, -1), "CENTER"),
        ("PADDING", (0, 0), (-1, -1), 6),
    ]))
    elements.append(summary_table)
    elements.append(Spacer(1, 10))

    member_rows = [r for r in rows if not r.get("is_unknown_joined")]
    unknown_rows = [r for r in rows if r.get("is_unknown_joined")]

    def build_people_table(source_rows, title_text):
        block = []
        block.append(Paragraph(f"<b>{title_text}</b>", styles["Heading3"]))
        table_data = [["Name", "Join", "Leave", "Duration", "Rejoins", "Status"]]
        for row in source_rows:
            table_data.append([
                Paragraph(row["participant_name"], styles["Normal"]),
                row["join_display"],
                row["leave_display"],
                str(row["duration_minutes"]),
                str(row["rejoin_count"]),
                row["status"],
            ])
        table = Table(table_data, repeatRows=1, colWidths=[180, 70, 70, 60, 52, 68])
        style = TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0f172a")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#94a3b8")),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("ALIGN", (1, 0), (-1, -1), "CENTER"),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("PADDING", (0, 0), (-1, -1), 5),
        ])
        for i in range(1, len(table_data)):
            status = table_data[i][5]
            if status == "PRESENT":
                style.add("TEXTCOLOR", (5, i), (5, i), colors.green)
            elif status == "LATE":
                style.add("TEXTCOLOR", (5, i), (5, i), colors.orange)
            elif status == "ABSENT":
                style.add("TEXTCOLOR", (5, i), (5, i), colors.red)
            elif status == "HOST":
                style.add("TEXTCOLOR", (5, i), (5, i), colors.HexColor("#1d4ed8"))
        table.setStyle(style)
        block.append(table)
        block.append(Spacer(1, 8))
        return block

    elements.extend(build_people_table(member_rows[:120], "Member Attendance Section"))
    if unknown_rows:
        elements.extend(build_people_table(unknown_rows[:40], "Unknown Participants Section"))

    criteria_text = (
        "<b>Attendance Criteria</b><br/>"
        f"• Present threshold for this meeting: {summary['present_threshold_minutes']} minutes<br/>"
        f"• Late threshold for this meeting: {summary['late_summary_threshold_minutes']} minutes<br/>"
        "• Smart health score uses attendance, duration, participation quality, and host visibility.<br/>"
        "• Host rows are preserved separately and not treated as absent.<br/>"
        "• Unknown participants are attendees not matched to your member directory."
    )
    criteria_table = Table([[Paragraph(criteria_text, styles["Normal"])]], colWidths=[520])
    criteria_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f8fafc")),
        ("GRID", (0, 0), (-1, -1), 0.7, colors.HexColor("#cbd5e1")),
        ("PADDING", (0, 0), (-1, -1), 8),
    ]))
    elements.append(criteria_table)
    elements.append(Spacer(1, 8))

    notes_text = f"<b>Meeting Health Summary:</b> {summary['meeting_health_score']} / 100<br/><b>Average Duration:</b> {summary.get('avg_duration_minutes', 0)} minutes<br/><b>Notes:</b> {summary['notes']}"
    notes_table = Table([[Paragraph(notes_text, styles["Normal"])]], colWidths=[520])
    notes_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#fefce8")),
        ("GRID", (0, 0), (-1, -1), 0.7, colors.HexColor("#fde68a")),
        ("PADDING", (0, 0), (-1, -1), 8),
    ]))
    elements.append(notes_table)

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
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800;900&display=swap" rel="stylesheet">
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <style>
        :root{
            --nav:#081226;
            --nav-2:#101a34;
            --bg1:#eef4ff;
            --bg2:#f8fbff;
            --bg3:#e8f0ff;
            --card:#ffffff;
            --card-soft:rgba(255,255,255,.74);
            --card-solid:#ffffff;
            --text:#0f172a;
            --muted:#64748b;
            --line:#dce5f4;
            --line-strong:#c7d6ee;
            --primary:#2563eb;
            --primary2:#4f46e5;
            --primary3:#7c3aed;
            --success:#16a34a;
            --warn:#f59e0b;
            --danger:#dc2626;
            --cyan:#0891b2;
            --soft:#eff6ff;
            --shadow:0 18px 46px rgba(15,23,42,.10);
            --shadow-soft:0 12px 28px rgba(15,23,42,.08);
            --radius:24px;
            --radius-lg:30px;
            --radius-sm:16px;
            --glass:blur(18px);
            --surface-ring:rgba(148,163,184,.14);
            --hero-grad:linear-gradient(135deg,#0f172a 0%, #1d4ed8 48%, #7c3aed 100%);
            --hero-glow:rgba(96,165,250,.18);
            --btn-grad:linear-gradient(135deg,#2563eb 0%, #4f46e5 52%, #7c3aed 100%);
            --chip-bg:rgba(255,255,255,.14);
            --bg-grid:rgba(148,163,184,.12);
        }
        body.dark{
            --nav:#020617;
            --nav-2:#111827;
            --bg1:#07111f;
            --bg2:#0b1528;
            --bg3:#111d35;
            --card:#0f172a;
            --card-soft:rgba(15,23,42,.66);
            --card-solid:#0f172a;
            --text:#e5eefc;
            --muted:#9fb2d3;
            --line:#24334d;
            --line-strong:#334155;
            --primary:#60a5fa;
            --primary2:#3b82f6;
            --primary3:#8b5cf6;
            --success:#22c55e;
            --warn:#fbbf24;
            --danger:#ef4444;
            --cyan:#22d3ee;
            --soft:#16233b;
            --shadow:0 20px 54px rgba(2,6,23,.42);
            --shadow-soft:0 14px 32px rgba(2,6,23,.3);
            --surface-ring:rgba(148,163,184,.16);
            --hero-grad:linear-gradient(135deg,#020617 0%, #1d4ed8 56%, #6d28d9 100%);
            --hero-glow:rgba(96,165,250,.16);
            --btn-grad:linear-gradient(135deg,#1d4ed8 0%, #4f46e5 54%, #7c3aed 100%);
            --chip-bg:rgba(255,255,255,.06);
            --bg-grid:rgba(148,163,184,.08);
        }
        *{box-sizing:border-box}
        html{scroll-behavior:smooth}
        body{
            margin:0;
            min-height:100vh;
            font-family:'Inter',Arial,sans-serif;
            color:var(--text);
            background:
                radial-gradient(circle at 18% 18%, rgba(59,130,246,.08), transparent 28%),
                radial-gradient(circle at 85% 14%, rgba(124,58,237,.1), transparent 24%),
                linear-gradient(135deg,var(--bg1),var(--bg2) 62%, var(--bg3));
            transition:background .25s ease,color .25s ease;
            position:relative;
            overflow-x:hidden;
        }
        body::selection{background:rgba(79,70,229,.18)}
        ::-webkit-scrollbar{width:10px;height:10px}
        ::-webkit-scrollbar-track{background:transparent}
        ::-webkit-scrollbar-thumb{background:rgba(100,116,139,.35);border-radius:999px}
        ::-webkit-scrollbar-thumb:hover{background:rgba(100,116,139,.5)}
        .app-bg{
            position:fixed; inset:0; pointer-events:none; overflow:hidden; z-index:0;
        }
        .bg-grid{
            position:absolute; inset:0;
            background-image:
                linear-gradient(to right, transparent 0, transparent 39px, var(--bg-grid) 40px),
                linear-gradient(to bottom, transparent 0, transparent 39px, var(--bg-grid) 40px);
            background-size:40px 40px;
            mask-image:radial-gradient(circle at center, rgba(0,0,0,.78), transparent 90%);
            opacity:.56;
        }
        .orb{
            position:absolute; border-radius:999px; filter:blur(22px); opacity:.42;
            animation:floatOrb 18s ease-in-out infinite;
        }
        .orb-1{width:320px;height:320px;background:rgba(59,130,246,.22);top:-70px;left:-70px}
        .orb-2{width:260px;height:260px;background:rgba(139,92,246,.20);top:14%;right:-60px;animation-delay:-5s}
        .orb-3{width:360px;height:360px;background:rgba(34,197,94,.11);bottom:-120px;left:18%;animation-delay:-9s}
        .orb-4{width:240px;height:240px;background:rgba(245,158,11,.11);bottom:16%;right:6%;animation-delay:-12s}
        .app-shell{position:relative;z-index:1}
        @keyframes floatOrb{
            0%,100%{transform:translate3d(0,0,0) scale(1)}
            50%{transform:translate3d(0,-22px,0) scale(1.05)}
        }
        @keyframes fadeRise{from{opacity:0;transform:translateY(10px)}to{opacity:1;transform:none}}
        @keyframes pulseGlow{0%,100%{box-shadow:0 0 0 0 rgba(34,197,94,.30)}50%{box-shadow:0 0 0 10px rgba(34,197,94,0)}}
        .topbar{
            position:sticky; top:0; z-index:30;
            padding:16px 28px;
            background:linear-gradient(90deg,rgba(8,18,38,.96),rgba(29,78,216,.88),rgba(124,58,237,.86));
            border-bottom:1px solid rgba(255,255,255,.08);
            backdrop-filter:blur(16px);
            box-shadow:0 12px 34px rgba(2,6,23,.22);
            display:flex; justify-content:space-between; align-items:center; gap:18px;
        }
        .brand-wrap{display:flex;align-items:center;gap:14px}
        .brand-badge{
            width:46px;height:46px;border-radius:15px;display:grid;place-items:center;font-size:18px;
            background:linear-gradient(135deg,rgba(255,255,255,.24),rgba(255,255,255,.08));
            border:1px solid rgba(255,255,255,.16);
            box-shadow:inset 0 1px 0 rgba(255,255,255,.20),0 12px 24px rgba(15,23,42,.18);
        }
        .brand{font-size:17px;font-weight:900;letter-spacing:-.02em;color:#fff}
        .brand-sub{font-size:12px;color:rgba(255,255,255,.74)}
        .top-actions{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
        .chip{
            display:inline-flex;align-items:center;gap:8px;
            height:38px; padding:0 14px; border-radius:999px;
            background:var(--chip-bg); color:#fff; border:1px solid rgba(255,255,255,.12);
            font-size:12px; font-weight:800; text-decoration:none;
            box-shadow:inset 0 1px 0 rgba(255,255,255,.10);
        }
        .chip-user{background:linear-gradient(135deg,rgba(134,239,172,.95),rgba(96,165,250,.95)); color:#0f172a}
        .theme-switch{
            position:relative; display:inline-flex; align-items:center; gap:10px;
            padding:6px 10px 6px 52px; min-height:38px; border-radius:999px;
            text-decoration:none; color:#fff; font-size:12px; font-weight:800;
            background:rgba(15,23,42,.28); border:1px solid rgba(255,255,255,.12);
            box-shadow:inset 0 1px 0 rgba(255,255,255,.10);
        }
        .theme-switch::before{
            content:""; position:absolute; left:6px; top:50%; transform:translateY(-50%);
            width:40px; height:26px; border-radius:999px;
            background:rgba(255,255,255,.14);
            border:1px solid rgba(255,255,255,.16);
        }
        .theme-switch::after{
            content:""; position:absolute; left:9px; top:50%; transform:translateY(-50%);
            width:20px; height:20px; border-radius:50%;
            background:linear-gradient(180deg,#fff,#dbeafe);
            box-shadow:0 4px 10px rgba(2,6,23,.22);
            transition:left .22s ease;
        }
        body.dark .theme-switch::after{left:25px}
        .wrap{display:flex;min-height:calc(100vh - 74px);align-items:stretch}
        .sidebar{
            width:264px; padding:22px 18px; position:sticky; top:74px; height:calc(100vh - 74px); overflow:auto;
            background:linear-gradient(180deg,rgba(255,255,255,.42),rgba(255,255,255,.28));
            backdrop-filter:var(--glass);
            border-right:1px solid rgba(148,163,184,.14);
        }
        body.dark .sidebar{background:linear-gradient(180deg,rgba(7,17,31,.55),rgba(7,17,31,.42))}
        .nav-group{display:flex;flex-direction:column;gap:8px}
        .sidebar a{
            display:flex; align-items:center; gap:12px; min-height:50px; padding:12px 14px;
            color:var(--text); text-decoration:none; border-radius:18px; font-weight:800;
            border:1px solid transparent; transition:all .18s ease;
        }
        .sidebar a .nav-icon{
            width:34px; height:34px; border-radius:12px; display:grid; place-items:center;
            background:rgba(255,255,255,.58); border:1px solid rgba(148,163,184,.16);
            box-shadow:inset 0 1px 0 rgba(255,255,255,.24);
            flex:0 0 auto;
        }
        body.dark .sidebar a .nav-icon{background:rgba(255,255,255,.04)}
        .sidebar a:hover,.sidebar a.active{
            transform:translateX(4px);
            background:linear-gradient(135deg,rgba(37,99,235,.14),rgba(124,58,237,.10));
            border-color:rgba(79,70,229,.18);
            box-shadow:var(--shadow-soft);
            color:var(--primary2);
        }
        body.dark .sidebar a:hover,body.dark .sidebar a.active{
            color:#e2e8f0; border-color:rgba(96,165,250,.14);
            background:linear-gradient(135deg,rgba(29,78,216,.20),rgba(124,58,237,.12));
        }
        .content{flex:1;padding:30px 32px 44px}
        .page-shell{max-width:1680px;margin:0 auto;width:100%}
        .hero{
            position:relative; overflow:hidden;
            background:var(--hero-grad); color:#fff; border-radius:32px; padding:30px 30px 26px;
            border:1px solid rgba(255,255,255,.08); box-shadow:var(--shadow);
            margin-bottom:18px;
        }
        .hero::before,.hero::after{
            content:""; position:absolute; border-radius:999px; background:var(--hero-glow);
            filter:blur(10px);
        }
        .hero::before{width:190px;height:190px;right:-38px;top:-82px}
        .hero::after{width:140px;height:140px;left:34%;bottom:-58px}
        .hero-grid{display:grid;grid-template-columns:minmax(0,1.28fr) minmax(320px,.72fr);gap:24px;align-items:end}
        .hero-title{font-size:34px;font-weight:900;letter-spacing:-.045em;margin:0 0 10px;line-height:1.08}
        .hero-copy{font-size:15px;line-height:1.75;color:rgba(255,255,255,.84);max-width:920px}
        .hero-stats{display:flex;gap:12px;flex-wrap:wrap;justify-content:flex-end;align-items:stretch}
        .hero-chip{
            min-width:132px; padding:14px 16px; border-radius:20px;
            background:rgba(255,255,255,.11); border:1px solid rgba(255,255,255,.12);
            box-shadow:inset 0 1px 0 rgba(255,255,255,.08);
        }
        .hero-chip .small{font-size:11px;text-transform:uppercase;letter-spacing:.08em;color:rgba(255,255,255,.64)}
        .hero-chip .big{font-size:18px;font-weight:900;margin-top:4px}
        .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:20px}
        .grid-2{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:20px}
        .grid-3{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:20px}
        .card{
            position:relative; overflow:hidden;
            background:var(--card-soft); border:1px solid var(--surface-ring); border-radius:var(--radius);
            padding:20px; box-shadow:var(--shadow); backdrop-filter:var(--glass);
            transition:transform .24s ease, box-shadow .24s ease, border-color .24s ease, background .24s ease;
            will-change:transform;
        }
        .card:hover{transform:translateY(-5px) scale(1.003); box-shadow:0 28px 58px rgba(15,23,42,.16)}
        body.dark .card:hover{box-shadow:0 32px 66px rgba(2,6,23,.48)}
        .card-tight{padding:16px}
        .card h3,.card h4,.card h5{margin:0 0 10px 0;letter-spacing:-.02em}
        .card h3{font-size:18px}
        .section-title{display:flex;justify-content:space-between;align-items:center;gap:12px;margin-bottom:10px}
        .section-title p{margin:0;color:var(--muted);font-size:12px}
        .metric{font-size:34px;font-weight:900;letter-spacing:-.05em;margin-top:8px;line-height:1}
        .metric-sub{font-size:12px;color:var(--muted);margin-top:10px}
        .kpi-card{
            min-height:140px;
            background:
                radial-gradient(circle at top right, rgba(99,102,241,.14) 0, rgba(99,102,241,.14) 16%, transparent 17%),
                linear-gradient(180deg, rgba(255,255,255,.16), transparent 58%),
                var(--card-soft);
        }
        body.dark .kpi-card{
            background:
                radial-gradient(circle at top right, rgba(96,165,250,.12) 0, rgba(96,165,250,.12) 16%, transparent 17%),
                linear-gradient(180deg, rgba(255,255,255,.02), transparent 58%),
                var(--card-soft);
        }
        .kpi-icon{
            width:44px;height:44px;border-radius:16px;display:grid;place-items:center;
            font-size:18px; background:linear-gradient(135deg,rgba(37,99,235,.18),rgba(124,58,237,.12));
            border:1px solid rgba(79,70,229,.12); margin-bottom:12px;
        }
        .muted{color:var(--muted);font-size:13px}
        .row{display:flex;gap:10px;flex-wrap:wrap}
        .stack{display:flex;flex-direction:column;gap:16px}
        .toolbar{display:flex;gap:10px;flex-wrap:wrap}
        .table-wrap{
            width:100%; overflow:auto; border-radius:22px;
            border:1px solid var(--line); background:linear-gradient(180deg,rgba(255,255,255,.22),transparent),var(--card-solid);
            box-shadow:inset 0 1px 0 rgba(255,255,255,.20);
        }
        table{width:100%;border-collapse:collapse;min-width:760px}
        th,td{padding:14px 15px;border-bottom:1px solid var(--line);text-align:left;font-size:13px;vertical-align:top}
        th{background:var(--soft);font-weight:900;color:var(--text);white-space:nowrap;position:sticky;top:0;z-index:1}
        tbody tr{transition:background .18s ease, transform .18s ease}
        tbody tr:hover td{background:rgba(59,130,246,.055)}
        body.dark tbody tr:hover td{background:rgba(96,165,250,.075)}
        td.long{word-break:break-word;white-space:normal;min-width:240px}
        .empty-state{
            padding:34px 26px; text-align:center; border-radius:24px;
            background:linear-gradient(180deg,rgba(255,255,255,.20),transparent),var(--card-soft);
            border:1px dashed var(--line-strong);
        }
        .empty-icon{
            width:74px;height:74px;border-radius:22px;margin:0 auto 14px;display:grid;place-items:center;
            font-size:28px;background:linear-gradient(135deg,rgba(37,99,235,.16),rgba(124,58,237,.12));
            border:1px solid rgba(79,70,229,.14)
        }
        .list-card{display:flex;flex-direction:column;gap:12px}
        .list-row{
            display:flex;align-items:flex-start;justify-content:space-between;gap:12px;
            padding:12px 0;border-bottom:1px dashed var(--line);
        }
        .list-row:last-child{border-bottom:none;padding-bottom:0}
        .list-row:first-child{padding-top:0}
        .mini-kpi{
            padding:14px;border-radius:18px;background:linear-gradient(180deg,rgba(255,255,255,.18),transparent),var(--card-soft);
            border:1px solid var(--surface-ring)
        }
        .mini-kpi .label{font-size:11px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted)}
        .mini-kpi .value{font-size:24px;font-weight:900;margin-top:6px}
        input,select,textarea{
            width:100%; padding:12px 13px; border-radius:14px; border:1px solid var(--line-strong);
            margin-top:6px; background:rgba(255,255,255,.94); color:#0f172a;
            transition:border-color .18s ease, box-shadow .18s ease, transform .18s ease;
            box-shadow:0 2px 0 rgba(255,255,255,.18) inset;
        }
        body.dark input, body.dark select, body.dark textarea{background:rgba(8,15,30,.88); color:#e5eefc; border-color:#334155}
        input:focus,select:focus,textarea:focus{outline:none;border-color:rgba(79,70,229,.48);box-shadow:0 0 0 4px rgba(79,70,229,.12)}
        textarea{min-height:90px;resize:vertical}
        label{font-size:12px;font-weight:800;color:var(--muted)}
        button,.btn{
            background:var(--btn-grad); color:#fff; border:none; padding:12px 17px; border-radius:15px; cursor:pointer;
            font-weight:900; text-decoration:none; display:inline-flex; align-items:center; justify-content:center; gap:8px; white-space:nowrap;
            transition:transform .16s ease, box-shadow .16s ease, opacity .16s ease; box-shadow:0 12px 24px rgba(79,70,229,.20);
            position:relative; overflow:hidden;
        }
        button::before,.btn::before{
            content:""; position:absolute; inset:0; background:linear-gradient(120deg,transparent,rgba(255,255,255,.22),transparent);
            transform:translateX(-140%); transition:transform .45s ease;
        }
        button:hover,.btn:hover{transform:translateY(-3px) scale(1.01);box-shadow:0 18px 30px rgba(79,70,229,.28)}
        button:hover::before,.btn:hover::before{transform:translateX(140%)}
        button:active,.btn:active{transform:translateY(0)}
        .btn.secondary{background:linear-gradient(180deg,#334155,#1f2937);box-shadow:0 10px 20px rgba(51,65,85,.18)}
        .btn.success{background:linear-gradient(180deg,#22c55e,#15803d);box-shadow:0 10px 20px rgba(34,197,94,.18)}
        .btn.warn{background:linear-gradient(180deg,#fbbf24,#d97706);color:#111827;box-shadow:0 10px 20px rgba(245,158,11,.18)}
        .btn.danger{background:linear-gradient(180deg,#ef4444,#b91c1c);box-shadow:0 10px 20px rgba(239,68,68,.18)}
        .btn.purple{background:linear-gradient(180deg,#8b5cf6,#6d28d9);box-shadow:0 10px 20px rgba(139,92,246,.20)}
        .btn.small{padding:8px 11px;font-size:12px;border-radius:12px}
        .flash{padding:13px 14px;border-radius:16px;margin-bottom:12px;font-weight:800;border:1px solid transparent;backdrop-filter:blur(8px)}
        .flash.success{background:#dcfce7;color:#166534;border-color:#86efac}
        .flash.error{background:#fee2e2;color:#991b1b;border-color:#fca5a5}
        .badge{
            display:inline-flex;align-items:center;gap:7px;padding:7px 11px;border-radius:999px;font-size:11px;font-weight:900;border:1px solid transparent;
        }
        .ok{background:#dcfce7;color:#166534;border-color:#bbf7d0}
        .warn{background:#fef3c7;color:#92400e;border-color:#fde68a}
        .danger{background:#fee2e2;color:#991b1b;border-color:#fecaca}
        .info{background:#dbeafe;color:#1d4ed8;border-color:#bfdbfe}
        .gray{background:#e2e8f0;color:#334155;border-color:#cbd5e1}
        body.dark .ok{background:#123524;color:#86efac;border-color:#166534}
        body.dark .warn{background:#4a3414;color:#fde68a;border-color:#a16207}
        body.dark .danger{background:#4a1d1d;color:#fecaca;border-color:#991b1b}
        body.dark .info{background:#18365f;color:#bfdbfe;border-color:#1d4ed8}
        body.dark .gray{background:#1f2937;color:#cbd5e1;border-color:#334155}
        .status-pill{padding:8px 12px;border-radius:999px;font-size:11px;font-weight:900;display:inline-flex;align-items:center;gap:8px}
        .status-live{background:rgba(34,197,94,.16);color:#166534;border:1px solid rgba(34,197,94,.22)}
        .status-idle{background:rgba(148,163,184,.14);color:var(--muted);border:1px solid rgba(148,163,184,.18)}
        .status-pulse{
            width:10px;height:10px;border-radius:50%;background:#22c55e;display:inline-block;animation:pulseGlow 1.5s infinite
        }
        .status-off{
            width:10px;height:10px;border-radius:50%;background:#94a3b8;display:inline-block
        }
        .tooltip{
            position:relative; display:inline-flex; align-items:center; justify-content:center;
            width:16px;height:16px;border-radius:50%; font-size:11px; margin-left:6px;
            background:rgba(59,130,246,.12); color:var(--primary); cursor:help; font-weight:900; border:1px solid rgba(59,130,246,.18)
        }
        .tooltip::after{
            content:attr(data-tip); position:absolute; left:50%; bottom:125%; transform:translateX(-50%) scale(.96);
            min-width:170px; max-width:280px; background:rgba(15,23,42,.94); color:#fff; border-radius:12px; padding:9px 10px;
            font-size:12px; line-height:1.45; white-space:normal; opacity:0; pointer-events:none; transition:.16s ease;
            box-shadow:0 16px 32px rgba(2,6,23,.28); z-index:20;
        }
        .tooltip:hover::after{opacity:1;transform:translateX(-50%) scale(1)}
        .chart-wrap{position:relative;height:360px}
        .chart-wrap.tall{height:420px}
        .chart-wrap.short{height:280px}
        .insight-list{display:flex;flex-direction:column;gap:10px}
        .insight-item{
            padding:12px 14px;border-radius:16px;border:1px solid var(--line);
            background:linear-gradient(180deg,rgba(255,255,255,.18),transparent),var(--card-solid)
        }
        .split-head{display:flex;justify-content:space-between;align-items:flex-start;gap:14px;flex-wrap:wrap}
        .progress-track{height:10px;border-radius:999px;background:rgba(148,163,184,.18);overflow:hidden}
        .progress-bar{height:100%;border-radius:999px;background:var(--btn-grad)}
        .mini-list{display:grid;gap:10px}
        .mini-item{padding:12px 13px;border-radius:16px;border:1px solid var(--line);background:rgba(255,255,255,.20)}
        body.dark .mini-item{background:rgba(255,255,255,.02)}
        .login-wrap{min-height:100vh;display:grid;place-items:center;padding:22px}
        .login-box{width:100%;max-width:1080px;display:grid;grid-template-columns:1.08fr .92fr;gap:24px;align-items:stretch}
        .login-side{background:var(--hero-grad);color:#fff;border-radius:30px;padding:34px;box-shadow:var(--shadow);position:relative;overflow:hidden}
        .login-side::before{content:"";position:absolute;inset:auto -40px -40px auto;width:180px;height:180px;border-radius:999px;background:rgba(255,255,255,.09)}
        .login-card{background:var(--card-soft);border:1px solid rgba(148,163,184,.18);border-radius:30px;padding:30px;box-shadow:var(--shadow);backdrop-filter:var(--glass)}
        .login-error{background:#fee2e2;color:#991b1b;border:1px solid #fca5a5;border-radius:12px;padding:10px 12px;margin:10px 0 14px 0;font-weight:800}
        .debug-box{background:#fff7ed;color:#9a3412;border:1px solid #fdba74;border-radius:12px;padding:14px;white-space:pre-wrap;word-break:break-word;font-family:monospace}
        .mobile-show{display:none}

        .glass-panel{background:linear-gradient(180deg,rgba(255,255,255,.12),rgba(255,255,255,.04));border:1px solid rgba(148,163,184,.18);box-shadow:0 18px 40px rgba(2,6,23,.22)}
        .ops-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:16px}
        .setting-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:14px}
        .setting-tile{padding:16px;border-radius:18px;border:1px solid var(--line);background:linear-gradient(180deg,rgba(255,255,255,.12),transparent),var(--card-soft)}
        .activity-timeline{display:flex;flex-direction:column;gap:12px}
        .activity-item{display:grid;grid-template-columns:56px 1fr;gap:12px;padding:14px;border-radius:18px;border:1px solid var(--line);background:linear-gradient(180deg,rgba(255,255,255,.10),transparent),var(--card-soft)}
        .activity-dot{width:42px;height:42px;border-radius:14px;display:grid;place-items:center;font-weight:900;background:linear-gradient(135deg,rgba(37,99,235,.22),rgba(124,58,237,.16));border:1px solid rgba(99,102,241,.18)}
        .two-col{display:grid;grid-template-columns:minmax(0,1.05fr) minmax(320px,.95fr);gap:16px}
        .app-note{padding:14px 16px;border-radius:16px;border:1px solid rgba(96,165,250,.18);background:linear-gradient(180deg,rgba(59,130,246,.12),rgba(37,99,235,.05));color:var(--text)}
        .mobile-actions{display:flex;gap:10px;flex-wrap:wrap}
        .profile-shell{display:grid;grid-template-columns:minmax(0,1fr) minmax(320px,.85fr);gap:16px}
        .stat-strip{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px}
        .compact-kpi{padding:14px 16px;border-radius:18px;background:linear-gradient(180deg,rgba(255,255,255,.10),transparent),var(--card-soft);border:1px solid var(--surface-ring)}
        .compact-kpi .k{font-size:11px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted)}
        .compact-kpi .v{font-size:22px;font-weight:900;margin-top:6px}
        @media (max-width:1320px){
            .content{padding:24px 24px 38px}
            .page-shell{max-width:1520px}
            .hero-grid,.grid-3,.grid-2{grid-template-columns:1fr}
        }
        .section-block{margin-top:22px}
        .kpi-card{min-height:156px}
        .table-wrap{box-shadow:0 10px 30px rgba(2,6,23,.10)}
        body.dark .table-wrap{box-shadow:0 14px 34px rgba(2,6,23,.28)}
        .sidebar a{min-height:54px}
        .sidebar a .nav-icon{width:36px;height:36px}
        .content > .page-shell > *{animation:fadeRise .32s ease both}
        .content > .page-shell > *:nth-child(2){animation-delay:.03s}
        .content > .page-shell > *:nth-child(3){animation-delay:.06s}
        .content > .page-shell > *:nth-child(4){animation-delay:.09s}
        .wow-balance{display:grid;grid-template-columns:minmax(0,1.2fr) minmax(320px,.8fr);gap:22px}
        .premium-stack{display:flex;flex-direction:column;gap:20px}
        .micro-float{transition:transform .24s ease, box-shadow .24s ease}
        .micro-float:hover{transform:translateY(-4px)}
        .card::after{content:"";position:absolute;inset:auto auto 0 0;width:100%;height:1px;background:linear-gradient(90deg,transparent,rgba(255,255,255,.10),transparent);opacity:.55}
        body.dark .card::before{content:"";position:absolute;inset:0;border-radius:inherit;padding:1px;background:linear-gradient(135deg,rgba(96,165,250,.12),rgba(139,92,246,.08),transparent 42%);-webkit-mask:linear-gradient(#fff 0 0) content-box, linear-gradient(#fff 0 0);-webkit-mask-composite:xor;mask-composite:exclude;pointer-events:none}
        .metric{letter-spacing:-.055em}
        .hero-chip .big{letter-spacing:-.035em}
        .table-wrap table{border-spacing:0}
        .alert-rail{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px;margin:16px 0 6px}
        .alert-rail .badge{display:flex;align-items:center;justify-content:flex-start;width:100%;padding:12px 14px;border-radius:16px;font-size:12px;line-height:1.45}
        .wide-shell{max-width:1740px;margin:0 auto;width:100%}
        @media (max-width:980px){
            .wrap{display:block}
            .sidebar{position:relative;top:0;height:auto;width:auto;border-right:none;border-bottom:1px solid rgba(148,163,184,.14)}
            .nav-group{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr))}
            .content{padding:18px}
            .login-box{grid-template-columns:1fr}
        }
        @media (max-width:680px){
            .topbar{padding:12px 14px}
            .brand-sub{display:none}
            .hero-title{font-size:24px}
            .hero{padding:20px}
            .content{padding:14px}
            .nav-group{grid-template-columns:1fr 1fr}
            .top-actions{gap:8px}
            .chip,.theme-switch{font-size:11px}
        }

        .page-shell{max-width:1720px;margin:0 auto}
        .content{padding:28px 30px 38px}
        .hero{animation:fadeRise .55s ease}
        .hero, .card, .mini-kpi, .mini-item, .table-wrap{position:relative}
        .hero::after{box-shadow:0 0 80px rgba(129,140,248,.24)}
        .card::before{
            content:"";position:absolute;inset:-1px;border-radius:inherit;padding:1px;
            background:linear-gradient(135deg, rgba(96,165,250,.18), rgba(168,85,247,.18), rgba(34,211,238,.12));
            -webkit-mask:linear-gradient(#000 0 0) content-box,linear-gradient(#000 0 0);
            -webkit-mask-composite:xor;mask-composite:exclude;pointer-events:none;opacity:.82;
        }
        .card:hover{transform:translateY(-4px) scale(1.006)}
        .wow-grid{display:grid;grid-template-columns:1.3fr .9fr;gap:16px}
        .alert-rail{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px;margin:16px 0}
        .alert-chip{padding:14px 16px;border-radius:18px;border:1px solid var(--surface-ring);background:linear-gradient(180deg,rgba(255,255,255,.14),transparent),var(--card-soft);box-shadow:var(--shadow-soft)}
        .alert-chip strong{display:block;font-size:13px;margin-bottom:4px}
        .alert-chip.ok{box-shadow:0 0 0 1px rgba(34,197,94,.18),0 0 30px rgba(34,197,94,.08)}
        .alert-chip.warn{box-shadow:0 0 0 1px rgba(245,158,11,.18),0 0 30px rgba(245,158,11,.08)}
        .alert-chip.danger{box-shadow:0 0 0 1px rgba(239,68,68,.18),0 0 30px rgba(239,68,68,.10)}
        .alert-chip.info{box-shadow:0 0 0 1px rgba(59,130,246,.18),0 0 30px rgba(59,130,246,.08)}
        .glow-card{box-shadow:0 16px 40px rgba(37,99,235,.12), 0 0 0 1px rgba(96,165,250,.08)}
        .glow-card:hover{box-shadow:0 24px 54px rgba(79,70,229,.18), 0 0 0 1px rgba(129,140,248,.14)}
        .metric{ text-shadow:0 0 24px rgba(96,165,250,.18)}
        .hero-chip .big, .metric{letter-spacing:-.06em}
        .table-wrap{overflow:auto hidden}
        .table-wrap table{min-width:100%}
        .table-wrap::-webkit-scrollbar-thumb{background:linear-gradient(180deg,#3b82f6,#8b5cf6)}
        .status-pulse{box-shadow:0 0 0 0 rgba(34,197,94,.5),0 0 22px rgba(34,197,94,.42)}
        .live-ping{display:inline-flex;align-items:center;gap:8px;padding:8px 12px;border-radius:999px;background:rgba(34,197,94,.12);border:1px solid rgba(34,197,94,.18);font-size:11px;font-weight:900}
        .spotlight-bar{height:12px;border-radius:999px;background:rgba(148,163,184,.14);overflow:hidden;position:relative}
        .spotlight-bar > span{display:block;height:100%;border-radius:inherit;background:linear-gradient(90deg,#22c55e,#3b82f6,#8b5cf6);box-shadow:0 0 18px rgba(96,165,250,.24)}
        .kpi-card .kpi-icon{box-shadow:0 0 0 1px rgba(99,102,241,.14),0 10px 28px rgba(59,130,246,.10)}
        .mini-chart-shell{padding:10px;border-radius:18px;background:linear-gradient(180deg,rgba(255,255,255,.08),transparent),rgba(2,6,23,.22);border:1px solid var(--line)}
        .heatmap-wrap{display:grid;grid-template-columns:repeat(12, minmax(0,1fr));gap:6px}
        .heat-cell{aspect-ratio:1/1;border-radius:10px;border:1px solid var(--surface-ring);display:grid;place-items:center;font-size:10px;color:var(--muted);transition:transform .16s ease, box-shadow .16s ease}
        .heat-cell:hover{transform:translateY(-2px) scale(1.03);box-shadow:0 12px 24px rgba(2,6,23,.18)}
        .heat-good{background:rgba(34,197,94,.16)}
        .heat-warn{background:rgba(245,158,11,.16)}
        .heat-bad{background:rgba(239,68,68,.16)}
        .heat-none{background:rgba(148,163,184,.08)}
        .insight-item{border-left:3px solid rgba(96,165,250,.34)}
        .hero-copy strong{color:#fff}
        .badge, .chip, .theme-switch, .btn, button{backdrop-filter:blur(10px)}
        @media (max-width: 1300px){.page-shell{max-width:1400px}.wow-grid{grid-template-columns:1fr}}
        @media (max-width: 900px){.content{padding:18px}.alert-rail{grid-template-columns:1fr}.table-wrap{overflow:auto}.hero-stats{justify-content:flex-start}}

    </style>
</head>
<body class="{{ 'dark' if session.get('theme') == 'dark' else '' }}">
<div class="app-bg">
    <div class="bg-grid"></div>
    <div class="orb orb-1"></div>
    <div class="orb orb-2"></div>
    <div class="orb orb-3"></div>
    <div class="orb orb-4"></div>
</div>
<div class="app-shell">
    <div class="topbar">
        <div class="brand-wrap">
            <div class="brand-badge">📹</div>
            <div>
                <div class="brand">Zoom Attendance Platform</div>
                <div class="brand-sub">Live tracking, analytics, reports and member intelligence</div>
            </div>
        </div>
        <div class="top-actions">
            {% if session.get('username') %}
            <span class="chip chip-user">👋 {{ session.get('username') }} ({{ session.get('role') }})</span>
            {% endif %}
            {% if session.get('user_id') %}
            <a href="{{ url_for('toggle_theme') }}" class="theme-switch">{{ 'Light Mode' if session.get('theme') == 'dark' else 'Dark Mode' }}</a>
            <a href="{{ url_for('profile') }}" class="chip">🙍 Profile</a>
            <a href="{{ url_for('logout') }}" class="chip">🚪 Logout</a>
            {% endif %}
        </div>
    </div>

    <div class="wrap">
        {% if nav %}
        <div class="sidebar">
            <div class="nav-group">
                {% for item in nav %}
                    {% set parts = item.label.split(' ', 1) %}
                    <a href="{{ item.href }}" class="{% if active == item.key %}active{% endif %}">
                        <span class="nav-icon">{{ parts[0] }}</span>
                        <span>{{ parts[1] if parts|length > 1 else item.label }}</span>
                    </a>
                {% endfor %}
            </div>
        </div>
        {% endif %}
        <div class="content">
            <div class="page-shell wide-shell">
                {% with msgs = get_flashed_messages(with_categories=true) %}
                    {% if msgs %}
                        {% for category, message in msgs %}
                            <div class="flash {{ 'error' if category == 'error' else 'success' }}">{{ message }}</div>
                        {% endfor %}
                    {% endif %}
                {% endwith %}
                {{ body|safe }}
            </div>
        </div>
    </div>
</div>
<script>
(function(){
    const tooltipMap = {
        'Total Meetings':'All meetings stored in the system.',
        'Active Members':'Members currently marked active and eligible for tracking.',
        'Attendance Health':'Present plus late participation divided by all finalized records.',
        'Live Status':'Shows whether a live meeting is currently being tracked.',
        'Present':'Met the present threshold for the meeting.',
        'Late':'Joined but stayed below the present threshold.',
        'Absent':'Did not meet the required duration or did not join.',
        'Unknown':'Participants not matched to a registered member.',
        'Predicted Next':'Simple prediction based on the recent average attendance.',
        'Duration':'Effective attendance duration after join/leave adjustment.',
        'Rejoins':'How many times the participant re-entered the meeting.',
        'Status':'Final classification for the participant or meeting.',
        'Attendance Trend':'Meeting attendance change across the selected period.',
        'Status Mix':'Distribution of present, late and absent records.',
        'Member Duration':'Total attended minutes for selected members.',
        'Health Delta':'Difference between the latest and previous meeting health.'
    };

    function applyAutoTooltips(){
        const selectors = 'th, h4, .label-with-tip';
        document.querySelectorAll(selectors).forEach((el) => {
            const raw = (el.dataset.tipKey || el.textContent || '').replace(/\\s+/g,' ').trim();
            if (!raw || el.querySelector('.tooltip')) return;
            if (!tooltipMap[raw]) return;
            const tip = document.createElement('span');
            tip.className = 'tooltip';
            tip.textContent = '?';
            tip.setAttribute('data-tip', tooltipMap[raw]);
            el.appendChild(tip);
        });
    }

    function animateMetrics(){
        document.querySelectorAll('.metric').forEach((el) => {
            if (el.dataset.animated === '1') return;
            const raw = (el.textContent || '').trim();
            const match = raw.match(/^-?\\d+(?:\\.\\d+)?/);
            if (!match) return;
            const value = parseFloat(match[0]);
            if (!Number.isFinite(value)) return;
            const suffix = raw.slice(match[0].length);
            const duration = 900;
            const start = performance.now();
            const isInt = Number.isInteger(value);
            el.dataset.animated = '1';
            function step(ts){
                const progress = Math.min((ts - start) / duration, 1);
                const eased = 1 - Math.pow(1 - progress, 3);
                const current = value * eased;
                el.textContent = (isInt ? Math.round(current) : current.toFixed(value % 1 ? 2 : 0)) + suffix;
                if (progress < 1) requestAnimationFrame(step);
                else el.textContent = raw;
            }
            requestAnimationFrame(step);
        });
    }

    function enhanceButtons(){
        document.querySelectorAll('button, .btn, .chip, .theme-switch').forEach((btn) => {
            btn.addEventListener('click', function(e){
                const ripple = document.createElement('span');
                const rect = btn.getBoundingClientRect();
                const size = Math.max(rect.width, rect.height);
                ripple.style.position = 'absolute';
                ripple.style.width = ripple.style.height = size + 'px';
                ripple.style.left = (e.clientX - rect.left - size / 2) + 'px';
                ripple.style.top = (e.clientY - rect.top - size / 2) + 'px';
                ripple.style.borderRadius = '999px';
                ripple.style.background = 'rgba(255,255,255,.22)';
                ripple.style.transform = 'scale(0)';
                ripple.style.pointerEvents = 'none';
                ripple.style.transition = 'transform .45s ease, opacity .45s ease';
                btn.appendChild(ripple);
                requestAnimationFrame(() => { ripple.style.transform = 'scale(2.6)'; ripple.style.opacity = '0'; });
                setTimeout(() => ripple.remove(), 480);
            }, {passive:true});
        });
    }

    function setupChartDefaults(){
        if (!window.Chart) return;
        const dark = document.body.classList.contains('dark');
        Chart.defaults.font.family = 'Inter, Arial, sans-serif';
        Chart.defaults.color = dark ? '#cbd5e1' : '#475569';
        Chart.defaults.plugins.legend.labels.usePointStyle = true;
        Chart.defaults.plugins.legend.labels.boxWidth = 10;
        Chart.defaults.plugins.legend.labels.padding = 16;
        Chart.defaults.plugins.legend.position = 'top';
        Chart.defaults.scale.grid.color = dark ? 'rgba(148,163,184,.12)' : 'rgba(148,163,184,.18)';
        Chart.defaults.scale.ticks.backdropColor = 'transparent';
        Chart.defaults.elements.line.borderWidth = 3;
        Chart.defaults.elements.line.tension = 0.42;
        Chart.defaults.elements.point.radius = 3;
        Chart.defaults.elements.point.hoverRadius = 6;
        Chart.defaults.maintainAspectRatio = false;
    }


    function enhanceWowEffects(){
        document.querySelectorAll('.kpi-card, .hero-chip, .mini-kpi').forEach((el, idx) => {
            el.classList.add('glow-card');
            el.style.animation = `fadeRise ${0.28 + (idx * 0.04)}s ease`;
        });
        document.querySelectorAll('.table-wrap table tbody tr').forEach((row) => {
            row.addEventListener('mouseenter', () => row.style.transform = 'translateX(2px)');
            row.addEventListener('mouseleave', () => row.style.transform = '');
        });
        const hero = document.querySelector('.hero');
        if (hero) {
            hero.addEventListener('mousemove', (e) => {
                const rect = hero.getBoundingClientRect();
                hero.style.backgroundPosition = `${((e.clientX - rect.left) / rect.width) * 100}% 50%`;
            });
        }
    }


    function polishLayoutSpacing(){
        document.querySelectorAll('.table-wrap').forEach((el)=>{
            if(el.closest('.card')) el.closest('.card').classList.add('micro-float');
        });
        document.querySelectorAll('.hero, .card').forEach((el)=>{
            el.classList.add('motion-soft');
        });
    }

    document.addEventListener('DOMContentLoaded', function(){
        applyAutoTooltips();
        animateMetrics();
        enhanceButtons();
        setupChartDefaults();
        polishLayoutSpacing();
        enhanceWowEffects();
    });
})();
</script>
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
        try:
            init_db()
            fix_database_compatibility()
            DB_INITIALIZED = True
        except Exception as e:
            print(f"⚠️ startup init skipped: {e}")


@app.errorhandler(Exception)
def handle_any_error(e):
    body = render_template_string(
        """
        <div class="hero">
            <div class="hero-grid">
                <div>
                    <div class="badge danger" style="margin-bottom:12px">Recovery Screen</div>
                    <h1 class="hero-title">Something went wrong, but the app is still safe</h1>
                    <div class="hero-copy">The request failed gracefully. Copy the technical details below and send them for support if the issue repeats.</div>
                </div>
                <div class="hero-stats">
                    <div class="hero-chip"><div class="small">Status</div><div class="big">Handled</div></div>
                    <div class="hero-chip"><div class="small">Action</div><div class="big">Retry Safe</div></div>
                </div>
            </div>
        </div>
        <div class="two-col">
            <div class="card glass-panel">
                <h3 style="margin-top:0">Technical Details</h3>
                <div class="debug-box">{{ error_text }}</div>
            </div>
            <div class="stack">
                <div class="card">
                    <h3 style="margin-top:0">Quick Recovery</h3>
                    <div class="mini-list">
                        <div class="mini-item"><div class="muted">Try opening Home again</div><div style="font-weight:900;margin-top:4px">Transient errors may clear automatically.</div></div>
                        <div class="mini-item"><div class="muted">Check data quality</div><div style="font-weight:900;margin-top:4px">Legacy or invalid rows are now handled more safely.</div></div>
                    </div>
                    <div class="mobile-actions" style="margin-top:14px">
                        <a class="btn" href="{{ url_for('home') }}">Go Home</a>
                        <a class="btn secondary" href="{{ url_for('login') }}">Back to Login</a>
                    </div>
                </div>
            </div>
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
                    <div class='login-error'>{{ login_error }}</div>\n                    <div class='app-note'>Use your assigned role credentials. The UI is mobile-friendly and tuned for dark SaaS mode.</div>
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
            <div class="hero-grid">
                <div>
                    <div class="badge info" style="margin-bottom:12px">Identity Center</div>
                    <h1 class="hero-title">My Profile & Security</h1>
                    <div class="hero-copy">Manage account identity, password security, and access posture from one polished control area.</div>
                </div>
                <div class="hero-stats">
                    <div class="hero-chip"><div class="small">Username</div><div class="big">{{ session.get('username') }}</div></div>
                    <div class="hero-chip"><div class="small">Role</div><div class="big">{{ session.get('role') }}</div></div>
                </div>
            </div>
        </div>
        <div class="stat-strip">
            <div class="compact-kpi"><div class="k">Account status</div><div class="v">Active</div></div>
            <div class="compact-kpi"><div class="k">Session theme</div><div class="v">{{ session.get('theme', 'light')|title }}</div></div>
            <div class="compact-kpi"><div class="k">Security mode</div><div class="v">Protected</div></div>
        </div>
        <div class="profile-shell" style="margin-top:16px">
            <div class="card glass-panel">
                <div class="section-title">
                    <div><h3 style="margin:0">Account Snapshot</h3><p>Profile identity and quick recovery guidance.</p></div>
                    <span class="badge ok">Stable</span>
                </div>
                <div class="mini-list">
                    <div class="mini-item"><div class="muted">Username</div><div style="font-weight:900;margin-top:4px">{{ session.get('username') }}</div></div>
                    <div class="mini-item"><div class="muted">Role</div><div style="font-weight:900;margin-top:4px">{{ session.get('role') }}</div></div>
                    <div class="mini-item"><div class="muted">Recommendation</div><div style="font-weight:900;margin-top:4px">Change your password regularly and avoid sharing admin credentials.</div></div>
                </div>
            </div>
            <div class="card">
                <div class="section-title"><div><h3 style="margin:0">Change Password</h3><p>Apply a new password without affecting current project data.</p></div></div>
                <form method="post">
                    <label>Current Password</label>
                    <input type="password" name="current_password" required>
                    <label>New Password</label>
                    <input type="password" name="new_password" required>
                    <label>Confirm New Password</label>
                    <input type="password" name="confirm_password" required>
                    <div class="app-note" style="margin:10px 0 14px 0">Use at least 4 characters. Longer passwords are safer for admin roles.</div>
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

            cur.execute("SELECT * FROM meetings ORDER BY id DESC LIMIT 8")
            recent_meetings = cur.fetchall()

            cur.execute("SELECT * FROM activity_log ORDER BY id DESC LIMIT 12")
            recent_activity = cur.fetchall()

    total_classified = present + late + absent
    health = round(((present + late) / total_classified) * 100, 2) if total_classified else 0
    latest_meeting = recent_meetings[0] if recent_meetings else None

    host_now = "No"
    unknown_live_count = 0
    if live_info and live_info.get("participants"):
        for participant_row in live_info.get("participants") or []:
            if participant_row.get("is_host") and participant_row.get("current_join") is not None:
                host_now = "Yes"
            if participant_row.get("current_join") is not None and not participant_row.get("is_member"):
                unknown_live_count += 1

    home_data = {
        "phase3_alerts": [
            {
                "level": "ok" if live_info else "info",
                "title": "Live monitoring active" if live_info else "System standing by",
                "text": "Webhook stream is tracking a current live meeting." if live_info else "No live session is open right now, but the control center is healthy.",
            },
            {
                "level": "warn" if latest_meeting and (latest_meeting.get("unknown_participants") or 0) > 0 else "ok",
                "title": "Unknown participant watch",
                "text": f"{(latest_meeting.get('unknown_participants') or 0) if latest_meeting else 0} unknown participant(s) detected in the latest meeting snapshot.",
            },
            {
                "level": "danger" if health < 75 else "ok",
                "title": "Attendance health signal",
                "text": "Attention is needed because attendance quality is below target." if health < 75 else "Attendance health is currently in a comfortable zone.",
            },
        ]
    }

    body = render_template_string(
        """
        <div class="hero">
            <div class="hero-grid">
                <div>
                    <div class="badge info" style="margin-bottom:12px">Control Center</div>
                    <h1 class="hero-title">Zoom Attendance Command Dashboard</h1>
                    <div class="hero-copy">
                        Monitor meetings, member participation, finalization quality, and reporting health from one <strong>premium control layer</strong> with richer signals, smoother interactions, and a stronger live-ops feel.
                    </div>
                    <div class="row" style="margin-top:16px">
                        <span class="badge ok">Stable tracking</span>
                        <span class="badge info">Reports ready</span>
                        <span class="badge warn">Analytics enabled</span>
                        <span class="badge gray">{{ 'Live meeting detected' if live_info else 'Waiting for next live session' }}</span>
                    </div>
                </div>
                <div class="hero-stats">
                    <div class="hero-chip">
                        <div class="small">System Health</div>
                        <div class="big">{{ health }}%</div>
                    </div>
                    <div class="hero-chip">
                        <div class="small">Current State</div>
                        <div class="big">{{ 'LIVE' if live_info else 'IDLE' }}</div>
                    </div>
                    <div class="hero-chip">
                        <div class="small">Members</div>
                        <div class="big">{{ active_members }}/{{ total_members }}</div>
                    </div>
                </div>
            </div>
        </div>

        <div class="alert-rail">
            <div class="alert-chip {{ 'ok' if live_info else 'info' }}">
                <strong>{{ 'Live monitoring active' if live_info else 'System standing by' }}</strong>
                <div class="muted">{{ 'Webhook stream is tracking a current live meeting.' if live_info else 'No live session is open right now, but the control center is healthy.' }}</div>
            </div>
            <div class="alert-chip {{ 'warn' if latest_meeting and (latest_meeting.unknown_participants or 0) > 0 else 'ok' }}">
                <strong>Unknown participant watch</strong>
                <div class="muted">{{ ((latest_meeting.unknown_participants or 0)|string) if latest_meeting else '0' }} unknown participant(s) detected in the latest meeting snapshot.</div>
            </div>
            <div class="alert-chip {{ 'danger' if health < 75 else 'ok' }}">
                <strong>Attendance health signal</strong>
                <div class="muted">{{ 'Attention is needed because attendance quality is below target.' if health < 75 else 'Attendance health is currently in a comfortable zone.' }}</div>
            </div>
        </div>

        <div class="alert-rail">
            <div class="alert-chip ok"><strong>Live engine status</strong><div class="muted">Participants are being recalculated in real time every refresh cycle.</div></div>
            <div class="alert-chip {{ 'warn' if host_now != 'Yes' else 'ok' }}"><strong>Host presence</strong><div class="muted">{{ 'Host is not currently active in the meeting.' if host_now != 'Yes' else 'Host presence has been detected successfully.' }}</div></div>
            <div class="alert-chip {{ 'danger' if unknown_live_count >= 3 else 'info' }}"><strong>Unknown participant watch</strong><div class="muted">{{ unknown_live_count }} unknown participant(s) are currently part of this session.</div></div>
        </div>

        <div class="grid">
            <div class="card kpi-card">
                <div class="kpi-icon">📂</div>
                <h4>Total Meetings</h4>
                <div class="metric">{{ total_meetings }}</div>
                <div class="metric-sub">Completed and live meetings recorded in PostgreSQL.</div>
            </div>
            <div class="card kpi-card">
                <div class="kpi-icon">👥</div>
                <h4>Active Members</h4>
                <div class="metric">{{ active_members }}</div>
                <div class="metric-sub">Total members in directory: {{ total_members }}</div>
            </div>
            <div class="card kpi-card">
                <div class="kpi-icon">🩺</div>
                <h4>Attendance Health</h4>
                <div class="metric">{{ health }}%</div>
                <div class="metric-sub">Present plus late records across finalized attendance rows.</div>
            </div>
            <div class="card kpi-card">
                <div class="kpi-icon">📡</div>
                <h4>Live Status</h4>
                <div class="metric">{{ 'LIVE' if live_info else 'IDLE' }}</div>
                <div class="metric-sub">Webhook monitoring status for current Zoom traffic.</div>
            </div>
        </div>

        <div class="alert-rail">
            {% for alert in data.phase3_alerts %}
            <div class="alert-chip {{ alert.level }}">
                <strong>{{ alert.title }}</strong>
                <div class="muted">{{ alert.text }}</div>
            </div>
            {% endfor %}
        </div>

        <div class="grid-2" style="margin-top:16px">
            <div class="card">
                <div class="section-title">
                    <div>
                        <h3 style="margin:0">Latest Meeting Spotlight</h3>
                        <p>Quick summary of the most recent tracked meeting.</p>
                    </div>
                    {% if latest_meeting %}
                    <span class="badge gray">{{ fmt_dt(latest_meeting.start_time) }}</span>
                    {% endif %}
                </div>
                {% if latest_meeting %}
                    <div class="split-head" style="margin-bottom:14px">
                        <div>
                            <div style="font-size:22px;font-weight:900;letter-spacing:-.03em">{{ latest_meeting.topic or 'Untitled Meeting' }}</div>
                            <div class="muted" style="margin-top:6px">Meeting ID: {{ latest_meeting.meeting_id or '-' }}</div>
                        </div>
                        <div class="row">
                            <span class="badge ok">Present {{ latest_meeting.present_count or 0 }}</span>
                            <span class="badge warn">Late {{ latest_meeting.late_count or 0 }}</span>
                            <span class="badge danger">Absent {{ latest_meeting.absent_count or 0 }}</span>
                            <span class="badge info">Unknown {{ latest_meeting.unknown_participants or 0 }}</span>
                        </div>
                    </div>
                    <div class="stack">
                        <div class="mini-kpi">
                            <div class="label">Command spotlight progress</div>
                            <div class="value">{{ latest_meeting.present_count or 0 }} + {{ latest_meeting.late_count or 0 }}</div>
                            {% set spotlight_total = (latest_meeting.present_count or 0) + (latest_meeting.late_count or 0) + (latest_meeting.absent_count or 0) %}
                            <div class="spotlight-bar" style="margin-top:10px">
                                <span style="width: {{ ((latest_meeting.present_count or 0) + (latest_meeting.late_count or 0)) / spotlight_total * 100 if spotlight_total else 0 }}%"></span>
                            </div>
                        </div>
                        <div class="toolbar">
                            <a class="btn" href="{{ url_for('meetings') }}">Open Meetings</a>
                            <a class="btn secondary" href="{{ url_for('analytics') }}">Open Analytics</a>
                            <a class="btn success" href="{{ url_for('live') }}">Open Live</a>
                        </div>
                    </div>
                {% else %}
                    <div class="empty-state">
                        <div class="empty-icon">📭</div>
                        <h3 style="margin-bottom:8px">No meeting summary available</h3>
                        <div class="muted">Once Zoom meetings are tracked and finalized, the latest meeting snapshot will appear here.</div>
                    </div>
                {% endif %}
            </div>

            <div class="card">
                <div class="section-title">
                    <div>
                        <h3 style="margin:0">Quick Actions</h3>
                        <p>Fast navigation into your most-used platform flows.</p>
                    </div>
                    <span class="badge {{ 'ok' if live_info else 'gray' }}">
                        <span class="{{ 'status-pulse' if live_info else 'status-off' }}"></span>
                        {{ 'Live now' if live_info else 'Idle now' }}
                    </span>
                </div>
                <div class="grid" style="grid-template-columns:repeat(2,minmax(0,1fr));gap:12px">
                    <a class="card card-tight" href="{{ url_for('live') }}" style="text-decoration:none;color:inherit">
                        <div class="kpi-icon">🟢</div>
                        <h4 style="margin:0">Live Monitor</h4>
                        <div class="muted">Track active participants, duration and live status.</div>
                    </a>
                    <a class="card card-tight" href="{{ url_for('analytics') }}" style="text-decoration:none;color:inherit">
                        <div class="kpi-icon">📈</div>
                        <h4 style="margin:0">Analytics</h4>
                        <div class="muted">Open charts, health view, risk members and exports.</div>
                    </a>
                    <a class="card card-tight" href="{{ url_for('members') }}" style="text-decoration:none;color:inherit">
                        <div class="kpi-icon">👥</div>
                        <h4 style="margin:0">Members</h4>
                        <div class="muted">Manage active members and import new people safely.</div>
                    </a>
                    <a class="card card-tight" href="{{ url_for('settings') }}" style="text-decoration:none;color:inherit">
                        <div class="kpi-icon">⚙️</div>
                        <h4 style="margin:0">Settings</h4>
                        <div class="muted">Tune thresholds and finalization behavior.</div>
                    </a>
                </div>
            </div>
        </div>

        <div class="grid-2" style="margin-top:16px">
            <div class="card">
                <div class="section-title">
                    <div>
                        <h3 style="margin:0">Recent Meetings</h3>
                        <p>Latest meeting sessions with participant counts and status.</p>
                    </div>
                    <a class="btn small secondary" href="{{ url_for('meetings') }}">See All</a>
                </div>
                <div class="table-wrap">
                    <table>
                        <tr>
                            <th>Date</th>
                            <th>Topic</th>
                            <th>Status</th>
                            <th>Participants</th>
                            <th>Health</th>
                        </tr>
                        {% for m in recent_meetings %}
                        {% set total_rows = (m.present_count or 0) + (m.late_count or 0) + (m.absent_count or 0) %}
                        {% set meeting_health = (((m.present_count or 0) + (m.late_count or 0)) / total_rows * 100) if total_rows else 0 %}
                        <tr>
                            <td>{{ fmt_dt(m.start_time) }}</td>
                            <td>{{ m.topic or 'Untitled Meeting' }}</td>
                            <td>
                                <span class="badge {{ 'ok' if m.status == 'live' else 'gray' }}">{{ m.status or '-' }}</span>
                            </td>
                            <td>{{ m.unique_participants or 0 }}</td>
                            <td>{{ '%.1f'|format(meeting_health) }}%</td>
                        </tr>
                        {% endfor %}
                    </table>
                </div>
            </div>

            <div class="card">
                <div class="section-title">
                    <div>
                        <h3 style="margin:0">Recent Activity</h3>
                        <p>Most recent system actions and webhook events.</p>
                    </div>
                    <a class="btn small secondary" href="{{ url_for('activity') }}">Open Log</a>
                </div>
                <div class="list-card">
                    {% for item in recent_activity %}
                    <div class="list-row">
                        <div>
                            <div style="font-weight:800">{{ item.action or '-' }}</div>
                            <div class="muted">{{ item.username or 'system' }}</div>
                        </div>
                        <div style="text-align:right;max-width:58%">
                            <div class="muted">{{ fmt_dt(item.created_at) }}</div>
                            <div style="margin-top:4px;font-size:12px">{{ item.details or '-' }}</div>
                        </div>
                    </div>
                    {% endfor %}
                </div>
            </div>
        </div>
        """,
        total_meetings=total_meetings,
        total_members=total_members,
        active_members=active_members,
        present=present,
        late=late,
        absent=absent,
        recent_meetings=recent_meetings,
        recent_activity=recent_activity,
        health=health,
        live_info=live_info,
        latest_meeting=latest_meeting,
        host_now=host_now,
        unknown_live_count=unknown_live_count,
        data=home_data,
        fmt_dt=fmt_dt,
    )
    return page("Home", body, "home")


@app.route("/live")
@login_required

def live():
    maybe_finalize_stale_live_meetings()
    info = read_live_snapshot()

    if not info:
        body = render_template_string(
            """
            <meta http-equiv='refresh' content='2'>
            <div class="hero">
                <div class="hero-grid">
                    <div>
                        <div class="badge gray" style="margin-bottom:12px">Live Monitor</div>
                        <h1 class="hero-title">Waiting for the next Zoom session</h1>
                        <div class="hero-copy">
                            The live dashboard auto-refreshes every 2 seconds. Start a meeting and send webhook events to see participants, durations, status, and live attendance flow here.
                        </div>
                    </div>
                    <div class="hero-stats">
                        <div class="hero-chip">
                            <div class="small">Refresh Rate</div>
                            <div class="big">2 sec</div>
                        </div>
                        <div class="hero-chip">
                            <div class="small">State</div>
                            <div class="big">IDLE</div>
                        </div>
                    </div>
                </div>
            </div>

            <div class="empty-state">
                <div class="empty-icon">📡</div>
                <h3 style="margin:0 0 8px 0">No active live meeting right now</h3>
                <div class="muted" style="max-width:700px;margin:0 auto">
                    Once Zoom sends participant join and leave events, this page will transform into the real-time live operations board.
                </div>
                <div class="toolbar" style="justify-content:center;margin-top:18px">
                    <a class="btn" href="{{ url_for('home') }}">Back to Home</a>
                    <a class="btn secondary" href="{{ url_for('meetings') }}">View Meeting History</a>
                </div>
            </div>
            """
        )
        return page("Live", body, "live")

    meeting = info["meeting"]
    participants = info["participants"]
    not_joined = info["not_joined_members"]

    rows_for_live = []
    start_dt = parse_dt(meeting.get("start_time")) or now_local()
    active_now = 0
    host_now = "No"
    member_live_count = 0
    unknown_live_count = 0
    live_join_feed = []
    live_unknown_rows = []
    live_known_rows = []
    for p in participants:
        live_status, live_total = get_live_status_for_row(p, start_dt)
        is_active_now = p.get("current_join") is not None
        if is_active_now:
            active_now += 1
        if p.get("is_host") and is_active_now:
            host_now = "Yes"
        if p.get("is_member"):
            member_live_count += 1
            live_known_rows.append(p)
        else:
            unknown_live_count += 1
            live_unknown_rows.append(p)
        entry = {
            "participant_name": p.get("participant_name"),
            "first_join": p.get("first_join"),
            "last_leave": p.get("last_leave"),
            "duration_min": mins_from_seconds(live_total),
            "rejoin_count": p.get("rejoin_count") or 0,
            "status": live_status,
            "is_active_now": is_active_now,
            "member_type": "Known" if p.get("is_member") else "Unknown",
        }
        rows_for_live.append(entry)
        live_join_feed.append({
            "name": p.get("participant_name") or "-",
            "time": fmt_time_ampm(p.get("first_join")) if p.get("first_join") else "-",
            "tag": "LIVE" if is_active_now else ("KNOWN" if p.get("is_member") else "UNKNOWN"),
        })
    known_unknown_ratio = f"{member_live_count} / {unknown_live_count}"
    live_risk_banner = "Healthy" if host_now == "Yes" and unknown_live_count <= max(1, member_live_count // 2) else ("Warning" if active_now > 0 else "Critical")

    body = render_template_string(
        """
        <meta http-equiv='refresh' content='2'>
        <div class="hero live-hero-shell">
            <div class="live-light live-light-1"></div>
            <div class="live-light live-light-2"></div>
            <div class="hero-grid">
                <div>
                    <div class="live-ping" style="margin-bottom:12px"><span class="status-pulse"></span> LIVE OPERATIONS BOARD</div>
                    <h1 class="hero-title">{{ meeting.topic or 'Untitled Meeting' }}</h1>
                    <div class="hero-copy">
                        Real-time command board for participant flow, host visibility, member presence, unknown risk, and attendance movement. This page refreshes every 2 seconds.
                    </div>
                    <div class="row" style="margin-top:16px;gap:10px;flex-wrap:wrap">
                        <span class="badge info">Meeting ID {{ meeting.meeting_id or '-' }}</span>
                        <span class="badge gray">Started {{ fmt_dt(meeting.start_time) }}</span>
                        <span class="badge {{ 'ok' if host_now == 'Yes' else 'warn' if active_now else 'danger' }}">Host {{ 'present' if host_now == 'Yes' else 'absent' }}</span>
                        <span class="badge {{ 'ok' if live_risk_banner == 'Healthy' else 'warn' if live_risk_banner == 'Warning' else 'danger' }}">Risk {{ live_risk_banner }}</span>
                    </div>
                </div>
                <div class="hero-stats">
                    <div class="hero-chip"><div class="small">Live Participants</div><div class="big live-counter">{{ active_now }}</div></div>
                    <div class="hero-chip"><div class="small">Known / Unknown</div><div class="big">{{ known_unknown_ratio }}</div></div>
                    <div class="hero-chip"><div class="small">Not Joined</div><div class="big">{{ not_joined|length }}</div></div>
                </div>
            </div>
        </div>

        <div class="grid" style="margin-top:16px">
            <div class="card kpi-card"><div class="kpi-icon">🔴</div><h4>Live Pulse</h4><div class="metric">{{ 'ACTIVE' if active_now else 'WAITING' }}</div><div class="metric-sub">Blinking board status with auto-refresh.</div></div>
            <div class="card kpi-card"><div class="kpi-icon">👥</div><h4>Live Participants Counter</h4><div class="metric">{{ active_now }}</div><div class="metric-sub">Participants currently inside the meeting.</div></div>
            <div class="card kpi-card"><div class="kpi-icon">🧑</div><h4>Host Status</h4><div class="metric">{{ 'Present' if host_now == 'Yes' else 'Absent' }}</div><div class="metric-sub">Current host visibility based on live webhook flow.</div></div>
            <div class="card kpi-card"><div class="kpi-icon">🪪</div><h4>Known vs Unknown</h4><div class="metric">{{ known_unknown_ratio }}</div><div class="metric-sub">Registered members compared with unmatched attendees.</div></div>
        </div>

        <div class="grid-2" style="margin-top:16px;grid-template-columns:minmax(0,1.45fr) minmax(320px,.55fr)">
            <div class="card">
                <div class="section-title">
                    <div>
                        <h3 style="margin:0">Live Participants Board</h3>
                        <p>Participant status, known/unknown split, duration growth, and rejoin count.</p>
                    </div>
                    <span class="badge ok"><span class="status-pulse"></span> Auto refresh</span>
                </div>
                <div class="table-wrap">
                    <table>
                        <tr>
                            <th>Name</th>
                            <th>Type</th>
                            <th>Join</th>
                            <th>Leave</th>
                            <th>Duration</th>
                            <th>Rejoins</th>
                            <th>Status</th>
                        </tr>
                        {% for p in live_rows %}
                        <tr>
                            <td><b>{{ p.participant_name }}</b></td>
                            <td><span class="badge {{ 'ok' if p.member_type == 'Known' else 'warn' }}">{{ p.member_type }}</span></td>
                            <td>{{ fmt_time_ampm(p.first_join) if p.first_join else '-' }}</td>
                            <td>{{ fmt_time_ampm(p.last_leave) if p.last_leave else ('Live now' if p.is_active_now else '-') }}</td>
                            <td>{{ p.duration_min }}</td>
                            <td>{{ p.rejoin_count }}</td>
                            <td>{% if p.is_active_now %}<span class="status-pill status-live"><span class="status-pulse"></span>{{ p.status }}</span>{% else %}<span class="badge {{ 'ok' if p.status == 'PRESENT' else 'warn' if p.status == 'LATE' else 'gray' if p.status == 'HOST' else 'danger' }}">{{ p.status }}</span>{% endif %}</td>
                        </tr>
                        {% endfor %}
                    </table>
                </div>
            </div>

            <div class="stack">
                <div class="card">
                    <div class="section-title"><div><h3 style="margin:0">Live Join Feed</h3><p>Scrolling feed of tracked joins and active rows.</p></div></div>
                    <div class="list-card" style="max-height:340px;overflow:auto">
                        {% for item in live_join_feed %}
                        <div class="list-row"><div><div style="font-weight:900">{{ item.name }}</div><div class="muted">Joined {{ item.time }}</div></div><span class="badge {{ 'ok' if item.tag == 'KNOWN' else 'info' if item.tag == 'LIVE' else 'warn' }}">{{ item.tag }}</span></div>
                        {% else %}
                        <div class="muted">No live join data yet.</div>
                        {% endfor %}
                    </div>
                </div>

                <div class="card">
                    <div class="section-title"><div><h3 style="margin:0">Members Not Yet Joined</h3><p>Active members still absent from the live session.</p></div></div>
                    {% if not_joined %}
                        <div class="list-card" style="max-height:300px;overflow:auto">
                            {% for m in not_joined[:18] %}
                            <div class="list-row"><div><div style="font-weight:800">{{ member_display_name(m) }}</div><div class="muted">{{ m.email or m.phone or 'No contact info' }}</div></div><span class="badge danger">Not joined</span></div>
                            {% endfor %}
                        </div>
                    {% else %}
                        <div class="empty-state" style="padding:22px 18px"><div class="empty-icon" style="width:58px;height:58px;font-size:22px">✅</div><div style="font-weight:900;margin-bottom:6px">All active members joined</div><div class="muted">No pending active member remains outside the current session.</div></div>
                    {% endif %}
                </div>
            </div>
        </div>
        """,
        meeting=meeting,
        live_rows=rows_for_live,
        not_joined=not_joined,
        active_now=active_now,
        host_now=host_now,
        member_live_count=member_live_count,
        unknown_live_count=unknown_live_count,
        known_unknown_ratio=known_unknown_ratio,
        live_risk_banner=live_risk_banner,
        live_join_feed=live_join_feed,
        fmt_dt=fmt_dt,
        fmt_time_ampm=fmt_time_ampm,
        member_display_name=member_display_name,
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
    latest_meeting = data.get("latest_meeting_summary")
    previous_meeting = data.get("previous_meeting_summary")
    comparison_delta = data.get("comparison_delta")

    body = render_template_string(
        """
        <div class="hero">
            <div class="hero-grid">
                <div>
                    <div class="badge info" style="margin-bottom:12px">Analytics Studio</div>
                    <h1 class="hero-title">Advanced Attendance Intelligence</h1>
                    <div class="hero-copy">
                        Explore attendance health, trend movement, member engagement, risk indicators, and exportable filtered views without changing your backend workflow.
                    </div>
                </div>
                <div class="hero-stats">
                    <div class="hero-chip">
                        <div class="small">Rows</div>
                        <div class="big">{{ data.summary.total_rows }}</div>
                    </div>
                    <div class="hero-chip">
                        <div class="small">Health</div>
                        <div class="big">{{ data.summary.current_meeting_health }} / 100</div>
                    </div>
                    <div class="hero-chip">
                        <div class="small">Predicted Next</div>
                        <div class="big">{{ data.summary.current_meeting_health }}</div>
                    </div>
                </div>
            </div>
        </div>

        <div class="card">
            <div class="section-title">
                <div>
                    <h3 style="margin:0">Analytics Filters</h3>
                    <p>Slice attendance by period, person, member, meeting, and participant type.</p>
                </div>
            </div>
            <form method="get">
                <div class="grid" style="grid-template-columns:1.1fr 1fr 1fr 1.15fr 1.1fr 1.1fr 1fr;">
                    <div>
                        <label>Period Mode</label>
                        <select name="period_mode">
                            <option value="day" {% if filters.period_mode == 'day' %}selected{% endif %}>Day</option>
                            <option value="week" {% if filters.period_mode == 'week' %}selected{% endif %}>Week</option>
                            <option value="month" {% if filters.period_mode == 'month' %}selected{% endif %}>Month</option>
                            <option value="year" {% if filters.period_mode == 'year' %}selected{% endif %}>Year</option>
                            <option value="custom" {% if filters.period_mode == 'custom' %}selected{% endif %}>Custom</option>
                        </select>
                    </div>
                    <div>
                        <label>From Date</label>
                        <input type="date" name="from_date" value="{{ filters.from_date }}">
                    </div>
                    <div>
                        <label>To Date</label>
                        <input type="date" name="to_date" value="{{ filters.to_date }}">
                    </div>
                    <div>
                        <label>Meeting</label>
                        <select name="meeting_uuid">
                            <option value="">All meetings</option>
                            {% for m in data.meetings %}
                            <option value="{{ m.meeting_uuid }}" {% if filters.meeting_uuid == m.meeting_uuid %}selected{% endif %}>
                                {{ m.topic or 'Untitled Meeting' }} - {{ fmt_dt(m.start_time) }}
                            </option>
                            {% endfor %}
                        </select>
                    </div>
                    <div>
                        <label>Members</label>
                        <select name="member_ids" multiple style="min-height:132px">
                            {% for m in data.members %}
                            <option value="{{ m.id }}" {% if m.id|string in filters.member_ids %}selected{% endif %}>{{ m.display_name or member_display_name(m) }}</option>
                            {% endfor %}
                        </select>
                    </div>
                    <div>
                        <label>Person Search</label>
                        <input type="text" name="person_name" value="{{ filters.person_name }}" placeholder="type participant name">
                    </div>
                    <div>
                        <label>Participant Type</label>
                        <select name="participant_type">
                            <option value="all" {% if filters.participant_type == 'all' %}selected{% endif %}>All</option>
                            <option value="member" {% if filters.participant_type == 'member' %}selected{% endif %}>Member</option>
                            <option value="unknown" {% if filters.participant_type == 'unknown' %}selected{% endif %}>Unknown</option>
                            <option value="host" {% if filters.participant_type == 'host' %}selected{% endif %}>Host</option>
                        </select>
                    </div>
                </div>
                <div class="toolbar" style="margin-top:8px">
                    <button type="submit">Apply Filters</button>
                    <a class="btn success" href="{{ export_csv_url }}">Export CSV</a>
                    <a class="btn secondary" href="{{ export_pdf_url }}">Export PDF</a>
                </div>
            </form>
        </div>

        <div class="grid" style="margin-top:16px">
            <div class="card kpi-card">
                <div class="kpi-icon">🧾</div>
                <h4>Total Rows</h4>
                <div class="metric">{{ data.summary.total_rows }}</div>
                <div class="metric-sub">Attendance records matching the current filter state.</div>
            </div>
            <div class="card kpi-card">
                <div class="kpi-icon">✅</div>
                <h4>Present</h4>
                <div class="metric">{{ data.summary.present_rows }}</div>
                <div class="metric-sub">Participants who met the present threshold.</div>
            </div>
            <div class="card kpi-card">
                <div class="kpi-icon">⏳</div>
                <h4>Late</h4>
                <div class="metric">{{ data.summary.late_rows }}</div>
                <div class="metric-sub">Attended but below the required present duration.</div>
            </div>
            <div class="card kpi-card">
                <div class="kpi-icon">🚫</div>
                <h4>Absent</h4>
                <div class="metric">{{ data.summary.absent_rows }}</div>
                <div class="metric-sub">Rows classified as absent in the filtered dataset.</div>
            </div>
            <div class="card kpi-card">
                <div class="kpi-icon">❓</div>
                <h4>Unknown</h4>
                <div class="metric">{{ data.summary.unknown_rows }}</div>
                <div class="metric-sub">Participants not matched to a registered member.</div>
            </div>
            <div class="card kpi-card">
                <div class="kpi-icon">🔮</div>
                <h4>Meeting Health</h4>
                <div class="metric">{{ data.summary.current_meeting_health }}</div>
                <div class="metric-sub">Weighted score from attendance, duration and participation.</div>
            </div>
        </div>

        <div class="grid-2" style="margin-top:16px">
            <div class="card">
                <div class="section-title">
                    <div>
                        <h3 style="margin:0">Attendance Trend</h3>
                        <p>Present, late and absent distribution over the selected period.</p>
                    </div>
                </div>
                <div class="chart-wrap tall"><canvas id="trendChart"></canvas></div>
            </div>
            <div class="card">
                <div class="section-title">
                    <div>
                        <h3 style="margin:0">Status Mix</h3>
                        <p>How the current filtered rows are distributed by classification.</p>
                    </div>
                </div>
                <div class="chart-wrap"><canvas id="statusMixChart"></canvas></div>
            </div>
        </div>

        <div class="grid-2" style="margin-top:16px">
            <div class="card">
                <div class="section-title">
                    <div>
                        <h3 style="margin:0">Member Duration</h3>
                        <p>{{ member_chart.subtitle }}</p>
                    </div>
                </div>
                {% if member_chart.empty %}
                    <div class="empty-state" style="padding:24px 18px">
                        <div class="empty-icon" style="width:58px;height:58px;font-size:22px">📊</div>
                        <div style="font-weight:900;margin-bottom:6px">No member duration data</div>
                        <div class="muted">Adjust filters or wait for tracked member attendance to appear.</div>
                    </div>
                {% else %}
                    <div class="chart-wrap"><canvas id="memberDurationChart"></canvas></div>
                {% endif %}
            </div>
            <div class="card">
                <div class="section-title">
                    <div>
                        <h3 style="margin:0">Health Snapshot</h3>
                        <p>Latest meeting comparison and summary performance indicators.</p>
                    </div>
                </div>
                <div class="stack">
                    <div class="grid-2">
                        <div class="mini-kpi">
                            <div class="label">Attendance Health</div>
                            <div class="value">{{ data.summary.attendance_health }}%</div>
                        </div>
                        <div class="mini-kpi">
                            <div class="label">Health Delta</div>
                            <div class="value">
                                {% if comparison_delta is not none %}
                                    {{ '+' if comparison_delta >= 0 else '' }}{{ comparison_delta }}
                                {% else %}
                                    -
                                {% endif %}
                            </div>
                        </div>
                    </div>
                    <div class="mini-list">
                        <div class="mini-item">
                            <div class="muted">Latest meeting</div>
                            <div style="font-weight:900;margin-top:4px">{{ latest_meeting.topic if latest_meeting else 'No meeting yet' }}</div>
                            <div class="muted" style="margin-top:4px">{{ fmt_dt(latest_meeting.start_time) if latest_meeting else '-' }}</div>
                        </div>
                        <div class="mini-item">
                            <div class="muted">Average attendance score</div>
                            <div style="font-weight:900;margin-top:4px">{{ data.summary.avg_attendance_score }}</div>
                        </div>
                        <div class="mini-item">
                            <div class="muted">Average engagement score</div>
                            <div style="font-weight:900;margin-top:4px">{{ data.summary.avg_engagement_score }}</div>
                        </div>
                    </div>
                </div>
            </div>
        </div>

        <div class="grid-3" style="margin-top:16px">
            <div class="card">
                <div class="section-title">
                    <div>
                        <h3 style="margin:0">Top Members</h3>
                        <p>Top performers ranked by weighted attendance score, consistency and duration.</p>
                    </div>
                </div>
                <div class="list-card">
                    {% for item in data.top_people %}
                    <div class="list-row">
                        <div>
                            <div style="font-weight:900">{{ item.name }}</div>
                            <div class="muted">Attendance {{ item.attendance_score }} · Engagement {{ item.engagement_score }}</div>
                        </div>
                        <span class="badge ok">{{ item.overall_score }}</span>
                    </div>
                    {% else %}
                    <div class="muted">No ranked members available.</div>
                    {% endfor %}
                </div>
            </div>

            <div class="card">
                <div class="section-title">
                    <div>
                        <h3 style="margin:0">Risk Members</h3>
                        <p>Members in warning or critical risk zone.</p>
                    </div>
                </div>
                <div class="list-card">
                    {% for item in data.risk_table[:8] %}
                    <div class="list-row">
                        <div>
                            <div style="font-weight:900">{{ item.name }}</div>
                            <div class="muted">{{ item.risk.label }} · Overall {{ item.overall_score }}</div>
                        </div>
                        <span class="badge {{ 'danger' if item.risk.short == 'CRITICAL' else 'warn' }}">{{ item.risk.short }}</span>
                    </div>
                    {% else %}
                    <div class="muted">No members are currently in warning or critical state.</div>
                    {% endfor %}
                </div>
            </div>

            <div class="card">
                <div class="section-title">
                    <div>
                        <h3 style="margin:0">Insights</h3>
                        <p>Auto-generated interpretation from the filtered dataset.</p>
                    </div>
                </div>
                <div class="insight-list">
                    {% for line in data.summary.insight_lines %}
                    <div class="insight-item">{{ line }}</div>
                    {% else %}
                    <div class="insight-item">Not enough data yet to generate analytics insights.</div>
                    {% endfor %}
                    {% if data.reminder_suggestion.count %}
                    <div class="insight-item">Reminder suggestion: {{ data.reminder_suggestion.message }}</div>
                    {% endif %}
                </div>
            </div>
        </div>

        <div class="grid-2" style="margin-top:16px">
            <div class="card">
                <div class="section-title">
                    <div>
                        <h3 style="margin:0">Operational Alerts</h3>
                        <p>Auto-detected reminders, unknown spikes, and meeting health warnings.</p>
                    </div>
                    <a class="btn warn small" href="{{ url_for('analytics_reminder', **request.args) }}">Trigger Reminder Suggestion</a>
                </div>
                <div class="insight-list">
                    {% for alert in data.alerts %}
                    <div class="insight-item" style="border-left:4px solid {% if alert.level == 'danger' %}#ef4444{% elif alert.level == 'warn' %}#f59e0b{% elif alert.level == 'ok' %}#22c55e{% else %}#3b82f6{% endif %}">
                        <div style="font-weight:900">{{ alert.title }}</div>
                        <div class="muted" style="margin-top:4px">{{ alert.text }}</div>
                    </div>
                    {% endfor %}
                </div>
            </div>
            <div class="card">
                <div class="section-title">
                    <div>
                        <h3 style="margin:0">Duration Distribution</h3>
                        <p>How attendance durations are distributed across the filtered records.</p>
                    </div>
                </div>
                <div class="mini-list">
                    {% for bucket, count in data.summary.duration_distribution.items() %}
                    <div class="mini-item">
                        <div class="muted">{{ bucket }} minutes</div>
                        <div style="font-weight:900;margin-top:4px">{{ count }} record(s)</div>
                    </div>
                    {% endfor %}
                </div>
            </div>
        </div>

        <div class="grid-2" style="margin-top:16px">
            <div class="card">
                <div class="section-title">
                    <div>
                        <h3 style="margin:0">Auto Actions</h3>
                        <p>Suggested next actions based on risk, live quality, and meeting intelligence.</p>
                    </div>
                </div>
                <div class="insight-list">
                    {% for action in data.auto_actions %}
                    <div class="insight-item">{{ action }}</div>
                    {% endfor %}
                </div>
            </div>
            <div class="card">
                <div class="section-title">
                    <div>
                        <h3 style="margin:0">Attendance Heatmap</h3>
                        <p>Recent participation footprint for the selected member scope.</p>
                    </div>
                </div>
                <div style="display:grid;grid-template-columns:repeat(14,minmax(0,1fr));gap:6px">
                    {% for cell in data.heatmap %}
                    <div title="{{ cell.title }}" style="height:24px;border-radius:7px;display:grid;place-items:center;font-size:10px;
                        background:{% if cell.css == 'heat-good' %}rgba(34,197,94,.35){% elif cell.css == 'heat-warn' %}rgba(245,158,11,.35){% elif cell.css == 'heat-bad' %}rgba(239,68,68,.35){% else %}rgba(148,163,184,.16){% endif %};
                        border:1px solid rgba(255,255,255,.06)">
                        {{ cell.day }}
                    </div>
                    {% endfor %}
                </div>
            </div>
            <div class="card">
                <div class="section-title">
                    <div>
                        <h3 style="margin:0">Unknown Match Suggestions</h3>
                        <p>Potential member matches for unknown participant names.</p>
                    </div>
                </div>
                <div class="list-card">
                    {% for suggestion in data.unknown_match_suggestions %}
                    <div class="list-row">
                        <div>
                            <div style="font-weight:900">{{ suggestion.unknown }}</div>
                            <div class="muted">Possible match: {{ suggestion.member }}</div>
                        </div>
                        <span class="badge info">{{ suggestion.score }}%</span>
                    </div>
                    {% else %}
                    <div class="muted">No likely unknown-to-member match suggestions right now.</div>
                    {% endfor %}
                </div>
            </div>
        </div>


        <script>
        (() => {
            const trendCanvas = document.getElementById('trendChart');
            if (trendCanvas) {
                new Chart(trendCanvas, {
                    type: 'line',
                    data: {
                        labels: {{ trend.labels|tojson }},
                        datasets: [
                            {
                                label: 'Present',
                                data: {{ trend.present|tojson }},
                                borderColor: '#22c55e',
                                backgroundColor: 'rgba(34,197,94,.12)',
                                fill: true
                            },
                            {
                                label: 'Late',
                                data: {{ trend.late|tojson }},
                                borderColor: '#f59e0b',
                                backgroundColor: 'rgba(245,158,11,.10)',
                                fill: true
                            },
                            {
                                label: 'Absent',
                                data: {{ trend.absent|tojson }},
                                borderColor: '#ef4444',
                                backgroundColor: 'rgba(239,68,68,.08)',
                                fill: true
                            }
                        ]
                    },
                    options: {
                        interaction: {mode: 'index', intersect: false},
                        plugins: {legend: {display: true}}
                    }
                });
            }

            const mixCanvas = document.getElementById('statusMixChart');
            if (mixCanvas) {
                new Chart(mixCanvas, {
                    type: 'doughnut',
                    data: {
                        labels: ['Present', 'Late', 'Absent'],
                        datasets: [{
                            data: [{{ data.summary.present_rows }}, {{ data.summary.late_rows }}, {{ data.summary.absent_rows }}],
                            backgroundColor: ['#22c55e','#f59e0b','#ef4444'],
                            borderWidth: 0,
                            hoverOffset: 8
                        }]
                    },
                    options: {
                        cutout: '68%',
                        plugins: {legend: {display: true}}
                    }
                });
            }

            const memberCanvas = document.getElementById('memberDurationChart');
            if (memberCanvas) {
                new Chart(memberCanvas, {
                    type: 'bar',
                    data: {
                        labels: {{ member_chart.labels|tojson }},
                        datasets: [{
                            label: 'Minutes',
                            data: {{ member_chart.chart_values|tojson }},
                            borderRadius: 10,
                            backgroundColor: ['rgba(37,99,235,.78)','rgba(79,70,229,.78)','rgba(124,58,237,.78)','rgba(34,197,94,.72)','rgba(8,145,178,.72)','rgba(245,158,11,.72)','rgba(239,68,68,.72)']
                        }]
                    },
                    options: {
                        plugins: {legend: {display: false}},
                        scales: {
                            x: {grid: {display: false}},
                            y: {beginAtZero: true}
                        }
                    }
                });
            }
        })();
        </script>
        """,
        filters=data["filters"],
        data=data,
        trend=trend,
        member_chart=member_chart,
        fmt_dt=fmt_dt,
        member_display_name=member_display_name,
        export_csv_url=export_csv_url,
        export_pdf_url=export_pdf_url,
        latest_meeting=latest_meeting,
        previous_meeting=previous_meeting,
        comparison_delta=comparison_delta,
        request=request,
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
                                    <a class='btn purple small' href='{{ url_for("meeting_excel", meeting_uuid=m.meeting_uuid) }}'>Excel</a>
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




@app.route("/meetings/<path:meeting_uuid>/report.xlsx")
@login_required
def meeting_excel(meeting_uuid):
    if not meeting_uuid:
        flash("Meeting UUID missing for this record.", "error")
        return redirect(url_for("meetings"))

    report_data = build_meeting_report_data(meeting_uuid)
    if not report_data:
        flash("Meeting report data not found.", "error")
        return redirect(url_for("meetings"))

    content = export_meeting_excel_bytes(report_data)
    filename = slugify(build_meeting_pdf_filename(report_data).replace(".pdf", "")) + ".csv"
    return Response(
        content,
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
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
            <div class="hero-grid">
                <div>
                    <div class="badge info" style="margin-bottom:12px">System Controls</div>
                    <h1 class="hero-title">Attendance Settings & Reliability Controls</h1>
                    <div class="hero-copy">Tune thresholds, stale-meeting finalization, and attendance rules with a clearer production-safe settings experience.</div>
                </div>
                <div class="hero-stats">
                    <div class="hero-chip"><div class="small">Present %</div><div class="big">{{ s.present_percentage }}</div></div>
                    <div class="hero-chip"><div class="small">Late %</div><div class="big">{{ s.late_count_as_present_percentage }}</div></div>
                </div>
            </div>
        </div>
        <div class="stat-strip">
            <div class="compact-kpi"><div class="k">Finalize seconds</div><div class="v">{{ s.meeting_finalize_seconds }}</div></div>
            <div class="compact-kpi"><div class="k">Late threshold</div><div class="v">{{ s.late_threshold_minutes }}m</div></div>
            <div class="compact-kpi"><div class="k">Fallback cache</div><div class="v">Enabled</div></div>
        </div>
        <div class="two-col" style="margin-top:16px">
            <div class="card glass-panel">
                <div class="section-title"><div><h3 style="margin:0">Rule Configuration</h3><p>All values continue to use your existing settings table and logic.</p></div></div>
                <form method='post'>
                    <div class="setting-grid">
                        <div class="setting-tile"><label>Present Percentage</label><input name='present_percentage' value='{{ s.present_percentage }}'></div>
                        <div class="setting-tile"><label>Late Count As Present Percentage</label><input name='late_count_as_present_percentage' value='{{ s.late_count_as_present_percentage }}'></div>
                        <div class="setting-tile"><label>Late Threshold Minutes</label><input name='late_threshold_minutes' value='{{ s.late_threshold_minutes }}'></div>
                        <div class="setting-tile"><label>Meeting Finalize Seconds</label><input name='meeting_finalize_seconds' value='{{ s.meeting_finalize_seconds }}'></div>
                    </div>
                    <div class="mobile-actions" style="margin-top:14px"><button type='submit'>Save Settings</button></div>
                </form>
            </div>
            <div class="stack">
                <div class="card">
                    <h3 style="margin-top:0">Reliability Notes</h3>
                    <div class="mini-list">
                        <div class="mini-item"><div class="muted">Startup flow</div><div style="font-weight:900;margin-top:4px">Initialization now fails gracefully instead of crashing the whole app.</div></div>
                        <div class="mini-item"><div class="muted">Settings cache</div><div style="font-weight:900;margin-top:4px">Cached defaults keep the app usable even during temporary DB issues.</div></div>
                        <div class="mini-item"><div class="muted">Legacy rows</div><div style="font-weight:900;margin-top:4px">Existing data stays compatible with old and new database values.</div></div>
                    </div>
                </div>
            </div>
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
            <div class="hero-grid">
                <div>
                    <div class="badge info" style="margin-bottom:12px">Audit Trail</div>
                    <h1 class="hero-title">Activity Timeline & System Events</h1>
                    <div class="hero-copy">Track operator actions, webhook events, and system movement through a cleaner audit experience.</div>
                </div>
                <div class="hero-stats">
                    <div class="hero-chip"><div class="small">Recent entries</div><div class="big">{{ rows|length }}</div></div>
                    <div class="hero-chip"><div class="small">View mode</div><div class="big">Timeline</div></div>
                </div>
            </div>
        </div>
        <div class="two-col">
            <div class="card glass-panel">
                <div class="section-title"><div><h3 style="margin:0">Recent Activity Timeline</h3><p>Latest log entries in a quick-scan operational format.</p></div></div>
                <div class="activity-timeline">
                    {% for a in rows[:18] %}
                    <div class="activity-item">
                        <div class="activity-dot">{{ (a.action or '•')[:1]|upper }}</div>
                        <div>
                            <div style="display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap">
                                <div style="font-weight:900">{{ a.action }}</div>
                                <div class="muted">{{ fmt_dt(a.created_at) }}</div>
                            </div>
                            <div class="muted" style="margin-top:4px">{{ a.username or 'system' }}</div>
                            <div style="margin-top:6px;font-size:13px">{{ a.details or '-' }}</div>
                        </div>
                    </div>
                    {% else %}
                    <div class="empty-state"><div class="empty-icon">📝</div><div style="font-weight:900;margin-bottom:6px">No activity yet</div><div class="muted">Once actions occur, the audit timeline will populate here.</div></div>
                    {% endfor %}
                </div>
            </div>
            <div class="stack">
                <div class="card">
                    <h3 style="margin-top:0">Activity Table</h3>
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
            </div>
        </div>
        """,
        rows=rows,
        fmt_dt=fmt_dt,
    )
    return page("Activity", body, "activity")


@app.route("/favicon.ico")
def favicon():
    return Response(status=204)


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


@app.route("/push/vapid-key")
@login_required
def push_vapid_key():
    if not is_web_push_configured():
        return jsonify({"ok": False, "error": "Web Push not configured"}), 503
    return jsonify({"ok": True, "publicKey": VAPID_PUBLIC_KEY})


@app.route("/push/subscribe", methods=["POST"])
@login_required
def push_subscribe():
    if not is_web_push_configured():
        return jsonify({"ok": False, "error": "Web Push not configured"}), 503

    data = request.get_json(silent=True) or {}
    ok, message = save_push_subscription(data, session.get("username"))
    if ok:
        log_activity("push_subscription_saved", session.get("username") or "anonymous")
        return jsonify({"ok": True, "message": message})
    return jsonify({"ok": False, "error": message}), 400


@app.route("/service-worker.js")
def service_worker_js():
    js = """
self.addEventListener('push', function(event) {
  let data = {};
  try {
    data = event.data ? event.data.json() : {};
  } catch (e) {
    data = { title: 'Notification', body: event.data ? event.data.text() : '' };
  }

  const title = data.title || 'Zoom Attendance Platform';
  const options = {
    body: data.body || '',
    icon: data.icon || '/static/icon.png',
    badge: data.badge || '/static/icon.png',
    data: { url: data.url || '/' }
  };

  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener('notificationclick', function(event) {
  event.notification.close();
  const targetUrl = (event.notification.data && event.notification.data.url) || '/';
  event.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true }).then(function(clientList) {
      for (const client of clientList) {
        if ('focus' in client) {
          client.navigate(targetUrl);
          return client.focus();
        }
      }
      if (clients.openWindow) {
        return clients.openWindow(targetUrl);
      }
    })
  );
});
"""
    return Response(js, mimetype="application/javascript")


@app.route("/push-setup")
@login_required
def push_setup():
    html = f"""
    <!doctype html>
    <html>
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>Web Push Setup</title>
        {DARK_THEME_CSS}
        <style>
            .push-wrap {{ max-width: 760px; margin: 40px auto; padding: 24px; }}
            .push-card {{ background: rgba(255,255,255,0.05); border: 1px solid rgba(255,255,255,0.12); border-radius: 18px; padding: 24px; box-shadow: 0 8px 30px rgba(0,0,0,0.35); }}
            .push-muted {{ color: #9ca3af; }}
            .push-row {{ display: flex; gap: 12px; flex-wrap: wrap; margin-top: 18px; }}
            .push-btn {{ cursor: pointer; }}
            .push-status {{ margin-top: 16px; padding: 12px 14px; border-radius: 12px; background: rgba(255,255,255,0.04); white-space: pre-wrap; }}
            a.push-link {{ color: #c4b5fd; text-decoration: none; }}
        </style>
    </head>
    <body>
        <div class="push-wrap">
            <div class="push-card">
                <h1 style="margin-top:0;">🔔 Browser Push Setup</h1>
                <p class="push-muted">Enable browser notifications for your account. This safely stores your browser subscription in the database for future smart alerts.</p>
                <div class="push-row">
                    <button class="push-btn" onclick="enablePush()">Enable Notifications</button>
                    <button class="push-btn" onclick="sendTestPush()">Send Test Push</button>
                    <a class="push-link" href="{url_for('home')}">← Back to Dashboard</a>
                </div>
                <div id="pushStatus" class="push-status">Status: Ready</div>
            </div>
        </div>
        <script>
        function urlBase64ToUint8Array(base64String) {{
            const padding = '='.repeat((4 - base64String.length % 4) % 4);
            const base64 = (base64String + padding).replace(/-/g, '+').replace(/_/g, '/');
            const rawData = atob(base64);
            return Uint8Array.from([...rawData].map(c => c.charCodeAt(0)));
        }}

        function setStatus(message) {{
            document.getElementById('pushStatus').textContent = message;
        }}

        async function enablePush() {{
            try {{
                if (!('serviceWorker' in navigator)) {{
                    setStatus('Service Worker is not supported in this browser.');
                    return;
                }}
                if (!('PushManager' in window)) {{
                    setStatus('Push notifications are not supported in this browser.');
                    return;
                }}

                const permission = await Notification.requestPermission();
                if (permission !== 'granted') {{
                    setStatus('Notification permission was not granted.');
                    return;
                }}

                const vapidResp = await fetch('{url_for('push_vapid_key')}');
                const vapidData = await vapidResp.json();
                if (!vapidData.ok) {{
                    setStatus('Unable to load VAPID key: ' + (vapidData.error || 'Unknown error'));
                    return;
                }}

                const registration = await navigator.serviceWorker.register('{url_for('service_worker_js')}');
                let subscription = await registration.pushManager.getSubscription();
                if (!subscription) {{
                    subscription = await registration.pushManager.subscribe({{
                        userVisibleOnly: true,
                        applicationServerKey: urlBase64ToUint8Array(vapidData.publicKey)
                    }});
                }}

                const saveResp = await fetch('{url_for('push_subscribe')}', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify(subscription)
                }});
                const saveData = await saveResp.json();
                if (saveData.ok) {{
                    setStatus('Notifications enabled successfully for this browser.');
                }} else {{
                    setStatus('Subscription save failed: ' + (saveData.error || 'Unknown error'));
                }}
            }} catch (err) {{
                setStatus('Push setup failed: ' + err);
            }}
        }}

        async function sendTestPush() {{
            try {{
                const resp = await fetch('{url_for('test_push')}');
                const data = await resp.json();
                setStatus('Test push result: ' + JSON.stringify(data));
            }} catch (err) {{
                setStatus('Test push failed: ' + err);
            }}
        }}
        </script>
    </body>
    </html>
    """
    return render_template_string(html)


@app.route("/test-push")
@login_required
def test_push():
    results = send_push_notification(
        title="Test Push from Zoom Attendance Platform",
        body="Browser push setup is working successfully.",
        target_username=session.get("username"),
        click_url=url_for("home", _external=True),
    )
    if results.get("sent", 0) > 0:
        log_activity("test_push_sent", session.get("username") or "unknown")
    return jsonify({"ok": results.get("sent", 0) > 0, **results})


@app.route("/test-email")
def test_email():
    target_email = (request.args.get("to") or "").strip()
    if not target_email:
        return "Please pass email like /test-email?to=yourgmail@gmail.com", 400

    ok, message = send_email(
        to_email=target_email,
        subject="Test Email from Zoom Attendance Platform",
        body="Hello,\n\nYour Gmail SMTP setup is working successfully.\n\nRegards,\nZoom Attendance Platform"
    )

    if ok:
        return f"✅ {message} -> {target_email}"
    return f"❌ {message}", 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=True)