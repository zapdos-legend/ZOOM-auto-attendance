print("APP STARTED")

import hashlib
import hmac
import os
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from flask import (
    Flask,
    flash,
    jsonify,
    redirect,
    render_template_string,
    request,
    send_from_directory,
    url_for,
)

from config import (
    HOST_NAME_HINT,
    INACTIVITY_CONFIRM_SECONDS,
    OUTPUT_FOLDER,
    PRESENT_PERCENTAGE,
    TIMEZONE_NAME,
    ZOOM_SECRET_TOKEN,
)
from modules.attendance import (
    build_attendee_rows,
    close_open_sessions,
    meeting_info,
    participants,
    process_join,
    process_leave,
    reset_runtime_state,
)
from modules.db import (
    add_member,
    delete_meeting,
    get_active_member_lookup,
    get_attendance_for_meeting,
    get_dashboard_analytics,
    get_members,
    get_recent_meetings,
    init_db,
    remove_member,
    save_meeting_and_attendance,
    set_member_active,
)
from modules.notifier import send_email_with_attachment_wrapper, send_whatsapp_report
from modules.report_generator import generate_reports

app = Flask(__name__)
app.secret_key = "attendance-partc-secret"

print("SERVER STARTING...")

os.makedirs("data", exist_ok=True)
os.makedirs("attendance_reports", exist_ok=True)

try:
    init_db()
    print("✅ DB Initialized")
except Exception as e:
    print("❌ DB Error:", e)

IST = ZoneInfo(TIMEZONE_NAME)
state_lock = threading.Lock()


def parse_zoom_time(timestr):
    return datetime.strptime(timestr, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=ZoneInfo("UTC")).astimezone(IST)


def now_ist():
    return datetime.now(IST)


def get_live_rows_for_dashboard():
    rows = []
    current_time = now_ist()

    for name, p in participants.items():
        total_seconds = p.get("total_seconds", 0.0)
        if p.get("current_join") is not None:
            extra = (current_time - p["current_join"]).total_seconds()
            if extra > 0:
                total_seconds += extra

        rows.append({
            "name": name,
            "status": "LIVE" if p.get("current_join") else "LEFT",
            "duration_minutes": round(total_seconds / 60.0, 2),
            "rejoins": p.get("rejoin_count", 0),
            "is_host": p.get("is_host", False),
        })

    rows.sort(key=lambda x: x["duration_minutes"], reverse=True)
    return rows


