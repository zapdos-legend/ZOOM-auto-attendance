import os
import csv
import json
import hmac
import hashlib
import sqlite3
import threading
from datetime import datetime, timezone
from flask import Flask, request, jsonify, redirect, url_for, send_from_directory, flash, render_template_string
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer

# =========================================================
# CONFIG
# =========================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
REPORT_DIR = os.path.join(BASE_DIR, "attendance_reports")
DB_FILE = os.path.join(DATA_DIR, "zoom_attendance.db")
LIVE_STATE_FILE = os.path.join(DATA_DIR, "live_state.json")

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(REPORT_DIR, exist_ok=True)

FLASK_SECRET_KEY = os.environ.get("FLASK_SECRET_KEY", "12345")
PRESENT_PERCENTAGE = int(os.environ.get("PRESENT_PERCENTAGE", "75"))
INACTIVITY_CONFIRM_SECONDS = int(os.environ.get("INACTIVITY_CONFIRM_SECONDS", "120"))
ZOOM_SECRET_TOKEN = os.environ.get("ZOOM_SECRET_TOKEN", "your_zoom_secret_token")
HOST_NAME_HINT = os.environ.get("HOST_NAME_HINT", "Akshay").strip().lower()

app = Flask(__name__)
app.secret_key = FLASK_SECRET_KEY
FINALIZE_TIMERS = {}


# =========================================================
# TIME HELPERS
# =========================================================
def now_utc():
    return datetime.now(timezone.utc)


def parse_zoom_time(value):
    if not value:
        return None
    try:
        if isinstance(value, datetime):
            return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
        value = str(value)
        if value.endswith("Z"):
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        dt = datetime.fromisoformat(value)
        return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def fmt_dt(dt):
    if not dt:
        return ""
    return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S")


def fmt_date(dt):
    if not dt:
        return ""
    return dt.astimezone().strftime("%Y-%m-%d")


def fmt_time(dt):
    if not dt:
        return ""
    return dt.astimezone().strftime("%I:%M:%S %p")


# =========================================================
# DB
# =========================================================
def get_conn():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    cur = conn.cursor()

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
            FOREIGN KEY (meeting_pk) REFERENCES meetings(id) ON DELETE CASCADE
        )
    """)

    conn.commit()
    conn.close()


def get_members(active_only=False):
    conn = get_conn()
    cur = conn.cursor()
    if active_only:
        cur.execute("SELECT * FROM members WHERE active = 1 ORDER BY name")
    else:
        cur.execute("SELECT * FROM members ORDER BY name")
    rows = cur.fetchall()
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
    cur.execute("UPDATE members SET active = CASE WHEN active = 1 THEN 0 ELSE 1 END WHERE id = ?", (member_id,))
    conn.commit()
    conn.close()


def delete_member(member_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM members WHERE id = ?", (member_id,))
    conn.commit()
    conn.close()


def save_meeting_and_attendance(meeting_meta, rows, csv_file, pdf_file):
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        INSERT INTO meetings (
            zoom_meeting_id, topic, meeting_date, start_time, end_time,
            total_minutes, csv_file, pdf_file
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


def get_recent_meetings(limit=50):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM meetings ORDER BY id DESC LIMIT ?", (limit,))
    rows = cur.fetchall()
    conn.close()
    return rows


def get_meeting(meeting_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM meetings WHERE id = ?", (meeting_id,))
    row = cur.fetchone()
    conn.close()
    return row


def get_attendance_rows(meeting_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT * FROM attendance
        WHERE meeting_pk = ?
        ORDER BY is_host DESC, duration_minutes DESC, participant_name ASC
    """, (meeting_id,))
    rows = cur.fetchall()
    conn.close()
    return rows


def delete_meeting(meeting_id):
    meeting = get_meeting(meeting_id)
    if not meeting:
        return

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM attendance WHERE meeting_pk = ?", (meeting_id,))
    cur.execute("DELETE FROM meetings WHERE id = ?", (meeting_id,))
    conn.commit()
    conn.close()

    for fname in [meeting["csv_file"], meeting["pdf_file"]]:
        if fname:
            path = os.path.join(REPORT_DIR, fname)
            if os.path.exists(path):
                try:
                    os.remove(path)
                except Exception:
                    pass


