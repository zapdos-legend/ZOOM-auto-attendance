    # UI_UPDATE_V8_APPEARANCE_ENGINE_SKELETON_APPLIED = True
# UI_UPDATE_V6_GLOBAL_THEME_SYSTEM_APPLIED = True

# UI_UPDATE_V3_ANALYTICS_TABS_DARK_REGISTER_APPLIED = True
# UI_UPDATE_V5_NOTIFICATION_CONTROL_FIX_APPLIED = True
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
import tempfile
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

# Lightweight in-process cache for heavy dashboards. Safe on Render: short TTL, no behavior change.
PERF_CACHE = {}
try:
    PERFORMANCE_CACHE_TTL_SECONDS = int(os.getenv("PERFORMANCE_CACHE_TTL_SECONDS", "45") or "45")
except Exception:
    PERFORMANCE_CACHE_TTL_SECONDS = 45


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


def _cache_make_key(prefix, payload):
    try:
        return prefix + ":" + json.dumps(payload, sort_keys=True, default=str)
    except Exception:
        return prefix + ":" + str(payload)


def _cache_get(key):
    if PERFORMANCE_CACHE_TTL_SECONDS <= 0:
        return None
    cached = PERF_CACHE.get(key)
    if not cached:
        return None
    created_at, value = cached
    if time.time() - created_at > PERFORMANCE_CACHE_TTL_SECONDS:
        PERF_CACHE.pop(key, None)
        return None
    return value


def _cache_set(key, value):
    if PERFORMANCE_CACHE_TTL_SECONDS <= 0:
        return value
    if len(PERF_CACHE) > 128:
        oldest_keys = sorted(PERF_CACHE, key=lambda k: PERF_CACHE[k][0])[:32]
        for old_key in oldest_keys:
            PERF_CACHE.pop(old_key, None)
    PERF_CACHE[key] = (time.time(), value)
    return value


def _cache_clear_prefix(prefix):
    for key in list(PERF_CACHE.keys()):
        if key.startswith(prefix + ":"):
            PERF_CACHE.pop(key, None)


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
    # Read the latest value from environment each time so Render/local .env changes
    # are respected after app restart. Supports both one-line \n escaped PEM
    # and real multi-line PEM formats.
    raw = os.getenv("VAPID_PRIVATE_KEY", VAPID_PRIVATE_KEY or "")
    if not raw:
        return ""
    raw = raw.strip().strip('"').strip("'")
    return raw.replace("\\n", "\n").replace("\\r", "").replace("\r", "").strip()


