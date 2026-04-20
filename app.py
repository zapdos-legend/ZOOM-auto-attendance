import csv
import hashlib
import hmac
import io
import os
from datetime import datetime
from functools import wraps
from zoneinfo import ZoneInfo

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
from psycopg.rows import dict_row
import psycopg
from dotenv import load_dotenv
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
    "present_percentage": os.getenv("PRESENT_PERCENTAGE", "70"),
    "late_count_as_present_percentage": os.getenv("LATE_COUNT_AS_PRESENT_PERCENTAGE", "40"),
    "late_threshold_minutes": os.getenv("LATE_THRESHOLD_MINUTES", "10"),
    "meeting_finalize_seconds": os.getenv("INACTIVITY_CONFIRM_SECONDS", "30"),
}

DB_INITIALIZED = False


def now_local() -> datetime:
    return datetime.now(ZoneInfo(TIMEZONE_NAME))


def parse_dt(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except Exception:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=ZoneInfo(TIMEZONE_NAME))
    return dt.astimezone(ZoneInfo(TIMEZONE_NAME))


def fmt_dt(dt):
    if not dt:
        return "-"
    parsed = parse_dt(dt)
    return parsed.strftime("%d-%m-%Y %H:%M:%S") if parsed else "-"


def fmt_time(dt):
    if not dt:
        return "-"
    parsed = parse_dt(dt)
    return parsed.strftime("%H:%M:%S") if parsed else "-"


def slugify(text: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in str(text))
    while "__" in safe:
        safe = safe.replace("__", "_")
    return safe.strip("_") or "report"


def db():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is missing")
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def table_exists(conn, table_name: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT EXISTS (
                SELECT 1
                FROM information_schema.tables
                WHERE table_schema = 'public' AND table_name = %s
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
                WHERE table_schema = 'public'
                  AND table_name = %s
                  AND column_name = %s
            ) AS exists_flag
            """,
            (table_name, column_name),
        )
        row = cur.fetchone()
        return bool(row and row["exists_flag"])


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
                WHERE schemaname = 'public' AND indexname = %s
            ) AS exists_flag
            """,
            (index_name,),
        )
        row = cur.fetchone()
        if not row or not row["exists_flag"]:
            cur.execute(create_sql)


def get_setting(name, cast=str):
    value = DEFAULT_SETTINGS.get(name)
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM settings WHERE key=%s", (name,))
            row = cur.fetchone()
            if row:
                value = row["value"]
    return cast(value)


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


def sync_special_user(conn, username: str, password: str, role: str):
    if not username or not password:
        return

    username = username.strip()
    password_hash = hash_password(password)

    with conn.cursor() as cur:
        cur.execute("SELECT * FROM users WHERE username=%s", (username,))
        existing = cur.fetchone()

        if existing:
            needs_update = (
                existing["password_hash"] != password_hash
                or existing["role"] != role
                or not existing["is_active"]
            )
            if needs_update:
                cur.execute(
                    """
                    UPDATE users
                    SET password_hash=%s,
                        role=%s,
                        is_active=TRUE
                    WHERE username=%s
                    """,
                    (password_hash, role, username),
                )
        else:
            cur.execute(
                """
                INSERT INTO users(username, password_hash, role, is_active)
                VALUES (%s, %s, %s, TRUE)
                """,
                (username, password_hash, role),
            )


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

        ensure_index(
            conn,
            "idx_attendance_meeting_uuid",
            "CREATE INDEX idx_attendance_meeting_uuid ON attendance(meeting_uuid)",
        )
        ensure_index(
            conn,
            "idx_attendance_member_id",
            "CREATE INDEX idx_attendance_member_id ON attendance(member_id)",
        )
        ensure_index(
            conn,
            "idx_meetings_status",
            "CREATE INDEX idx_meetings_status ON meetings(status)",
        )

        with conn.cursor() as cur:
            for key, value in DEFAULT_SETTINGS.items():
                cur.execute(
                    """
                    INSERT INTO settings(key, value)
                    VALUES (%s, %s)
                    ON CONFLICT (key) DO NOTHING
                    """,
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


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        return fn(*args, **kwargs)
    return wrapper


def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if session.get("role") != "admin":
            flash("Admin access required.", "error")
            return redirect(url_for("home"))
        return fn(*args, **kwargs)
    return wrapper


def find_member(name: str, email: str | None = None):
    norm_name = (name or "").strip().lower()
    norm_email = (email or "").strip().lower()
    with db() as conn:
        with conn.cursor() as cur:
            if norm_email:
                cur.execute(
                    "SELECT * FROM members WHERE active=TRUE AND lower(email)=%s LIMIT 1",
                    (norm_email,),
                )
                row = cur.fetchone()
                if row:
                    return row
            cur.execute(
                "SELECT * FROM members WHERE active=TRUE AND lower(full_name)=%s LIMIT 1",
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
    meeting_uuid = str(payload_object.get("uuid") or payload_object.get("id") or "")
    meeting_id = str(payload_object.get("id") or "")
    topic = payload_object.get("topic") or "Zoom Meeting"
    host_name = payload_object.get("host_name") or payload_object.get("host_email") or ""
    start_time = parse_dt(payload_object.get("start_time")) or now_local()
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM meetings WHERE meeting_uuid=%s", (meeting_uuid,))
            row = cur.fetchone()
            if row:
                return row
            cur.execute(
                """
                INSERT INTO meetings(meeting_uuid, meeting_id, topic, host_name, start_time, status)
                VALUES (%s, %s, %s, %s, %s, 'live') RETURNING *
                """,
                (meeting_uuid, meeting_id, topic, host_name, start_time),
            )
            row = cur.fetchone()
        conn.commit()
    return row


def update_participant(meeting_uuid, participant_name, participant_email, event_time, event_type):
    is_host = HOST_NAME_HINT and HOST_NAME_HINT in (participant_name or "").strip().lower()
    member = find_member(participant_name, participant_email)
    key = participant_key(participant_name, participant_email)

    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM attendance WHERE meeting_uuid=%s AND participant_key=%s",
                (meeting_uuid, key),
            )
            row = cur.fetchone()

            if not row:
                first_join = event_time if event_type == "join" else None
                current_join = event_time if event_type == "join" else None
                last_leave = event_time if event_type == "leave" else None
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
                        bool(member),
                        member["id"] if member else None,
                        is_host,
                        "JOINED" if event_type == "join" else "LEFT",
                    ),
                )
                row = cur.fetchone()

            if event_type == "join":
                rejoin_count = row["rejoin_count"]
                if row["first_join"] is not None:
                    rejoin_count += 1
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
                        bool(member),
                        member["id"] if member else None,
                        is_host,
                        row["id"],
                    ),
                )
            else:
                total_seconds = row["total_seconds"] or 0
                if row["current_join"]:
                    delta = int((event_time - parse_dt(row["current_join"])).total_seconds())
                    total_seconds += max(delta, 0)
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
                        bool(member),
                        member["id"] if member else None,
                        is_host,
                        row["id"],
                    ),
                )
        conn.commit()

    refresh_live_meeting_summary(meeting_uuid)