def finalize_meeting_after_delay(expected_uuid):
    time.sleep(INACTIVITY_CONFIRM_SECONDS)

    with state_lock:
        if not meeting_info.get("pending_end"):
            return

        if meeting_info.get("current_uuid") != expected_uuid:
            return

        if meeting_info.get("report_generated"):
            return

        last_activity = meeting_info.get("last_activity_time")
        if last_activity and (now_ist() - last_activity).total_seconds() < INACTIVITY_CONFIRM_SECONDS:
            return

        start_time = meeting_info.get("start_time")
        end_time = meeting_info.get("end_time_candidate") or meeting_info.get("end_time")

        if not start_time or not end_time:
            print("⚠️ Missing start or end time, skipping finalize.")
            return

        close_open_sessions(end_time)

        total_minutes = max(round((end_time - start_time).total_seconds() / 60.0, 2), 0.0)
        rows = build_attendee_rows(total_minutes, PRESENT_PERCENTAGE, HOST_NAME_HINT)

        member_lookup = get_active_member_lookup()
        present_names = {row["name"].strip().lower() for row in rows}

        for member_key, member in member_lookup.items():
            if member_key not in present_names:
                rows.append({
                    "name": member["name"],
                    "email": member["email"],
                    "join_time_str": "-",
                    "leave_time_str": "-",
                    "duration_minutes": 0.0,
                    "rejoins": 0,
                    "status": "ABSENT",
                    "is_member": 1,
                    "is_host": 0,
                })

        for row in rows:
            if row["name"].strip().lower() in member_lookup:
                row["is_member"] = 1

        rows.sort(key=lambda x: (x["is_member"], x["duration_minutes"]), reverse=True)

        meeting_meta = {
            "zoom_meeting_id": str(meeting_info.get("meeting_id", "")),
            "topic": meeting_info.get("topic", "Meeting"),
            "meeting_date": start_time.strftime("%d-%m-%Y"),
            "start_time": start_time.strftime("%H:%M:%S"),
            "end_time": end_time.strftime("%H:%M:%S"),
            "total_minutes": total_minutes,
        }

        csv_file, pdf_file = generate_reports(rows, meeting_meta)
        save_meeting_and_attendance(meeting_meta, rows, csv_file, pdf_file)

        subject = f"Attendance Report - {meeting_meta['topic']}"
        body = (
            f"Meeting Topic: {meeting_meta['topic']}\n"
            f"Meeting ID: {meeting_meta['zoom_meeting_id']}\n"
            f"Date: {meeting_meta['meeting_date']}\n"
            f"Start Time: {meeting_meta['start_time']}\n"
            f"End Time: {meeting_meta['end_time']}\n"
            f"Total Duration: {meeting_meta['total_minutes']} minutes\n"
        )
        send_email_with_attachment_wrapper(subject, body, [csv_file, pdf_file])

        for row in rows:
            if row["is_member"] == 1 and row["status"] == "ABSENT" and row.get("email"):
                absent_subject = f"Absent Notice - {meeting_meta['topic']}"
                absent_body = (
                    f"Hello {row['name']},\n\n"
                    f"You were absent or below the required attendance threshold for today's meeting.\n\n"
                    f"Topic: {meeting_meta['topic']}\n"
                    f"Date: {meeting_meta['meeting_date']}\n"
                    f"Required attendance: {PRESENT_PERCENTAGE}% of the meeting duration.\n"
                )
                send_email_with_attachment_wrapper(absent_subject, absent_body, [], row["email"])

        top5 = [r for r in rows if r["is_host"] == 0][:5]
        top_text = "\n".join([f"{i+1}) {r['name']} - {r['duration_minutes']} min" for i, r in enumerate(top5)]) or "No attendees"

        send_whatsapp_report(
            f"📌 Meeting Summary\n"
            f"Topic: {meeting_meta['topic']}\n"
            f"Date: {meeting_meta['meeting_date']}\n"
            f"Start: {meeting_meta['start_time']}\n"
            f"End: {meeting_meta['end_time']}\n\n"
            f"🏆 Top Attendees:\n{top_text}"
        )

        meeting_info["end_time"] = end_time
        meeting_info["report_generated"] = True
        meeting_info["pending_end"] = False

        print(f"✅ Attendance finalized for meeting {meeting_meta['zoom_meeting_id']}")

        reset_runtime_state()


@app.route("/")
def home():
    return (
        '<h2>✅ Zoom Attendance System Running</h2>'
        '<p><a href="/dashboard">Open Dashboard</a></p>',
        200,
    )