def get_vapid_private_key_file():
    # pywebpush accepts either a PEM file path or a base64 DER key string.
    # Our .env stores the private key text, so we safely write it to a temp PEM
    # file and pass the file path. This avoids ASN.1/DER parsing errors.
    private_key_text = get_vapid_private_key_value()
    if not private_key_text:
        return ""
    key_path = os.path.join(tempfile.gettempdir(), "zoom_attendance_vapid_private_key.pem")
    with open(key_path, "w", encoding="utf-8", newline="\n") as key_file:
        key_file.write(private_key_text)
        if not private_key_text.endswith("\n"):
            key_file.write("\n")
    return key_path

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
                        vapid_private_key=get_vapid_private_key_file(),
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
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS smart_alert_states (
                    alert_key TEXT PRIMARY KEY,
                    alert_type TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    current_state TEXT NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS smart_alert_logs (
                    id SERIAL PRIMARY KEY,
                    alert_key TEXT NOT NULL,
                    alert_type TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    previous_state TEXT,
                    current_state TEXT NOT NULL,
                    title TEXT NOT NULL,
                    message TEXT NOT NULL,
                    email_sent BOOLEAN NOT NULL DEFAULT FALSE,
                    push_sent INTEGER NOT NULL DEFAULT 0,
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
        if table_exists(conn, "smart_alert_logs"):
            ensure_index(conn, "idx_smart_alert_logs_key", "CREATE INDEX idx_smart_alert_logs_key ON smart_alert_logs(alert_key)")
            ensure_index(conn, "idx_smart_alert_logs_created", "CREATE INDEX idx_smart_alert_logs_created ON smart_alert_logs(created_at)")
        # Performance indexes for dashboards, filters and monthly register.
        ensure_index(conn, "idx_meetings_start_time", "CREATE INDEX idx_meetings_start_time ON meetings(start_time)")
        ensure_index(conn, "idx_meetings_uuid_start", "CREATE INDEX idx_meetings_uuid_start ON meetings(meeting_uuid, start_time)")
        ensure_index(conn, "idx_attendance_member_status", "CREATE INDEX idx_attendance_member_status ON attendance(member_id, final_status)")
        ensure_index(conn, "idx_attendance_uuid_member", "CREATE INDEX idx_attendance_uuid_member ON attendance(meeting_uuid, member_id)")

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
    _cache_clear_prefix("analytics")
    _cache_clear_prefix("graph_analytics")
    _cache_clear_prefix("attendance_register")


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
    try:
        evaluate_smart_alerts_for_meeting(meeting_uuid)
    except Exception as e:
        print(f"⚠️ Smart alert evaluation skipped: {e}")
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



def _graph_date_value(value):
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except Exception:
        return None


def _graph_multi_values(name):
    values = request.args.getlist(name)
    if len(values) == 1 and "," in values[0]:
        values = [item.strip() for item in values[0].split(",")]
    clean = []
    for value in values:
        text = str(value or "").strip()
        if text and text != "__all__":
            clean.append(text)
    return clean


def graph_analytics_options():
    with db() as conn:
        with conn.cursor() as cur:
            member_name_expr = member_name_sql(conn)
            cur.execute(f"SELECT id, {member_name_expr} AS display_name FROM members WHERE {ACTIVE_MEMBER_SQL} ORDER BY COALESCE({member_name_expr}, '')")
            members = cur.fetchall()
            cur.execute("""
                SELECT DISTINCT to_char(CAST(start_time AS TEXT)::timestamp, 'YYYY-MM') AS month_value,
                       to_char(CAST(start_time AS TEXT)::timestamp, 'Mon YYYY') AS month_label
                FROM meetings
                WHERE start_time IS NOT NULL
                ORDER BY month_value DESC
                LIMIT 36
            """)
            months = cur.fetchall()
            cur.execute("""
                SELECT DISTINCT to_char(CAST(start_time AS TEXT)::timestamp, 'YYYY') AS year_value
                FROM meetings
                WHERE start_time IS NOT NULL
                ORDER BY year_value DESC
                LIMIT 10
            """)
            years = cur.fetchall()
    return {
        "members": [{"id": m.get("id"), "name": m.get("display_name") or f"Member {m.get('id')}"} for m in members],
        "months": [{"value": m.get("month_value"), "label": m.get("month_label") or m.get("month_value")} for m in months if m.get("month_value")],
        "years": [y.get("year_value") for y in years if y.get("year_value")],
    }


def _graph_analytics_payload_uncached():
    x_axis = str(request.args.get("x_axis", "date") or "date").lower()
    if x_axis not in ("date", "month", "year"):
        x_axis = "date"
    y_axis = str(request.args.get("y_axis", "count") or "count").lower()
    if y_axis not in ("count", "percentage"):
        y_axis = "count"

    from_date = _graph_date_value(request.args.get("from_date"))
    to_date = _graph_date_value(request.args.get("to_date"))
    months = _graph_multi_values("months")
    years = _graph_multi_values("years")
    raw_member_ids = _graph_multi_values("member_ids")
    member_ids = [int(v) for v in raw_member_ids if str(v).isdigit()]

    where = ["m.start_time IS NOT NULL"]
    params = []
    if x_axis == "date":
        if from_date:
            where.append("CAST(m.start_time AS TEXT)::date >= %s")
            params.append(from_date)
        if to_date:
            where.append("CAST(m.start_time AS TEXT)::date <= %s")
            params.append(to_date)
    elif x_axis == "month" and months:
        where.append("to_char(CAST(m.start_time AS TEXT)::timestamp, 'YYYY-MM') = ANY(%s)")
        params.append(months)
    elif x_axis == "year" and years:
        where.append("to_char(CAST(m.start_time AS TEXT)::timestamp, 'YYYY') = ANY(%s)")
        params.append(years)

    attendance_sql = f"""
        SELECT a.member_id, a.participant_name, a.final_status, a.is_member, a.total_seconds, a.current_join, m.start_time
        FROM attendance a
        JOIN meetings m ON m.meeting_uuid = a.meeting_uuid
        WHERE {' AND '.join(where)}
        ORDER BY m.start_time ASC
    """

    duration_where = list(where)
    duration_params = list(params)
    duration_where.append("CAST(a.is_member AS TEXT) IN ('1','true','t','True','TRUE')")
    duration_where.append("a.member_id IS NOT NULL")
    if from_date and x_axis != "date":
        duration_where.append("CAST(m.start_time AS TEXT)::date >= %s")
        duration_params.append(from_date)
    if to_date and x_axis != "date":
        duration_where.append("CAST(m.start_time AS TEXT)::date <= %s")
        duration_params.append(to_date)
    if member_ids:
        duration_where.append("a.member_id = ANY(%s)")
        duration_params.append(member_ids)

    duration_sql = f"""
        SELECT a.member_id, a.participant_name, a.total_seconds, a.current_join, m.start_time
        FROM attendance a
        JOIN meetings m ON m.meeting_uuid = a.meeting_uuid
        WHERE {' AND '.join(duration_where)}
        ORDER BY m.start_time ASC, a.participant_name ASC
    """

    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(attendance_sql, params)
            attendance_rows = cur.fetchall()
            cur.execute(duration_sql, duration_params)
            duration_rows = cur.fetchall()
            member_name_expr = member_name_sql(conn)
            cur.execute(f"SELECT id, {member_name_expr} AS display_name FROM members ORDER BY COALESCE({member_name_expr}, '')")
            member_rows = cur.fetchall()

    member_names = {int(m["id"]): (m.get("display_name") or f"Member {m.get('id')}") for m in member_rows if m.get("id") is not None}
    trend_buckets = defaultdict(lambda: {"present": 0, "late": 0, "absent": 0, "unknown": 0, "total": 0, "sort": ""})
    for row in attendance_rows:
        dt = parse_dt(row.get("start_time"))
        if not dt:
            continue
        if x_axis == "year":
            key = dt.strftime("%Y")
            label = key
        elif x_axis == "month":
            key = dt.strftime("%Y-%m")
            label = dt.strftime("%b %Y")
        else:
            key = dt.strftime("%Y-%m-%d")
            label = dt.strftime("%d-%m-%Y")
        bucket = trend_buckets[label]
        bucket["sort"] = key
        bucket["total"] += 1
        status = str(row.get("final_status") or "").upper()
        if status == "PRESENT":
            bucket["present"] += 1
        elif status == "LATE":
            bucket["late"] += 1
        elif status == "ABSENT":
            bucket["absent"] += 1
        if not row.get("is_member"):
            bucket["unknown"] += 1

    labels = sorted(trend_buckets.keys(), key=lambda k: trend_buckets[k]["sort"])

    def series_value(label, field):
        value = trend_buckets[label][field]
        total = trend_buckets[label]["total"] or 0
        return round((value / total) * 100, 2) if y_axis == "percentage" and total else value

    selected_single_member = len(member_ids) == 1
    duration_buckets = defaultdict(float)
    for row in duration_rows:
        seconds = float(row.get("total_seconds") or 0)
        current_join = parse_dt(row.get("current_join"))
        if current_join:
            seconds += max((now_local() - current_join).total_seconds(), 0)
        minutes = seconds / 60.0
        if selected_single_member:
            dt = parse_dt(row.get("start_time"))
            label = dt.strftime("%d-%m-%Y") if dt else "Unknown Date"
            sort_key = dt.strftime("%Y-%m-%d") if dt else label
            duration_buckets[(sort_key, label)] += minutes
        else:
            mid = row.get("member_id")
            name = member_names.get(int(mid), row.get("participant_name") or f"Member {mid}") if mid else (row.get("participant_name") or "Unknown")
            duration_buckets[(name.lower(), name)] += minutes

    duration_items = sorted(duration_buckets.items(), key=lambda item: item[0][0])
    duration_labels = [item[0][1] for item in duration_items]
    duration_values = [round(item[1], 2) for item in duration_items]
    if not selected_single_member and len(duration_labels) > 60:
        combined = sorted(zip(duration_labels, duration_values), key=lambda item: item[1], reverse=True)[:60]
        duration_labels = [item[0] for item in combined]
        duration_values = [item[1] for item in combined]

    return {
        "trend": {
            "x_axis": x_axis,
            "y_axis": y_axis,
            "labels": labels,
            "present": [series_value(k, "present") for k in labels],
            "late": [series_value(k, "late") for k in labels],
            "absent": [series_value(k, "absent") for k in labels],
            "unknown": [series_value(k, "unknown") for k in labels],
        },
        "duration": {
            "mode": "single_member_date_duration" if selected_single_member else "members_total_duration",
            "labels": duration_labels,
            "values": duration_values,
            "selected_member_name": member_names.get(member_ids[0]) if selected_single_member else "",
        },
    }





SMART_ALERT_EMAIL_TO = os.getenv("SMART_ALERT_EMAIL_TO", os.getenv("EMAIL_RECEIVER", "")).strip()
UNKNOWN_SPIKE_COUNT = int(os.getenv("UNKNOWN_SPIKE_COUNT", "5") or "5")
UNKNOWN_SPIKE_PERCENT = float(os.getenv("UNKNOWN_SPIKE_PERCENT", "30") or "30")

NOTIFICATION_ALERT_TYPE_LABELS = {
    "member_risk": "Member Critical / Warning Risk",
    "declining_trend": "Declining Trend",
    "host_absent": "Host Absent",
    "unknown_participant_spike": "Unknown Participant Spike",
}
NOTIFICATION_DEFAULT_ALERT_TYPES = list(NOTIFICATION_ALERT_TYPE_LABELS.keys())
NOTIFICATION_DEFAULT_TEMPLATE = "{title}\n\n{message}\n\nState: {state}"


def _json_setting(name, default):
    raw = get_setting(name, str)
    if not raw:
        return default
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, type(default)) else default
    except Exception:
        if isinstance(default, list):
            return [x.strip() for x in str(raw).split(",") if x.strip()] or default
        return default


def get_notification_settings():
    alert_types = _json_setting("notification_alert_types", NOTIFICATION_DEFAULT_ALERT_TYPES)
    timings = _json_setting("notification_timings", ["before", "during", "after"])
    return {
        "email_enabled": get_setting("notification_email_enabled", str).strip().lower() not in ("0", "false", "no", "off"),
        "push_enabled": get_setting("notification_push_enabled", str).strip().lower() not in ("0", "false", "no", "off"),
        "alert_types": alert_types,
        "timings": timings,
        "message_template": get_setting("notification_message_template", str) or NOTIFICATION_DEFAULT_TEMPLATE,
        "test_email_to": get_setting("notification_test_email_to", str) or SMART_ALERT_EMAIL_TO,
    }


def save_notification_settings(form):
    set_setting("notification_email_enabled", "true" if form.get("email_enabled") else "false")
    set_setting("notification_push_enabled", "true" if form.get("push_enabled") else "false")
    set_setting("notification_alert_types", json.dumps(form.getlist("alert_types")))
    set_setting("notification_timings", json.dumps(form.getlist("timings")))
    set_setting("notification_message_template", form.get("message_template", NOTIFICATION_DEFAULT_TEMPLATE))
    set_setting("notification_test_email_to", form.get("test_email_to", "").strip())


def notification_alert_allowed(alert_type, phase="after"):
    settings = get_notification_settings()
    return (alert_type in settings.get("alert_types", [])) and (phase in settings.get("timings", []))


def _format_notification_message(template, title, message, state, alert_type, member=None, meeting=None):
    try:
        return (template or NOTIFICATION_DEFAULT_TEMPLATE).format(
            title=title or "Smart Alert",
            message=message or "",
            state=state or "active",
            alert_type=alert_type or "general",
            member_name=(member or {}).get("name") or (member or {}).get("full_name") or "",
            meeting_topic=(meeting or {}).get("topic") or "",
        )
    except Exception:
        return f"{title}\n\n{message}\n\nState: {state}"


def _alert_entity(member=None, meeting=None):
    if member:
        return "member", str(member.get("id") or member.get("member_id") or member.get("name") or "unknown")
    if meeting:
        return "meeting", str(meeting.get("meeting_uuid") or meeting.get("id") or "unknown")
    return "system", "global"


def trigger_alert(member=None, alert_type="general", state="active", message="", title=None, meeting=None):
    """Send smart alert only when alert state changes; store every state-change log."""
    try:
        entity_type, entity_id = _alert_entity(member=member, meeting=meeting)
        alert_key = f"{entity_type}:{entity_id}:{alert_type}"
        title = title or f"Smart Alert: {alert_type.replace('_', ' ').title()}"
        state = str(state or "active")
        message = message or title
        previous_state = None
        should_send = False

        with db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT current_state FROM smart_alert_states WHERE alert_key=%s", (alert_key,))
                row = cur.fetchone()
                previous_state = row.get("current_state") if row else None
                should_send = previous_state != state
                if not should_send:
                    return {"sent": False, "reason": "state_unchanged", "alert_key": alert_key}
                cur.execute(
                    """
                    INSERT INTO smart_alert_states(alert_key, alert_type, entity_type, entity_id, current_state, updated_at)
                    VALUES (%s,%s,%s,%s,%s,NOW())
                    ON CONFLICT (alert_key)
                    DO UPDATE SET current_state=EXCLUDED.current_state, updated_at=NOW()
                    """,
                    (alert_key, alert_type, entity_type, entity_id, state),
                )
            conn.commit()

        settings = get_notification_settings()
        formatted_message = _format_notification_message(
            settings.get("message_template"), title, message, state, alert_type, member=member, meeting=meeting
        )

        recipient = (member or {}).get("email") or SMART_ALERT_EMAIL_TO or settings.get("test_email_to")
        email_sent = False
        if settings.get("email_enabled") and recipient and notification_alert_allowed(alert_type, "after"):
            email_sent, _ = send_email(
                recipient,
                title,
                formatted_message,
                f"<h2>{title}</h2><p>{formatted_message.replace(chr(10), '<br>')}</p><p><b>State:</b> {state}</p>",
            )

        push_result = {"sent": 0}
        if settings.get("push_enabled") and notification_alert_allowed(alert_type, "after"):
            push_result = send_push_notification(title, formatted_message, click_url=url_for("analytics", _external=True))
        push_sent = int((push_result or {}).get("sent", 0))
        message = formatted_message
        with db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO smart_alert_logs(alert_key, alert_type, entity_type, entity_id, previous_state, current_state, title, message, email_sent, push_sent)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    """,
                    (alert_key, alert_type, entity_type, entity_id, previous_state, state, title, message, bool(email_sent), push_sent),
                )
            conn.commit()
        return {"sent": True, "alert_key": alert_key, "email_sent": bool(email_sent), "push_sent": push_sent}
    except Exception as exc:
        print(f"⚠️ Smart alert skipped: {exc}")
        return {"sent": False, "error": str(exc)}


def _member_alert_people_for_meeting(conn, meeting_uuid):
    with conn.cursor() as cur:
        name_expr = member_name_sql(conn)
        cur.execute(
            f"""
            SELECT m.id, {name_expr} AS name, m.email,
                   COUNT(a.id) AS meetings,
                   SUM(CASE WHEN a.final_status IN ('PRESENT','HOST') THEN 1 ELSE 0 END) AS present,
                   SUM(CASE WHEN a.final_status='LATE' THEN 1 ELSE 0 END) AS late,
                   SUM(CASE WHEN a.final_status='ABSENT' THEN 1 ELSE 0 END) AS absent,
                   COALESCE(SUM(a.total_seconds),0)/60.0 AS minutes,
                   COALESCE(SUM(a.rejoin_count),0) AS rejoins,
                   MAX(a.last_leave) AS last_seen
            FROM members m
            JOIN attendance target ON target.member_id=m.id AND target.meeting_uuid=%s
            LEFT JOIN attendance a ON a.member_id=m.id
            WHERE {ACTIVE_MEMBER_SQL}
            GROUP BY m.id, name, m.email
            ORDER BY name
            """,
            (meeting_uuid,),
        )
        people = cur.fetchall()
        for person in people:
            cur.execute(
                """
                SELECT final_status, total_seconds
                FROM attendance
                WHERE member_id=%s
                ORDER BY COALESCE(last_leave, first_join, created_at) ASC
                LIMIT 20
                """,
                (person.get("id"),),
            )
            points = []
            for row in cur.fetchall():
                st = str(row.get("final_status") or "").upper()
                if st in ("PRESENT", "HOST"):
                    points.append(100)
                elif st == "LATE":
                    points.append(60)
                elif st == "ABSENT":
                    points.append(0)
            person["score_points"] = points
        return people


def evaluate_smart_alerts_for_meeting(meeting_uuid):
    try:
        with db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM meetings WHERE meeting_uuid=%s", (meeting_uuid,))
                meeting = cur.fetchone()
                if not meeting:
                    return
                total_participants = int(meeting.get("unique_participants") or 0)
                unknown_count = int(meeting.get("unknown_participants") or 0)
                host_present = bool(meeting.get("host_present"))

            if not host_present:
                trigger_alert(
                    alert_type="host_absent",
                    state="host_absent",
                    meeting=meeting,
                    title="🚨 Host absent detected",
                    message=f"Host was not detected in meeting: {meeting.get('topic') or meeting_uuid}.",
                )
            else:
                trigger_alert(alert_type="host_absent", state="resolved", meeting=meeting, title="✅ Host alert resolved", message="Host is detected again.")

            unknown_pct = (unknown_count / total_participants * 100.0) if total_participants else 0.0
            if unknown_count >= UNKNOWN_SPIKE_COUNT or unknown_pct >= UNKNOWN_SPIKE_PERCENT:
                trigger_alert(
                    alert_type="unknown_participant_spike",
                    state=f"unknown_{unknown_count}_{round(unknown_pct,1)}",
                    meeting=meeting,
                    title="⚠️ Unknown participant spike",
                    message=f"Meeting has {unknown_count} unknown participants ({round(unknown_pct,1)}%).",
                )

            people = _member_alert_people_for_meeting(conn, meeting_uuid)
            avg_ref = 1.0
            if people:
                avg_ref = max(sum(float(p.get("minutes") or 0) for p in people) / max(len(people), 1), 1.0)
            for person in people:
                intel = build_member_intelligence(person, avg_ref)
                risk_short = intel.get("risk", {}).get("short")
                if risk_short in ("CRITICAL", "WARNING"):
                    trigger_alert(
                        member=person,
                        alert_type="member_risk",
                        state=risk_short.lower(),
                        title=f"{intel['risk']['emoji']} {risk_short.title()} risk: {person.get('name')}",
                        message=f"{person.get('name')} is now in {risk_short} risk. Overall score: {intel.get('overall_score')}%. Attendance score: {intel.get('attendance_score')}%.",
                    )
                trend_short = intel.get("trend", {}).get("short")
                if trend_short == "DECLINING":
                    trigger_alert(
                        member=person,
                        alert_type="declining_trend",
                        state="declining",
                        title=f"📉 Declining trend: {person.get('name')}",
                        message=f"{person.get('name')} attendance trend is declining. Delta: {intel.get('trend', {}).get('delta')}.",
                    )
    except Exception as exc:
        print(f"⚠️ evaluate_smart_alerts_for_meeting skipped: {exc}")

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


def _analytics_data_uncached(filters):
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

        /* ===== GLOBAL THEME SYSTEM LAYER (SAFE: variables only, no structure changes) ===== */
        body[data-app-theme="default-saas-dark"]{
            --nav:#020617;--nav-2:#111827;--bg1:#06111f;--bg2:#0b1220;--bg3:#101a2f;
            --card:#0f172a;--card-soft:rgba(15,23,42,.72);--card-solid:#0f172a;--text:#e5eefc;--muted:#9fb2d3;
            --line:#24334d;--line-strong:#334155;--primary:#60a5fa;--primary2:#3b82f6;--primary3:#8b5cf6;
            --success:#22c55e;--warn:#fbbf24;--danger:#ef4444;--cyan:#22d3ee;--soft:#16233b;
            --shadow:0 20px 54px rgba(2,6,23,.42);--shadow-soft:0 14px 32px rgba(2,6,23,.30);
            --surface-ring:rgba(148,163,184,.16);--hero-grad:linear-gradient(135deg,#020617 0%,#1d4ed8 56%,#6d28d9 100%);
            --hero-glow:rgba(96,165,250,.16);--btn-grad:linear-gradient(135deg,#1d4ed8 0%,#4f46e5 54%,#7c3aed 100%);
            --chip-bg:rgba(255,255,255,.06);--bg-grid:rgba(148,163,184,.08);
        }
        body[data-app-theme="notion-clean"]{
            --nav:#111827;--nav-2:#1f2937;--bg1:#f7f6f3;--bg2:#fbfaf7;--bg3:#efece6;
            --card:#ffffff;--card-soft:rgba(255,255,255,.82);--card-solid:#ffffff;--text:#1f2937;--muted:#6b7280;
            --line:#e5e7eb;--line-strong:#d1d5db;--primary:#111827;--primary2:#374151;--primary3:#6b7280;
            --success:#15803d;--warn:#b45309;--danger:#b91c1c;--cyan:#0f766e;--soft:#f3f4f6;
            --shadow:0 18px 42px rgba(17,24,39,.08);--shadow-soft:0 10px 26px rgba(17,24,39,.06);
            --surface-ring:rgba(17,24,39,.10);--hero-grad:linear-gradient(135deg,#111827 0%,#374151 100%);
            --hero-glow:rgba(17,24,39,.10);--btn-grad:linear-gradient(135deg,#111827,#374151);
            --chip-bg:rgba(255,255,255,.15);--bg-grid:rgba(107,114,128,.10);
        }
        body[data-app-theme="stripe-glow"]{
            --nav:#0f172a;--nav-2:#312e81;--bg1:#f6f8ff;--bg2:#eef4ff;--bg3:#f8f0ff;
            --card:#ffffff;--card-soft:rgba(255,255,255,.78);--card-solid:#ffffff;--text:#0f172a;--muted:#64748b;
            --line:#dbeafe;--line-strong:#bfdbfe;--primary:#635bff;--primary2:#7c3aed;--primary3:#06b6d4;
            --success:#10b981;--warn:#f59e0b;--danger:#ef4444;--cyan:#06b6d4;--soft:#eef2ff;
            --shadow:0 24px 60px rgba(99,91,255,.18);--shadow-soft:0 14px 34px rgba(99,91,255,.13);
            --surface-ring:rgba(99,91,255,.18);--hero-grad:linear-gradient(135deg,#635bff 0%,#7c3aed 48%,#06b6d4 100%);
            --hero-glow:rgba(99,91,255,.22);--btn-grad:linear-gradient(135deg,#635bff,#7c3aed,#06b6d4);
            --chip-bg:rgba(255,255,255,.18);--bg-grid:rgba(99,91,255,.10);
        }
        body[data-app-theme="vercel-minimal"]{
            --nav:#000000;--nav-2:#111111;--bg1:#ffffff;--bg2:#fafafa;--bg3:#f5f5f5;
            --card:#ffffff;--card-soft:rgba(255,255,255,.92);--card-solid:#ffffff;--text:#000000;--muted:#666666;
            --line:#e5e5e5;--line-strong:#cfcfcf;--primary:#000000;--primary2:#111111;--primary3:#404040;
            --success:#15803d;--warn:#b45309;--danger:#dc2626;--cyan:#0369a1;--soft:#f5f5f5;
            --shadow:0 16px 44px rgba(0,0,0,.08);--shadow-soft:0 10px 24px rgba(0,0,0,.06);
            --surface-ring:rgba(0,0,0,.10);--hero-grad:linear-gradient(135deg,#000000,#262626);
            --hero-glow:rgba(0,0,0,.10);--btn-grad:linear-gradient(135deg,#000,#262626);
            --chip-bg:rgba(255,255,255,.13);--bg-grid:rgba(0,0,0,.06);
        }
        body[data-app-theme="netflix-dark"]{
            --nav:#050505;--nav-2:#141414;--bg1:#050505;--bg2:#111111;--bg3:#1f0a0a;
            --card:#141414;--card-soft:rgba(20,20,20,.78);--card-solid:#141414;--text:#f5f5f5;--muted:#a3a3a3;
            --line:#2b2b2b;--line-strong:#404040;--primary:#e50914;--primary2:#b20710;--primary3:#f97316;
            --success:#22c55e;--warn:#fbbf24;--danger:#ef4444;--cyan:#f97316;--soft:#1f1f1f;
            --shadow:0 24px 60px rgba(0,0,0,.55);--shadow-soft:0 14px 32px rgba(0,0,0,.38);
            --surface-ring:rgba(229,9,20,.22);--hero-grad:linear-gradient(135deg,#050505 0%,#7f1d1d 54%,#e50914 100%);
            --hero-glow:rgba(229,9,20,.20);--btn-grad:linear-gradient(135deg,#e50914,#b20710);
            --chip-bg:rgba(255,255,255,.07);--bg-grid:rgba(229,9,20,.07);
        }
        body[data-app-theme="college-formal"]{
            --nav:#172554;--nav-2:#1e3a8a;--bg1:#eef2ff;--bg2:#f8fafc;--bg3:#e0e7ff;
            --card:#ffffff;--card-soft:rgba(255,255,255,.82);--card-solid:#ffffff;--text:#172554;--muted:#475569;
            --line:#cbd5e1;--line-strong:#94a3b8;--primary:#1d4ed8;--primary2:#1e40af;--primary3:#7c2d12;
            --success:#166534;--warn:#a16207;--danger:#b91c1c;--cyan:#155e75;--soft:#e0e7ff;
            --shadow:0 18px 48px rgba(30,58,138,.13);--shadow-soft:0 12px 28px rgba(30,58,138,.10);
            --surface-ring:rgba(30,58,138,.16);--hero-grad:linear-gradient(135deg,#172554 0%,#1d4ed8 65%,#f59e0b 100%);
            --hero-glow:rgba(30,64,175,.16);--btn-grad:linear-gradient(135deg,#1e3a8a,#1d4ed8);
            --chip-bg:rgba(255,255,255,.16);--bg-grid:rgba(30,58,138,.09);
        }
        body[data-app-theme="purple-neon"]{
            --nav:#12001f;--nav-2:#2e1065;--bg1:#0b0014;--bg2:#160024;--bg3:#25003d;
            --card:#160024;--card-soft:rgba(22,0,36,.78);--card-solid:#160024;--text:#f5e8ff;--muted:#d8b4fe;
            --line:#4c1d95;--line-strong:#6d28d9;--primary:#c084fc;--primary2:#a855f7;--primary3:#22d3ee;
            --success:#4ade80;--warn:#fde047;--danger:#fb7185;--cyan:#22d3ee;--soft:#2e1065;
            --shadow:0 24px 72px rgba(168,85,247,.24);--shadow-soft:0 14px 34px rgba(168,85,247,.18);
            --surface-ring:rgba(192,132,252,.24);--hero-grad:linear-gradient(135deg,#160024 0%,#7e22ce 50%,#22d3ee 100%);
            --hero-glow:rgba(192,132,252,.28);--btn-grad:linear-gradient(135deg,#a855f7,#7e22ce,#22d3ee);
            --chip-bg:rgba(255,255,255,.07);--bg-grid:rgba(192,132,252,.09);
        }
        body[data-app-theme="light-professional"]{
            --nav:#0f172a;--nav-2:#1e293b;--bg1:#f8fafc;--bg2:#ffffff;--bg3:#eef2f7;
            --card:#ffffff;--card-soft:rgba(255,255,255,.86);--card-solid:#ffffff;--text:#0f172a;--muted:#64748b;
            --line:#e2e8f0;--line-strong:#cbd5e1;--primary:#2563eb;--primary2:#1d4ed8;--primary3:#0f766e;
            --success:#16a34a;--warn:#d97706;--danger:#dc2626;--cyan:#0891b2;--soft:#f1f5f9;
            --shadow:0 18px 46px rgba(15,23,42,.09);--shadow-soft:0 12px 28px rgba(15,23,42,.07);
            --surface-ring:rgba(148,163,184,.18);--hero-grad:linear-gradient(135deg,#0f172a 0%,#2563eb 58%,#0f766e 100%);
            --hero-glow:rgba(37,99,235,.14);--btn-grad:linear-gradient(135deg,#2563eb,#1d4ed8);
            --chip-bg:rgba(255,255,255,.14);--bg-grid:rgba(148,163,184,.10);
        }
        body[data-app-theme="default-saas-dark"],body[data-app-theme="netflix-dark"],body[data-app-theme="purple-neon"]{color-scheme:dark}
        body[data-app-theme="default-saas-dark"] .sidebar,body[data-app-theme="netflix-dark"] .sidebar,body[data-app-theme="purple-neon"] .sidebar{
            background:linear-gradient(180deg,rgba(7,17,31,.55),rgba(7,17,31,.42));
        }
        body[data-app-theme="notion-clean"] .sidebar,body[data-app-theme="stripe-glow"] .sidebar,body[data-app-theme="vercel-minimal"] .sidebar,body[data-app-theme="college-formal"] .sidebar,body[data-app-theme="light-professional"] .sidebar{
            background:linear-gradient(180deg,rgba(255,255,255,.72),rgba(255,255,255,.50));
        }
        body[data-app-theme="netflix-dark"] .topbar{background:linear-gradient(90deg,#050505,#7f1d1d,#e50914)}
        body[data-app-theme="purple-neon"] .topbar{background:linear-gradient(90deg,#12001f,#7e22ce,#22d3ee)}
        body[data-app-theme="vercel-minimal"] .topbar{background:linear-gradient(90deg,#000,#18181b)}
        body[data-app-theme="notion-clean"] .topbar{background:linear-gradient(90deg,#111827,#374151)}
        body[data-app-theme="college-formal"] .topbar{background:linear-gradient(90deg,#172554,#1d4ed8,#92400e)}
        body[data-app-theme="light-professional"] .topbar{background:linear-gradient(90deg,#0f172a,#2563eb,#0f766e)}
        .global-theme-control{
            display:inline-flex;align-items:center;gap:8px;height:38px;padding:0 12px;border-radius:999px;
            color:#fff;background:rgba(15,23,42,.28);border:1px solid rgba(255,255,255,.14);
            box-shadow:inset 0 1px 0 rgba(255,255,255,.10);font-size:12px;font-weight:800;
        }
        .global-theme-control select{
            height:28px;max-width:166px;border:0;outline:0;border-radius:999px;padding:0 28px 0 10px;
            font-weight:900;color:var(--text);background:var(--card-solid);box-shadow:0 6px 16px rgba(2,6,23,.12);
        }
        @media (max-width: 900px){.global-theme-control{width:100%;justify-content:space-between}.global-theme-control select{max-width:210px}}
        /* ===== END GLOBAL THEME SYSTEM LAYER ===== */

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


        /* =========================================================
           UI_UPDATE_V8_APPEARANCE_ENGINE_SKELETON_APPLIED
           SaaS Appearance Engine + Premium Skeleton Layer
           Safe CSS-only theme layer. Does not modify attendance/webhook logic.
        ========================================================= */
        :root{--theme-accent:#6366f1;--theme-accent-2:#8b5cf6;--theme-accent-3:#22d3ee;--theme-danger:#ef4444;--theme-success:#22c55e;--theme-warning:#f59e0b;--theme-bg:#0b1020;--theme-bg-2:#111827;--theme-card:rgba(15,23,42,.82);--theme-card-soft:rgba(255,255,255,.075);--theme-text:#f8fafc;--theme-muted:#94a3b8;--theme-line:rgba(148,163,184,.18);--theme-glow:rgba(99,102,241,.25);}
        body[data-app-theme="default-saas-dark"]{--bg:#07111f;--bg2:#0f172a;--card:rgba(15,23,42,.88);--card-soft:rgba(255,255,255,.07);--text:#f8fafc;--muted:#9ca3af;--line:rgba(148,163,184,.18);--surface-ring:rgba(99,102,241,.22);--brand:#6366f1;--brand2:#8b5cf6;--theme-accent:#6366f1;--theme-accent-2:#8b5cf6;--theme-accent-3:#22d3ee;--theme-glow:rgba(99,102,241,.28);--hero-grad:linear-gradient(135deg,#1d4ed8,#6d28d9,#0891b2);--shadow:0 18px 55px rgba(2,6,23,.44);--shadow-soft:0 14px 34px rgba(2,6,23,.25);}
        body[data-app-theme="notion-clean"]{--bg:#f7f6f3;--bg2:#ffffff;--card:#ffffff;--card-soft:#fbfaf8;--text:#1f2937;--muted:#6b7280;--line:#e5e7eb;--surface-ring:#e5e7eb;--brand:#111827;--brand2:#6b7280;--theme-accent:#111827;--theme-accent-2:#64748b;--theme-accent-3:#0f766e;--theme-glow:rgba(15,23,42,.08);--hero-grad:linear-gradient(135deg,#ffffff,#f3f4f6);--shadow:0 16px 36px rgba(15,23,42,.08);--shadow-soft:0 10px 26px rgba(15,23,42,.06);}
        body[data-app-theme="stripe-glow"]{--bg:#070b1a;--bg2:#0f172a;--card:rgba(15,23,42,.86);--card-soft:rgba(99,102,241,.09);--text:#eef2ff;--muted:#a5b4fc;--line:rgba(129,140,248,.22);--surface-ring:rgba(99,102,241,.30);--brand:#635bff;--brand2:#00d4ff;--theme-accent:#635bff;--theme-accent-2:#00d4ff;--theme-accent-3:#7c3aed;--theme-glow:rgba(0,212,255,.28);--hero-grad:linear-gradient(135deg,#635bff,#7c3aed,#00d4ff);--shadow:0 22px 60px rgba(99,91,255,.20);--shadow-soft:0 16px 40px rgba(0,212,255,.12);}
        body[data-app-theme="vercel-minimal"]{--bg:#000000;--bg2:#0a0a0a;--card:#0f0f0f;--card-soft:#111111;--text:#fafafa;--muted:#a3a3a3;--line:#262626;--surface-ring:#333333;--brand:#ffffff;--brand2:#737373;--theme-accent:#ffffff;--theme-accent-2:#a3a3a3;--theme-accent-3:#525252;--theme-glow:rgba(255,255,255,.12);--hero-grad:linear-gradient(135deg,#000,#18181b);--shadow:0 20px 60px rgba(0,0,0,.65);--shadow-soft:0 12px 32px rgba(0,0,0,.45);}
        body[data-app-theme="netflix-dark"]{--bg:#080808;--bg2:#141414;--card:#181818;--card-soft:#202020;--text:#ffffff;--muted:#b3b3b3;--line:rgba(229,9,20,.20);--surface-ring:rgba(229,9,20,.28);--brand:#e50914;--brand2:#b91c1c;--theme-accent:#e50914;--theme-accent-2:#f97316;--theme-accent-3:#ef4444;--theme-glow:rgba(229,9,20,.30);--hero-grad:linear-gradient(135deg,#e50914,#7f1d1d,#111);--shadow:0 22px 60px rgba(229,9,20,.16);--shadow-soft:0 16px 40px rgba(0,0,0,.48);}
        body[data-app-theme="college-formal"]{--bg:#f3efe4;--bg2:#fffaf0;--card:#fff8e7;--card-soft:#fffdf6;--text:#172554;--muted:#64748b;--line:#d6c7a1;--surface-ring:#c9b57e;--brand:#1e3a8a;--brand2:#92400e;--theme-accent:#1e3a8a;--theme-accent-2:#92400e;--theme-accent-3:#047857;--theme-glow:rgba(30,58,138,.12);--hero-grad:linear-gradient(135deg,#1e3a8a,#92400e);--shadow:0 18px 45px rgba(30,58,138,.14);--shadow-soft:0 12px 30px rgba(146,64,14,.10);}
        body[data-app-theme="purple-neon"]{--bg:#070014;--bg2:#130029;--card:rgba(24,0,46,.88);--card-soft:rgba(168,85,247,.10);--text:#faf5ff;--muted:#d8b4fe;--line:rgba(217,70,239,.26);--surface-ring:rgba(168,85,247,.34);--brand:#a855f7;--brand2:#ec4899;--theme-accent:#a855f7;--theme-accent-2:#ec4899;--theme-accent-3:#22d3ee;--theme-glow:rgba(236,72,153,.35);--hero-grad:linear-gradient(135deg,#7e22ce,#db2777,#0891b2);--shadow:0 22px 70px rgba(168,85,247,.22);--shadow-soft:0 18px 44px rgba(236,72,153,.14);}
        body[data-app-theme="light-professional"]{--bg:#eef2f7;--bg2:#ffffff;--card:#ffffff;--card-soft:#f8fafc;--text:#0f172a;--muted:#64748b;--line:#dbe4ef;--surface-ring:#cbd5e1;--brand:#2563eb;--brand2:#0ea5e9;--theme-accent:#2563eb;--theme-accent-2:#0ea5e9;--theme-accent-3:#10b981;--theme-glow:rgba(37,99,235,.12);--hero-grad:linear-gradient(135deg,#2563eb,#0ea5e9);--shadow:0 18px 42px rgba(15,23,42,.10);--shadow-soft:0 12px 28px rgba(15,23,42,.07);}
        body[data-app-theme]{background:radial-gradient(circle at 8% 8%, var(--theme-glow), transparent 30%),linear-gradient(135deg,var(--bg),var(--bg2));color:var(--text);transition:background .35s ease,color .25s ease;}
        body[data-app-theme] .topbar, body[data-app-theme] .sidebar{background:linear-gradient(180deg,rgba(255,255,255,.06),rgba(255,255,255,.02)),var(--card);border-color:var(--line);}body[data-app-theme] .card, body[data-app-theme] .hero, body[data-app-theme] .table-wrap, body[data-app-theme] .setting-tile, body[data-app-theme] .mini-item, body[data-app-theme] .mini-kpi, body[data-app-theme] .kpi-card{background:linear-gradient(180deg,rgba(255,255,255,.08),rgba(255,255,255,.02)),var(--card);border-color:var(--line);box-shadow:var(--shadow-soft);}body[data-app-theme] table, body[data-app-theme] th, body[data-app-theme] td{border-color:var(--line);}body[data-app-theme] th{background:linear-gradient(180deg,rgba(255,255,255,.10),rgba(255,255,255,.02)),var(--card-soft);color:var(--text);}body[data-app-theme] input, body[data-app-theme] select, body[data-app-theme] textarea{background:var(--card-soft);color:var(--text);border-color:var(--line);}body[data-app-theme] button, body[data-app-theme] .btn{background:linear-gradient(135deg,var(--theme-accent),var(--theme-accent-2));box-shadow:0 12px 26px var(--theme-glow);}body[data-app-theme] .badge.info, body[data-app-theme] .chip, body[data-app-theme] .theme-switch{border-color:var(--line);background:linear-gradient(135deg,rgba(255,255,255,.10),rgba(255,255,255,.03));color:var(--text);}body[data-app-theme] .sidebar a.active{background:linear-gradient(135deg,var(--theme-accent),var(--theme-accent-2));color:#fff;box-shadow:0 12px 30px var(--theme-glow);}body[data-app-theme] .orb{background:var(--theme-glow)!important;}body[data-app-theme="notion-clean"] .orb, body[data-app-theme="light-professional"] .orb, body[data-app-theme="college-formal"] .orb{opacity:.28;}
        .appearance-studio-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:16px;margin-top:18px}.appearance-card{cursor:pointer;min-height:170px;border-radius:24px;padding:18px;border:1px solid var(--line);background:linear-gradient(180deg,rgba(255,255,255,.10),rgba(255,255,255,.03)),var(--card);box-shadow:var(--shadow-soft);transition:transform .24s ease, box-shadow .24s ease, border-color .24s ease;position:relative;overflow:hidden}.appearance-card:hover{transform:translateY(-5px);box-shadow:var(--shadow)}.appearance-card.active{border-color:var(--theme-accent);box-shadow:0 0 0 1px var(--theme-accent),0 18px 45px var(--theme-glow)}.appearance-card .preview-band{height:56px;border-radius:18px;margin-bottom:14px;background:linear-gradient(135deg,var(--p1),var(--p2),var(--p3));box-shadow:0 14px 34px rgba(0,0,0,.16)}.appearance-card h3{margin:0 0 6px}.appearance-card p{margin:0;color:var(--muted);font-size:13px;line-height:1.5}.appearance-controls{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:14px;margin-top:16px}.appearance-control{padding:16px;border-radius:20px;border:1px solid var(--line);background:var(--card-soft)}
        .premium-skeleton,.rt-skeleton{position:relative;overflow:hidden;border-radius:18px;background:linear-gradient(90deg,rgba(148,163,184,.10),rgba(148,163,184,.18),rgba(148,163,184,.10));background-size:220% 100%;box-shadow:inset 0 0 0 1px rgba(255,255,255,.05),0 18px 44px rgba(0,0,0,.12);animation:premiumSkeletonShimmer 1.28s ease-in-out infinite}.premium-skeleton::after,.rt-skeleton::after{content:"";position:absolute;inset:0;background:linear-gradient(110deg,transparent 25%,rgba(255,255,255,.16) 42%,transparent 58%);transform:translateX(-120%);animation:premiumSkeletonSweep 1.65s infinite}.premium-skeleton-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:14px}.premium-skeleton-card{min-height:135px;border-radius:22px}.premium-skeleton-line{height:14px;border-radius:999px;margin:10px 0}.premium-skeleton-line.short{width:42%}.premium-skeleton-line.medium{width:68%}.premium-skeleton-line.long{width:92%}@keyframes premiumSkeletonShimmer{0%{background-position:0% 0}100%{background-position:220% 0}}@keyframes premiumSkeletonSweep{100%{transform:translateX(120%)}}
        body.anim-off *{animation:none!important;transition:none!important}body.anim-minimal *{transition-duration:.12s!important}body.anim-smooth *{transition-duration:.28s!important}body.anim-full .card,body.anim-full .hero,body.anim-full .appearance-card{transition:transform .35s ease, box-shadow .35s ease, border-color .35s ease}body.anim-full .card:hover,body.anim-full .hero:hover{transform:translateY(-3px)}

    </style>
</head>
<body class="{{ 'dark' if session.get('theme') == 'dark' else '' }}">

<script>
(function(){
    const allowedThemes = ['default-saas-dark','notion-clean','stripe-glow','vercel-minimal','netflix-dark','college-formal','purple-neon','light-professional'];
    const savedTheme = localStorage.getItem('zoomAttendanceGlobalTheme') || 'default-saas-dark';
    document.body.setAttribute('data-app-theme', allowedThemes.includes(savedTheme) ? savedTheme : 'default-saas-dark');
})();
</script>
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
            <label class="global-theme-control" title="Switch full app theme">
                🎨 Theme
                <select id="globalThemeSelect" aria-label="Global theme selector">
                    <option value="default-saas-dark">Default SaaS Dark</option>
                    <option value="notion-clean">Notion Clean</option>
                    <option value="stripe-glow">Stripe Glow</option>
                    <option value="vercel-minimal">Vercel Minimal</option>
                    <option value="netflix-dark">Netflix Dark</option>
                    <option value="college-formal">College Formal</option>
                    <option value="purple-neon">Purple Neon</option>
                    <option value="light-professional">Light Professional</option>
                </select>
            </label>
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


    function setupGlobalThemeSystem(){
        const allowedThemes = ['default-saas-dark','notion-clean','stripe-glow','vercel-minimal','netflix-dark','college-formal','purple-neon','light-professional'];
        const select = document.getElementById('globalThemeSelect');
        const savedTheme = localStorage.getItem('zoomAttendanceGlobalTheme') || 'default-saas-dark';
        const currentTheme = allowedThemes.includes(savedTheme) ? savedTheme : 'default-saas-dark';
        document.body.setAttribute('data-app-theme', currentTheme);
        if(select){
            select.value = currentTheme;
            select.addEventListener('change', function(){
                const nextTheme = allowedThemes.includes(this.value) ? this.value : 'default-saas-dark';
                localStorage.setItem('zoomAttendanceGlobalTheme', nextTheme);
                document.body.setAttribute('data-app-theme', nextTheme);
                if(window.Chart){
                    setupChartDefaults();
                    window.dispatchEvent(new CustomEvent('zoom-theme-changed', {detail:{theme: nextTheme}}));
                }
            });
        }
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



    function applyAnimationLevel(){const allowed=['off','minimal','smooth','full'];const level=allowed.includes(localStorage.getItem('zoomAttendanceAnimationLevel'))?localStorage.getItem('zoomAttendanceAnimationLevel'):'smooth';document.body.classList.remove('anim-off','anim-minimal','anim-smooth','anim-full');document.body.classList.add('anim-'+level);const sel=document.getElementById('animationLevelSelect');if(sel)sel.value=level;}
    function getThemePalette(){const cs=getComputedStyle(document.body);return{text:cs.getPropertyValue('--text').trim()||'#e5e7eb',muted:cs.getPropertyValue('--muted').trim()||'#94a3b8',grid:cs.getPropertyValue('--line').trim()||'rgba(148,163,184,.18)',a:cs.getPropertyValue('--theme-accent').trim()||'#6366f1',b:cs.getPropertyValue('--theme-accent-2').trim()||'#8b5cf6',c:cs.getPropertyValue('--theme-accent-3').trim()||'#22d3ee',ok:cs.getPropertyValue('--theme-success').trim()||'#22c55e',warn:cs.getPropertyValue('--theme-warning').trim()||'#f59e0b',danger:cs.getPropertyValue('--theme-danger').trim()||'#ef4444'};}
    function updateChartsForTheme(){if(!window.Chart)return;const p=getThemePalette();Chart.defaults.color=p.text;Chart.defaults.scale.grid.color=p.grid;Chart.defaults.plugins.legend.labels.color=p.text;const colors=[p.ok,p.warn,p.danger,p.c,p.a,p.b];try{Object.values(Chart.instances||{}).forEach((chart)=>{if(!chart||!chart.data)return;chart.options.color=p.text;if(chart.options.scales){Object.values(chart.options.scales).forEach((scale)=>{scale.grid=scale.grid||{};scale.ticks=scale.ticks||{};scale.grid.color=p.grid;scale.ticks.color=p.text;});}(chart.data.datasets||[]).forEach((ds,idx)=>{if(ds.label&&/present/i.test(ds.label)){ds.borderColor=p.ok;ds.backgroundColor=p.ok;}else if(ds.label&&/late/i.test(ds.label)){ds.borderColor=p.warn;ds.backgroundColor=p.warn;}else if(ds.label&&/absent/i.test(ds.label)){ds.borderColor=p.danger;ds.backgroundColor=p.danger;}else if(ds.label&&/unknown/i.test(ds.label)){ds.borderColor=p.c;ds.backgroundColor=p.c;}else{ds.borderColor=colors[idx%colors.length];ds.backgroundColor=colors[idx%colors.length];}});chart.update('none');});}catch(e){console.warn('Theme chart update skipped',e);}}
    function setupAppearanceEngineV8(){const allowedThemes=['default-saas-dark','notion-clean','stripe-glow','vercel-minimal','netflix-dark','college-formal','purple-neon','light-professional'];const savedTheme=localStorage.getItem('zoomAttendanceGlobalTheme')||'default-saas-dark';const currentTheme=allowedThemes.includes(savedTheme)?savedTheme:'default-saas-dark';document.body.setAttribute('data-app-theme',currentTheme);document.querySelectorAll('[data-theme-apply]').forEach((el)=>{const theme=el.getAttribute('data-theme-apply');el.classList.toggle('active',theme===currentTheme);el.addEventListener('click',function(){localStorage.setItem('zoomAttendanceGlobalTheme',theme);document.body.setAttribute('data-app-theme',theme);const topSelect=document.getElementById('globalThemeSelect');if(topSelect)topSelect.value=theme;document.querySelectorAll('[data-theme-apply]').forEach(c=>c.classList.toggle('active',c.getAttribute('data-theme-apply')===theme));setupChartDefaults();updateChartsForTheme();window.dispatchEvent(new CustomEvent('zoom-theme-changed',{detail:{theme}}));});});const topSelect=document.getElementById('globalThemeSelect');if(topSelect){topSelect.value=currentTheme;topSelect.addEventListener('change',function(){localStorage.setItem('zoomAttendanceGlobalTheme',this.value);document.body.setAttribute('data-app-theme',this.value);document.querySelectorAll('[data-theme-apply]').forEach(c=>c.classList.toggle('active',c.getAttribute('data-theme-apply')===this.value));setupChartDefaults();updateChartsForTheme();});}const animSelect=document.getElementById('animationLevelSelect');if(animSelect){animSelect.addEventListener('change',function(){localStorage.setItem('zoomAttendanceAnimationLevel',this.value);applyAnimationLevel();});}applyAnimationLevel();setTimeout(updateChartsForTheme,250);}
    window.setupAppearanceEngineV8=setupAppearanceEngineV8;

    document.addEventListener('DOMContentLoaded', function(){
        setupAppearanceEngineV8();
        setupGlobalThemeSystem();
        applyAutoTooltips();
        animateMetrics();
        enhanceButtons();
        setupChartDefaults();
        polishLayoutSpacing();
        enhanceWowEffects();
    });
})();
</script>

<!-- AI Level 3 Floating Bot -->
<style>.ai-floating-bot{position:fixed;right:22px;bottom:22px;z-index:9999}.ai-bot-orb{width:58px;height:58px;border-radius:50%;display:grid;place-items:center;background:linear-gradient(135deg,#6366f1,#22d3ee);box-shadow:0 18px 50px rgba(34,211,238,.35);cursor:pointer;font-size:26px}.ai-bot-panel{display:none;position:absolute;right:0;bottom:72px;width:360px;max-width:calc(100vw - 30px);background:rgba(2,6,23,.96);border:1px solid rgba(99,102,241,.35);border-radius:22px;padding:14px;box-shadow:0 30px 90px rgba(0,0,0,.45);color:#e5e7eb}.ai-bot-panel.open{display:block}.ai-bot-panel textarea{width:100%;min-height:68px;border-radius:14px;border:1px solid rgba(99,102,241,.35);background:#020617;color:#e5e7eb;padding:10px}.ai-bot-answer{white-space:pre-wrap;background:rgba(15,23,42,.9);border-radius:14px;padding:10px;margin-top:10px;max-height:220px;overflow:auto}.ai-bot-actions{display:flex;gap:6px;flex-wrap:wrap;margin:8px 0}.ai-bot-actions button{font-size:11px;padding:7px 9px;border-radius:999px}</style><div class="ai-floating-bot"><div class="ai-bot-panel" id="aiBotPanel"><b>🧠 AI Assistant</b><div class="ai-bot-actions"><button onclick="aiBotAsk('Who is at risk?')">Risk</button><button onclick="aiBotAsk('List members below 50%')">Below 50%</button><button onclick="aiBotAsk('Summarize last meeting')">Summary</button><button onclick="location.href='/ai-intelligence'">Dashboard</button></div><textarea id="aiBotInput" placeholder="Ask attendance question..."></textarea><button onclick="aiBotAsk(document.getElementById('aiBotInput').value)">Ask</button><div class="ai-bot-answer" id="aiBotAnswer">Ask me anything related to attendance, members, risk, late trend, reminders, or reports.</div></div><div class="ai-bot-orb" onclick="document.getElementById('aiBotPanel').classList.toggle('open')">🤖</div></div><script>function aiBotAsk(q){if(!q)return;const a=document.getElementById('aiBotAnswer');a.innerText='Thinking...';fetch('/api/ai-assistant-level3',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({query:q})}).then(r=>r.json()).then(d=>{a.innerText=d.response||'No answer';}).catch(()=>{a.innerText='AI assistant temporarily unavailable.';});}</script>

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
        {"key": "ai_intelligence", "label": "🧠 AI Intelligence", "href": url_for("ai_intelligence")},
        {"key": "attendance_register", "label": "📒 Attendance Register", "href": url_for("attendance_register")},
        {"key": "notification_control", "label": "🔔 Notification Control", "href": url_for("notification_control")},
        {"key": "appearance", "label": "🎨 Appearance Studio", "href": url_for("appearance")},
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


# UI_UPDATE_V7_REALTIME_LIVE_DASHBOARD_APPLIED = True


def _live_seconds_since(value):
    dt = parse_dt(value)
    if not dt:
        return 0
    return max(int((now_local() - dt).total_seconds()), 0)


def build_live_snapshot_payload(include_feed=True):
    """Lightweight live dashboard payload for AJAX polling. Does not change attendance logic."""
    maybe_finalize_stale_live_meetings()
    info = read_live_snapshot()
    server_now = now_local()
    if not info:
        return {
            "ok": True,
            "has_live": False,
            "server_now": server_now.isoformat(),
            "meeting": None,
            "summary": {
                "active_now": 0,
                "known_count": 0,
                "unknown_count": 0,
                "not_joined_count": 0,
                "total_tracked": 0,
                "host_present": False,
                "meeting_duration_seconds": 0,
                "risk": "Idle",
            },
            "participants": [],
            "not_joined": [],
            "feed": [],
        }

    meeting = info.get("meeting") or {}
    participants = info.get("participants") or []
    not_joined_members = info.get("not_joined_members") or []
    start_dt = parse_dt(meeting.get("start_time")) or server_now

    active_now = 0
    known_active = 0
    unknown_active = 0
    known_total = 0
    unknown_total = 0
    host_present = False
    participant_payload = []
    feed_items = []

    for p in participants:
        is_active_now = p.get("current_join") is not None
        is_known = bool(p.get("is_member"))
        is_host = bool(p.get("is_host"))
        live_status, live_total = get_live_status_for_row(p, start_dt)
        if is_active_now:
            active_now += 1
            if is_known:
                known_active += 1
            else:
                unknown_active += 1
            if is_host:
                host_present = True
        if is_known:
            known_total += 1
        else:
            unknown_total += 1

        row_id = str(p.get("id") or p.get("participant_key") or p.get("participant_name") or "")
        current_join = parse_dt(p.get("current_join"))
        participant_payload.append({
            "id": row_id,
            "name": p.get("participant_name") or "-",
            "email": p.get("participant_email") or "",
            "type": "Known" if is_known else "Unknown",
            "is_known": is_known,
            "is_host": is_host,
            "is_active": is_active_now,
            "first_join": fmt_time_ampm(p.get("first_join")) if p.get("first_join") else "-",
            "last_leave": fmt_time_ampm(p.get("last_leave")) if p.get("last_leave") else ("Live now" if is_active_now else "-"),
            "duration_seconds": int(live_total or 0),
            "duration_min": mins_from_seconds(live_total),
            "rejoins": p.get("rejoin_count") or 0,
            "status": live_status,
            "current_join_iso": current_join.isoformat() if current_join else "",
        })

        if include_feed:
            if p.get("first_join"):
                feed_items.append({
                    "id": f"join-{row_id}",
                    "name": p.get("participant_name") or "-",
                    "time": fmt_time_ampm(p.get("first_join")),
                    "kind": "join",
                    "label": "Joined",
                    "tag": "LIVE" if is_active_now else ("KNOWN" if is_known else "UNKNOWN"),
                    "sort": (parse_dt(p.get("first_join")) or start_dt).timestamp(),
                })
            if p.get("last_leave") and not is_active_now:
                feed_items.append({
                    "id": f"leave-{row_id}",
                    "name": p.get("participant_name") or "-",
                    "time": fmt_time_ampm(p.get("last_leave")),
                    "kind": "leave",
                    "label": "Left",
                    "tag": "LEFT",
                    "sort": (parse_dt(p.get("last_leave")) or start_dt).timestamp(),
                })

    feed_items = sorted(feed_items, key=lambda x: x.get("sort", 0), reverse=True)[:30]
    risk = "Healthy" if host_present and unknown_active <= max(1, known_active // 2) else ("Warning" if active_now > 0 else "Critical")

    return {
        "ok": True,
        "has_live": True,
        "server_now": server_now.isoformat(),
        "meeting": {
            "uuid": meeting.get("meeting_uuid") or "",
            "id": meeting.get("meeting_id") or "-",
            "topic": meeting.get("topic") or "Untitled Meeting",
            "start_time": fmt_dt(meeting.get("start_time")),
            "start_iso": start_dt.isoformat(),
        },
        "summary": {
            "active_now": active_now,
            "known_count": known_active,
            "unknown_count": unknown_active,
            "known_total": known_total,
            "unknown_total": unknown_total,
            "not_joined_count": len(not_joined_members),
            "total_tracked": len(participants),
            "host_present": host_present,
            "meeting_duration_seconds": max(int((server_now - start_dt).total_seconds()), 0),
            "risk": risk,
        },
        "participants": participant_payload,
        "not_joined": [
            {
                "id": m.get("id"),
                "name": member_display_name(m),
                "contact": m.get("email") or m.get("phone") or "No contact info",
            }
            for m in not_joined_members[:40]
        ],
        "feed": feed_items,
    }


@app.route("/api/live-snapshot")
@login_required
def api_live_snapshot():
    return jsonify(build_live_snapshot_payload(include_feed=True))


@app.route("/api/live-summary")
@login_required
def api_live_summary():
    payload = build_live_snapshot_payload(include_feed=False)
    return jsonify({
        "ok": payload.get("ok"),
        "has_live": payload.get("has_live"),
        "server_now": payload.get("server_now"),
        "meeting": payload.get("meeting"),
        "summary": payload.get("summary"),
    })


@app.route("/api/live-feed")
@login_required
def api_live_feed():
    payload = build_live_snapshot_payload(include_feed=True)
    return jsonify({
        "ok": payload.get("ok"),
        "has_live": payload.get("has_live"),
        "server_now": payload.get("server_now"),
        "feed": payload.get("feed", []),
    })


@app.route("/live")
@login_required

def live():
    body = render_template_string(
        """
        <style>
            .rt-live-shell{position:relative;overflow:hidden;border:1px solid rgba(99,102,241,.22)}
            .rt-live-shell:before{content:"";position:absolute;inset:-80px;background:radial-gradient(circle at 15% 15%,rgba(239,68,68,.20),transparent 30%),radial-gradient(circle at 90% 10%,rgba(59,130,246,.16),transparent 34%),radial-gradient(circle at 70% 90%,rgba(34,197,94,.14),transparent 30%);pointer-events:none;filter:blur(2px)}
            .rt-live-inner{position:relative;z-index:1}
            .rt-live-badge{display:inline-flex;align-items:center;gap:9px;border-radius:999px;padding:8px 13px;background:rgba(239,68,68,.12);border:1px solid rgba(239,68,68,.34);color:#fecaca;font-size:12px;font-weight:900;letter-spacing:.08em;text-transform:uppercase}
            .rt-pulse-dot{width:10px;height:10px;border-radius:999px;background:#ef4444;box-shadow:0 0 0 rgba(239,68,68,.7);animation:rtPulse 1.25s infinite}
            @keyframes rtPulse{0%{box-shadow:0 0 0 0 rgba(239,68,68,.7);transform:scale(.92)}70%{box-shadow:0 0 0 13px rgba(239,68,68,0);transform:scale(1)}100%{box-shadow:0 0 0 0 rgba(239,68,68,0);transform:scale(.92)}}
            .rt-connection{display:inline-flex;align-items:center;gap:8px;border-radius:999px;padding:8px 12px;font-size:12px;font-weight:900;border:1px solid rgba(148,163,184,.22);background:rgba(15,23,42,.55)}
            .rt-connection.ok{color:#86efac;border-color:rgba(34,197,94,.26)}
            .rt-connection.bad{color:#fecaca;border-color:rgba(239,68,68,.34)}
            .rt-stat-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:14px;margin-top:16px}
            .rt-stat{border-radius:22px;border:1px solid rgba(148,163,184,.18);background:linear-gradient(180deg,rgba(255,255,255,.07),rgba(255,255,255,.025));padding:18px;transition:transform .25s ease,border-color .25s ease,box-shadow .25s ease}
            .rt-stat:hover{transform:translateY(-2px);border-color:rgba(99,102,241,.35);box-shadow:0 16px 40px rgba(0,0,0,.28)}
            .rt-stat-label{font-size:12px;text-transform:uppercase;letter-spacing:.07em;color:#94a3b8;font-weight:900}.rt-stat-value{font-size:32px;font-weight:950;margin-top:8px}.rt-stat-sub{font-size:12px;color:#94a3b8;margin-top:3px}
            .rt-host.present{color:#86efac}.rt-host.absent{color:#fca5a5}
            .rt-skeleton{position:relative;overflow:hidden;background:rgba(148,163,184,.10);border-radius:14px;min-height:18px}.rt-skeleton:after{content:"";position:absolute;inset:0;background:linear-gradient(90deg,transparent,rgba(255,255,255,.12),transparent);transform:translateX(-100%);animation:rtShimmer 1.35s infinite}@keyframes rtShimmer{100%{transform:translateX(100%)}}
            .rt-row{transition:background .35s ease,opacity .35s ease,transform .35s ease}.rt-row.new{background:rgba(34,197,94,.13)!important;animation:rtNewRow 1.8s ease}.rt-row.left{opacity:.48}.rt-row.fade-out{opacity:0;transform:translateX(15px)}@keyframes rtNewRow{0%{box-shadow:inset 4px 0 #22c55e;background:rgba(34,197,94,.28)}100%{box-shadow:inset 0 0 transparent}}
            .rt-feed-item{animation:rtFeedIn .45s ease;border-left:3px solid rgba(99,102,241,.55)}@keyframes rtFeedIn{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:translateY(0)}}
            .rt-duration{font-variant-numeric:tabular-nums;font-weight:900}.rt-empty-live{padding:38px 18px;text-align:center}.rt-hidden{display:none!important}
            .rt-live-table td,.rt-live-table th{transition:background .25s ease,color .25s ease}
        </style>

        <div class="hero rt-live-shell">
            <div class="rt-live-inner">
                <div class="row" style="justify-content:space-between;gap:12px;align-items:flex-start;flex-wrap:wrap">
                    <div>
                        <div class="rt-live-badge"><span class="rt-pulse-dot"></span><span id="rtLiveBadgeText">LIVE OPERATIONS BOARD</span></div>
                        <h1 class="hero-title" id="rtMeetingTopic" style="margin-top:14px">Live Dashboard</h1>
                        <div class="hero-copy" id="rtMeetingCopy">Real-time animated dashboard powered by lightweight AJAX polling. No full page reload, no webhook logic change.</div>
                        <div class="row" style="margin-top:16px;gap:10px;flex-wrap:wrap">
                            <span class="badge info" id="rtMeetingId">Meeting ID -</span>
                            <span class="badge gray" id="rtMeetingStarted">Started -</span>
                            <span class="badge gray" id="rtMeetingDuration">Duration 00:00:00</span>
                            <span class="badge gray" id="rtRiskBadge">Risk Idle</span>
                        </div>
                    </div>
                    <div class="stack" style="align-items:flex-end">
                        <div id="rtConnection" class="rt-connection bad">● Connecting...</div>
                        <div class="muted" style="font-size:12px">Polling every 2.5 seconds</div>
                    </div>
                </div>

                <div class="rt-stat-grid">
                    <div class="rt-stat"><div class="rt-stat-label">Live Participants</div><div class="rt-stat-value" data-counter="active_now">0</div><div class="rt-stat-sub">Currently inside meeting</div></div>
                    <div class="rt-stat"><div class="rt-stat-label">Known Members</div><div class="rt-stat-value" data-counter="known_count">0</div><div class="rt-stat-sub">Registered and active now</div></div>
                    <div class="rt-stat"><div class="rt-stat-label">Unknown</div><div class="rt-stat-value" data-counter="unknown_count">0</div><div class="rt-stat-sub">Unmatched live participants</div></div>
                    <div class="rt-stat"><div class="rt-stat-label">Host Status</div><div class="rt-stat-value rt-host absent" id="rtHostStatus">Absent</div><div class="rt-stat-sub">Present / absent indicator</div></div>
                    <div class="rt-stat"><div class="rt-stat-label">Not Joined</div><div class="rt-stat-value" data-counter="not_joined_count">0</div><div class="rt-stat-sub">Active members pending</div></div>
                </div>
            </div>
        </div>

        <div id="rtLoadingSkeleton" class="grid-2" style="margin-top:16px;grid-template-columns:minmax(0,1.45fr) minmax(320px,.55fr)">
            <div class="card"><div class="rt-skeleton" style="height:26px;width:260px;margin-bottom:18px"></div><div class="rt-skeleton" style="height:320px"></div></div>
            <div class="card"><div class="rt-skeleton" style="height:26px;width:160px;margin-bottom:18px"></div><div class="rt-skeleton" style="height:320px"></div></div>
        </div>

        <div id="rtLiveContent" class="grid-2 rt-hidden" style="margin-top:16px;grid-template-columns:minmax(0,1.45fr) minmax(320px,.55fr)">
            <div class="card">
                <div class="section-title">
                    <div><h3 style="margin:0">Live Participants Board</h3><p>Animated participant status, duration, join/leave movement and known/unknown split.</p></div>
                    <span class="badge ok"><span class="rt-pulse-dot" style="width:8px;height:8px"></span> AJAX Live</span>
                </div>
                <div id="rtNoLive" class="rt-empty-live">
                    <div class="empty-icon">📡</div>
                    <h3 style="margin:0 0 8px 0">Waiting for active Zoom meeting</h3>
                    <div class="muted">Start a meeting and send Zoom webhook events. This dashboard will update automatically without reload.</div>
                </div>
                <div class="table-wrap" id="rtTableWrap">
                    <table class="rt-live-table">
                        <thead><tr><th>Name</th><th>Type</th><th>Join</th><th>Leave</th><th>Duration</th><th>Rejoins</th><th>Status</th></tr></thead>
                        <tbody id="rtParticipantsBody"></tbody>
                    </table>
                </div>
            </div>

            <div class="stack">
                <div class="card">
                    <div class="section-title"><div><h3 style="margin:0">Join / Leave Feed</h3><p>New entries animate in automatically.</p></div></div>
                    <div class="list-card" id="rtFeed" style="max-height:340px;overflow:auto"></div>
                </div>
                <div class="card">
                    <div class="section-title"><div><h3 style="margin:0">Members Not Yet Joined</h3><p>Active members absent from the current session.</p></div></div>
                    <div class="list-card" id="rtNotJoined" style="max-height:300px;overflow:auto"></div>
                </div>
            </div>
        </div>

        <script>
        (function(){
            const pollMs = 2500;
            const state = { knownRows:new Map(), lastSnapshot:null, failed:0, firstLoad:true, meetingStart:null, durationTimer:null };
            const $ = (id) => document.getElementById(id);
            const counters = {};

            function esc(v){return String(v ?? '').replace(/[&<>'"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));}
            function fmtSeconds(sec){sec=Math.max(0,parseInt(sec||0,10)); const h=String(Math.floor(sec/3600)).padStart(2,'0'); const m=String(Math.floor((sec%3600)/60)).padStart(2,'0'); const s=String(sec%60).padStart(2,'0'); return `${h}:${m}:${s}`;}
            function badgeClass(status){status=String(status||'').toUpperCase(); if(status==='PRESENT'||status==='HOST') return 'ok'; if(status==='LATE') return 'warn'; if(status==='JOINED') return 'info'; if(status==='ABSENT') return 'danger'; return 'gray';}
            function setConnection(ok,msg){const el=$('rtConnection'); el.className='rt-connection '+(ok?'ok':'bad'); el.textContent=(ok?'● Connected':'● Reconnecting')+(msg?` · ${msg}`:'');}
            function animateCounter(key,next){const el=document.querySelector(`[data-counter="${key}"]`); if(!el) return; const from=counters[key] ?? parseInt(el.textContent||'0',10) || 0; const to=parseInt(next||0,10); counters[key]=to; const start=performance.now(); const dur=450; function step(t){const p=Math.min(1,(t-start)/dur); const val=Math.round(from+(to-from)*(1-Math.pow(1-p,3))); el.textContent=val; if(p<1) requestAnimationFrame(step);} requestAnimationFrame(step);}
            function startDurationClock(baseSeconds){ if(state.durationTimer) clearInterval(state.durationTimer); let startTick=Date.now(); state.durationTimer=setInterval(()=>{const elapsed=Math.floor((Date.now()-startTick)/1000); $('rtMeetingDuration').textContent='Duration '+fmtSeconds((baseSeconds||0)+elapsed); updateActiveDurations(elapsed);},1000); }
            function updateActiveDurations(extra){ document.querySelectorAll('[data-base-duration]').forEach(el=>{const active=el.getAttribute('data-active')==='1'; const base=parseInt(el.getAttribute('data-base-duration')||'0',10); el.textContent=fmtSeconds(base+(active?extra:0));}); }
            function renderParticipants(rows){ const body=$('rtParticipantsBody'); const incoming=new Set(); let html=''; (rows||[]).forEach(p=>{const id=String(p.id||p.name); incoming.add(id); const isNew=!state.knownRows.has(id) && !state.firstLoad; const active=p.is_active; state.knownRows.set(id,p); html+=`<tr class="rt-row ${isNew?'new':''} ${active?'':'left'}" data-row-id="${esc(id)}"><td><b>${esc(p.name)}</b>${p.is_host?' <span class="badge info">HOST</span>':''}</td><td><span class="badge ${p.is_known?'ok':'warn'}">${esc(p.type)}</span></td><td>${esc(p.first_join)}</td><td>${esc(p.last_leave)}</td><td><span class="rt-duration" data-base-duration="${parseInt(p.duration_seconds||0,10)}" data-active="${active?'1':'0'}">${fmtSeconds(p.duration_seconds)}</span></td><td>${esc(p.rejoins)}</td><td>${active?`<span class="status-pill status-live"><span class="status-pulse"></span>${esc(p.status)}</span>`:`<span class="badge ${badgeClass(p.status)}">${esc(p.status)}</span>`}</td></tr>`; });
                for (const id of Array.from(state.knownRows.keys())) { if(!incoming.has(id)) state.knownRows.delete(id); }
                body.innerHTML=html || '<tr><td colspan="7" class="muted">No participant data yet.</td></tr>'; }
            function renderFeed(feed){ const box=$('rtFeed'); if(!feed || !feed.length){box.innerHTML='<div class="muted">No join/leave events yet.</div>';return;} box.innerHTML=feed.map(item=>`<div class="list-row rt-feed-item"><div><div style="font-weight:900">${esc(item.name)}</div><div class="muted">${esc(item.label)} · ${esc(item.time)}</div></div><span class="badge ${item.kind==='join'?'ok':'gray'}">${esc(item.tag)}</span></div>`).join(''); }
            function renderNotJoined(rows){ const box=$('rtNotJoined'); if(!rows || !rows.length){box.innerHTML='<div class="empty-state" style="padding:22px 18px"><div class="empty-icon" style="width:58px;height:58px;font-size:22px">✅</div><div style="font-weight:900;margin-bottom:6px">All active members joined</div><div class="muted">No pending active member remains outside the current session.</div></div>';return;} box.innerHTML=rows.map(m=>`<div class="list-row"><div><div style="font-weight:800">${esc(m.name)}</div><div class="muted">${esc(m.contact)}</div></div><span class="badge danger">Not joined</span></div>`).join(''); }
            function renderSnapshot(data){
                $('rtLoadingSkeleton').classList.add('rt-hidden'); $('rtLiveContent').classList.remove('rt-hidden'); state.firstLoad=false;
                if(!data.has_live){ $('rtLiveBadgeText').textContent='LIVE DASHBOARD IDLE'; $('rtMeetingTopic').textContent='Waiting for the next Zoom session'; $('rtMeetingCopy').textContent='No active live meeting right now. This page will reconnect automatically when Zoom webhook events arrive.'; $('rtMeetingId').textContent='Meeting ID -'; $('rtMeetingStarted').textContent='Started -'; $('rtRiskBadge').textContent='Risk Idle'; $('rtRiskBadge').className='badge gray'; $('rtHostStatus').textContent='Absent'; $('rtHostStatus').className='rt-stat-value rt-host absent'; ['active_now','known_count','unknown_count','not_joined_count'].forEach(k=>animateCounter(k,0)); $('rtNoLive').classList.remove('rt-hidden'); $('rtTableWrap').classList.add('rt-hidden'); renderFeed([]); renderNotJoined([]); startDurationClock(0); return; }
                const m=data.meeting||{}, s=data.summary||{}; $('rtLiveBadgeText').textContent='LIVE OPERATIONS BOARD'; $('rtMeetingTopic').textContent=m.topic||'Untitled Meeting'; $('rtMeetingCopy').textContent='Real-time command board for participant flow, host visibility, member presence, unknown risk, and attendance movement.'; $('rtMeetingId').textContent='Meeting ID '+(m.id||'-'); $('rtMeetingStarted').textContent='Started '+(m.start_time||'-'); $('rtRiskBadge').textContent='Risk '+(s.risk||'Idle'); $('rtRiskBadge').className='badge '+(s.risk==='Healthy'?'ok':s.risk==='Warning'?'warn':'danger'); $('rtHostStatus').textContent=s.host_present?'Present':'Absent'; $('rtHostStatus').className='rt-stat-value rt-host '+(s.host_present?'present':'absent'); animateCounter('active_now',s.active_now); animateCounter('known_count',s.known_count); animateCounter('unknown_count',s.unknown_count); animateCounter('not_joined_count',s.not_joined_count); $('rtNoLive').classList.add('rt-hidden'); $('rtTableWrap').classList.remove('rt-hidden'); renderParticipants(data.participants||[]); renderFeed(data.feed||[]); renderNotJoined(data.not_joined||[]); startDurationClock(s.meeting_duration_seconds||0);
            }
            async function poll(){ try{ const res=await fetch('{{ url_for('api_live_snapshot') }}',{headers:{'Accept':'application/json'},cache:'no-store'}); if(!res.ok) throw new Error('HTTP '+res.status); const data=await res.json(); state.failed=0; setConnection(true,'updated '+new Date().toLocaleTimeString()); renderSnapshot(data); }catch(err){ state.failed++; setConnection(false, state.failed>1?'retrying':''); console.warn('Live polling failed',err); } finally { setTimeout(poll,pollMs); } }
            poll();
        })();
        </script>
        """
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
    graph_options = graph_analytics_options()

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

        <style>
        .dash-showcase{display:grid;grid-template-columns:180px minmax(0,1fr) 310px;gap:14px;margin-top:16px;align-items:start}
        .dash-mini-sidebar{background:#0f172a;color:#e5e7eb;border-radius:16px;padding:14px;box-shadow:0 14px 35px rgba(15,23,42,.18);position:sticky;top:92px}
        .dash-mini-brand{font-weight:950;font-size:15px;line-height:1.25;margin-bottom:14px;display:flex;gap:8px;align-items:center}
        .dash-mini-nav{display:grid;gap:8px}.dash-mini-nav a,.dash-note{border-radius:12px;padding:10px 11px;text-decoration:none;color:#e5e7eb;font-weight:800;font-size:13px;background:rgba(255,255,255,.04)}
        .dash-mini-nav a.active{background:linear-gradient(135deg,#6d28d9,#7c3aed);box-shadow:0 12px 26px rgba(109,40,217,.30)}
        .dash-note{margin-top:14px;background:#fff8d6;color:#28334a;border:1px solid #f1d976;font-size:12px;line-height:1.5}
        .dash-main-title{display:flex;justify-content:space-between;gap:10px;align-items:center;margin-bottom:12px}
        .dash-title-pill{margin:auto;background:#0b274b;color:#fff;border-radius:11px;padding:10px 26px;font-size:24px;font-weight:950;letter-spacing:.5px;text-align:center;box-shadow:0 10px 25px rgba(2,6,23,.18)}
        .dash-actions{display:flex;gap:16px;align-items:center;white-space:nowrap;color:#0f172a;font-weight:850}
        body.dark .dash-actions{color:#e5e7eb}
        .dash-card{background:rgba(255,255,255,.92);border:1px solid rgba(15,23,42,.10);border-radius:14px;box-shadow:0 8px 24px rgba(15,23,42,.10);padding:16px;color:#172033}
        body.dark .dash-card{background:rgba(15,23,42,.76);border-color:rgba(148,163,184,.20);color:#e5e7eb}
        .analytics-layout{display:grid;grid-template-columns:minmax(0,1fr) 240px;gap:14px}
        .chart-title{font-weight:950;font-size:16px;margin-bottom:8px}.chart-sub{font-size:12px;color:#64748b;margin-top:-4px;margin-bottom:8px}
        body.dark .chart-sub{color:#94a3b8}.chart-big{height:310px}.chart-small{height:260px}
        .control-stack{border-left:1px solid rgba(148,163,184,.25);padding-left:14px;display:grid;gap:10px}.control-title{font-weight:950;color:#1e3a8a;margin-bottom:4px}
        body.dark .control-title{color:#bfdbfe}.control-stack label,.side-control label{font-size:12px;font-weight:900;color:#334155;margin-bottom:4px;display:block}body.dark .control-stack label,body.dark .side-control label{color:#cbd5e1}
        .control-stack input,.control-stack select,.side-control input,.side-control select{height:34px;padding:6px 9px;border-radius:8px;font-size:13px}
        .apply-wide{width:100%;justify-content:center;margin-top:6px;border-radius:8px}
        .bottom-grid{display:grid;grid-template-columns:minmax(0,1.15fr) minmax(240px,.75fr);gap:14px;margin-top:14px}.participant-chart-grid{display:grid;grid-template-columns:minmax(0,1fr) 220px;gap:14px}
        .side-help{background:#fff7df;border:1px solid #e7cb8a;border-radius:16px;padding:14px;color:#2f3142;position:sticky;top:92px}.side-help h3{font-size:14px;margin:0 0 8px;color:#0f172a}.side-help ul{margin:0;padding-left:18px;line-height:1.65;font-size:13px}
        .checkbox-select{position:relative}.checkbox-select-btn{width:100%;text-align:left;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;padding:8px 10px;border-radius:8px;font-size:13px;min-height:34px}.checkbox-select-menu{display:none;position:absolute;z-index:80;top:calc(100% + 6px);left:0;right:0;max-height:230px;overflow:auto;background:rgba(255,255,255,.98);color:#172033;border:1px solid rgba(148,163,184,.35);border-radius:12px;box-shadow:0 20px 45px rgba(0,0,0,.22);padding:8px}.checkbox-select.open .checkbox-select-menu{display:block}.checkbox-select-menu label{display:flex;gap:8px;align-items:center;padding:7px;border-radius:9px;cursor:pointer;font-size:13px}.checkbox-select-menu label:hover{background:rgba(99,102,241,.12)}.checkbox-select-menu input{width:auto;height:auto}body.dark .checkbox-select-menu{background:#0f172a;color:#e5e7eb}
        .month-year-box{background:#fff8df;border:1px solid #e4c779;border-radius:10px;padding:10px;display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:8px}.month-year-box h4{margin:0 0 6px;font-size:12px}.month-year-box label{display:flex;gap:7px;align-items:center;font-size:12px;margin:4px 0}.month-year-box input{height:auto}.register-table-wrap{max-height:72vh;overflow:auto;border-radius:18px;border:1px solid rgba(148,163,184,.22)}.register-table{border-collapse:separate;border-spacing:0;width:max-content;min-width:100%}.register-table th,.register-table td{min-width:44px;text-align:center;padding:9px 10px;border-bottom:1px solid rgba(148,163,184,.16);border-right:1px solid rgba(148,163,184,.12)}.register-table th{position:sticky;top:0;z-index:4;background:#111827}.register-table .sticky-member{position:sticky;left:0;z-index:5;min-width:230px;text-align:left;background:#111827}.register-table td.sticky-member{z-index:3;background:rgba(15,23,42,.98);font-weight:800;cursor:pointer}.reg-cell{font-weight:900;border-radius:9px;color:#08111f}.reg-p{background:#22c55e}.reg-l{background:#facc15}.reg-a{background:#ef4444;color:#fff}.reg-u{background:#94a3b8}.reg-empty{color:#94a3b8}.modal-backdrop{display:none;position:fixed;inset:0;background:rgba(2,6,23,.68);z-index:999;align-items:center;justify-content:center;padding:18px}.modal-backdrop.show{display:flex}.modal-card{max-width:460px;width:100%;background:#0f172a;border:1px solid rgba(148,163,184,.3);border-radius:22px;padding:22px;box-shadow:0 30px 80px rgba(0,0,0,.45)}
        @media(max-width:1180px){.dash-showcase{grid-template-columns:1fr}.dash-mini-sidebar,.side-help{position:static}.analytics-layout,.bottom-grid,.participant-chart-grid{grid-template-columns:1fr}.dash-main-title{flex-direction:column}.dash-title-pill{width:100%;font-size:18px}.control-stack{border-left:0;padding-left:0}}
        </style>


        <style>
        /* Compact graph analytics: remove helper panels and give charts more space */
        .dash-mini-sidebar,.dash-actions,.side-help{display:none!important;}
        .dash-showcase{grid-template-columns:minmax(0,1fr)!important;}
        .analytics-layout{grid-template-columns:minmax(0,1fr)!important;}
        .bottom-grid{grid-template-columns:minmax(0,1fr)!important;}
        .bottom-grid > .dash-card:nth-child(2){display:none!important;}
        .participant-chart-grid{grid-template-columns:minmax(0,1fr) 260px!important;}
        .chart-big{height:360px!important}.chart-small{height:330px!important;}
        .checkbox-select-menu{z-index:9999!important;}
        /* ANALYTICS_TABS_V3: organized navigation without removing old analytics */
        .analytics-tab-shell{position:sticky;top:78px;z-index:70;margin:16px 0 14px;padding:10px;border-radius:18px;background:rgba(2,6,23,.72);border:1px solid rgba(96,165,250,.22);backdrop-filter:blur(16px);box-shadow:0 18px 45px rgba(0,0,0,.28)}
        .analytics-tab-nav{display:flex;gap:10px;overflow-x:auto;scrollbar-width:thin;padding:2px}
        .analytics-tab-nav a{flex:0 0 auto;text-decoration:none;color:#cbd5e1;background:rgba(15,23,42,.9);border:1px solid rgba(148,163,184,.18);border-radius:14px;padding:11px 14px;font-size:13px;font-weight:950;transition:transform .18s ease,background .18s ease,border-color .18s ease,box-shadow .18s ease,color .18s ease}
        .analytics-tab-nav a:hover,.analytics-tab-nav a.active{transform:translateY(-2px);color:#fff;background:linear-gradient(135deg,#2563eb,#7c3aed);border-color:rgba(191,219,254,.55);box-shadow:0 14px 30px rgba(37,99,235,.28)}
        .analytics-anchor-section{scroll-margin-top:154px;animation:analyticsFadeIn .24s ease both}
        @keyframes analyticsFadeIn{from{opacity:.72;transform:translateY(6px)}to{opacity:1;transform:none}}
        html{scroll-behavior:smooth}
        </style>
        <div class="analytics-tab-shell" id="analyticsTabsV3">
            <nav class="analytics-tab-nav" aria-label="Analytics sections">
                <a class="active" href="#analyticsOverview">Overview</a>
                <a href="#graphAnalyticsSection">Graph Analytics</a>
                <a href="{{ url_for('attendance_register') }}">Register</a>
                <a href="#analyticsMembers">Members</a>
                <a href="#analyticsRisk">Risk</a>
                <a href="#analyticsTrends">Trends</a>
                <a href="#analyticsReports">Reports</a>
            </nav>
        </div>
        <div class="dash-showcase analytics-anchor-section" id="graphAnalyticsSection">
            <aside class="dash-mini-sidebar">
                <div class="dash-mini-brand">📊 Analytical<br>Dashboard</div>
                <nav class="dash-mini-nav">
                    <a class="active" href="#graphAnalyticsSection">Overview</a>
                    <a href="#gaTrendChart">Attendance Graphs</a>
                    <a href="{{ url_for('attendance_register') }}">Register View</a>
                    <a href="#analyticsRows">Participants</a>
                    <a href="{{ export_pdf_url }}">Reports</a>
                </nav>
                <div class="dash-note"><b>GRAPH 1: PARTICIPATION OVER TIME</b><br>Line graph with 4 lines: Present, Late, Absent and Unknown.</div>
                <div class="dash-note" style="background:#eaf4ff;border-color:#93c5fd"><b>GRAPH 2: TIME SPENT</b><br>Multiple members → members on X-axis.<br>Single member → date vs duration.</div>
            </aside>

            <main>
                <div class="dash-main-title">
                    <div class="dash-title-pill">1. ANALYTICAL DASHBOARD (GRAPHS & INSIGHTS)</div>
                    <div class="dash-actions"><span>⬇ Export</span><span>⟳ Refresh</span><span>⚿ Filters</span></div>
                </div>

                <div class="analytics-layout">
                    <div class="dash-card">
                        <div class="analytics-layout" style="grid-template-columns:minmax(0,1fr) 230px">
                            <div>
                                <div class="chart-title">Participants Over Time</div>
                                <div class="chart-big"><canvas id="gaTrendChart"></canvas></div>
                            </div>
                            <div class="control-stack">
                                <div class="control-title">Graph 1 Controls</div>
                                <div><label>X-Axis</label><select id="gaXAxis"><option value="date">Date</option><option value="month">Month</option><option value="year">Year</option></select></div>
                                <div><label>Y-Axis</label><select id="gaYAxis"><option value="count">Number of Participants</option><option value="percentage">Percentage</option></select></div>
                                <div class="ga-date-filter"><label>From Date</label><input type="date" id="gaFromDate"></div>
                                <div class="ga-date-filter"><label>To Date</label><input type="date" id="gaToDate"></div>
                                <button type="button" class="apply-wide" id="gaApplyBtn">Apply</button>
                            </div>
                        </div>
                    </div>

                    <aside class="side-help">
                        <h3>AXIS & DATE SELECTION OPTIONS</h3>
                        <ul>
                            <li>Choose Date / Month / Year for X-axis</li>
                            <li>Choose Count or Percentage for Y-axis</li>
                            <li>Select date range for Date mode</li>
                            <li>Select multiple months or years with checkboxes</li>
                        </ul>
                        <div class="month-year-box">
                            <div class="ga-month-filter" style="display:none">
                                <h4>If X-Axis = Month</h4>
                                <div class="checkbox-select" data-target="gaMonths">
                                    <button type="button" class="checkbox-select-btn">All months</button>
                                    <div class="checkbox-select-menu">
                                        <label><input type="checkbox" value="__all__" checked> All Months</label>
                                        {% for month in graph_options.months %}<label><input type="checkbox" value="{{ month.value }}"> {{ month.label }}</label>{% endfor %}
                                    </div>
                                </div>
                            </div>
                            <div class="ga-year-filter" style="display:none">
                                <h4>If X-Axis = Year</h4>
                                <div class="checkbox-select" data-target="gaYears">
                                    <button type="button" class="checkbox-select-btn">All years</button>
                                    <div class="checkbox-select-menu">
                                        <label><input type="checkbox" value="__all__" checked> All Years</label>
                                        {% for year in graph_options.years %}<label><input type="checkbox" value="{{ year }}"> {{ year }}</label>{% endfor %}
                                    </div>
                                </div>
                            </div>
                        </div>
                    </aside>
                </div>

                <div class="bottom-grid">
                    <div class="dash-card">
                        <div class="participant-chart-grid">
                            <div>
                                <div class="chart-title">Time Spent by Participants (In Minutes)</div>
                                <div class="chart-sub" id="gaDurationHint">All selected members total duration in minutes.</div>
                                <div class="chart-small"><canvas id="gaDurationChart"></canvas></div>
                            </div>
                            <div class="side-control">
                                <div class="control-title">Graph 2 Controls</div>
                                <label>Select Participants</label>
                                <div class="checkbox-select" data-target="gaMembers">
                                    <button type="button" class="checkbox-select-btn">All members</button>
                                    <div class="checkbox-select-menu">
                                        <label><input type="checkbox" value="__all__" checked> All Members</label>
                                        {% for member in graph_options.members %}<label><input type="checkbox" value="{{ member.id }}"> {{ member.name }}</label>{% endfor %}
                                    </div>
                                </div>
                                <div style="margin-top:10px"><label>From Date</label><input type="date" id="gaDurationFromDate"></div>
                                <div style="margin-top:10px"><label>To Date</label><input type="date" id="gaDurationToDate"></div>
                                <button type="button" class="apply-wide" onclick="document.getElementById('gaApplyBtn').click()">Apply</button>
                            </div>
                        </div>
                    </div>
                    <div class="dash-card" style="border:1px solid #93c5fd">
                        <div class="chart-sub" style="font-weight:900;color:#1d4ed8">If Single Participant Selected</div>
                        <div class="chart-title" id="gaTrendHint">Time Over Time</div>
                        <p style="margin:0;color:#64748b;font-size:13px">When only one member is selected, Graph 2 automatically changes to date vs duration.</p>
                    </div>
                </div>
            </main>
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

        <div class="grid-2 analytics-anchor-section" id="analyticsTrends" style="margin-top:16px">
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
                        <h3 id="analyticsMembers" class="analytics-anchor-section" style="margin:0">Top Members</h3>
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
                        <h3 id="analyticsRisk" class="analytics-anchor-section" style="margin:0">Risk Members</h3>
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
                        <h3 id="analyticsReports" class="analytics-anchor-section" style="margin:0">Operational Alerts</h3>
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
            const tabShell = document.getElementById('analyticsTabsV3');
            if (tabShell) {
                const tabLinks = Array.from(tabShell.querySelectorAll('a[href^="#"]'));
                const sections = tabLinks.map(a => document.querySelector(a.getAttribute('href'))).filter(Boolean);
                tabLinks.forEach(link => link.addEventListener('click', () => {
                    tabLinks.forEach(a => a.classList.remove('active'));
                    link.classList.add('active');
                }));
                if ('IntersectionObserver' in window && sections.length) {
                    const observer = new IntersectionObserver(entries => {
                        const visible = entries.filter(e => e.isIntersecting).sort((a,b)=>b.intersectionRatio-a.intersectionRatio)[0];
                        if (!visible) return;
                        const active = tabLinks.find(a => a.getAttribute('href') === '#' + visible.target.id);
                        if (active) { tabLinks.forEach(a => a.classList.remove('active')); active.classList.add('active'); }
                    }, {rootMargin:'-35% 0px -55% 0px', threshold:[.1,.25,.5]});
                    sections.forEach(section => observer.observe(section));
                }
            }
        })();

        (() => {
            const graphSection = document.getElementById('graphAnalyticsSection');
            const gaXAxis = document.getElementById('gaXAxis');
            const gaYAxis = document.getElementById('gaYAxis');
            const gaFromDate = document.getElementById('gaFromDate');
            const gaToDate = document.getElementById('gaToDate');
            const gaDurationFromDate = document.getElementById('gaDurationFromDate');
            const gaDurationToDate = document.getElementById('gaDurationToDate');
            const gaMonths = document.querySelector('[data-target="gaMonths"]');
            const gaYears = document.querySelector('[data-target="gaYears"]');
            const gaMembers = document.querySelector('[data-target="gaMembers"]');
            const gaApplyBtn = document.getElementById('gaApplyBtn');
            const gaTrendHint = document.getElementById('gaTrendHint');
            const gaDurationHint = document.getElementById('gaDurationHint');
            let gaTrendChart = null;
            let gaDurationChart = null;
            let gaLoaded = false;

            const valueLabelPlugin = {
                id: 'valueLabelPlugin',
                afterDatasetsDraw(chart) {
                    if (chart.config.type !== 'bar') return;
                    const {ctx} = chart;
                    ctx.save();
                    ctx.font = '700 11px Inter, Arial';
                    ctx.textAlign = 'center';
                    ctx.textBaseline = 'bottom';
                    ctx.fillStyle = getComputedStyle(document.body).color || '#e5e7eb';
                    chart.data.datasets.forEach((dataset, datasetIndex) => {
                        const meta = chart.getDatasetMeta(datasetIndex);
                        meta.data.forEach((bar, index) => {
                            const value = dataset.data[index];
                            if (value === null || value === undefined) return;
                            ctx.fillText(value, bar.x, bar.y - 6);
                        });
                    });
                    ctx.restore();
                }
            };

            function selectedValues(boxEl) {
                if (!boxEl) return [];
                const checked = Array.from(boxEl.querySelectorAll('input[type="checkbox"]:checked')).map(input => input.value);
                if (!checked.length || checked.includes('__all__')) return [];
                return checked;
            }

            function setupCheckboxSelect(boxEl) {
                if (!boxEl) return;
                const btn = boxEl.querySelector('.checkbox-select-btn');
                const inputs = Array.from(boxEl.querySelectorAll('input[type="checkbox"]'));
                const allInput = inputs.find(input => input.value === '__all__');
                const refreshLabel = () => {
                    const selected = inputs.filter(input => input.checked && input.value !== '__all__');
                    if (!selected.length || (allInput && allInput.checked)) {
                        btn.textContent = allInput ? allInput.parentElement.textContent.trim() : 'All';
                    } else if (selected.length === 1) {
                        btn.textContent = selected[0].parentElement.textContent.trim();
                    } else {
                        btn.textContent = `${selected.length} selected`;
                    }
                };
                btn?.addEventListener('click', (event) => {
                    event.stopPropagation();
                    document.querySelectorAll('.checkbox-select.open').forEach(el => { if (el !== boxEl) el.classList.remove('open'); });
                    boxEl.classList.toggle('open');
                });
                inputs.forEach(input => input.addEventListener('change', () => {
                    if (input.value === '__all__' && input.checked) {
                        inputs.forEach(other => { if (other !== input) other.checked = false; });
                    } else if (input.value !== '__all__' && input.checked && allInput) {
                        allInput.checked = false;
                    }
                    if (!inputs.some(item => item.checked) && allInput) allInput.checked = true;
                    refreshLabel();
                    if (gaLoaded) loadGraphAnalytics();
                }));
                refreshLabel();
            }
            document.addEventListener('click', () => document.querySelectorAll('.checkbox-select.open').forEach(el => el.classList.remove('open')));
            document.querySelectorAll('.checkbox-select').forEach(box => {
                box.addEventListener('click', (event) => event.stopPropagation());
                const menu = box.querySelector('.checkbox-select-menu');
                if (menu) menu.addEventListener('click', (event) => event.stopPropagation());
                setupCheckboxSelect(box);
            });

            function updateGraphFilterVisibility() {
                if (!gaXAxis) return;
                const mode = gaXAxis.value;
                document.querySelectorAll('.ga-date-filter').forEach(el => el.style.display = mode === 'date' ? '' : 'none');
                document.querySelectorAll('.ga-month-filter').forEach(el => el.style.display = mode === 'month' ? '' : 'none');
                document.querySelectorAll('.ga-year-filter').forEach(el => el.style.display = mode === 'year' ? '' : 'none');
            }

            function buildGraphQuery() {
                const params = new URLSearchParams();
                params.set('x_axis', gaXAxis?.value || 'date');
                params.set('y_axis', gaYAxis?.value || 'count');
                if ((gaXAxis?.value || 'date') === 'date') {
                    const fromVal = gaDurationFromDate?.value || gaFromDate?.value;
                    const toVal = gaDurationToDate?.value || gaToDate?.value;
                    if (fromVal) params.set('from_date', fromVal);
                    if (toVal) params.set('to_date', toVal);
                }
                selectedValues(gaMonths).forEach(v => params.append('months', v));
                selectedValues(gaYears).forEach(v => params.append('years', v));
                selectedValues(gaMembers).forEach(v => params.append('member_ids', v));
                return params.toString();
            }

            async function loadGraphAnalytics() {
                if (!graphSection) return;
                graphSection.classList.add('loading');
                try {
                    const response = await fetch(`{{ url_for('analytics_graph_data') }}?${buildGraphQuery()}`, {
                        headers: {'X-Requested-With': 'XMLHttpRequest'}
                    });
                    if (!response.ok) throw new Error('Graph request failed');
                    const payload = await response.json();
                    renderTrendGraph(payload.trend);
                    renderDurationGraph(payload.duration);
                    gaLoaded = true;
                } catch (err) {
                    console.error(err);
                    if (gaTrendHint) gaTrendHint.textContent = 'Unable to load graph analytics. Please check server logs.';
                } finally {
                    graphSection.classList.remove('loading');
                }
            }

            function renderTrendGraph(trend) {
                const canvas = document.getElementById('gaTrendChart');
                if (!canvas || !window.Chart) return;
                if (gaTrendChart) gaTrendChart.destroy();
                const suffix = trend.y_axis === 'percentage' ? '%' : '';
                if (gaTrendHint) gaTrendHint.textContent = `X-axis: ${trend.x_axis}. Y-axis: ${trend.y_axis}.`;
                gaTrendChart = new Chart(canvas, {
                    type: 'line',
                    data: {
                        labels: trend.labels,
                        datasets: [
                            {label: 'Present', data: trend.present, borderColor: '#22c55e', backgroundColor: 'rgba(34,197,94,.10)', fill: false},
                            {label: 'Late', data: trend.late, borderColor: '#f59e0b', backgroundColor: 'rgba(245,158,11,.10)', fill: false},
                            {label: 'Absent', data: trend.absent, borderColor: '#ef4444', backgroundColor: 'rgba(239,68,68,.10)', fill: false},
                            {label: 'Unknown', data: trend.unknown, borderColor: '#38bdf8', backgroundColor: 'rgba(56,189,248,.10)', fill: false}
                        ]
                    },
                    options: {
                        responsive: true,
                        interaction: {mode: 'index', intersect: false},
                        plugins: {
                            legend: {display: true},
                            tooltip: {callbacks: {label: ctx => `${ctx.dataset.label}: ${ctx.parsed.y}${suffix}`}}
                        },
                        scales: {y: {beginAtZero: true, ticks: {callback: value => `${value}${suffix}`}}}
                    }
                });
            }

            function renderDurationGraph(duration) {
                const canvas = document.getElementById('gaDurationChart');
                if (!canvas || !window.Chart) return;
                if (gaDurationChart) gaDurationChart.destroy();
                const single = duration.mode === 'single_member_date_duration';
                if (gaDurationHint) {
                    gaDurationHint.textContent = single
                        ? `${duration.selected_member_name || 'Selected member'}: date vs duration in minutes.`
                        : 'Selected members: total duration in minutes.';
                }
                gaDurationChart = new Chart(canvas, {
                    type: 'bar',
                    plugins: [valueLabelPlugin],
                    data: {
                        labels: duration.labels,
                        datasets: [{
                            label: 'Minutes',
                            data: duration.values,
                            borderRadius: 10,
                            backgroundColor: duration.labels.map((_, i) => `hsla(${(i * 47) % 360}, 72%, 55%, .78)`)
                        }]
                    },
                    options: {
                        responsive: true,
                        plugins: {legend: {display: false}},
                        scales: {
                            x: {grid: {display: false}},
                            y: {beginAtZero: true, title: {display: true, text: 'Minutes'}}
                        }
                    }
                });
            }

            updateGraphFilterVisibility();
            [gaXAxis, gaYAxis].forEach(el => el && el.addEventListener('change', () => {
                updateGraphFilterVisibility();
                if (gaLoaded) loadGraphAnalytics();
            }));
            [gaFromDate, gaToDate, gaDurationFromDate, gaDurationToDate].forEach(el => el && el.addEventListener('change', () => {
                if (gaLoaded) loadGraphAnalytics();
            }));
            gaApplyBtn?.addEventListener('click', loadGraphAnalytics);

            if (graphSection && 'IntersectionObserver' in window) {
                const observer = new IntersectionObserver(entries => {
                    if (entries.some(entry => entry.isIntersecting) && !gaLoaded) {
                        loadGraphAnalytics();
                        observer.disconnect();
                    }
                }, {rootMargin: '200px'});
                observer.observe(graphSection);
            } else {
                loadGraphAnalytics();
            }
        })();

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
        graph_options=graph_options,
    )
    return page("Analytics", body, "analytics")




def _month_days(year, month):
    if month == 12:
        next_month = date(year + 1, 1, 1)
    else:
        next_month = date(year, month + 1, 1)
    return (next_month - date(year, month, 1)).days


def _attendance_register_payload_uncached(year=None, month=None, search="", page=1, per_page=25, all_rows=False):
    today = today_local()
    try:
        year = int(year or today.year)
    except Exception:
        year = today.year
    try:
        month = int(month or today.month)
    except Exception:
        month = today.month
    if month < 1 or month > 12:
        month = today.month

    days_count = _month_days(year, month)
    start_day = date(year, month, 1)
    end_day = date(year, month, days_count)
    search_text = (search or "").strip().lower()
    try:
        page = max(int(page or 1), 1)
    except Exception:
        page = 1
    try:
        per_page = max(5, min(int(per_page or 25), 100))
    except Exception:
        per_page = 25
    offset = (page - 1) * per_page

    with db() as conn:
        with conn.cursor() as cur:
            name_expr = member_name_sql(conn)
            member_params = []
            member_where = [ACTIVE_MEMBER_SQL]
            if search_text:
                member_where.append(f"lower(COALESCE({name_expr}, '')) LIKE %s")
                member_params.append(f"%{search_text}%")

            cur.execute(
                f"""
                SELECT COUNT(*) AS total_count
                FROM members
                WHERE {' AND '.join(member_where)}
                """,
                member_params,
            )
            total_members_count = int((cur.fetchone() or {}).get("total_count") or 0)

            if all_rows:
                cur.execute(
                    f"""
                    SELECT id, {name_expr} AS display_name, email
                    FROM members
                    WHERE {' AND '.join(member_where)}
                    ORDER BY COALESCE({name_expr}, '')
                    """,
                    member_params,
                )
            else:
                cur.execute(
                    f"""
                    SELECT id, {name_expr} AS display_name, email
                    FROM members
                    WHERE {' AND '.join(member_where)}
                    ORDER BY COALESCE({name_expr}, '')
                    LIMIT %s OFFSET %s
                    """,
                    member_params + [per_page, offset],
                )
            members = cur.fetchall()

            cur.execute(
                """
                SELECT DISTINCT CAST(start_time AS TEXT)::date AS meeting_date
                FROM meetings
                WHERE start_time IS NOT NULL
                  AND CAST(start_time AS TEXT)::date BETWEEN %s AND %s
                """,
                (start_day, end_day),
            )
            meeting_dates = {r.get("meeting_date") for r in cur.fetchall() if r.get("meeting_date")}

            cur.execute(
                """
                SELECT a.member_id, a.final_status, a.is_member, CAST(m.start_time AS TEXT)::date AS meeting_date
                FROM attendance a
                JOIN meetings m ON m.meeting_uuid = a.meeting_uuid
                WHERE m.start_time IS NOT NULL
                  AND CAST(m.start_time AS TEXT)::date BETWEEN %s AND %s
                  AND a.member_id IS NOT NULL
                """,
                (start_day, end_day),
            )
            attendance_rows = cur.fetchall()

            cur.execute(
                """
                SELECT DISTINCT to_char(CAST(start_time AS TEXT)::timestamp, 'YYYY') AS year_value
                FROM meetings
                WHERE start_time IS NOT NULL
                ORDER BY year_value DESC
                LIMIT 12
                """
            )
            years = [r.get("year_value") for r in cur.fetchall() if r.get("year_value")]

    priority = {"P": 4, "L": 3, "A": 2, "U": 1, "": 0}
    status_by_member_day = {}
    for row in attendance_rows:
        mid = row.get("member_id")
        day_value = row.get("meeting_date")
        if not mid or not day_value:
            continue
        status = str(row.get("final_status") or "").upper()
        if status in ("PRESENT", "HOST"):
            mark = "P"
        elif status == "LATE":
            mark = "L"
        elif status == "ABSENT":
            mark = "A"
        else:
            mark = "U"
        key = (int(mid), day_value.day)
        if priority[mark] > priority.get(status_by_member_day.get(key, ""), 0):
            status_by_member_day[key] = mark

    days = list(range(1, days_count + 1))
    rows = []
    for member in members:
        mid = int(member.get("id"))
        cells = []
        totals = {"P": 0, "L": 0, "A": 0, "U": 0}
        for day in days:
            current = date(year, month, day)
            mark = status_by_member_day.get((mid, day), "")
            if not mark and current in meeting_dates:
                mark = "A"
            if mark in totals:
                totals[mark] += 1
            cells.append(mark)
        counted = totals["P"] + totals["L"] + totals["A"] + totals["U"]
        attendance_pct = round(((totals["P"] + totals["L"] * 0.5) / counted) * 100, 2) if counted else 0
        total_meetings = counted
        rows.append({
            "id": mid,
            "name": member.get("display_name") or f"Member {mid}",
            "email": member.get("email") or "",
            "cells": cells,
            "totals": totals,
            "total_meetings": total_meetings,
            "attendance_pct": attendance_pct,
        })

    month_names = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"]
    if str(today.year) not in years:
        years.insert(0, str(today.year))
    if str(year) not in years:
        years.insert(0, str(year))

    return {
        "year": year,
        "month": month,
        "month_name": month_names[month - 1],
        "years": years,
        "days": days,
        "rows": rows,
        "meeting_days": sorted([d.day for d in meeting_dates]),
        "summary": {"members": len(rows), "meeting_days": len(meeting_dates), "total_members": total_members_count},
        "pagination": {
            "page": page,
            "per_page": per_page,
            "total": total_members_count,
            "pages": max(1, (total_members_count + per_page - 1) // per_page),
            "has_prev": page > 1,
            "has_next": page * per_page < total_members_count,
        },
    }


def analytics_data(filters):
    key = _cache_make_key("analytics", filters or {})
    cached = _cache_get(key)
    if cached is not None:
        return cached
    return _cache_set(key, _analytics_data_uncached(filters))


def graph_analytics_payload():
    key = _cache_make_key("graph_analytics", {
        "x_axis": request.args.get("x_axis", "date"),
        "y_axis": request.args.get("y_axis", "count"),
        "from_date": request.args.get("from_date", ""),
        "to_date": request.args.get("to_date", ""),
        "months": request.args.getlist("months"),
        "years": request.args.getlist("years"),
        "member_ids": request.args.getlist("member_ids"),
    })
    cached = _cache_get(key)
    if cached is not None:
        return cached
    return _cache_set(key, _graph_analytics_payload_uncached())


def attendance_register_payload(year=None, month=None, search="", page=1, per_page=25, all_rows=False):
    key = _cache_make_key("attendance_register", {
        "year": year, "month": month, "search": search,
        "page": page, "per_page": per_page, "all_rows": all_rows,
    })
    cached = _cache_get(key)
    if cached is not None:
        return cached
    return _cache_set(key, _attendance_register_payload_uncached(year, month, search, page, per_page, all_rows))


@app.route("/attendance-register")
@login_required
def attendance_register():
    today = today_local()
    data = attendance_register_payload(
        request.args.get("year", today.year),
        request.args.get("month", today.month),
        request.args.get("search", ""),
        request.args.get("page", 1),
        request.args.get("per_page", 25),
    )
    body = render_template_string(
        """
        <style>
        .reg-dashboard-shell{display:grid;grid-template-columns:180px minmax(0,1fr) 210px;gap:14px;align-items:start;margin-top:8px}
        .reg-side-note{background:#eefdf0;border:1px solid #8bd49a;border-radius:14px;padding:14px;font-size:12px;line-height:1.55;color:#12351d;position:sticky;top:92px}
        .reg-side-note b{display:block;margin-bottom:7px;color:#14532d}.reg-feature-box{background:#f5f0ff;border:1px solid #bca7f5;border-radius:14px;padding:16px;color:#3b2a73;line-height:1.7;position:sticky;top:92px}.reg-feature-box h3{margin:0 0 8px;font-size:16px}.reg-feature-box ul{margin:0;padding-left:18px;font-size:13px}
        .register-book{background:linear-gradient(135deg,#7c4a22,#4b2d16);padding:12px;border-radius:20px;box-shadow:0 18px 40px rgba(77,45,22,.35), inset 0 0 0 3px rgba(255,255,255,.12)}
        .register-paper{background:#fffdf4;color:#1f2937;border-radius:13px;padding:14px;box-shadow:inset 0 0 0 1px #d7c9a5}
        .register-heading{display:flex;justify-content:center;margin:-28px 0 10px}.register-heading span{background:#14532d;color:#fff;border-radius:8px;padding:8px 36px;font-weight:950;font-size:22px;box-shadow:0 7px 20px rgba(20,83,45,.28)}
        .reg-topbar{display:flex;justify-content:space-between;align-items:center;gap:10px;margin-bottom:8px;flex-wrap:wrap}.reg-month-nav{display:flex;align-items:center;gap:8px}.reg-month-pill{background:#f8fafc;border:1px solid #cbd5e1;border-radius:7px;padding:6px 14px;font-weight:900}.reg-controls{display:flex;gap:8px;align-items:end;flex-wrap:wrap}.reg-controls input,.reg-controls select{height:34px;border-radius:8px;border:1px solid #cbd5e1;padding:6px 10px}.reg-controls label{font-size:11px;font-weight:900;color:#475569;display:block;margin-bottom:2px}.reg-controls .btn,.reg-controls button{height:34px;padding:7px 10px;border-radius:8px;font-size:12px}
        .register-table-wrap{max-height:72vh;overflow:auto;border-radius:10px;border:1px solid #cfc2a4;background:#fffdf4}.register-table{border-collapse:separate;border-spacing:0;width:max-content;min-width:100%;font-size:13px}.register-table th,.register-table td{min-width:38px;text-align:center;padding:8px;border-bottom:1px solid #d8cdb5;border-right:1px solid #d8cdb5}.register-table th{position:sticky;top:0;z-index:4;background:#f3ebd8;color:#111827}.register-table .sticky-member{position:sticky;left:0;z-index:5;min-width:180px;text-align:left;background:#f3ebd8}.register-table td.sticky-member{z-index:3;background:#fff8df;font-weight:900;cursor:pointer}.register-table td.sticky-member:hover{outline:2px solid #22c55e;border-radius:8px}.reg-cell{font-weight:950;border-radius:6px}.reg-p{color:#15803d}.reg-l{color:#ea580c}.reg-a{color:#dc2626}.reg-u{color:#64748b}.reg-empty{color:#cbd5e1}.modal-backdrop{display:none;position:fixed;inset:0;background:rgba(2,6,23,.68);z-index:999;align-items:center;justify-content:center;padding:18px}.modal-backdrop.show{display:flex}.modal-card{max-width:460px;width:100%;background:#0f172a;color:#e5e7eb;border:1px solid rgba(148,163,184,.3);border-radius:22px;padding:22px;box-shadow:0 30px 80px rgba(0,0,0,.45)}
        @media print{.sidebar,.topbar,.reg-side-note,.reg-feature-box,.reg-controls,.reg-month-nav{display:none!important}.main{margin:0!important}.register-book{box-shadow:none;background:#fff;padding:0}.register-heading span{color:#000;background:#fff;border:1px solid #000}.register-table-wrap{max-height:none;overflow:visible}.register-table th{position:static}.register-table .sticky-member{position:static}}
        @media(max-width:1180px){.reg-dashboard-shell{grid-template-columns:1fr}.reg-side-note,.reg-feature-box{position:static}.register-heading span{font-size:17px;padding:8px 14px}}
        </style>


        <style>
        /* Premium readable register theme: keeps book structure, fixes color clarity and spacing */
        .reg-dashboard-shell{grid-template-columns:minmax(0,1fr)!important;}
        .reg-side-note,.reg-feature-box{display:none!important;}
        .register-book{background:linear-gradient(135deg,#3a2418,#6b4428 45%,#2b1b12)!important;border:1px solid rgba(255,232,180,.18)!important;box-shadow:0 24px 70px rgba(0,0,0,.42), inset 0 0 0 3px rgba(255,255,255,.08)!important;}
        .register-paper{background:linear-gradient(180deg,#fffaf0,#fff7df)!important;border-color:#d6bd8b!important;color:#172033!important;}
        /* DARK_REGISTER_THEME_V3: darker register with colorful P/L/A/U cells */
        .reg-dashboard-shell{background:radial-gradient(circle at top,#13213b 0%,#07111f 46%,#030712 100%)!important;color:#e5e7eb!important;}
        .register-book{background:linear-gradient(135deg,rgba(15,23,42,.96),rgba(2,6,23,.98))!important;border:1px solid rgba(59,130,246,.32)!important;box-shadow:0 28px 70px rgba(0,0,0,.55)!important;}
        .register-paper{background:rgba(8,13,27,.96)!important;border:1px solid rgba(148,163,184,.20)!important;box-shadow:inset 0 0 0 1px rgba(255,255,255,.03)!important;}
        .register-heading span{background:linear-gradient(90deg,#0f766e,#2563eb,#7c3aed)!important;color:white!important;letter-spacing:.3px;box-shadow:0 14px 34px rgba(37,99,235,.35)!important;}
        .register-table-wrap{background:#07111f!important;border-color:rgba(59,130,246,.35)!important;box-shadow:0 18px 45px rgba(0,0,0,.42)!important;}
        .register-table{border-spacing:4px!important;background:#07111f!important;}
        .register-table th{background:linear-gradient(180deg,#10223f,#0b162b)!important;color:#eaf2ff!important;border:1px solid rgba(96,165,250,.34)!important;font-weight:950;}
        .register-table th.reg-total-head{background:linear-gradient(180deg,#1e3a8a,#172554)!important;color:#dbeafe!important;}
        .register-table td{background:#111827!important;color:#e5e7eb!important;border:1px solid rgba(148,163,184,.20)!important;}
        .register-table .sticky-member{background:#0b1220!important;color:#f8fafc!important;box-shadow:4px 0 16px rgba(0,0,0,.35)!important;}
        .register-table td.sticky-member{background:#0f172a!important;color:#f8fafc!important;}
        .register-table td.reg-total-cell{background:#1e293b!important;color:#bfdbfe!important;font-weight:950!important;}
        .register-table td.reg-p{background:linear-gradient(135deg,#064e3b,#16a34a)!important;color:#ecfdf5!important;font-weight:1000!important;text-shadow:0 1px 2px rgba(0,0,0,.35);}
        .register-table td.reg-l{background:linear-gradient(135deg,#78350f,#f59e0b)!important;color:#fff7ed!important;font-weight:1000!important;text-shadow:0 1px 2px rgba(0,0,0,.30);}
        .register-table td.reg-a{background:linear-gradient(135deg,#7f1d1d,#ef4444)!important;color:#fff1f2!important;font-weight:1000!important;text-shadow:0 1px 2px rgba(0,0,0,.30);}
        .register-table td.reg-u{background:linear-gradient(135deg,#334155,#94a3b8)!important;color:#f8fafc!important;font-weight:1000!important;text-shadow:0 1px 2px rgba(0,0,0,.35);}
        .register-table td.reg-empty{background:#101827!important;color:#334155!important;}
        .register-table td.reg-p,.register-table td.reg-l,.register-table td.reg-a,.register-table td.reg-u{border-radius:8px!important;box-shadow:0 4px 12px rgba(0,0,0,.20),inset 0 0 0 1px rgba(255,255,255,.12)!important;}
        .reg-month-pill{background:#0f172a!important;color:#dbeafe!important;border-color:rgba(96,165,250,.45)!important;}
        .reg-controls input,.reg-controls select{background:#0b1220!important;color:#e5e7eb!important;border-color:rgba(96,165,250,.35)!important;}
        .reg-controls label{color:#bfdbfe!important;}
        .reg-side-note,.reg-feature-box{background:rgba(15,23,42,.86)!important;color:#dbeafe!important;border-color:rgba(96,165,250,.25)!important;box-shadow:0 18px 40px rgba(0,0,0,.38)!important;}
        .register-book.reg-light{background:linear-gradient(135deg,#7c4a22,#4b2d16)!important;border-color:rgba(255,232,180,.25)!important;}
        .register-book.reg-light .register-paper{background:linear-gradient(180deg,#fffaf0,#fff7df)!important;color:#172033!important;border-color:#d6bd8b!important;}
        .register-book.reg-light .register-table-wrap{background:#fffdf4!important;border-color:#cfc2a4!important;box-shadow:none!important;}
        .register-book.reg-light .register-table{background:#fffdf4!important;border-spacing:2px!important;}
        .register-book.reg-light .register-table th{background:#064e3b!important;color:#fff!important;border-color:#d8cdb5!important;}
        .register-book.reg-light .register-table td{background:#fffaf0!important;color:#1f2937!important;border-color:#d8cdb5!important;}
        .register-book.reg-light .register-table .sticky-member,.register-book.reg-light .register-table td.sticky-member{background:#fff0c7!important;color:#111827!important;}
        .register-book.reg-light .register-table td.reg-total-cell{background:#e0f2fe!important;color:#0f172a!important;}
        .register-book.reg-light .register-table td.reg-p{background:#bbf7d0!important;color:#15803d!important;text-shadow:none!important;}
        .register-book.reg-light .register-table td.reg-l{background:#fed7aa!important;color:#c2410c!important;text-shadow:none!important;}
        .register-book.reg-light .register-table td.reg-a{background:#fecaca!important;color:#b91c1c!important;text-shadow:none!important;}
        .register-book.reg-light .register-table td.reg-u{background:#e5e7eb!important;color:#475569!important;text-shadow:none!important;}
        .register-book.reg-light .register-table td.reg-empty{background:#fffaf0!important;color:#d6bd8b!important;}
        .register-book.reg-light .reg-month-pill{background:#f8fafc!important;color:#0f172a!important;border-color:#cbd5e1!important;}
        .register-book.reg-light .reg-controls input,.register-book.reg-light .reg-controls select{background:#fff!important;color:#111827!important;border-color:#cbd5e1!important;}
        .register-book.reg-light .reg-controls label{color:#475569!important;}
        .reg-pagination{display:flex;gap:8px;align-items:center;justify-content:flex-end;margin-top:10px;flex-wrap:wrap;color:#cbd5e1;font-weight:800}
        .reg-pagination a,.reg-pagination span{padding:7px 10px;border-radius:8px;background:#0f172a;border:1px solid rgba(96,165,250,.35);color:#dbeafe;text-decoration:none}
        .reg-pagination .disabled{opacity:.45}
        </style>
        <div class="reg-dashboard-shell">
            <aside class="reg-side-note">
                <b>MONTHLY REGISTER VIEW</b>
                Each page represents a month.<br><br>
                <b>Cells</b>
                <span style="color:#15803d;font-weight:900">P</span> Present - Green<br>
                <span style="color:#ea580c;font-weight:900">L</span> Late - Orange<br>
                <span style="color:#dc2626;font-weight:900">A</span> Absent - Red<br>
                <span style="color:#64748b;font-weight:900">U</span> Unknown - Gray<br><br>
                Click on participant name to view summary.
            </aside>

            <main class="register-book">
                <div class="register-heading"><span>2. ATTENDANCE REGISTER (MONTHLY VIEW)</span></div>
                <div style="display:flex;justify-content:flex-end;margin:-6px 0 8px"><button type="button" id="registerThemeToggle" class="btn secondary small">🌙 Dark Register</button></div>
                <div class="register-paper">
                    <form method="get" class="reg-topbar">
                        <div class="reg-month-nav">
                            {% set prev_month = 12 if data.month == 1 else data.month - 1 %}
                            {% set prev_year = data.year - 1 if data.month == 1 else data.year %}
                            {% set next_month = 1 if data.month == 12 else data.month + 1 %}
                            {% set next_year = data.year + 1 if data.month == 12 else data.year %}
                            <a class="btn secondary small" href="{{ url_for('attendance_register', month=prev_month, year=prev_year, search=request.args.get('search','')) }}">‹</a>
                            <span class="reg-month-pill">{{ data.month_name }} {{ data.year }}</span>
                            <a class="btn secondary small" href="{{ url_for('attendance_register', month=next_month, year=next_year, search=request.args.get('search','')) }}">›</a>
                        </div>
                        <div class="reg-controls">
                            <div><label>Month</label><select name="month" id="regMonth">{% for i in range(1, 13) %}<option value="{{ i }}" {% if i == data.month %}selected{% endif %}>{{ month_names[i-1] }}</option>{% endfor %}</select></div>
                            <div><label>Year</label><select name="year" id="regYear">{% for y in data.years %}<option value="{{ y }}" {% if y|string == data.year|string %}selected{% endif %}>{{ y }}</option>{% endfor %}</select></div>
                            <div><label>Search member</label><input type="text" name="search" id="regSearch" value="{{ request.args.get('search','') }}" placeholder="member name"></div>
                            <button type="submit">Apply</button>
                            <button type="button" onclick="window.print()">Print</button>
                            <a class="btn secondary" href="{{ url_for('attendance_register_export_pdf', month=data.month, year=data.year, search=request.args.get('search','')) }}">PDF</a>
                            <a class="btn success" href="{{ url_for('attendance_register_export_excel', month=data.month, year=data.year, search=request.args.get('search','')) }}">Excel</a>
                        </div>
                    </form>

                    <div class="register-table-wrap">
                        <table class="register-table" id="attendanceRegisterTable">
                            <thead>
                                <tr>
                                    <th class="sticky-member">Name</th>
                                    <th class="reg-total-head">Total</th>
                                    {% for d in data.days %}<th>{{ d }}</th>{% endfor %}
                                    <th>P</th><th>L</th><th>A</th><th>U</th><th>%</th>
                                </tr>
                            </thead>
                            <tbody>
                                {% for row in data.rows %}
                                <tr>
                                    <td class="sticky-member reg-member" data-name="{{ row.name }}" data-present="{{ row.totals.P }}" data-late="{{ row.totals.L }}" data-absent="{{ row.totals.A }}" data-unknown="{{ row.totals.U }}" data-total="{{ row.total_meetings }}" data-percent="{{ row.attendance_pct }}">{{ row.name }}</td>
                                    <td class="reg-total-cell">{{ row.total_meetings }}</td>
                                    {% for cell in row.cells %}<td class="reg-cell {% if cell == 'P' %}reg-p{% elif cell == 'L' %}reg-l{% elif cell == 'A' %}reg-a{% elif cell == 'U' %}reg-u{% else %}reg-empty{% endif %}">{{ cell or '' }}</td>{% endfor %}
                                    <td>{{ row.totals.P }}</td><td>{{ row.totals.L }}</td><td>{{ row.totals.A }}</td><td>{{ row.totals.U }}</td><td>{{ row.attendance_pct }}%</td>
                                </tr>
                                {% endfor %}
                            </tbody>
                        </table>
                    </div>
                    <div class="reg-pagination">
                        {% set pg = data.pagination %}
                        {% if pg.has_prev %}
                            <a href="{{ url_for('attendance_register', month=data.month, year=data.year, search=request.args.get('search',''), page=pg.page-1, per_page=pg.per_page) }}">‹ Previous</a>
                        {% else %}
                            <span class="disabled">‹ Previous</span>
                        {% endif %}
                        <span>Page {{ pg.page }} / {{ pg.pages }} · {{ pg.total }} members</span>
                        {% if pg.has_next %}
                            <a href="{{ url_for('attendance_register', month=data.month, year=data.year, search=request.args.get('search',''), page=pg.page+1, per_page=pg.per_page) }}">Next ›</a>
                        {% else %}
                            <span class="disabled">Next ›</span>
                        {% endif %}
                    </div>
                </div>
            </main>

            <aside class="reg-feature-box">
                <h3>FEATURES</h3>
                <ul>
                    <li>Book-style monthly pages</li>
                    <li>Auto adjust days 28/29/30/31</li>
                    <li>Color coded attendance</li>
                    <li>Click name → View summary</li>
                    <li>Easy month navigation</li>
                    <li>PDF, Excel and Print</li>
                </ul>
            </aside>
        </div>

        <div class="modal-backdrop" id="regModal">
            <div class="modal-card">
                <div class="section-title"><h3 id="regModalName" style="margin:0">Member</h3><button type="button" id="regModalClose">Close</button></div>
                <div class="grid-2">
                    <div class="mini-kpi"><div class="label">Total Meetings</div><div class="value" id="regModalTotal">0</div></div>
                    <div class="mini-kpi"><div class="label">Total Present</div><div class="value" id="regModalP">0</div></div>
                    <div class="mini-kpi"><div class="label">Total Late</div><div class="value" id="regModalL">0</div></div>
                    <div class="mini-kpi"><div class="label">Total Absent</div><div class="value" id="regModalA">0</div></div>
                    <div class="mini-kpi"><div class="label">Attendance %</div><div class="value" id="regModalPct">0%</div></div>
                </div>
            </div>
        </div>

        <script>
        (() => {
            const modal = document.getElementById('regModal');
            const closeBtn = document.getElementById('regModalClose');
            document.querySelectorAll('.reg-member').forEach(cell => {
                cell.addEventListener('click', () => {
                    document.getElementById('regModalName').textContent = cell.dataset.name || 'Member';
                    document.getElementById('regModalTotal').textContent = cell.dataset.total || '0';
                    document.getElementById('regModalP').textContent = cell.dataset.present || '0';
                    document.getElementById('regModalL').textContent = cell.dataset.late || '0';
                    document.getElementById('regModalA').textContent = cell.dataset.absent || '0';
                    document.getElementById('regModalPct').textContent = (cell.dataset.percent || '0') + '%';
                    modal.classList.add('show');
                });
            });
            closeBtn?.addEventListener('click', () => modal.classList.remove('show'));
            modal?.addEventListener('click', e => { if (e.target === modal) modal.classList.remove('show'); });
            const book = document.querySelector('.register-book');
            const themeBtn = document.getElementById('registerThemeToggle');
            function applyRegisterTheme(mode){
                if(!book || !themeBtn) return;
                const light = mode === 'light';
                book.classList.toggle('reg-light', light);
                themeBtn.textContent = light ? '☀️ Light Register' : '🌙 Dark Register';
                localStorage.setItem('registerThemeMode', light ? 'light' : 'dark');
            }
            applyRegisterTheme(localStorage.getItem('registerThemeMode') || 'dark');
            themeBtn?.addEventListener('click', () => applyRegisterTheme(book.classList.contains('reg-light') ? 'dark' : 'light'));
        })();
        </script>
        """,
        data=data,
        month_names=["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"],
        request=request,
    )
    return page("Attendance Register", body, "attendance_register")


@app.route("/attendance-register/data")
@login_required
def attendance_register_data():
    return jsonify(attendance_register_payload(
        request.args.get("year"),
        request.args.get("month"),
        request.args.get("search", ""),
        request.args.get("page", 1),
        request.args.get("per_page", 25),
    ))


@app.route("/attendance-register/export/excel")
@login_required
def attendance_register_export_excel():
    data = attendance_register_payload(request.args.get("year"), request.args.get("month"), request.args.get("search", ""), all_rows=True)
    output = io.StringIO()
    output.write("<html><head><meta charset='utf-8'></head><body><table border='1'>")
    output.write(f"<tr><th colspan='{len(data['days']) + 7}'>Attendance Register - {data['month_name']} {data['year']}</th></tr>")
    output.write("<tr><th>Member</th><th>Total</th>" + "".join(f"<th>{d}</th>" for d in data["days"]) + "<th>P</th><th>L</th><th>A</th><th>U</th><th>%</th></tr>")
    for row in data["rows"]:
        output.write(f"<tr><td>{row['name']}</td><td>{row['total_meetings']}</td>" + "".join(f"<td>{c or '-'}</td>" for c in row["cells"]) + f"<td>{row['totals']['P']}</td><td>{row['totals']['L']}</td><td>{row['totals']['A']}</td><td>{row['totals']['U']}</td><td>{row['attendance_pct']}%</td></tr>")
    output.write("</table></body></html>")
    filename = f"attendance_register_{data['year']}_{data['month']:02d}.xls"
    return Response(output.getvalue(), mimetype="application/vnd.ms-excel", headers={"Content-Disposition": f"attachment; filename={filename}"})


@app.route("/attendance-register/export/pdf")
@login_required
def attendance_register_export_pdf():
    data = attendance_register_payload(request.args.get("year"), request.args.get("month"), request.args.get("search", ""), all_rows=True)
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter)
    styles = getSampleStyleSheet()
    story = [Paragraph(f"Attendance Register - {data['month_name']} {data['year']}", styles["Title"]), Spacer(1, 10)]
    table_data = [["Member", "Total"] + [str(d) for d in data["days"]] + ["P", "L", "A", "U", "%"]]
    for row in data["rows"][:80]:
        table_data.append([row["name"][:24], row["total_meetings"]] + [c or "-" for c in row["cells"]] + [row["totals"]["P"], row["totals"]["L"], row["totals"]["A"], row["totals"]["U"], f"{row['attendance_pct']}%"])
    tbl = Table(table_data, repeatRows=1)
    tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#111827")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
        ("FONTSIZE", (0, 0), (-1, -1), 6),
        ("BACKGROUND", (1, 1), (-6, -1), colors.whitesmoke),
    ]))
    story.append(tbl)
    doc.build(story)
    buffer.seek(0)
    return send_file(buffer, as_attachment=True, download_name=f"attendance_register_{data['year']}_{data['month']:02d}.pdf", mimetype="application/pdf")


@app.route("/analytics/graph-data")
@login_required
def analytics_graph_data():
    maybe_finalize_stale_live_meetings()
    return jsonify(graph_analytics_payload())


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
    try:
        page_no = max(int(request.args.get("page", 1)), 1)
    except Exception:
        page_no = 1
    per_page = 50
    offset = (page_no - 1) * per_page

    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS total FROM meetings")
            total_meetings = int((cur.fetchone() or {}).get("total") or 0)
            cur.execute("SELECT * FROM meetings ORDER BY id DESC LIMIT %s OFFSET %s", (per_page, offset))
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



@app.route("/notification-control", methods=["GET", "POST"])
@login_required
def notification_control():
    result_message = None
    result_type = "ok"
    if request.method == "POST":
        action = request.form.get("action", "save")
        if action == "save":
            save_notification_settings(request.form)
            log_activity("notification_settings_saved", session.get("username") or "unknown")
            result_message = "Notification settings saved successfully."
        elif action == "test_email":
            target = (request.form.get("test_email_to") or get_notification_settings().get("test_email_to") or SMART_ALERT_EMAIL_TO).strip()
            if target:
                ok, msg = send_email(target, "Test Email from Zoom Attendance Platform", "Your Notification Control Center email test is working successfully.", "<h2>Notification Control Center</h2><p>Your email test is working successfully.</p>")
                result_message = ("Test email sent to " + target) if ok else ("Test email failed: " + str(msg))
                result_type = "ok" if ok else "danger"
            else:
                result_message = "Please enter a test email address first."
                result_type = "danger"
        elif action == "test_push":
            push_result = send_push_notification("Test Push from Zoom Attendance Platform", "Your Notification Control Center push test is working successfully.", target_username=session.get("username"), click_url=url_for("notification_control", _external=True))
            result_message = f"Push test result: sent={push_result.get('sent', 0)}, failed={push_result.get('failed', 0)}"
            result_type = "ok" if push_result.get("sent", 0) > 0 else "danger"
    settings_data = get_notification_settings()
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT alert_type, entity_type, entity_id, previous_state, current_state, title, message, email_sent, push_sent, created_at
                FROM smart_alert_logs
                ORDER BY created_at DESC
                LIMIT 80
            """)
            logs = cur.fetchall()
    body = render_template_string("""
        <style>
        .notif-shell{display:grid;grid-template-columns:minmax(0,1fr) 420px;gap:18px;align-items:start}.notif-card{background:linear-gradient(145deg,rgba(15,23,42,.96),rgba(2,6,23,.98));border:1px solid rgba(99,102,241,.28);border-radius:24px;padding:22px;box-shadow:0 24px 70px rgba(0,0,0,.42)}.notif-title{display:flex;justify-content:space-between;gap:12px;align-items:center;margin-bottom:18px}.notif-title h2{margin:0;font-size:24px}.notif-title p{margin:5px 0 0;color:#94a3b8}.notif-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}.notif-box{background:rgba(15,23,42,.9);border:1px solid rgba(148,163,184,.18);border-radius:18px;padding:16px}.notif-box h3{margin:0 0 12px;font-size:16px}.toggle-row,.check-row{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:10px 0;border-bottom:1px solid rgba(148,163,184,.10)}.toggle-row:last-child,.check-row:last-child{border-bottom:0}.notif-input,.notif-textarea{width:100%;border-radius:12px;border:1px solid rgba(96,165,250,.28);background:#08111f;color:#e5e7eb;padding:11px 12px}.notif-textarea{min-height:120px;resize:vertical}.switch{position:relative;width:52px;height:28px}.switch input{display:none}.slider{position:absolute;inset:0;background:#334155;border-radius:999px;cursor:pointer;transition:.2s}.slider:before{content:"";position:absolute;width:22px;height:22px;left:3px;top:3px;background:white;border-radius:50%;transition:.2s}.switch input:checked + .slider{background:linear-gradient(90deg,#2563eb,#7c3aed)}.switch input:checked + .slider:before{transform:translateX(24px)}.notif-actions{display:flex;gap:10px;flex-wrap:wrap;margin-top:16px}.notif-actions button{border:0;border-radius:12px;padding:11px 14px;font-weight:900;color:white;background:linear-gradient(90deg,#2563eb,#7c3aed)}.notif-actions .secondary{background:#1e293b}.notif-actions .success{background:#16a34a}.notif-log{max-height:620px;overflow:auto}.log-item{border-bottom:1px solid rgba(148,163,184,.12);padding:12px 0}.log-title{font-weight:950;color:#f8fafc}.log-meta{font-size:12px;color:#94a3b8;margin-top:4px}.log-msg{font-size:13px;color:#cbd5e1;margin-top:6px;line-height:1.45}.pill-ok{background:rgba(34,197,94,.14);color:#86efac;border:1px solid rgba(34,197,94,.28);padding:5px 8px;border-radius:999px;font-size:12px;font-weight:900}@media(max-width:1100px){.notif-shell{grid-template-columns:1fr}.notif-grid{grid-template-columns:1fr}}
        </style>
        <div class="hero"><div class="hero-grid"><div><div class="badge">Notification Control Center</div><h1 class="hero-title">Smart alert delivery controls</h1><div class="hero-copy">Enable or disable Email/Push, select alert types, customize messages, test delivery, and review alert logs.</div></div><div class="hero-stats"><div class="hero-chip"><div class="small">Email</div><div class="big">{{ 'ON' if settings.email_enabled else 'OFF' }}</div></div><div class="hero-chip"><div class="small">Push</div><div class="big">{{ 'ON' if settings.push_enabled else 'OFF' }}</div></div></div></div></div>
        {% if result_message %}<div class="card" style="margin-bottom:16px">{{ result_message }}</div>{% endif %}
        <div class="notif-shell"><form method="post" class="notif-card"><div class="notif-title"><div><h2>Controls</h2><p>Connected with your existing smart alert system.</p></div><span class="pill-ok">No spam: state-change only</span></div><div class="notif-grid"><div class="notif-box"><h3>Delivery Channels</h3><label class="toggle-row"><span>Email alerts</span><span class="switch"><input type="checkbox" name="email_enabled" {% if settings.email_enabled %}checked{% endif %}><span class="slider"></span></span></label><label class="toggle-row"><span>Push alerts</span><span class="switch"><input type="checkbox" name="push_enabled" {% if settings.push_enabled %}checked{% endif %}><span class="slider"></span></span></label><div style="margin-top:12px"><label class="small">Test email receiver</label><input class="notif-input" name="test_email_to" value="{{ settings.test_email_to }}" placeholder="your@email.com"></div></div><div class="notif-box"><h3>Alert Types</h3>{% for key,label in alert_labels.items() %}<label class="check-row"><span>{{ label }}</span><input type="checkbox" name="alert_types" value="{{ key }}" {% if key in settings.alert_types %}checked{% endif %}></label>{% endfor %}</div><div class="notif-box"><h3>Timing Control</h3>{% for key,label in [('before','Before meeting'),('during','During meeting'),('after','After meeting')] %}<label class="check-row"><span>{{ label }}</span><input type="checkbox" name="timings" value="{{ key }}" {% if key in settings.timings %}checked{% endif %}></label>{% endfor %}</div><div class="notif-box"><h3>Message Template</h3><textarea class="notif-textarea" name="message_template">{{ settings.message_template }}</textarea><div class="muted" style="font-size:12px;margin-top:8px">Available: {title}, {message}, {state}, {alert_type}, {member_name}, {meeting_topic}</div></div></div><div class="notif-actions"><button type="submit" name="action" value="save">Save Controls</button><button type="submit" class="success" name="action" value="test_email">Test Email</button><button type="submit" class="secondary" name="action" value="test_push">Test Push</button></div></form><div class="notif-card notif-log"><div class="notif-title"><div><h2>Alert Logs</h2><p>Latest smart alert state-change records.</p></div></div>{% if logs %}{% for log in logs %}<div class="log-item"><div class="log-title">{{ log.title }}</div><div class="log-meta">{{ fmt_dt(log.created_at) }} · {{ log.alert_type }} · {{ log.previous_state or '-' }} → {{ log.current_state }} · Email {{ '✓' if log.email_sent else '×' }} · Push {{ log.push_sent }}</div><div class="log-msg">{{ log.message }}</div></div>{% endfor %}{% else %}<div class="muted">No alert logs yet.</div>{% endif %}</div></div>
    """, settings=settings_data, alert_labels=NOTIFICATION_ALERT_TYPE_LABELS, logs=logs, result_message=result_message, result_type=result_type, fmt_dt=fmt_dt)
    return page("Notification Control", body, "notification_control")

@app.route("/appearance")
@login_required
def appearance():
    themes = [("default-saas-dark","Default SaaS Dark","Premium dark dashboard with blue-purple SaaS glow.","#0b1020","#6366f1","#22d3ee"),("notion-clean","Notion Clean","Clean white workspace style for focused admin work.","#f7f6f3","#111827","#64748b"),("stripe-glow","Stripe Glow","High-end product dashboard style with gradient glow.","#070b1a","#635bff","#00d4ff"),("vercel-minimal","Vercel Minimal","Black and white minimal engineering console.","#000000","#ffffff","#737373"),("netflix-dark","Netflix Dark","Deep cinematic dark mode with red highlights.","#080808","#e50914","#f97316"),("college-formal","College Formal","Formal cream and navy palette for academic presentations.","#f3efe4","#1e3a8a","#92400e"),("purple-neon","Purple Neon","Futuristic neon purple interface for live dashboards.","#070014","#a855f7","#ec4899"),("light-professional","Light Professional","Modern light business dashboard with clean blue accents.","#eef2f7","#2563eb","#0ea5e9")]
    body = render_template_string("""
        <div class="hero"><div class="hero-grid"><div><div class="badge info" style="margin-bottom:12px">Appearance Engine</div><h1 class="hero-title">🎨 Appearance Studio</h1><div class="hero-copy">One-click full system theme switching with premium skeleton loading, animation control, glow effects, and Chart.js theme sync.</div></div><div class="hero-stats"><div class="hero-chip"><div class="small">Themes</div><div class="big">8</div></div><div class="hero-chip"><div class="small">Storage</div><div class="big">Local</div></div></div></div></div>
        <div class="appearance-controls"><div class="appearance-control"><label>Animation Level</label><select id="animationLevelSelect"><option value="off">Off</option><option value="minimal">Minimal</option><option value="smooth">Smooth</option><option value="full">Full</option></select><div class="muted" style="margin-top:8px">Saved in browser using localStorage. Affects transitions, hover motion, and loading polish.</div></div><div class="appearance-control"><label>Premium Skeleton Preview</label><div class="premium-skeleton-grid" style="margin-top:10px"><div class="premium-skeleton premium-skeleton-card"></div><div><div class="premium-skeleton premium-skeleton-line long"></div><div class="premium-skeleton premium-skeleton-line medium"></div><div class="premium-skeleton premium-skeleton-line short"></div></div></div></div></div>
        <div class="appearance-studio-grid">{% for key, name, desc, p1, p2, p3 in themes %}<div class="appearance-card" data-theme-apply="{{ key }}" style="--p1:{{ p1 }};--p2:{{ p2 }};--p3:{{ p3 }}"><div class="preview-band"></div><h3>{{ name }}</h3><p>{{ desc }}</p><div class="row" style="margin-top:14px"><span class="badge info">Click to Apply</span></div></div>{% endfor %}</div>
        <script>document.addEventListener('DOMContentLoaded',function(){if(window.setupAppearanceEngineV8){window.setupAppearanceEngineV8();}});</script>
    """, themes=themes)
    return page("Appearance Studio", body, "appearance")


# =========================
# UI_UPDATE_V10_AI_LEVEL3_SMART_ENGINE_APPLIED = True
# UI_UPDATE_V10_1_AI_LEVEL3_PERFORMANCE_FIX_APPLIED = True
# =========================

AI_LEVEL3_LOW_ATTENDANCE_DEFAULT = 50.0

def _ai_percent(num, den):
    den = den or 0
    if den <= 0:
        return 0.0
    return round((float(num or 0) / float(den)) * 100, 2)

def _ai_parse_threshold(query, default=AI_LEVEL3_LOW_ATTENDANCE_DEFAULT):
    import re
    q = (query or '').lower()
    for pattern in [r'(?:below|less than|under|<)\s*(\d{1,3})\s*%?', r'(\d{1,3})\s*%\s*(?:attendance|attend)']:
        m = re.search(pattern, q)
        if m:
            return float(max(0, min(100, int(m.group(1)))))
    return float(default)

def _ai_parse_days(query, default=None):
    import re
    q = (query or '').lower()
    m = re.search(r'last\s+(\d{1,3})\s+(?:day|days)', q)
    if m:
        return max(1, min(365, int(m.group(1))))
    if 'last week' in q or 'past week' in q:
        return 7
    if 'last month' in q or 'past month' in q:
        return 30
    if 'this month' in q:
        return 31
    return default

def _ai_date_filter_sql(days=None, meeting_alias='mt'):
    if days:
        return f" AND {meeting_alias}.start_time >= NOW() - INTERVAL '{int(days)} days' "
    return ""

def _ai_member_stats(days=None, limit=None):
    cache_key = _cache_make_key('ai_member_stats', {'days': days, 'limit': limit})
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    with db() as conn:
        with conn.cursor() as cur:
            name_expr = member_name_sql(conn)
            date_filter = _ai_date_filter_sql(days, 'mt')
            cur.execute(f"""
                SELECT m.id, {name_expr} AS name, m.email,
                    COUNT(a.id) AS total_records,
                    SUM(CASE WHEN a.final_status IN ('PRESENT','HOST') THEN 1 ELSE 0 END) AS present_count,
                    SUM(CASE WHEN a.final_status='LATE' THEN 1 ELSE 0 END) AS late_count,
                    SUM(CASE WHEN a.final_status='ABSENT' THEN 1 ELSE 0 END) AS absent_count,
                    COALESCE(SUM(a.total_seconds),0) AS total_seconds,
                    MAX(mt.start_time) AS last_seen
                FROM members m
                LEFT JOIN attendance a ON a.member_id=m.id
                LEFT JOIN meetings mt ON mt.meeting_uuid=a.meeting_uuid
                WHERE {ACTIVE_MEMBER_SQL} {date_filter}
                GROUP BY m.id, name, m.email
                ORDER BY name ASC
            """)
            rows = cur.fetchall()
    result = []
    for row in rows:
        total = int(row.get('total_records') or 0)
        present = int(row.get('present_count') or 0)
        late = int(row.get('late_count') or 0)
        absent = int(row.get('absent_count') or 0)
        attendance_pct = _ai_percent(present + late, total)
        absent_pct = _ai_percent(absent, total)
        if attendance_pct < 50:
            risk = 'Critical'; suggestion = 'Immediate follow-up needed. Send reminder and personally check availability.'
        elif attendance_pct < 75:
            risk = 'Warning'; suggestion = 'Send reminder before next meeting and monitor consistency.'
        else:
            risk = 'Healthy'; suggestion = 'Maintain current engagement.'
        trend = 'Declining' if total >= 4 and absent_pct >= 50 else ('Improving/Consistent' if attendance_pct >= 85 else 'Stable')
        tag = 'No Data' if total == 0 else ('Consistent' if attendance_pct >= 85 else ('Risky' if attendance_pct < 50 else ('Irregular' if absent_pct >= 30 else 'Stable')))
        result.append({'id': row.get('id'), 'name': row.get('name') or f"Member {row.get('id')}", 'email': row.get('email') or '', 'total': total, 'present': present, 'late': late, 'absent': absent, 'attendance_pct': attendance_pct, 'duration_minutes': round((row.get('total_seconds') or 0)/60.0,2), 'last_seen': row.get('last_seen'), 'risk': risk, 'trend': trend, 'tag': tag, 'suggestion': suggestion})
    if limit:
        result = result[:int(limit)]
    return _cache_set(cache_key, result)

def _ai_recent_meetings(limit=8):
    cache_key = _cache_make_key('ai_recent_meetings', {'limit': limit})
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, meeting_uuid, topic, start_time, end_time, unique_participants, member_participants,
                       unknown_participants, present_count, late_count, absent_count, host_present, status
                FROM meetings
                ORDER BY start_time DESC NULLS LAST, id DESC
                LIMIT %s
            """, (limit,))
            rows = cur.fetchall()
    return _cache_set(cache_key, rows)

def _ai_meeting_health_score(meeting):
    present = int(meeting.get('present_count') or 0); late = int(meeting.get('late_count') or 0); absent = int(meeting.get('absent_count') or 0); unknown = int(meeting.get('unknown_participants') or 0)
    total = present + late + absent
    score = (_ai_percent(present + late, total) if total else 0) - min(30, unknown*5) + (10 if meeting.get('host_present') else -10)
    return max(0, min(100, round(score, 1)))

def generate_ai_level3_insights():
    insights=[]; meetings=_ai_recent_meetings(6); members=_ai_member_stats()
    critical=[m for m in members if m['risk']=='Critical']; warning=[m for m in members if m['risk']=='Warning']
    top=sorted([m for m in members if m['total']>0], key=lambda x:(x['attendance_pct'],x['duration_minutes']), reverse=True)[:1]
    worst=sorted([m for m in members if m['total']>0], key=lambda x:x['attendance_pct'])[:1]
    if len(meetings)>=2:
        latest,previous=meetings[0],meetings[1]
        lt=(latest.get('present_count') or 0)+(latest.get('late_count') or 0)+(latest.get('absent_count') or 0)
        pt=(previous.get('present_count') or 0)+(previous.get('late_count') or 0)+(previous.get('absent_count') or 0)
        delta=round(_ai_percent((latest.get('present_count') or 0)+(latest.get('late_count') or 0),lt)-_ai_percent((previous.get('present_count') or 0)+(previous.get('late_count') or 0),pt),2)
        insights.append({'title':'Attendance trend changed','severity':'warning' if delta<0 else 'info','category':'Trend','message':f'Attendance changed by {delta}% compared with previous meeting.','recommendation':'Send reminders to low attendance members.' if delta<0 else 'Maintain current engagement pattern.'})
        ls=parse_dt(latest.get('start_time')); le=parse_dt(latest.get('end_time')); ps=parse_dt(previous.get('start_time')); pe=parse_dt(previous.get('end_time'))
        if ls and le and ps and pe:
            insights.append({'title':'Meeting duration comparison','severity':'info','category':'Duration','message':f'Latest meeting was {round(((le-ls)-(pe-ps)).total_seconds()/60,1)} minutes different from previous meeting.','recommendation':'Keep meeting duration consistent for better attendance patterns.'})
    if critical: insights.append({'title':'Critical members detected','severity':'critical','category':'Risk','message':f'{len(critical)} member(s) are below 50% attendance.','recommendation':'Use AI Assistant: send reminder to them.'})
    if warning: insights.append({'title':'Warning-risk members detected','severity':'warning','category':'Risk','message':f'{len(warning)} member(s) are between 50–75% attendance.','recommendation':'Monitor and send early reminders.'})
    if meetings:
        latest=meetings[0]
        if (latest.get('unknown_participants') or 0)>=5: insights.append({'title':'Unknown participant spike','severity':'warning','category':'Security','message':f"Latest meeting had {latest.get('unknown_participants')} unknown participant(s).",'recommendation':'Review unknown users and ask members to join with registered names.'})
        if (latest.get('late_count') or 0)>=3: insights.append({'title':'Late trend increased','severity':'warning','category':'Punctuality','message':f"Latest meeting had {latest.get('late_count')} late participant(s).",'recommendation':'Send pre-meeting reminder 10 minutes earlier.'})
    if top: insights.append({'title':'Top performer','severity':'info','category':'Performance','message':f"{top[0]['name']} is leading with {top[0]['attendance_pct']}% attendance.",'recommendation':'Appreciate consistent attendance to improve motivation.'})
    if worst: insights.append({'title':'Worst performer','severity':'critical' if worst[0]['attendance_pct']<50 else 'warning','category':'Performance','message':f"{worst[0]['name']} has {worst[0]['attendance_pct']}% attendance.",'recommendation':'Follow up personally and send attendance reminder.'})
    return insights[:12]

def _ai_find_member_by_query(query):
    q=(query or '').lower(); best=None
    for m in _ai_member_stats():
        name=(m.get('name') or '').lower()
        if name and name in q: return m
        for part in name.split():
            if len(part)>=3 and part in q: best=m
    return best

def _ai_format_members_list(members,title='Members'):
    if not members: return f'{title}: No matching members found.'
    lines=[f'{title}:']
    for idx,m in enumerate(members[:20],1): lines.append(f"{idx}. {m['name']} — {m['attendance_pct']}% attendance, Risk: {m['risk']}, Trend: {m['trend']}")
    if len(members)>20: lines.append(f'...and {len(members)-20} more.')
    return '\n'.join(lines)

def _ai_low_attendance_members(query=''):
    threshold=_ai_parse_threshold(query); days=_ai_parse_days(query)
    return sorted([m for m in _ai_member_stats(days=days) if m['total']>0 and m['attendance_pct']<threshold], key=lambda x:x['attendance_pct'])

def _ai_bot_answer(query):
    q=(query or '').strip().lower(); last_targets=session.get('ai_last_targets',[]) or []
    if not q: return {'response':'Ask me about attendance, risk members, late trend, top performers, last meeting, or reminders.','targets':[]}
    if ('send' in q or 'remind' in q or 'reminder' in q) and ('them' in q or 'low attendance' in q or 'risk' in q):
        if 'them' in q and last_targets:
            lookup={int(m['id']):m for m in _ai_member_stats() if m.get('id') is not None}; targets=[lookup.get(int(x)) for x in last_targets if str(x).isdigit() and lookup.get(int(x))]
        else: targets=_ai_low_attendance_members(q)
        sent=0; failed=[]
        for m in targets:
            if not m.get('email'): failed.append(m['name']); continue
            ok,_=send_email(m['email'],'Attendance Reminder',f"Hello {m['name']}, your attendance is currently {m['attendance_pct']}%. Please attend upcoming meetings regularly.")
            sent += 1 if ok else 0
            if not ok: failed.append(m['name'])
        return {'response':f"Reminder sent to {sent} member(s). Failed/no email: {', '.join(failed) if failed else 'None'}", 'targets':[m['id'] for m in targets]}
    if 'export' in q and ('low' in q or 'risk' in q or 'attendance' in q): return {'response':'Export links: /ai/export/low-attendance.csv or /ai/export/low-attendance.pdf','targets':last_targets}
    if 'below' in q or 'less than' in q or 'under' in q or 'low attendance' in q or '<' in q:
        members=_ai_low_attendance_members(q); session['ai_last_targets']=[m['id'] for m in members if m.get('id') is not None]
        return {'response':_ai_format_members_list(members,f"Members below {_ai_parse_threshold(q)}% attendance"),'targets':session['ai_last_targets']}
    if 'risk' in q or 'critical' in q or 'warning' in q:
        members=[m for m in _ai_member_stats() if m['risk'] in ('Critical','Warning')]; session['ai_last_targets']=[m['id'] for m in members if m.get('id') is not None]
        return {'response':_ai_format_members_list(members,'At-risk members'),'targets':session['ai_last_targets']}
    if 'top' in q or 'best' in q or 'performer' in q: return {'response':_ai_format_members_list(sorted([m for m in _ai_member_stats() if m['total']>0], key=lambda x:(x['attendance_pct'],x['duration_minutes']), reverse=True)[:10],'Top performers'),'targets':[]}
    if 'worst' in q or 'lowest' in q or 'poor' in q:
        members=sorted([m for m in _ai_member_stats() if m['total']>0], key=lambda x:x['attendance_pct'])[:10]; session['ai_last_targets']=[m['id'] for m in members if m.get('id') is not None]
        return {'response':_ai_format_members_list(members,'Worst performers'),'targets':session['ai_last_targets']}
    if 'late' in q:
        lines=['Late trend from recent meetings:']+[f"• {fmt_date(m.get('start_time'))} — {m.get('late_count') or 0} late participant(s)" for m in _ai_recent_meetings(5)]
        return {'response':'\n'.join(lines),'targets':[]}
    if 'unknown' in q:
        lines=['Unknown participant trend:']+[f"• {fmt_date(m.get('start_time'))} — {m.get('unknown_participants') or 0} unknown participant(s)" for m in _ai_recent_meetings(5)]
        return {'response':'\n'.join(lines),'targets':[]}
    if 'last meeting' in q or 'summarize' in q or 'summary' in q:
        meetings=_ai_recent_meetings(1)
        if not meetings: return {'response':'No meeting found yet.','targets':[]}
        m=meetings[0]; return {'response':f"Last meeting summary: {m.get('topic') or 'Meeting'} on {fmt_dt(m.get('start_time'))}. Present: {m.get('present_count') or 0}, Late: {m.get('late_count') or 0}, Absent: {m.get('absent_count') or 0}, Unknown: {m.get('unknown_participants') or 0}. Health score: {_ai_meeting_health_score(m)}/100.",'targets':[]}
    member=_ai_find_member_by_query(q)
    if member: return {'response':f"{member['name']} insight: Attendance {member['attendance_pct']}%, Risk {member['risk']}, Trend {member['trend']}, Tag {member['tag']}. Suggestion: {member['suggestion']}",'targets':[member['id']]}
    if 'why' in q and ('drop' in q or 'decrease' in q or 'down' in q):
        related=[i for i in generate_ai_level3_insights() if i['category'] in ('Trend','Risk','Punctuality')]
        return {'response':'Possible reasons:\n'+'\n'.join([f"• {i['message']} Recommendation: {i['recommendation']}" for i in related[:5]]) if related else 'Need more records for stronger analysis.','targets':[]}
    insights=generate_ai_level3_insights()[:3]
    return {'response':'Here are current smart insights:\n'+'\n'.join([f"• {i['title']}: {i['message']}" for i in insights]) if insights else 'I can answer attendance, risk, member, late, unknown, summary, top/worst performer, and reminder questions using your current data.','targets':[]}

@app.route('/api/ai-assistant-level3', methods=['POST'])
@login_required
def api_ai_assistant_level3():
    payload=request.get_json(silent=True) or {}
    return jsonify(_ai_bot_answer(payload.get('query','')))

@app.route('/api/ai-insights-level3')
@login_required
def api_ai_insights_level3():
    return jsonify({'insights':generate_ai_level3_insights(),'members':_ai_member_stats(),'meetings':[dict(m) for m in _ai_recent_meetings(8)]})

@app.route('/api/member-intelligence/<int:member_id>')
@login_required
def api_member_intelligence_level3(member_id):
    matches=[m for m in _ai_member_stats() if int(m.get('id') or 0)==int(member_id)]
    return (jsonify(matches[0]) if matches else (jsonify({'error':'Member not found'}),404))

@app.route('/ai/export/low-attendance.csv')
@login_required
def ai_export_low_attendance_csv():
    output=io.StringIO(); writer=csv.writer(output); writer.writerow(['Name','Email','Attendance %','Risk','Trend','Suggestion'])
    for m in _ai_low_attendance_members('below 75'): writer.writerow([m['name'],m['email'],m['attendance_pct'],m['risk'],m['trend'],m['suggestion']])
    return Response(output.getvalue(), mimetype='text/csv', headers={'Content-Disposition':'attachment; filename=ai_low_attendance_report.csv'})

@app.route('/ai/export/low-attendance.pdf')
@login_required
def ai_export_low_attendance_pdf():
    buf=io.BytesIO(); doc=SimpleDocTemplate(buf,pagesize=letter); styles=getSampleStyleSheet(); story=[Paragraph('AI Low Attendance Report',styles['Title']),Spacer(1,12)]
    data=[['Name','Attendance %','Risk','Suggestion']]+[[m['name'],str(m['attendance_pct']),m['risk'],m['suggestion'][:60]] for m in _ai_low_attendance_members('below 75')[:50]]
    table=Table(data, repeatRows=1); table.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,0),colors.HexColor('#111827')),('TEXTCOLOR',(0,0),(-1,0),colors.white),('GRID',(0,0),(-1,-1),0.25,colors.grey),('FONTSIZE',(0,0),(-1,-1),8)])); story.append(table); doc.build(story); buf.seek(0)
    return send_file(buf, as_attachment=True, download_name='ai_low_attendance_report.pdf', mimetype='application/pdf')

@app.route('/ai-intelligence')
@login_required
def ai_intelligence():
    insights=generate_ai_level3_insights(); members=_ai_member_stats(); meetings=_ai_recent_meetings(8); critical=len([m for m in members if m['risk']=='Critical']); warning=len([m for m in members if m['risk']=='Warning']); latest_score=_ai_meeting_health_score(meetings[0]) if meetings else 0; avg_duration=round(sum([m['duration_minutes'] for m in members])/max(len(members),1),2)
    logs=[]
    try:
        with db() as conn:
            with conn.cursor() as cur:
                if table_exists(conn,'smart_alert_logs'):
                    cur.execute('SELECT title, message, current_state, created_at FROM smart_alert_logs ORDER BY created_at DESC LIMIT 8'); logs=cur.fetchall()
    except Exception: logs=[]
    heat_members=members[:20]; heat_meetings=list(reversed(meetings[:12])); heat=[]
    # Fast heatmap: one lightweight query instead of member x meeting nested queries.
    # This prevents Render/Gunicorn worker timeout and Neon connection overload.
    try:
        heat_member_ids=[m.get('id') for m in heat_members if m.get('id') is not None]
        heat_meeting_uuids=[mt.get('meeting_uuid') for mt in heat_meetings if mt.get('meeting_uuid')]
        status_map={}
        if heat_member_ids and heat_meeting_uuids:
            with db() as conn:
                with conn.cursor() as cur:
                    cur.execute('''
                        SELECT member_id, meeting_uuid, final_status
                        FROM attendance
                        WHERE member_id = ANY(%s) AND meeting_uuid = ANY(%s)
                    ''', (heat_member_ids, heat_meeting_uuids))
                    for r in cur.fetchall():
                        status_map[(r.get('member_id'), r.get('meeting_uuid'))] = r.get('final_status') or 'NO_DATA'
        for mem in heat_members:
            row={'name':mem.get('name') or 'Member','cells':[]}
            for mt in heat_meetings:
                row['cells'].append(status_map.get((mem.get('id'), mt.get('meeting_uuid')), 'NO_DATA'))
            heat.append(row)
    except Exception as exc:
        print(f"AI heatmap skipped safely: {exc}")
        heat=[]
    body=render_template_string('''
    <style>.ai-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:16px}.ai-card{background:rgba(15,23,42,.78);border:1px solid rgba(148,163,184,.18);border-radius:22px;padding:18px;box-shadow:0 18px 60px rgba(0,0,0,.28)}.ai-big{font-size:30px;font-weight:950}.ai-chat{display:grid;grid-template-columns:minmax(0,1fr) 390px;gap:18px}.ai-msg{white-space:pre-wrap;background:rgba(15,23,42,.85);border:1px solid rgba(148,163,184,.16);padding:12px;border-radius:16px;margin:10px 0}.ai-input{width:100%;border-radius:14px;border:1px solid rgba(99,102,241,.3);background:#020617;color:#e5e7eb;padding:13px}.ai-suggest{display:flex;flex-wrap:wrap;gap:8px;margin:10px 0}.ai-suggest button{border:0;border-radius:999px;padding:9px 12px;background:rgba(99,102,241,.2);color:#c7d2fe;font-weight:800}.risk-critical{color:#fecaca}.risk-warning{color:#fde68a}.risk-healthy{color:#bbf7d0}.heat{overflow:auto}.heat table{border-collapse:separate;border-spacing:4px;width:100%}.heat td,.heat th{font-size:12px;padding:8px;border-radius:8px;text-align:center}.h-PRESENT,.h-HOST{background:#166534;color:#dcfce7}.h-LATE{background:#92400e;color:#fef3c7}.h-ABSENT{background:#7f1d1d;color:#fee2e2}.h-NO_DATA{background:#334155;color:#cbd5e1}@media(max-width:1100px){.ai-grid{grid-template-columns:1fr 1fr}.ai-chat{grid-template-columns:1fr}}@media(max-width:700px){.ai-grid{grid-template-columns:1fr}}</style>
    <div class="hero"><div class="hero-grid"><div><div class="badge info">AI Level 3</div><h1 class="hero-title">🧠 AI Intelligence Center</h1><div class="hero-copy">Natural query assistant, actionable reminders, context memory, member intelligence, risk heatmap, meeting health score, smart alerts, and report export — all without paid APIs.</div></div><div class="hero-stats"><div class="hero-chip"><div class="small">Health Score</div><div class="big">{{ latest_score }}/100</div></div><div class="hero-chip"><div class="small">Critical</div><div class="big">{{ critical }}</div></div></div></div></div>
    <div class="ai-grid"><div class="ai-card"><div class="small">Critical Members</div><div class="ai-big risk-critical">{{ critical }}</div></div><div class="ai-card"><div class="small">Warning Members</div><div class="ai-big risk-warning">{{ warning }}</div></div><div class="ai-card"><div class="small">Avg Duration</div><div class="ai-big">{{ avg_duration }}m</div></div><div class="ai-card"><div class="small">Latest Meeting Health</div><div class="ai-big">{{ latest_score }}/100</div></div></div>
    <div class="ai-chat" style="margin-top:18px"><div class="ai-card"><h2>🤖 Smart Assistant</h2><div id="aiGreeting" class="ai-msg">Analyzing your latest attendance data...</div><div class="ai-suggest"><button onclick="aiAsk('Who is at risk?')">At-risk members</button><button onclick="aiAsk('List members below 50%')">Below 50%</button><button onclick="aiAsk('Show top performers')">Top performers</button><button onclick="aiAsk('Why attendance dropped?')">Why dropped?</button><button onclick="aiAsk('Summarize last meeting')">Last meeting</button><button onclick="aiAsk('Send reminder to them')">Remind them</button><button onclick="location.href='/ai/export/low-attendance.pdf'">Export PDF</button><button onclick="location.href='/ai/export/low-attendance.csv'">Export CSV</button></div><input id="aiLevel3Input" class="ai-input" placeholder="Ask attendance question..."><div style="margin-top:10px"><button onclick="aiAsk(document.getElementById('aiLevel3Input').value)">Ask AI</button></div><div id="aiLevel3Answer" class="ai-msg">Ready.</div></div><div class="ai-card"><h2>💡 Insights</h2>{% for i in insights %}<div class="ai-msg"><b>{{ i.title }}</b><br><span class="small">{{ i.category }} · {{ i.severity }}</span><br>{{ i.message }}<br><b>Recommendation:</b> {{ i.recommendation }}</div>{% endfor %}</div></div>
    <div class="ai-card" style="margin-top:18px"><h2>👤 Member Intelligence</h2><div class="table-wrap"><table><thead><tr><th>Name</th><th>Attendance %</th><th>Trend</th><th>Risk</th><th>Tag</th><th>Suggestion</th></tr></thead><tbody>{% for m in members %}<tr><td>{{ m.name }}</td><td>{{ m.attendance_pct }}%</td><td>{{ m.trend }}</td><td class="risk-{{ m.risk|lower }}">{{ m.risk }}</td><td>{{ m.tag }}</td><td>{{ m.suggestion }}</td></tr>{% endfor %}</tbody></table></div></div>
    <div class="ai-card heat" style="margin-top:18px"><h2>🧠 Risk Heatmap</h2><table><thead><tr><th>Member</th>{% for mt in heat_meetings %}<th>{{ fmt_date(mt.start_time) }}</th>{% endfor %}</tr></thead><tbody>{% for row in heat %}<tr><th>{{ row.name }}</th>{% for c in row.cells %}<td class="h-{{ c }}">{{ 'P' if c in ['PRESENT','HOST'] else 'L' if c=='LATE' else 'A' if c=='ABSENT' else '-' }}</td>{% endfor %}</tr>{% endfor %}</tbody></table></div>
    <div class="ai-card" style="margin-top:18px"><h2>🔥 Smart Alert Panel</h2>{% if logs %}{% for l in logs %}<div class="ai-msg"><b>{{ l.title }}</b><br>{{ l.message }}<br><span class="small">{{ fmt_dt(l.created_at) }} · {{ l.current_state }}</span></div>{% endfor %}{% else %}<div class="muted">No smart alert logs yet.</div>{% endif %}</div>
    <script>function aiAsk(q){if(!q)return;document.getElementById('aiLevel3Answer').innerText='Thinking...';fetch('/api/ai-assistant-level3',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({query:q})}).then(r=>r.json()).then(d=>{document.getElementById('aiLevel3Answer').innerText=d.response||'No answer';});}fetch('/api/ai-insights-level3').then(r=>r.json()).then(d=>{let ins=(d.insights||[]).slice(0,2).map(x=>'• '+x.message).join('\n');document.getElementById('aiGreeting').innerText=ins||'No critical insight right now.';}).catch(()=>{});</script>
    ''', insights=insights, members=members, critical=critical, warning=warning, latest_score=latest_score, avg_duration=avg_duration, logs=logs, heat=heat, heat_meetings=heat_meetings, fmt_date=fmt_date, fmt_dt=fmt_dt)
    return page('AI Intelligence', body, 'ai_intelligence')

# =========================
# END UI_UPDATE_V10_AI_LEVEL3_SMART_ENGINE_APPLIED
# =========================


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=True)
