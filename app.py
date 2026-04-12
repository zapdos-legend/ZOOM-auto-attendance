print("APP STARTED")

from flask import Flask, request, jsonify
from datetime import datetime, timedelta
import os
import json

from modules.attendance import process_join, process_leave, participants, meeting_info
from modules.report_generator import generate_reports
from modules.notifier import send_email_with_attachment_wrapper, send_whatsapp_report
from modules.db import init_db, save_attendance_to_db

from config import LIVE_DATA_FILE, IST_OFFSET_HOURS, IST_OFFSET_MINUTES, OUTPUT_FOLDER

app = Flask(__name__)

print("SERVER STARTING...")

# Initialize DB
init_db()

IST_OFFSET = timedelta(hours=IST_OFFSET_HOURS, minutes=IST_OFFSET_MINUTES)


def parse_zoom_time(timestr):
    utc_time = datetime.strptime(timestr, "%Y-%m-%dT%H:%M:%SZ")
    return utc_time + IST_OFFSET


def save_live_data():
    os.makedirs(os.path.dirname(LIVE_DATA_FILE), exist_ok=True)

    live_data = {
        "meeting": {
            "topic": meeting_info.get("topic"),
            "meeting_id": meeting_info.get("meeting_id"),
            "start_time": str(meeting_info.get("start_time")) if meeting_info.get("start_time") else None,
            "end_time": str(meeting_info.get("end_time")) if meeting_info.get("end_time") else None,
        },
        "participants": {},
    }

    for name, p in participants.items():
        live_data["participants"][name] = {
            "first_join": str(p.get("first_join")) if p.get("first_join") else None,
            "last_leave": str(p.get("last_leave")) if p.get("last_leave") else None,
            "total_seconds": p.get("total_seconds", 0),
            "current_join": str(p.get("current_join")) if p.get("current_join") else None,
            "rejoin_count": p.get("rejoin_count", 0),
            "status": p.get("status", ""),
        }

    with open(LIVE_DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(live_data, f, indent=4)


@app.route("/")
def home():
    return "Zoom Attendance System Running ✅", 200


@app.route("/zoom/webhook", methods=["POST"])
def zoom_webhook():
    data = request.get_json(silent=True) or {}
    event = data.get("event")

    print("Webhook Event:", event)

    # Zoom endpoint validation
    if event == "endpoint.url_validation":
        plain_token = data.get("payload", {}).get("plainToken", "")
        return jsonify({
            "plainToken": plain_token
        }), 200

    obj = data.get("payload", {}).get("object", {})

    if event == "meeting.participant_joined":
        participant = obj.get("participant", {})
        name = participant.get("user_name", "Unknown")
        join_time_raw = participant.get("join_time")

        if join_time_raw:
            join_time = parse_zoom_time(join_time_raw)
        else:
            join_time = datetime.utcnow() + IST_OFFSET

        meeting_info["topic"] = obj.get("topic", "")
        meeting_info["meeting_id"] = obj.get("id", "")

        if meeting_info.get("start_time") is None and obj.get("start_time"):
            meeting_info["start_time"] = parse_zoom_time(obj.get("start_time"))

        process_join(name, join_time, meeting_info.get("start_time"))
        print(f"✅ Joined: {name}")
        save_live_data()

    elif event == "meeting.participant_left":
        participant = obj.get("participant", {})
        name = participant.get("user_name", "Unknown")
        leave_time_raw = participant.get("leave_time")

        if leave_time_raw:
            leave_time = parse_zoom_time(leave_time_raw)
        else:
            leave_time = datetime.utcnow() + IST_OFFSET

        process_leave(name, leave_time)
        print(f"❌ Left: {name}")
        save_live_data()

    elif event == "meeting.ended":
        meeting_info["topic"] = obj.get("topic", "")
        meeting_info["meeting_id"] = obj.get("id", "")

        if obj.get("start_time"):
            meeting_info["start_time"] = parse_zoom_time(obj.get("start_time"))
        if obj.get("end_time"):
            meeting_info["end_time"] = parse_zoom_time(obj.get("end_time"))

        # Close any still-open participant sessions at meeting end
        final_end = meeting_info.get("end_time")
        if final_end:
            for name, p in participants.items():
                current_join = p.get("current_join")
                if current_join is not None:
                    extra_seconds = (final_end - current_join).total_seconds()
                    if extra_seconds > 0:
                        p["total_seconds"] = p.get("total_seconds", 0) + extra_seconds
                    p["last_leave"] = final_end
                    p["current_join"] = None

        csv_file, pdf_file = generate_reports(participants, meeting_info, OUTPUT_FOLDER)

        print(f"✅ CSV Saved: {csv_file}")
        print(f"✅ PDF Saved: {pdf_file}")

        # Save attendance into DB
        for name, p in participants.items():
            duration_minutes = round(p.get("total_seconds", 0) / 60, 2)
            save_attendance_to_db(
                name,
                str(p.get("first_join")) if p.get("first_join") else "",
                str(p.get("last_leave")) if p.get("last_leave") else "",
                duration_minutes
            )

        subject = f"Attendance Report - {meeting_info.get('topic', '')}"
        body = f"""Meeting Topic: {meeting_info.get('topic', '')}
Meeting ID: {meeting_info.get('meeting_id', '')}
Start Time: {meeting_info.get('start_time')}
End Time: {meeting_info.get('end_time')}
"""

        send_email_with_attachment_wrapper(subject, body, [csv_file, pdf_file])
        send_whatsapp_report("✅ Attendance Report Generated!")

        save_live_data()

    return jsonify({"status": "ok"}), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)