@app.route("/dashboard")
def dashboard():
    try:
        live_rows = get_live_rows_for_dashboard()
        members = get_members()
        meetings = get_recent_meetings(limit=30)
        analytics = get_dashboard_analytics(HOST_NAME_HINT)

        present_count = analytics["status_counts"].get("PRESENT", 0)
        absent_count = analytics["status_counts"].get("ABSENT", 0)

        html = """
        <!doctype html>
        <html>
        <head>
            <title>Zoom Attendance Dashboard</title>
            <style>
                body { font-family: Arial, sans-serif; margin: 20px; background: #f7f7f7; }
                h1, h2, h3 { margin-bottom: 8px; }
                .card { background: white; border-radius: 12px; padding: 16px; margin-bottom: 18px; box-shadow: 0 2px 8px rgba(0,0,0,0.08); }
                .grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; }
                .metric { background: #fafafa; border: 1px solid #ddd; border-radius: 10px; padding: 12px; }
                table { width: 100%; border-collapse: collapse; margin-top: 8px; background: white; }
                th, td { border: 1px solid #ddd; padding: 8px; text-align: center; }
                th { background: #efefef; }
                .row { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
                input[type=text], input[type=email] { width: 100%; padding: 8px; margin-bottom: 8px; box-sizing: border-box; }
                button, .refresh-btn { padding: 8px 12px; cursor: pointer; text-decoration: none; display: inline-block; }
                .actions form { display: inline-block; margin: 0 4px; }
                .small { color: #555; font-size: 14px; }
                .topbar { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; }
                .refresh-btn { background: #0d6efd; color: white; border-radius: 8px; }
            </style>
        </head>
        <body>
            <div class="topbar">
                <div>
                    <h1>📊 Zoom Attendance Dashboard</h1>
                    <p class="small">Permanent dashboard link: /dashboard</p>
                </div>
                <div>
                    <a class="refresh-btn" href="{{ url_for('dashboard') }}">Refresh Dashboard</a>
                </div>
            </div>

            {% with messages = get_flashed_messages() %}
              {% if messages %}
                <div class="card">
                  {% for msg in messages %}
                    <p>{{ msg }}</p>
                  {% endfor %}
                </div>
              {% endif %}
            {% endwith %}

            <div class="card">
                <h2>🟢 Live Attendance</h2>
                <table>
                    <tr>
                        <th>Name</th>
                        <th>Status</th>
                        <th>Duration (Min)</th>
                        <th>Rejoins</th>
                        <th>Host</th>
                    </tr>
                    {% for row in live_rows %}
                    <tr>
                        <td>{{ row.name }}</td>
                        <td>{{ row.status }}</td>
                        <td>{{ row.duration_minutes }}</td>
                        <td>{{ row.rejoins }}</td>
                        <td>{{ 'Yes' if row.is_host else 'No' }}</td>
                    </tr>
                    {% endfor %}
                    {% if not live_rows %}
                    <tr><td colspan="5">No live attendees right now.</td></tr>
                    {% endif %}
                </table>
            </div>

            <div class="row">
                <div class="card">
                    <h2>👥 Members</h2>
                    <form method="POST" action="{{ url_for('add_member_route') }}">
                        <input type="text" name="name" placeholder="Name" required>
                        <input type="email" name="email" placeholder="Email">
                        <input type="text" name="whatsapp" placeholder="WhatsApp">
                        <button type="submit">Add Member</button>
                    </form>

                    <table>
                        <tr>
                            <th>ID</th>
                            <th>Name</th>
                            <th>Email</th>
                            <th>Active</th>
                            <th>Action</th>
                        </tr>
                        {% for m in members %}
                        <tr>
                            <td>{{ m[0] }}</td>
                            <td>{{ m[1] }}</td>
                            <td>{{ m[2] }}</td>
                            <td>{{ 'Yes' if m[4] == 1 else 'No' }}</td>
                            <td class="actions">
                                <form method="POST" action="{{ url_for('toggle_member_route', member_id=m[0]) }}">
                                    <input type="hidden" name="active" value="{{ 0 if m[4] == 1 else 1 }}">
                                    <button type="submit">{{ 'Deactivate' if m[4] == 1 else 'Activate' }}</button>
                                </form>
                                <form method="POST" action="{{ url_for('remove_member_route', member_id=m[0]) }}">
                                    <button type="submit">Delete</button>
                                </form>
                            </td>
                        </tr>
                        {% endfor %}
                        {% if not members %}
                        <tr><td colspan="5">No members added yet.</td></tr>
                        {% endif %}
                    </table>

                    <h3>📩 Manual Reminder</h3>
                    <form method="POST" action="{{ url_for('send_reminder_route') }}">
                        <input type="text" name="topic" placeholder="Meeting Topic" required>
                        <input type="text" name="meeting_time" placeholder="Meeting Time e.g. 06:00 PM" required>
                        <button type="submit">Send Reminder Now</button>
                    </form>
                </div>

                <div class="card">
                    <h2>📈 Analytics</h2>
                    <div class="grid">
                        <div class="metric"><b>Total Meetings</b><br>{{ analytics.total_meetings }}</div>
                        <div class="metric"><b>Active Members</b><br>{{ analytics.active_members }}</div>
                        <div class="metric"><b>Present Records</b><br>{{ present_count }}</div>
                        <div class="metric"><b>Absent Records</b><br>{{ absent_count }}</div>
                    </div>

                    <h3>🏆 Top Attendees</h3>
                    <table>
                        <tr><th>Name</th><th>Total Minutes</th></tr>
                        {% for t in analytics.top_rows %}
                        <tr>
                            <td>{{ t[0] }}</td>
                            <td>{{ t[1] }}</td>
                        </tr>
                        {% endfor %}
                        {% if not analytics.top_rows %}
                        <tr><td colspan="2">No data yet.</td></tr>
                        {% endif %}
                    </table>
                </div>
            </div>

            <div class="card">
                <h2>📂 Recent Meetings</h2>
                <table>
                    <tr>
                        <th>ID</th>
                        <th>Topic</th>
                        <th>Date</th>
                        <th>Start</th>
                        <th>End</th>
                        <th>Total Min</th>
                        <th>CSV</th>
                        <th>PDF</th>
                        <th>View</th>
                        <th>Delete</th>
                    </tr>
                    {% for mt in meetings %}
                    <tr>
                        <td>{{ mt[0] }}</td>
                        <td>{{ mt[2] }}</td>
                        <td>{{ mt[3] }}</td>
                        <td>{{ mt[4] }}</td>
                        <td>{{ mt[5] }}</td>
                        <td>{{ mt[6] }}</td>
                        <td><a href="{{ url_for('download_report', filename=mt[7]) }}" target="_blank">CSV</a></td>
                        <td><a href="{{ url_for('download_report', filename=mt[8]) }}" target="_blank">PDF</a></td>
                        <td><a href="{{ url_for('meeting_detail', meeting_pk=mt[0]) }}">Open</a></td>
                        <td>
                            <form method="POST" action="{{ url_for('delete_meeting_route', meeting_pk=mt[0]) }}">
                                <button type="submit">Delete</button>
                            </form>
                        </td>
                    </tr>
                    {% endfor %}
                    {% if not meetings %}
                    <tr><td colspan="10">No meetings saved yet.</td></tr>
                    {% endif %}
                </table>
            </div>
        </body>
        </html>
        """
        return render_template_string(
            html,
            live_rows=live_rows,
            members=members,
            meetings=meetings,
            analytics=analytics,
            present_count=present_count,
            absent_count=absent_count,
        )
    except Exception as e:
        return f"<h2>Dashboard Error:</h2><pre>{str(e)}</pre>"