def get_analytics():
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("SELECT COUNT(*) AS c FROM meetings")
    total_meetings = cur.fetchone()["c"]

    cur.execute("SELECT COUNT(*) AS c FROM members")
    total_members = cur.fetchone()["c"]

    cur.execute("SELECT COUNT(*) AS c FROM members WHERE active = 1")
    active_members = cur.fetchone()["c"]

    cur.execute("""
        SELECT status, COUNT(*) AS c
        FROM attendance
        WHERE is_member = 1 AND is_host = 0
        GROUP BY status
    """)
    status_counts = {row["status"]: row["c"] for row in cur.fetchall()}

    present_count = status_counts.get("PRESENT", 0)
    late_count = status_counts.get("LATE", 0)
    absent_count = status_counts.get("ABSENT", 0)

    total_member_marks = present_count + late_count + absent_count
    attendance_rate = round(((present_count + late_count) / total_member_marks) * 100, 2) if total_member_marks else 0.0

    cur.execute("""
        SELECT participant_name, ROUND(SUM(duration_minutes), 2) AS total_duration
        FROM attendance
        WHERE is_host = 0
        GROUP BY participant_name
        ORDER BY total_duration DESC
        LIMIT 10
    """)
    top_attendees = cur.fetchall()

    cur.execute("""
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
        ORDER BY total_duration DESC
    """)
    member_stats = cur.fetchall()

    cur.execute("""
        SELECT
            m.id,
            m.topic,
            m.meeting_date,
            ROUND(m.total_minutes, 2) AS total_minutes,
            SUM(CASE WHEN a.status='PRESENT' AND a.is_member=1 AND a.is_host=0 THEN 1 ELSE 0 END) AS present_count,
            SUM(CASE WHEN a.status='LATE' AND a.is_member=1 AND a.is_host=0 THEN 1 ELSE 0 END) AS late_count,
            SUM(CASE WHEN a.status='ABSENT' AND a.is_member=1 AND a.is_host=0 THEN 1 ELSE 0 END) AS absent_count
        FROM meetings m
        LEFT JOIN attendance a ON a.meeting_pk = m.id
        GROUP BY m.id
        ORDER BY m.id DESC
        LIMIT 10
    """)
    recent_stats = cur.fetchall()

    conn.close()
    return {
        "total_meetings": total_meetings,
        "total_members": total_members,
        "active_members": active_members,
        "present_count": present_count,
        "late_count": late_count,
        "absent_count": absent_count,
        "attendance_rate": attendance_rate,
        "top_attendees": top_attendees,
        "member_stats": member_stats,
        "recent_stats": recent_stats,
    }


# =========================================================
# LIVE STATE
# =========================================================
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
    lookup = {}
    for row in rows:
        lookup[row["name"].strip().lower()] = row
    return lookup


# =========================================================
# ZOOM SECURITY
# =========================================================
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
    return {
        "plainToken": plain_token,
        "encryptedToken": encrypted_token,
    }


# =========================================================
# ATTENDANCE LOGIC
# =========================================================
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
            "is_host": HOST_NAME_HINT in key.lower(),
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

    timer = threading.Timer(INACTIVITY_CONFIRM_SECONDS, finalize_meeting, args=[meeting_id])
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
        })

    actual_minutes = round(max(0, (ended_at - started_at).total_seconds()) / 60.0, 2)
    total_meeting_minutes = round(max(actual_minutes, max_participant_minutes), 2)
    threshold_minutes = round((PRESENT_PERCENTAGE / 100.0) * total_meeting_minutes, 2)

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
            })

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
        else:
            row["status"] = "PRESENT" if row["duration_minutes"] >= threshold_minutes else "LATE"

    rows.sort(key=lambda x: (
        x["is_host"] == 0,
        -x["duration_minutes"],
        x["name"].lower()
    ))

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_topic = "".join(c if c.isalnum() or c in ("_", "-") else "_" for c in meeting.get("topic", "meeting"))[:40]
    csv_file = os.path.join(REPORT_DIR, f"{safe_topic}_{stamp}.csv")
    pdf_file = os.path.join(REPORT_DIR, f"{safe_topic}_{stamp}.pdf")

    meeting_meta = {
        "zoom_meeting_id": str(meeting.get("zoom_meeting_id", "")),
        "topic": meeting.get("topic", "Untitled Meeting"),
        "meeting_date": fmt_date(started_at),
        "start_time": fmt_time(started_at),
        "end_time": fmt_time(ended_at),
        "total_minutes": total_meeting_minutes,
        "threshold_minutes": threshold_minutes,
    }

    generate_csv_report(csv_file, meeting_meta, rows)
    generate_pdf_report(pdf_file, meeting_meta, rows)
    save_meeting_and_attendance(meeting_meta, rows, csv_file, pdf_file)

    state["meeting"]["finalized"] = True
    save_live_state(state)
    reset_live_state()