def finalize_meeting(meeting_uuid, ended_at=None):
    ended_at = parse_dt(ended_at) or now_local()
    present_percentage = get_setting("present_percentage", int)
    late_pct = get_setting("late_count_as_present_percentage", int)
    late_threshold_minutes = get_setting("late_threshold_minutes", int)

    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM meetings WHERE meeting_uuid=%s", (meeting_uuid,))
            meeting = cur.fetchone()
            if not meeting:
                return None

            start_time = parse_dt(meeting["start_time"]) or ended_at
            end_time = ended_at if ended_at > start_time else start_time
            total_seconds = max(int((end_time - start_time).total_seconds()), 0)
            required_present = total_seconds * present_percentage / 100.0
            required_late = total_seconds * late_pct / 100.0

            cur.execute("SELECT * FROM attendance WHERE meeting_uuid=%s ORDER BY participant_name", (meeting_uuid,))
            rows = cur.fetchall()

            present_count = late_count = absent_count = 0
            member_participants = unknown_participants = 0
            host_present = False

            for row in rows:
                total = row["total_seconds"] or 0
                if row["current_join"]:
                    total += max(int((end_time - parse_dt(row["current_join"])).total_seconds()), 0)
                first_join = parse_dt(row["first_join"])
                delay_minutes = None
                if first_join:
                    delay_minutes = max((first_join - start_time).total_seconds() / 60.0, 0)

                if total >= required_present:
                    final_status = "PRESENT"
                    present_count += 1
                elif total >= required_late or (delay_minutes is not None and delay_minutes > late_threshold_minutes and total > 0):
                    final_status = "LATE"
                    late_count += 1
                else:
                    final_status = "ABSENT"
                    absent_count += 1

                if row["is_member"]:
                    member_participants += 1
                else:
                    unknown_participants += 1
                if row["is_host"]:
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


def read_live_snapshot():
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM meetings WHERE status='live' ORDER BY id DESC LIMIT 1")
            meeting = cur.fetchone()
            if not meeting:
                return None
            meeting_uuid = meeting.get("meeting_uuid")
            if not meeting_uuid:
                return {
                    "meeting": meeting,
                    "participants": [],
                    "active_now": [],
                    "not_joined_members": [],
                }
            cur.execute("SELECT * FROM attendance WHERE meeting_uuid=%s ORDER BY participant_name", (meeting_uuid,))
            participants = cur.fetchall()
            cur.execute("SELECT * FROM members WHERE active=TRUE ORDER BY full_name")
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


def analytics_data(filters):
    where = ["1=1"]
    params = []

    if filters.get("from_date"):
        where.append("CAST(m.start_time AS TEXT)::date >= %s")
        params.append(filters["from_date"])
    if filters.get("to_date"):
        where.append("CAST(m.start_time AS TEXT)::date <= %s")
        params.append(filters["to_date"])
    if filters.get("meeting_uuid"):
        where.append("a.meeting_uuid = %s")
        params.append(filters["meeting_uuid"])
    if filters.get("member_id"):
        where.append("a.member_id = %s")
        params.append(int(filters["member_id"]))
    if filters.get("person_name"):
        where.append("lower(a.participant_name) LIKE %s")
        params.append(f"%{filters['person_name'].strip().lower()}%")
    if filters.get("participant_type") == "member":
        where.append("a.is_member = TRUE")
    elif filters.get("participant_type") == "unknown":
        where.append("a.is_member = FALSE")

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

            cur.execute("SELECT * FROM members WHERE active=TRUE ORDER BY full_name")
            members = cur.fetchall()
            cur.execute("SELECT id, meeting_uuid, topic, start_time FROM meetings ORDER BY id DESC LIMIT 200")
            meetings = cur.fetchall()

    total_rows = len(rows)
    present_rows = sum(1 for r in rows if r.get("final_status") == "PRESENT")
    late_rows = sum(1 for r in rows if r.get("final_status") == "LATE")
    absent_rows = sum(1 for r in rows if r.get("final_status") == "ABSENT")
    unknown_rows = sum(1 for r in rows if not r.get("is_member"))
    avg_minutes = round(sum((r.get("total_seconds") or 0) for r in rows) / 60 / total_rows, 2) if total_rows else 0

    by_person = {}
    by_meeting = {}
    for r in rows:
        key = r.get("participant_name") or "Unknown Participant"
        by_person.setdefault(key, {"name": key, "meetings": 0, "minutes": 0, "present": 0, "late": 0, "absent": 0})
        by_person[key]["meetings"] += 1
        by_person[key]["minutes"] += (r.get("total_seconds") or 0) / 60
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
                "topic": r.get("topic") or "Untitled Meeting",
                "start_time": r.get("start_time"),
                "present": 0,
                "late": 0,
                "absent": 0,
                "unknown": 0,
            },
        )
        if r.get("final_status") == "PRESENT":
            by_meeting[mk]["present"] += 1
        elif r.get("final_status") == "LATE":
            by_meeting[mk]["late"] += 1
        elif r.get("final_status") == "ABSENT":
            by_meeting[mk]["absent"] += 1
        if not r.get("is_member"):
            by_meeting[mk]["unknown"] += 1

    top_people = sorted(by_person.values(), key=lambda x: (x["present"], x["minutes"]), reverse=True)[:5]
    low_people = sorted(by_person.values(), key=lambda x: (x["present"], -x["absent"], -x["minutes"]))[:5]
    unknown_board = sorted(
        [v for k, v in by_person.items() if any((rr.get("participant_name") == k and not rr.get("is_member")) for rr in rows)],
        key=lambda x: x["meetings"],
        reverse=True,
    )[:10]

    return {
        "filters": filters,
        "rows": rows,
        "members": members,
        "meetings": meetings,
        "summary": {
            "total_rows": total_rows,
            "present_rows": present_rows,
            "late_rows": late_rows,
            "absent_rows": absent_rows,
            "unknown_rows": unknown_rows,
            "avg_minutes": avg_minutes,
        },
        "top_people": top_people,
        "low_people": low_people,
        "unknown_board": unknown_board,
        "meeting_compare": list(by_meeting.values())[:20],
    }