@app.route("/meeting/<int:meeting_pk>")
def meeting_detail(meeting_pk):
    rows = get_attendance_for_meeting(meeting_pk)
    html = """
    <h2>Meeting Attendance Detail</h2>
    <p><a href="{{ url_for('dashboard') }}">Back to Dashboard</a></p>
    <table border="1" cellpadding="8" cellspacing="0">
        <tr>
            <th>Name</th><th>Join</th><th>Leave</th><th>Duration</th><th>Rejoins</th><th>Status</th><th>Member</th><th>Host</th>
        </tr>
        {% for r in rows %}
        <tr>
            <td>{{ r[0] }}</td>
            <td>{{ r[1] }}</td>
            <td>{{ r[2] }}</td>
            <td>{{ r[3] }}</td>
            <td>{{ r[4] }}</td>
            <td>{{ r[5] }}</td>
            <td>{{ 'Yes' if r[6] == 1 else 'No' }}</td>
            <td>{{ 'Yes' if r[7] == 1 else 'No' }}</td>
        </tr>
        {% endfor %}
    </table>
    """
    return render_template_string(html, rows=rows)


@app.route("/reports/<path:filename>")
def download_report(filename):
    return send_from_directory(OUTPUT_FOLDER, filename, as_attachment=True)


@app.route("/members/add", methods=["POST"])
def add_member_route():
    name = request.form.get("name", "").strip()
    email = request.form.get("email", "").strip()
    whatsapp = request.form.get("whatsapp", "").strip()

    if name:
        add_member(name, email, whatsapp, 1)
        flash(f"✅ Member added: {name}")
    else:
        flash("❌ Name is required.")

    return redirect(url_for("dashboard"))


@app.route("/members/remove/<int:member_id>", methods=["POST"])
def remove_member_route(member_id):
    remove_member(member_id)
    flash("🗑 Member removed.")
    return redirect(url_for("dashboard"))


@app.route("/members/toggle/<int:member_id>", methods=["POST"])
def toggle_member_route(member_id):
    active = int(request.form.get("active", "1"))
    set_member_active(member_id, active)
    flash("✅ Member status updated.")
    return redirect(url_for("dashboard"))


@app.route("/meetings/delete/<int:meeting_pk>", methods=["POST"])
def delete_meeting_route(meeting_pk):
    files = delete_meeting(meeting_pk)
    if files:
        csv_file, pdf_file = files
        for file_name in [csv_file, pdf_file]:
            if file_name:
                path = os.path.join(OUTPUT_FOLDER, file_name)
                if os.path.exists(path):
                    os.remove(path)
    flash("🗑 Meeting data deleted.")
    return redirect(url_for("dashboard"))