# =========================================================
# REPORTS
# =========================================================
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
    doc = SimpleDocTemplate(file_path, pagesize=A4)
    styles = getSampleStyleSheet()
    story = []

    story.append(Paragraph("Attendance Report", styles["Title"]))
    story.append(Spacer(1, 12))

    story.append(Paragraph(f"<b>Topic:</b> {meeting_meta['topic']}", styles["Normal"]))
    story.append(Paragraph(f"<b>Meeting ID:</b> {meeting_meta['zoom_meeting_id']}", styles["Normal"]))
    story.append(Paragraph(f"<b>Date:</b> {meeting_meta['meeting_date']}", styles["Normal"]))
    story.append(Paragraph(f"<b>Start Time:</b> {meeting_meta['start_time']}", styles["Normal"]))
    story.append(Paragraph(f"<b>End Time:</b> {meeting_meta['end_time']}", styles["Normal"]))
    story.append(Spacer(1, 6))
    story.append(Paragraph(f"<b>Total Meeting Duration:</b> {meeting_meta['total_minutes']} minutes", styles["Normal"]))
    story.append(Spacer(1, 10))

    table_data = [["Name", "Join", "Leave", "Duration", "Rejoins", "Status"]]
    for row in rows:
        table_data.append([
            row["name"],
            row["join_time_str"],
            row["leave_time_str"],
            str(row["duration_minutes"]),
            str(row["rejoins"]),
            row["status"],
        ])

    table = Table(table_data, repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.black),
        ("GRID", (0, 0), (-1, -1), 0.7, colors.black),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    story.append(table)
    story.append(Spacer(1, 14))

    present_count = sum(1 for r in rows if r["status"] == "PRESENT")
    late_count = sum(1 for r in rows if r["status"] == "LATE")
    absent_count = sum(1 for r in rows if r["status"] == "ABSENT")

    note_box_data = [[Paragraph(
        f"""
        <b>■ Attendance Criteria</b><br/>
        ■ Present = Duration ≥ {PRESENT_PERCENTAGE}% of total meeting duration<br/>
        ■ Late = Duration &lt; {PRESENT_PERCENTAGE}% of total meeting duration<br/>
        ■ Absent = Did not join the meeting (for added members only)<br/><br/>
        <b>■ Present Threshold For This Meeting:</b> {meeting_meta['threshold_minutes']} minutes
        """,
        styles["Normal"]
    )]]

    note_box = Table(note_box_data, colWidths=[470])
    note_box.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 1.5, colors.black),
        ("PADDING", (0, 0), (-1, -1), 8),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))

    story.append(Paragraph(f"<b>Present Count:</b> {present_count}", styles["Normal"]))
    story.append(Paragraph(f"<b>Late Count:</b> {late_count}", styles["Normal"]))
    story.append(Paragraph(f"<b>Absent Count:</b> {absent_count}", styles["Normal"]))
    story.append(Spacer(1, 10))
    story.append(note_box)

    doc.build(story)