def export_csv_bytes(rows):
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow([
        "Meeting Topic", "Meeting ID", "Meeting Start", "Participant", "Email", "Member", "Host",
        "First Join", "Last Leave", "Duration (Min)", "Rejoins", "Final Status"
    ])
    for r in rows:
        writer.writerow([
            r.get("topic") or "",
            r.get("meeting_id") or "",
            fmt_dt(r.get("start_time")),
            r.get("participant_name") or "",
            r.get("participant_email") or "",
            "Yes" if r.get("is_member") else "No",
            "Yes" if r.get("is_host") else "No",
            fmt_dt(r.get("first_join")),
            fmt_dt(r.get("last_leave")),
            round((r.get("total_seconds") or 0) / 60, 2),
            r.get("rejoin_count") or 0,
            r.get("final_status") or "-",
        ])
    return out.getvalue().encode("utf-8")


def export_pdf_bytes(title, rows, summary):
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter)
    styles = getSampleStyleSheet()
    elements = [Paragraph(f"<b>{title}</b>", styles["Title"]), Spacer(1, 12)]
    elements.append(Paragraph(f"Generated: {fmt_dt(now_local())}", styles["Normal"]))
    elements.append(Paragraph(
        f"Total: {summary['total_rows']} | Present: {summary['present_rows']} | Late: {summary['late_rows']} | Absent: {summary['absent_rows']} | Unknown: {summary['unknown_rows']}",
        styles["Normal"],
    ))
    elements.append(Spacer(1, 12))
    data = [["Topic", "Participant", "Member", "Duration", "Rejoins", "Status"]]
    for r in rows[:120]:
        data.append([
            (r.get("topic") or "")[:18],
            (r.get("participant_name") or "")[:16],
            "Yes" if r.get("is_member") else "No",
            str(round((r.get("total_seconds") or 0) / 60, 2)),
            str(r.get("rejoin_count") or 0),
            r.get("final_status") or "-",
        ])
    table = Table(data, repeatRows=1)
    style = TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1f2937")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
    ])
    for i in range(1, len(data)):
        status = data[i][5]
        if status == "PRESENT":
            style.add("TEXTCOLOR", (5, i), (5, i), colors.green)
        elif status == "LATE":
            style.add("TEXTCOLOR", (5, i), (5, i), colors.orange)
        else:
            style.add("TEXTCOLOR", (5, i), (5, i), colors.red)
    table.setStyle(style)
    elements.append(table)
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
        :root {
            --bg1:#eef4ff; --bg2:#f8fbff; --nav:#0f172a; --primary:#2563eb; --ok:#16a34a; --warn:#f59e0b; --danger:#dc2626; --muted:#64748b;
        }
        * { box-sizing:border-box; }
        body { margin:0; font-family:Arial, sans-serif; background:linear-gradient(135deg,var(--bg1),var(--bg2)); color:#0f172a; }
        .topbar { background:var(--nav); color:white; padding:14px 22px; display:flex; justify-content:space-between; align-items:center; position:sticky; top:0; z-index:20; }
        .brand { font-size:20px; font-weight:700; }
        .wrap { display:flex; min-height:calc(100vh - 56px); }
        .sidebar { width:240px; background:rgba(255,255,255,0.82); backdrop-filter: blur(8px); padding:18px; border-right:1px solid #dbe5f0; }
        .sidebar a { display:block; padding:12px 14px; color:#0f172a; text-decoration:none; border-radius:14px; margin-bottom:8px; font-weight:600; }
        .sidebar a:hover, .sidebar a.active { background:#dbeafe; color:#1d4ed8; }
        .content { flex:1; padding:24px; }
        .grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); gap:16px; }
        .card { background:rgba(255,255,255,0.92); border-radius:22px; padding:18px; box-shadow:0 10px 25px rgba(15,23,42,.08); border:1px solid rgba(255,255,255,0.7); }
        .card h3, .card h4 { margin:0 0 10px 0; }
        .metric { font-size:30px; font-weight:800; margin-top:8px; }
        .muted { color:var(--muted); font-size:13px; }
        .row { display:flex; gap:12px; flex-wrap:wrap; }
        .badge { display:inline-block; padding:6px 10px; border-radius:999px; font-size:12px; font-weight:700; }
        .ok { background:#dcfce7; color:#166534; }
        .warn { background:#fef3c7; color:#92400e; }
        .danger { background:#fee2e2; color:#991b1b; }
        .info { background:#dbeafe; color:#1d4ed8; }
        table { width:100%; border-collapse:collapse; background:white; border-radius:14px; overflow:hidden; }
        th, td { padding:10px 12px; border-bottom:1px solid #e5e7eb; text-align:left; font-size:14px; }
        th { background:#eff6ff; position:sticky; top:56px; }
        input, select, textarea { width:100%; padding:10px 12px; border-radius:12px; border:1px solid #cbd5e1; margin-top:6px; margin-bottom:10px; background:white; }
        button, .btn { background:var(--primary); color:white; border:none; padding:10px 14px; border-radius:12px; cursor:pointer; font-weight:700; text-decoration:none; display:inline-block; }
        .btn.secondary { background:#334155; }
        .btn.success { background:var(--ok); }
        .btn.warn { background:var(--warn); color:black; }
        .btn.danger { background:var(--danger); }
        .flash { padding:12px 14px; border-radius:14px; margin-bottom:12px; font-weight:700; border:1px solid transparent; }
        .flash.success { background:#dcfce7; color:#166534; border-color:#86efac; }
        .flash.error { background:#fee2e2; color:#991b1b; border-color:#fca5a5; }
        .login-box { max-width:420px; margin:80px auto; }
        .small { font-size:12px; }
        .section-title { margin:0 0 14px 0; }
        .login-error {
            background:#fee2e2;
            color:#991b1b;
            border:1px solid #fca5a5;
            border-radius:12px;
            padding:10px 12px;
            margin:10px 0 14px 0;
            font-weight:700;
        }
        .debug-box {
            background:#fff7ed;
            color:#9a3412;
            border:1px solid #fdba74;
            border-radius:12px;
            padding:14px;
            white-space:pre-wrap;
            word-break:break-word;
            font-family:monospace;
        }
    </style>
</head>
<body>
{% if session.get('user_id') %}
<div class="topbar">
    <div class="brand">Zoom Attendance Platform</div>
    <div>
        <span class="badge info">{{ session.get('username') }} ({{ session.get('role') }})</span>
        <a class="btn secondary" href="{{ url_for('logout') }}">Logout</a>
    </div>
</div>
<div class="wrap">
    <div class="sidebar">
        {% for item in nav %}
            <a href="{{ item.href }}" class="{% if item.key == active %}active{% endif %}">{{ item.label }}</a>
        {% endfor %}
    </div>
    <div class="content">
        {% with messages = get_flashed_messages(with_categories=true) %}
            {% for category, message in messages %}
                <div class="flash {{ category }}">{{ message }}</div>
            {% endfor %}
        {% endwith %}
        {{ body|safe }}
    </div>
</div>
{% else %}
<div class="content">
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
    ]
    return render_template_string(BASE_HTML, title=title, body=body, nav=nav, active=active)


@app.before_request
def startup_once():
    global DB_INITIALIZED
    if not DB_INITIALIZED:
        init_db()
        DB_INITIALIZED = True


@app.errorhandler(Exception)
def handle_any_error(e):
    body = render_template_string(
        """
        <div class="card">
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
                    "SELECT * FROM users WHERE username=%s AND is_active=TRUE",
                    (username,),
                )
                user = cur.fetchone()

        if user and user["password_hash"] == hash_password(password):
            session["user_id"] = user["id"]
            session["username"] = user["username"]
            session["role"] = user["role"]
            log_activity("login", f"{username} logged in")
            return redirect(url_for("home"))

        login_error = "Invalid username or password"
        flash("Invalid username or password", "error")

    body = render_template_string(
        """
        <div class='login-box card'>
            <h2>Login</h2>
            <p class='muted'>Use your admin or viewer account.</p>

            {% if login_error %}
                <div class='login-error'>{{ login_error }}</div>
            {% endif %}

            <form method='post'>
                <label>Username</label>
                <input name='username' required value='{{ request.form.get("username", "") if request.method == "POST" else "" }}'>
                <label>Password</label>
                <input type='password' name='password' required>
                <button type='submit'>Login</button>
            </form>
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


@app.route("/home")
@login_required
def home():
    live_info = read_live_snapshot()
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS c FROM meetings")
            total_meetings = cur.fetchone()["c"]
            cur.execute("SELECT COUNT(*) AS c FROM members")
            total_members = cur.fetchone()["c"]
            cur.execute("SELECT COUNT(*) AS c FROM members WHERE active=TRUE")
            active_members = cur.fetchone()["c"]
            cur.execute("SELECT COUNT(*) AS c FROM attendance WHERE final_status='PRESENT'")
            present = cur.fetchone()["c"]
            cur.execute("SELECT COUNT(*) AS c FROM attendance WHERE final_status='LATE'")
            late = cur.fetchone()["c"]
            cur.execute("SELECT COUNT(*) AS c FROM attendance WHERE final_status='ABSENT'")
            absent = cur.fetchone()["c"]
            cur.execute("SELECT * FROM meetings ORDER BY id DESC LIMIT 5")
            recent_meetings = cur.fetchall()
            cur.execute("SELECT * FROM activity_log ORDER BY id DESC LIMIT 8")
            recent_activity = cur.fetchall()

    total_classified = present + late + absent
    health = round(((present + late) / total_classified) * 100, 2) if total_classified else 0
    body = render_template_string(
        """
        <h2 class='section-title'>Platform Overview</h2>
        <div class='grid'>
            <div class='card'><h4>Total Meetings</h4><div class='metric'>{{ total_meetings }}</div></div>
            <div class='card'><h4>Active Members</h4><div class='metric'>{{ active_members }}</div><div class='muted'>Total members: {{ total_members }}</div></div>
            <div class='card'><h4>Attendance Health</h4><div class='metric'>{{ health }}%</div><div class='muted'>Present + Late across finalized records</div></div>
            <div class='card'><h4>Live Status</h4><div class='metric'>{{ 'LIVE' if live_info else 'IDLE' }}</div><div class='muted'>Current meeting monitoring</div></div>
        </div>
        <br>
        <div class='grid'>
            <div class='card'>
                <h3>Latest Meeting Spotlight</h3>
                {% if recent_meetings %}
                    <div><b>{{ recent_meetings[0].topic or 'Untitled Meeting' }}</b></div>
                    <div class='muted'>{{ fmt_dt(recent_meetings[0].start_time) }}</div>
                    <div class='row' style='margin-top:12px'>
                        <span class='badge ok'>Present {{ recent_meetings[0].present_count }}</span>
                        <span class='badge warn'>Late {{ recent_meetings[0].late_count }}</span>
                        <span class='badge danger'>Absent {{ recent_meetings[0].absent_count }}</span>
                        <span class='badge info'>Unknown {{ recent_meetings[0].unknown_participants }}</span>
                    </div>
                {% else %}
                    <div class='muted'>No meetings yet.</div>
                {% endif %}
            </div>
            <div class='card'>
                <h3>Quick Actions</h3>
                <div class='row'>
                    <a class='btn' href='{{ url_for("live") }}'>Open Live Dashboard</a>
                    <a class='btn success' href='{{ url_for("analytics") }}'>Open Analytics</a>
                    <a class='btn secondary' href='{{ url_for("meetings") }}'>Open Meetings</a>
                </div>
            </div>
        </div>
        <br>
        <div class='grid'>
            <div class='card'>
                <h3>Recent Meetings</h3>
                <table>
                    <tr><th>Date</th><th>Topic</th><th>Status</th><th>Participants</th></tr>
                    {% for m in recent_meetings %}
                        <tr>
                            <td>{{ fmt_dt(m.start_time) }}</td>
                            <td>{{ m.topic }}</td>
                            <td>{{ m.status }}</td>
                            <td>{{ m.unique_participants }}</td>
                        </tr>
                    {% endfor %}
                </table>
            </div>
            <div class='card'>
                <h3>Recent Activity</h3>
                <table>
                    <tr><th>When</th><th>Action</th><th>Details</th></tr>
                    {% for a in recent_activity %}
                        <tr>
                            <td>{{ fmt_dt(a.created_at) }}</td>
                            <td>{{ a.action }}</td>
                            <td>{{ a.details }}</td>
                        </tr>
                    {% endfor %}
                </table>
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
    )
    return page("Home", body, "home")


@app.route("/live")
@login_required
def live():
    info = read_live_snapshot()
    if not info:
        body = "<div class='card'><h2>Live Dashboard</h2><p class='muted'>No live meeting is active right now.</p></div>"
        return page("Live", body, "live")

    meeting = info["meeting"]
    participants = info["participants"]
    active_now = info["active_now"]
    not_joined = info["not_joined_members"]
    joined_only_count = len(participants)
    host_now = "Yes" if any(p.get("is_host") and p.get("current_join") is not None for p in participants) else "No"

    body = render_template_string(
        """
        <meta http-equiv='refresh' content='15'>
        <h2 class='section-title'>Live Dashboard</h2>
        <div class='grid'>
            <div class='card'><h4>Topic</h4><div class='metric' style='font-size:22px'>{{ meeting.topic or 'Untitled Meeting' }}</div></div>
            <div class='card'><h4>Meeting ID</h4><div class='metric'>{{ meeting.meeting_id or '-' }}</div></div>
            <div class='card'><h4>Joined Only Count</h4><div class='metric'>{{ joined_only_count }}</div></div>
            <div class='card'><h4>Active Now</h4><div class='metric'>{{ active_now|length }}</div><div class='muted'>Host present: {{ host_now }}</div></div>
        </div>
        <br>
        <div class='grid'>
            <div class='card'>
                <h3>Live Participants</h3>
                <table>
                    <tr><th>Name</th><th>Type</th><th>Status</th><th>Duration (Min)</th><th>Rejoins</th></tr>
                    {% for p in participants %}
                        <tr>
                            <td>{{ p.participant_name }}</td>
                            <td>{% if p.is_host %}<span class='badge info'>Host</span>{% elif p.is_member %}<span class='badge ok'>Member</span>{% else %}<span class='badge warn'>Unknown</span>{% endif %}</td>
                            <td>{% if p.current_join %}<span class='badge ok'>Live</span>{% else %}<span class='badge danger'>Left</span>{% endif %}</td>
                            <td>{{ ((p.total_seconds or 0)/60)|round(2) }}</td>
                            <td>{{ p.rejoin_count or 0 }}</td>
                        </tr>
                    {% endfor %}
                </table>
            </div>
            <div class='card'>
                <h3>Active Members Not Yet Joined</h3>
                {% if not_joined %}
                <table>
                    <tr><th>Name</th><th>Email</th></tr>
                    {% for m in not_joined %}
                        <tr><td>{{ m.full_name }}</td><td>{{ m.email or '-' }}</td></tr>
                    {% endfor %}
                </table>
                {% else %}
                    <div class='muted'>All active members have joined or there are no active members.</div>
                {% endif %}
            </div>
        </div>
        """,
        meeting=meeting,
        participants=participants,
        active_now=active_now,
        not_joined=not_joined,
        joined_only_count=joined_only_count,
        host_now=host_now,
    )
    return page("Live", body, "live")


@app.route("/members", methods=["GET", "POST"])
@login_required
def members():
    if request.method == "POST":
        action = request.form.get("action")
        if action == "add" and session.get("role") == "admin":
            full_name = request.form.get("full_name", "").strip()
            email = request.form.get("email", "").strip() or None
            phone = request.form.get("phone", "").strip() or None
            tags = request.form.get("tags", "").strip() or None
            if full_name:
                with db() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            "INSERT INTO members(full_name, email, phone, tags, active) VALUES (%s,%s,%s,%s,TRUE)",
                            (full_name, email, phone, tags),
                        )
                    conn.commit()
                log_activity("member_add", full_name)
                flash("Member added successfully.", "success")
        elif action == "toggle" and session.get("role") == "admin":
            member_id = int(request.form.get("member_id"))
            with db() as conn:
                with conn.cursor() as cur:
                    cur.execute("UPDATE members SET active = NOT active WHERE id=%s", (member_id,))
                conn.commit()
            log_activity("member_toggle", str(member_id))
            flash("Member status updated.", "success")
        elif action == "import_csv" and session.get("role") == "admin":
            file = request.files.get("csv_file")
            imported = 0
            if file:
                stream = io.StringIO(file.stream.read().decode("utf-8"))
                reader = csv.DictReader(stream)
                with db() as conn:
                    with conn.cursor() as cur:
                        for row in reader:
                            name = (row.get("full_name") or row.get("name") or "").strip()
                            if not name:
                                continue
                            email = (row.get("email") or "").strip() or None
                            phone = (row.get("phone") or "").strip() or None
                            tags = (row.get("tags") or "").strip() or None
                            cur.execute(
                                "INSERT INTO members(full_name, email, phone, tags, active) VALUES (%s,%s,%s,%s,TRUE)",
                                (name, email, phone, tags),
                            )
                            imported += 1
                    conn.commit()
                log_activity("member_import", f"Imported {imported} members")
                flash(f"Imported {imported} members.", "success")
        return redirect(url_for("members"))

    q = request.args.get("q", "").strip().lower()
    with db() as conn:
        with conn.cursor() as cur:
            if q:
                cur.execute(
                    "SELECT * FROM members WHERE lower(full_name) LIKE %s OR lower(COALESCE(email,'')) LIKE %s ORDER BY active DESC, full_name",
                    (f"%{q}%", f"%{q}%"),
                )
            else:
                cur.execute("SELECT * FROM members ORDER BY active DESC, full_name")
            rows = cur.fetchall()

    body = render_template_string(
        """
        <h2 class='section-title'>Members</h2>
        <div class='grid'>
            <div class='card'>
                <h3>Add Member</h3>
                {% if session.get('role') == 'admin' %}
                <form method='post'>
                    <input type='hidden' name='action' value='add'>
                    <label>Full Name</label><input name='full_name' required>
                    <label>Email</label><input name='email'>
                    <label>Phone</label><input name='phone'>
                    <label>Tags</label><input name='tags' placeholder='team, batch, group'>
                    <button type='submit'>Save Member</button>
                </form>
                {% else %}<div class='muted'>Viewer can only view members.</div>{% endif %}
            </div>
            <div class='card'>
                <h3>CSV Import</h3>
                <div class='muted'>Expected columns: full_name, email, phone, tags</div>
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
            <form method='get'>
                <label>Search Member</label>
                <input name='q' value='{{ q }}' placeholder='Search by name or email'>
                <button type='submit'>Search</button>
            </form>
            <table>
                <tr><th>Name</th><th>Email</th><th>Phone</th><th>Tags</th><th>Status</th>{% if session.get('role') == 'admin' %}<th>Action</th>{% endif %}</tr>
                {% for m in rows %}
                    <tr>
                        <td>{{ m.full_name }}</td>
                        <td>{{ m.email or '-' }}</td>
                        <td>{{ m.phone or '-' }}</td>
                        <td>{{ m.tags or '-' }}</td>
                        <td>{% if m.active %}<span class='badge ok'>Active</span>{% else %}<span class='badge danger'>Inactive</span>{% endif %}</td>
                        {% if session.get('role') == 'admin' %}
                        <td>
                            <form method='post'>
                                <input type='hidden' name='action' value='toggle'>
                                <input type='hidden' name='member_id' value='{{ m.id }}'>
                                <button type='submit' class='btn warn'>Toggle</button>
                            </form>
                        </td>
                        {% endif %}
                    </tr>
                {% endfor %}
            </table>
        </div>
        """,
        rows=rows,
        q=q,
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
                    with conn.cursor() as cur:
                        cur.execute(
                            "INSERT INTO users(username, password_hash, role, is_active) VALUES (%s,%s,%s,TRUE)",
                            (username, hash_password(password), role),
                        )
                    conn.commit()
                log_activity("user_add", username)
                flash("User created.", "success")
        elif action == "toggle":
            user_id = int(request.form.get("user_id"))
            with db() as conn:
                with conn.cursor() as cur:
                    cur.execute("UPDATE users SET is_active = NOT is_active WHERE id=%s", (user_id,))
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
        return redirect(url_for("users"))

    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM users ORDER BY id DESC")
            rows = cur.fetchall()

    body = render_template_string(
        """
        <h2 class='section-title'>Users & Roles</h2>
        <div class='grid'>
            <div class='card'>
                <h3>Create User</h3>
                <form method='post'>
                    <input type='hidden' name='action' value='add'>
                    <label>Username</label><input name='username' required>
                    <label>Password</label><input name='password' required>
                    <label>Role</label>
                    <select name='role'><option value='viewer'>viewer</option><option value='admin'>admin</option></select>
                    <button type='submit'>Create</button>
                </form>
            </div>
            <div class='card'>
                <h3>Role Guide</h3>
                <div class='muted'>Admin can manage members, users, settings, imports. Viewer can safely view live, meetings, analytics, and reports.</div>
            </div>
        </div>
        <br>
        <div class='card'>
            <table>
                <tr><th>Username</th><th>Role</th><th>Status</th><th>Created</th><th>Toggle</th><th>Change Password</th></tr>
                {% for u in rows %}
                <tr>
                    <td>{{ u.username }}</td>
                    <td>{{ u.role }}</td>
                    <td>{% if u.is_active %}<span class='badge ok'>Active</span>{% else %}<span class='badge danger'>Disabled</span>{% endif %}</td>
                    <td>{{ fmt_dt(u.created_at) }}</td>
                    <td>
                        <form method='post'>
                            <input type='hidden' name='action' value='toggle'>
                            <input type='hidden' name='user_id' value='{{ u.id }}'>
                            <button class='btn warn' type='submit'>Toggle</button>
                        </form>
                    </td>
                    <td>
                        <form method='post'>
                            <input type='hidden' name='action' value='password'>
                            <input type='hidden' name='user_id' value='{{ u.id }}'>
                            <input name='new_password' placeholder='new password' required>
                            <button class='btn secondary' type='submit'>Update</button>
                        </form>
                    </td>
                </tr>
                {% endfor %}
            </table>
        </div>
        """,
        rows=rows,
        fmt_dt=fmt_dt,
    )
    return page("Users", body, "users")


@app.route("/analytics")
@login_required
def analytics():
    filters = {
        "from_date": request.args.get("from_date", ""),
        "to_date": request.args.get("to_date", ""),
        "meeting_uuid": request.args.get("meeting_uuid", ""),
        "member_id": request.args.get("member_id", ""),
        "person_name": request.args.get("person_name", ""),
        "participant_type": request.args.get("participant_type", "all"),
    }
    data = analytics_data(filters)
    chart_labels = [p["name"] for p in data["top_people"]]
    chart_values = [round(p["minutes"], 2) for p in data["top_people"]]
    body = render_template_string(
        """
        <h2 class='section-title'>Analytics</h2>
        <div class='card'>
            <form method='get'>
                <div class='grid'>
                    <div><label>From Date</label><input type='date' name='from_date' value='{{ filters.from_date }}'></div>
                    <div><label>To Date</label><input type='date' name='to_date' value='{{ filters.to_date }}'></div>
                    <div><label>Meeting</label>
                        <select name='meeting_uuid'>
                            <option value=''>All meetings</option>
                            {% for m in data.meetings %}
                            <option value='{{ m.meeting_uuid }}' {% if filters.meeting_uuid == m.meeting_uuid %}selected{% endif %}>{{ m.topic or 'Untitled Meeting' }} - {{ fmt_dt(m.start_time) }}</option>
                            {% endfor %}
                        </select>
                    </div>
                    <div><label>Member</label>
                        <select name='member_id'>
                            <option value=''>All members</option>
                            {% for m in data.members %}
                            <option value='{{ m.id }}' {% if filters.member_id == (m.id|string) %}selected{% endif %}>{{ m.full_name }}</option>
                            {% endfor %}
                        </select>
                    </div>
                    <div><label>Person Search</label><input name='person_name' value='{{ filters.person_name }}' placeholder='type any participant name'></div>
                    <div><label>Participant Type</label>
                        <select name='participant_type'>
                            <option value='all' {% if filters.participant_type == 'all' %}selected{% endif %}>All</option>
                            <option value='member' {% if filters.participant_type == 'member' %}selected{% endif %}>Members</option>
                            <option value='unknown' {% if filters.participant_type == 'unknown' %}selected{% endif %}>Unknown / non-member</option>
                        </select>
                    </div>
                </div>
                <button type='submit'>Apply Filters</button>
                <a class='btn success' href='{{ url_for("export_analytics_csv", **filters) }}'>Export CSV</a>
                <a class='btn secondary' href='{{ url_for("export_analytics_pdf", **filters) }}'>Export PDF</a>
            </form>
        </div>
        <br>
        <div class='grid'>
            <div class='card'><h4>Total Rows</h4><div class='metric'>{{ data.summary.total_rows }}</div></div>
            <div class='card'><h4>Present</h4><div class='metric'>{{ data.summary.present_rows }}</div></div>
            <div class='card'><h4>Late</h4><div class='metric'>{{ data.summary.late_rows }}</div></div>
            <div class='card'><h4>Unknown</h4><div class='metric'>{{ data.summary.unknown_rows }}</div></div>
        </div>
        <br>
        <div class='grid'>
            <div class='card'>
                <h3>Top Attendance Minutes</h3>
                <canvas id='topChart'></canvas>
            </div>
            <div class='card'>
                <h3>Top Performers</h3>
                <table><tr><th>Name</th><th>Meetings</th><th>Minutes</th><th>Present</th></tr>
                {% for p in data.top_people %}
                    <tr><td>{{ p.name }}</td><td>{{ p.meetings }}</td><td>{{ p.minutes|round(2) }}</td><td>{{ p.present }}</td></tr>
                {% endfor %}
                </table>
            </div>
        </div>
        <br>
        <div class='grid'>
            <div class='card'>
                <h3>Low Performers</h3>
                <table><tr><th>Name</th><th>Present</th><th>Late</th><th>Absent</th></tr>
                {% for p in data.low_people %}
                    <tr><td>{{ p.name }}</td><td>{{ p.present }}</td><td>{{ p.late }}</td><td>{{ p.absent }}</td></tr>
                {% endfor %}
                </table>
            </div>
            <div class='card'>
                <h3>Unknown Participant Leaderboard</h3>
                <table><tr><th>Name</th><th>Meetings</th><th>Minutes</th></tr>
                {% for p in data.unknown_board %}
                    <tr><td>{{ p.name }}</td><td>{{ p.meetings }}</td><td>{{ p.minutes|round(2) }}</td></tr>
                {% endfor %}
                </table>
            </div>
        </div>
        <br>
        <div class='card'>
            <h3>Meeting Comparison</h3>
            <table>
                <tr><th>Meeting</th><th>Date</th><th>Present</th><th>Late</th><th>Absent</th><th>Unknown</th></tr>
                {% for m in data.meeting_compare %}
                    <tr><td>{{ m.topic }}</td><td>{{ fmt_dt(m.start_time) }}</td><td>{{ m.present }}</td><td>{{ m.late }}</td><td>{{ m.absent }}</td><td>{{ m.unknown }}</td></tr>
                {% endfor %}
            </table>
        </div>
        <br>
        <div class='card'>
            <h3>Filtered Attendance Rows</h3>
            <table>
                <tr><th>Topic</th><th>Participant</th><th>Member</th><th>Duration</th><th>Rejoins</th><th>Status</th></tr>
                {% for r in data.rows[:150] %}
                    <tr>
                        <td>{{ r.topic }}</td>
                        <td>{{ r.participant_name }}</td>
                        <td>{% if r.is_member %}Yes{% else %}No{% endif %}</td>
                        <td>{{ ((r.total_seconds or 0)/60)|round(2) }}</td>
                        <td>{{ r.rejoin_count or 0 }}</td>
                        <td>{{ r.final_status }}</td>
                    </tr>
                {% endfor %}
            </table>
        </div>
        <script>
        new Chart(document.getElementById('topChart'), {
            type:'bar',
            data:{labels: {{ chart_labels|tojson }}, datasets:[{label:'Minutes', data: {{ chart_values|tojson }} }]},
            options:{responsive:true, plugins:{legend:{display:false}}}
        });
        </script>
        """,
        data=data,
        filters=filters,
        chart_labels=chart_labels,
        chart_values=chart_values,
        fmt_dt=fmt_dt,
    )
    return page("Analytics", body, "analytics")


@app.route("/analytics/export.csv")
@login_required
def export_analytics_csv():
    data = analytics_data(dict(request.args))
    content = export_csv_bytes(data["rows"])
    filename = f"analytics_{slugify(now_local().strftime('%Y%m%d_%H%M%S'))}.csv"
    return Response(content, mimetype="text/csv", headers={"Content-Disposition": f"attachment; filename={filename}"})


@app.route("/analytics/export.pdf")
@login_required
def export_analytics_pdf():
    data = analytics_data(dict(request.args))
    pdf = export_pdf_bytes("Filtered Analytics Report", data["rows"], data["summary"])
    return send_file(io.BytesIO(pdf), download_name="analytics_report.pdf", mimetype="application/pdf", as_attachment=True)


@app.route("/meetings")
@login_required
def meetings():
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM meetings ORDER BY id DESC LIMIT 200")
            rows = cur.fetchall()
    body = render_template_string(
        """
        <h2 class='section-title'>Meetings</h2>
        <div class='card'>
            <table>
                <tr><th>Date</th><th>Topic</th><th>Status</th><th>Participants</th><th>Members</th><th>Unknown</th><th>Reports</th></tr>
                {% for m in rows %}
                <tr>
                    <td>{{ fmt_dt(m.start_time) }}</td>
                    <td>{{ m.topic or 'Untitled Meeting' }}</td>
                    <td>{{ m.status }}</td>
                    <td>{{ m.unique_participants }}</td>
                    <td>{{ m.member_participants }}</td>
                    <td>{{ m.unknown_participants }}</td>
                    <td>
                        <a class='btn success' href='{{ url_for("meeting_csv", meeting_uuid=m.meeting_uuid) }}'>CSV</a>
                        <a class='btn secondary' href='{{ url_for("meeting_pdf", meeting_uuid=m.meeting_uuid) }}'>PDF</a>
                    </td>
                </tr>
                {% endfor %}
            </table>
        </div>
        """,
        rows=rows,
        fmt_dt=fmt_dt,
    )
    return page("Meetings", body, "meetings")


@app.route("/meetings/<meeting_uuid>/report.csv")
@login_required
def meeting_csv(meeting_uuid):
    data = analytics_data({"meeting_uuid": meeting_uuid})
    content = export_csv_bytes(data["rows"])
    return Response(content, mimetype="text/csv", headers={"Content-Disposition": f"attachment; filename={slugify(meeting_uuid)}.csv"})


@app.route("/meetings/<meeting_uuid>/report.pdf")
@login_required
def meeting_pdf(meeting_uuid):
    data = analytics_data({"meeting_uuid": meeting_uuid})
    pdf = export_pdf_bytes("Meeting Report", data["rows"], data["summary"])
    return send_file(io.BytesIO(pdf), download_name=f"{slugify(meeting_uuid)}.pdf", mimetype="application/pdf", as_attachment=True)


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
        <h2 class='section-title'>Settings</h2>
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
            cur.execute("SELECT * FROM activity_log ORDER BY id DESC LIMIT 200")
            rows = cur.fetchall()
    body = render_template_string(
        """
        <h2 class='section-title'>Activity Log</h2>
        <div class='card'>
            <table>
                <tr><th>Time</th><th>User</th><th>Action</th><th>Details</th></tr>
                {% for a in rows %}
                <tr><td>{{ fmt_dt(a.created_at) }}</td><td>{{ a.username or '-' }}</td><td>{{ a.action }}</td><td>{{ a.details }}</td></tr>
                {% endfor %}
            </table>
        </div>
        """,
        rows=rows,
        fmt_dt=fmt_dt,
    )
    return page("Activity", body, "activity")


@app.route("/health")
def health():
    return jsonify({"ok": True, "time": fmt_dt(now_local())})


@app.route("/zoom/webhook", methods=["POST"])
def zoom_webhook():
    payload = request.get_json(force=True, silent=True) or {}

    if payload.get("event") == "endpoint.url_validation":
        plain = payload.get("payload", {}).get("plainToken", "")
        encrypted = hmac.new(ZOOM_SECRET_TOKEN.encode("utf-8"), plain.encode("utf-8"), hashlib.sha256).hexdigest() if ZOOM_SECRET_TOKEN else ""
        return jsonify({"plainToken": plain, "encryptedToken": encrypted})

    if not verify_zoom_signature(request):
        return jsonify({"message": "invalid signature"}), 401

    event = payload.get("event")
    obj = payload.get("payload", {}).get("object", {})
    participant = payload.get("payload", {}).get("object", {}).get("participant", {}) or payload.get("payload", {}).get("object", {})

    if event == "meeting.started":
        meeting = ensure_meeting(obj)
        log_activity("zoom_started", meeting["meeting_uuid"])
        return jsonify({"ok": True})

    if event in ("meeting.participant_joined", "meeting.participant_left"):
        meeting = ensure_meeting(obj)
        meeting_uuid = meeting["meeting_uuid"]

        event_raw = (
            participant.get("join_time")
            or participant.get("leave_time")
            or (
                datetime.fromtimestamp(payload.get("event_ts") / 1000, tz=ZoneInfo(TIMEZONE_NAME)).isoformat()
                if isinstance(payload.get("event_ts"), (int, float))
                else None
            )
        )
        event_time = parse_dt(event_raw) or now_local()

        participant_name = participant.get("user_name") or participant.get("participant_user_name") or participant.get("name") or "Unknown Participant"
        participant_email = participant.get("email") or participant.get("user_email") or None
        update_participant(meeting_uuid, participant_name, participant_email, event_time, "join" if event.endswith("joined") else "leave")
        log_activity("zoom_participant_event", f"{event} :: {participant_name}")
        return jsonify({"ok": True})

    if event in ("meeting.ended", "meeting.end"):
        meeting = ensure_meeting(obj)
        finalized = finalize_meeting(meeting["meeting_uuid"], parse_dt(obj.get("end_time")) or now_local())
        log_activity("zoom_meeting_ended", meeting["meeting_uuid"])
        return jsonify({"ok": True, "finalized": bool(finalized)})

    return jsonify({"ok": True, "ignored": event})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=True)