@app.route("/send-reminder", methods=["POST"])
def send_reminder_route():
    topic = request.form.get("topic", "").strip()
    meeting_time = request.form.get("meeting_time", "").strip()

    if not topic or not meeting_time:
        flash("❌ Topic and meeting time are required.")
        return redirect(url_for("dashboard"))

    members = get_members(active_only=True)
    count = 0

    for _, name, email, _, _ in members:
        if email:
            subject = f"Meeting Reminder - {topic}"
            body = (
                f"Hello {name},\n\n"
                f"This is a reminder for your upcoming meeting.\n\n"
                f"Topic: {topic}\n"
                f"Meeting Time: {meeting_time}\n\n"
                f"Please join on time."
            )
            send_email_with_attachment_wrapper(subject, body, [], email)
            count += 1

    send_whatsapp_report(f"📩 Reminder sent for meeting '{topic}' at {meeting_time}")
    flash(f"✅ Reminder sent to {count} active members by email.")
    return redirect(url_for("dashboard"))


@app.route("/zoom/webhook", methods=["POST"])
def zoom_webhook():
    data = request.get_json(silent=True) or {}
    event = data.get("event")

    print("Webhook Event:", event)

    if event == "endpoint.url_validation":
        plain_token = data.get("payload", {}).get("plainToken", "")
        encrypted_token = hmac.new(
            ZOOM_SECRET_TOKEN.encode(),
            plain_token.encode(),
            hashlib.sha256,
        ).hexdigest()

        return jsonify({
            "plainToken": plain_token,
            "encryptedToken": encrypted_token,
        }), 200

    obj = data.get("payload", {}).get("object", {})
    meeting_info["last_activity_time"] = now_ist()

    if event == "meeting.participant_joined":
        with state_lock:
            meeting_info["pending_end"] = False
            meeting_info["report_generated"] = False
            meeting_info["topic"] = obj.get("topic", meeting_info.get("topic", ""))
            meeting_info["meeting_id"] = str(obj.get("id", meeting_info.get("meeting_id", "")))
            meeting_info["host_id"] = str(obj.get("host_id", meeting_info.get("host_id", "")))
            meeting_info["current_uuid"] = obj.get("uuid", meeting_info.get("current_uuid", ""))

            if meeting_info.get("start_time") is None and obj.get("start_time"):
                meeting_info["start_time"] = parse_zoom_time(obj["start_time"])

            participant = obj.get("participant", {})
            name = participant.get("user_name", "Unknown")
            email = participant.get("email", "")
            participant_user_id = str(
                participant.get("participant_user_id")
                or participant.get("id")
                or participant.get("user_id")
                or ""
            )
            join_time = parse_zoom_time(participant["join_time"]) if participant.get("join_time") else now_ist()

            is_host = participant_user_id == meeting_info.get("host_id", "")
            process_join(name, join_time, participant_user_id, email, is_host)

    elif event == "meeting.participant_left":
        with state_lock:
            meeting_info["pending_end"] = False
            participant = obj.get("participant", {})
            name = participant.get("user_name", "Unknown")
            leave_time = parse_zoom_time(participant["leave_time"]) if participant.get("leave_time") else now_ist()
            process_leave(name, leave_time)

    elif event == "meeting.ended":
        with state_lock:
            meeting_info["topic"] = obj.get("topic", meeting_info.get("topic", ""))
            meeting_info["meeting_id"] = str(obj.get("id", meeting_info.get("meeting_id", "")))
            meeting_info["host_id"] = str(obj.get("host_id", meeting_info.get("host_id", "")))
            meeting_info["current_uuid"] = obj.get("uuid", meeting_info.get("current_uuid", ""))

            if obj.get("start_time"):
                meeting_info["start_time"] = parse_zoom_time(obj["start_time"])
            if obj.get("end_time"):
                meeting_info["end_time_candidate"] = parse_zoom_time(obj["end_time"])

            meeting_info["pending_end"] = True

            t = threading.Thread(
                target=finalize_meeting_after_delay,
                args=(meeting_info["current_uuid"],),
                daemon=True,
            )
            t.start()

    return jsonify({"status": "ok"}), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)