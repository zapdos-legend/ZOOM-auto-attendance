print("APP STARTED")

from flask import Flask, request, jsonify
from datetime import datetime, timedelta
import os, json

# ✅ Import your modules
from modules.attendance import process_join, process_leave, participants, meeting_info
from modules.report_generator import generate_reports
from modules.notifier import send_email_with_attachment, send_whatsapp_report
from modules.db import init_db, save_attendance_to_db

from config import LIVE_DATA_FILE, IST_OFFSET_HOURS, IST_OFFSET_MINUTES, OUTPUT_FOLDER

app = Flask(__name__)

print("SERVER STARTING...")

# ✅ Initialize DB (important for Render)
init_db()

IST_OFFSET = timedelta(hours=IST_OFFSET_HOURS, minutes=IST_OFFSET_MINUTES)


def parse_zoom_time(timestr):
    utc_time = datetime.strptime(timestr, "%Y-%m-%dT%H:%M:%SZ")
    return utc_time + IST_OFFSET


def save_live_data():
    os.makedirs("data", exist_ok=True)

    live_data = {
        "meeting": meeting_info.copy(),
        "participants": {}
    }

    for name, p in participants.items():
        live_data["participants"][name] = {
            "first_join": str(p["first_join"]) if p["first_join"] else None,
            "last_leave": str(p["last_leave"]) if p["last_leave"] else None,
            "total_seconds": p["total_seconds"],
            "current_join": str(p["current_join"]) if p["current_join"] else None,
            "status": p.get("status", "")
        }

    if live_data["meeting"]["start_time"]:
        live_data["meeting"]["start_time"] = str(live_data["meeting"]["start_time"])

    if live_data["meeting"]["end_time"]:
        live_data["meeting"]["end_time"] = str(live_data["meeting"]["end_time"])

    with open(LIVE_DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(live_data, f, indent=4)


@app.route("/")
def home():
    return "Zoom Attendance System Running ✅"


@app.route("/zoom/webhook", methods=["POST"])
def zoom_webhook():
    data = request.json
    event = data.get("event")

    print(f"\nWebhook Received: {event}")

    # ✅ Zoom validation
    if event == "endpoint.url_validation":
        plain_token = data["payload"]["plainToken"]
        return jsonify({"plainToken": plain_token})

    obj = data.get("payload", {}).get("object", {})

    # ✅ Participant Joined
    if event == "meeting.participant_joined":
        name = obj["participant"]["user_name"]
        join_time = parse_zoom_time(obj["participant"]["join_time"])

        meeting_info["topic"] = obj.get("topic", "")
        meeting_info["meeting_id"] = obj.get("id", "")

        if meeting_info["start_time"] is None:
            meeting_info["start_time"] = parse_zoom_time(obj.get("start_time"))

        process_join(name, join_time, meeting_info["start_time"])
        save_live_data()

    # ✅ Participant Left
    elif event == "meeting.participant_left":
        name = obj["participant"]["user_name"]
        leave_time = parse_zoom_time(obj["participant"]["leave_time"])

        process_leave(name, leave_time)
        save_live_data()

    # ✅ Meeting Ended
    elif event == "meeting.ended":
        meeting_info["topic"] = obj.get("topic", "")
        meeting_info["meeting_id"] = obj.get("id", "")

        meeting_info["start_time"] = parse_zoom_time(obj.get("start_time"))
        meeting_info["end_time"] = parse_zoom_time(obj.get("end_time"))

        csv_file, pdf_file = generate_reports(participants, meeting_info, OUTPUT_FOLDER)

        print(f"✅ CSV Saved: {csv_file}")
        print(f"✅ PDF Saved: {pdf_file}")

        # ✅ Save to DB
        for name, p in participants.items():
            duration = p["total_seconds"] / 60
            save_attendance_to_db(
                name,
                str(p["first_join"]),
                str(p["last_leave"]),
                duration
            )

        # ✅ Email + WhatsApp
        subject = f"Attendance Report - {meeting_info['topic']}"
        body = f"""
Meeting Topic: {meeting_info['topic']}
Meeting ID: {meeting_info['meeting_id']}
Start Time: {meeting_info['start_time']}
End Time: {meeting_info['end_time']}
"""

        send_email_with_attachment(subject, body, [csv_file, pdf_file])
        send_whatsapp_report("✅ Attendance Report Generated!")

        save_live_data()

    return jsonify({"status": "ok"}), 200


# ✅ IMPORTANT: Render PORT FIX
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)