# =========================================================
# UI
# =========================================================
BASE_HTML = """
<!DOCTYPE html>
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
        .nav { margin-top:10px; }
        .nav a { color:#c7d2fe; text-decoration:none; margin-right:16px; font-weight:600; }
        .container { padding:20px; max-width:1200px; margin:auto; }
        .grid { display:grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap:16px; margin-bottom:20px; }
        .card { background:white; border-radius:16px; padding:18px; box-shadow:0 4px 18px rgba(0,0,0,0.08); margin-bottom:20px; }
        .metric .label { color:#6b7280; font-size:13px; margin-bottom:8px; }
        .metric .value { font-size:28px; font-weight:700; color:#111827; }
        table { width:100%; border-collapse: collapse; }
        th, td { border-bottom:1px solid #e5e7eb; padding:10px; text-align:left; font-size:14px; }
        th { background:#f9fafb; }
        .btn { display:inline-block; text-decoration:none; border:none; background:#2563eb; color:white; padding:8px 12px; border-radius:10px; cursor:pointer; font-size:13px; }
        .btn-secondary { background:#6b7280; }
        .btn-danger { background:#dc2626; }
        input { width:100%; box-sizing:border-box; padding:10px; margin-bottom:10px; border:1px solid #d1d5db; border-radius:10px; }
        .row { display:grid; grid-template-columns:1fr 1fr 1fr auto; gap:10px; align-items:end; }
        .tiny { color:#6b7280; font-size:12px; }
        .flash { background:#eef2ff; padding:12px; border-radius:10px; margin-bottom:14px; }
    </style>
</head>
<body>
    <div class="top">
        <h1>Zoom Attendance Platform</h1>
        <div class="nav">
            <a href="{{ url_for('dashboard_live') }}">Live</a>
            <a href="{{ url_for('dashboard_members') }}">Members</a>
            <a href="{{ url_for('dashboard_analytics') }}">Analytics</a>
            <a href="{{ url_for('dashboard_meetings') }}">Recent Meetings</a>
        </div>
    </div>
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


def render_page(title, content, auto_refresh=None):
    return render_template_string(BASE_HTML, title=title, content=content, auto_refresh=auto_refresh)


# =========================================================
# ROUTES
# =========================================================
@app.route("/")
def home():
    return redirect(url_for("dashboard_live"))


@app.route("/dashboard/live")
def dashboard_live():
    state = load_live_state()
    meeting = state["meeting"]
    participants = list(state["participants"].values())

    live_rows = []
    live_count = 0
    left_count = 0
    max_minutes = 0.0
    host_name = "-"

    processed = []
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

    for p in processed:
        max_minutes = max(max_minutes, p["minutes"])
        if p["status"] == "LIVE":
            live_count += 1
        else:
            left_count += 1
        if p["is_host"]:
            host_name = p["name"]

        live_rows.append(f"""
            <tr>
                <td>{p['name']}</td>
                <td>{p['status']}</td>
                <td>{p['minutes']}</td>
                <td>{p['rejoins']}</td>
                <td>{"Yes" if p['is_host'] else "No"}</td>
            </tr>
        """)

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
        <p class="tiny">Auto refresh every 2 seconds. Sorted by highest duration first.</p>
        <table>
            <tr>
                <th>Name</th>
                <th>Status</th>
                <th>Duration (Min)</th>
                <th>Rejoins</th>
                <th>Host</th>
            </tr>
            {''.join(live_rows) if live_rows else '<tr><td colspan="5">No participant data yet.</td></tr>'}
        </table>
    </div>
    """
    return render_page("Live Dashboard", content, auto_refresh=2)


@app.route("/dashboard/members")
def dashboard_members():
    members = get_members(active_only=False)

    member_rows = []
    for m in members:
        member_rows.append(f"""
            <tr>
                <td>{m['id']}</td>
                <td>{m['name']}</td>
                <td>{m['email'] or ''}</td>
                <td>{m['whatsapp'] or ''}</td>
                <td>{"Yes" if m['active'] else "No"}</td>
                <td>
                    <a class="btn btn-secondary" href="{url_for('member_toggle', member_id=m['id'])}">{"Deactivate" if m['active'] else "Activate"}</a>
                    <a class="btn btn-danger" href="{url_for('member_delete', member_id=m['id'])}" onclick="return confirm('Delete this member?')">Delete</a>
                </td>
            </tr>
        """)

    active_members = sum(1 for m in members if m["active"] == 1)

    content = f"""
    <div class="grid">
        <div class="card metric"><div class="label">Total Members</div><div class="value">{len(members)}</div></div>
        <div class="card metric"><div class="label">Active Members</div><div class="value">{active_members}</div></div>
    </div>

    <div class="card">
        <h2>Members</h2>
        <form method="POST" action="{url_for('member_add')}">
            <div class="row">
                <div><input type="text" name="name" placeholder="Name" required></div>
                <div><input type="text" name="email" placeholder="Email"></div>
                <div><input type="text" name="whatsapp" placeholder="WhatsApp"></div>
                <div><button class="btn" type="submit">Add / Update Member</button></div>
            </div>
        </form>
        <table>
            <tr>
                <th>ID</th>
                <th>Name</th>
                <th>Email</th>
                <th>WhatsApp</th>
                <th>Active</th>
                <th>Action</th>
            </tr>
            {''.join(member_rows) if member_rows else '<tr><td colspan="6">No members added yet.</td></tr>'}
        </table>
    </div>
    """
    return render_page("Members Dashboard", content)


@app.route("/members/add", methods=["POST"])
def member_add():
    try:
        add_or_update_member(
            request.form.get("name", ""),
            request.form.get("email", ""),
            request.form.get("whatsapp", ""),
        )
        flash("✅ Member added/updated successfully.", "ok")
    except Exception as e:
        flash(f"❌ {e}", "bad")
    return redirect(url_for("dashboard_members"))


@app.route("/members/toggle/<int:member_id>")
def member_toggle(member_id):
    toggle_member(member_id)
    flash("✅ Member status updated.", "ok")
    return redirect(url_for("dashboard_members"))


@app.route("/members/delete/<int:member_id>")
def member_delete(member_id):
    delete_member(member_id)
    flash("✅ Member deleted.", "ok")
    return redirect(url_for("dashboard_members"))


@app.route("/send-reminder", methods=["POST", "GET"])
def send_reminder_disabled():
    flash("ℹ️ Reminder module is disabled for now.", "ok")
    return redirect(url_for("dashboard_members"))


@app.route("/dashboard/analytics")
def dashboard_analytics():
    a = get_analytics()

    top_rows = []
    for row in a["top_attendees"]:
        top_rows.append(f"<tr><td>{row['participant_name']}</td><td>{row['total_duration']}</td></tr>")

    member_stat_rows = []
    for row in a["member_stats"]:
        member_stat_rows.append(
            f"<tr><td>{row['participant_name']}</td><td>{row['present_count']}</td><td>{row['late_count']}</td><td>{row['absent_count']}</td><td>{row['total_duration']}</td><td>{row['avg_duration']}</td><td>{row['attendance_percentage']}%</td></tr>"
        )

    recent_rows = []
    for row in a["recent_stats"]:
        recent_rows.append(
            f"<tr><td>{row['topic']}</td><td>{row['meeting_date']}</td><td>{row['total_minutes']}</td><td>{row['present_count'] or 0}</td><td>{row['late_count'] or 0}</td><td>{row['absent_count'] or 0}</td></tr>"
        )

    content = f"""
    <div class="grid">
        <div class="card metric"><div class="label">Total Meetings</div><div class="value">{a['total_meetings']}</div></div>
        <div class="card metric"><div class="label">Total Members</div><div class="value">{a['total_members']}</div></div>
        <div class="card metric"><div class="label">Active Members</div><div class="value">{a['active_members']}</div></div>
        <div class="card metric"><div class="label">Present Count</div><div class="value">{a['present_count']}</div></div>
        <div class="card metric"><div class="label">Late Count</div><div class="value">{a['late_count']}</div></div>
        <div class="card metric"><div class="label">Absent Count</div><div class="value">{a['absent_count']}</div></div>
        <div class="card metric"><div class="label">Attendance Rate</div><div class="value">{a['attendance_rate']}%</div></div>
    </div>

    <div class="card">
        <h2>Top Attendees</h2>
        <table>
            <tr><th>Name</th><th>Total Duration (Min)</th></tr>
            {''.join(top_rows) if top_rows else '<tr><td colspan="2">No data available.</td></tr>'}
        </table>
    </div>

    <div class="card">
        <h2>Member Performance Summary</h2>
        <p class="tiny">This section shows each added member's meeting performance: how many times they were Present, Late, Absent, their total duration, average duration, and overall attendance percentage.</p>
        <table>
            <tr>
                <th>Name</th><th>Present</th><th>Late</th><th>Absent</th><th>Total Duration</th><th>Avg Duration</th><th>Attendance %</th>
            </tr>
            {''.join(member_stat_rows) if member_stat_rows else '<tr><td colspan="7">No member analytics yet.</td></tr>'}
        </table>
    </div>

    <div class="card">
        <h2>Recent Meeting Summary</h2>
        <table>
            <tr>
                <th>Topic</th><th>Date</th><th>Total Minutes</th><th>Present</th><th>Late</th><th>Absent</th>
            </tr>
            {''.join(recent_rows) if recent_rows else '<tr><td colspan="6">No meetings yet.</td></tr>'}
        </table>
    </div>
    """
    return render_page("Analytics Dashboard", content)


@app.route("/dashboard/meetings")
def dashboard_meetings():
    meetings = get_recent_meetings()
    rows = []

    for m in meetings:
        csv_btn = f'<a class="btn" href="{url_for("download_report", filename=m["csv_file"])}">CSV</a>' if m["csv_file"] else ""
        pdf_btn = f'<a class="btn btn-secondary" href="{url_for("download_report", filename=m["pdf_file"])}">PDF</a>' if m["pdf_file"] else ""
        open_btn = f'<a class="btn" href="{url_for("meeting_detail", meeting_id=m["id"])}">Open</a>'
        delete_btn = f'<a class="btn btn-danger" href="{url_for("meeting_delete", meeting_id=m["id"])}" onclick="return confirm(\'Delete this meeting?\')">Delete</a>'
        rows.append(f"""
            <tr>
                <td>{m['id']}</td>
                <td>{m['topic']}</td>
                <td>{m['meeting_date']}</td>
                <td>{m['start_time']}</td>
                <td>{m['end_time']}</td>
                <td>{round(m['total_minutes'] or 0, 2)}</td>
                <td>{csv_btn} {pdf_btn}</td>
                <td>{open_btn} {delete_btn}</td>
            </tr>
        """)

    content = f"""
    <div class="card">
        <h2>Recent Meetings</h2>
        <table>
            <tr>
                <th>ID</th><th>Topic</th><th>Date</th><th>Start</th><th>End</th><th>Total Minutes</th><th>Reports</th><th>Action</th>
            </tr>
            {''.join(rows) if rows else '<tr><td colspan="8">No meetings saved yet.</td></tr>'}
        </table>
    </div>
    """
    return render_page("Meetings Dashboard", content)


@app.route("/meeting/<int:meeting_id>")
def meeting_detail(meeting_id):
    meeting = get_meeting(meeting_id)
    if not meeting:
        return "Meeting not found", 404

    rows = get_attendance_rows(meeting_id)
    tr = []
    for r in rows:
        tr.append(f"""
            <tr>
                <td>{r['participant_name']}</td>
                <td>{r['join_time'] or '-'}</td>
                <td>{r['leave_time'] or '-'}</td>
                <td>{r['duration_minutes']}</td>
                <td>{r['rejoins']}</td>
                <td>{r['status']}</td>
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
        <p><b>Start:</b> {meeting['start_time']}</p>
        <p><b>End:</b> {meeting['end_time']}</p>
        <p><b>Total Duration:</b> {round(meeting['total_minutes'] or 0, 2)} minutes</p>
    </div>

    <div class="card">
        <table>
            <tr>
                <th>Name</th><th>Join Time</th><th>Leave Time</th><th>Minutes</th><th>Rejoins</th><th>Status</th><th>Member</th><th>Host</th>
            </tr>
            {''.join(tr) if tr else '<tr><td colspan="8">No attendance rows.</td></tr>'}
        </table>
    </div>
    """
    return render_page("Meeting Detail", content)


@app.route("/meeting/<int:meeting_id>/delete")
def meeting_delete(meeting_id):
    delete_meeting(meeting_id)
    flash("✅ Meeting deleted successfully.", "ok")
    return redirect(url_for("dashboard_meetings"))


@app.route("/report/<path:filename>")
def download_report(filename):
    return send_from_directory(REPORT_DIR, filename, as_attachment=True)


# =========================================================
# ZOOM WEBHOOK
# =========================================================
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
init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port, debug=False)