from flask import Flask, request, jsonify
from datetime import datetime, timedelta
import os, json

from modules.attendance import process_join, process_leave, participants, meeting_info
from modules.report_generator import generate_reports
from modules.notifier import send_email_with_attachment, send_whatsapp_text
from modules.db import init_db, save_attendance_to_db, get_members
from config import LIVE_DATA_FILE, IST_OFFSET_HOURS, IST_OFFSET_MINUTES, OUTPUT_FOLDER

app = Flask(__name__)

IST_OFFSET = timedelta(hours=IST_OFFSET_HOURS, minutes=IST_OFFSET_MINUTES)

print("APP STARTED")
print("SERVER STARTING...")


def parse_zoom_time(timestr):
    utc_time = datetime.strptime(timestr, "%Y-%m-%dT%H:%M:%SZ")
    return utc_time + IST_OFFSET


def save_live_data():
    os.makedirs("data", exist_ok=True)

    live_data = {
        "meeting": {
            "topic": meeting_info["topic"],
            "meeting_id": meeting_info["meeting_id"],
            "start_time": str(meeting_info["start_time"]) if meeting_info["start_time"] else None,
            "end_time": str(meeting_info["end_time"]) if meeting_info["end_time"] else None
        },
        "participants": {}
    }

    for name, p in participants.items():
        live_data["participants"][name] = {
            "first_join": str(p["first_join"]) if p["first_join"] else None,
            "last_leave": str(p["last_leave"]) if p["last_leave"] else None,
            "total_seconds": p["total_seconds"],
            "current_join": str(p["current_join"]) if p["current_join"] else None,
            "status": p.get("status", ""),
            "rejoin_count": p.get("rejoin_count", 0)
        }

    with open(LIVE_DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(live_data, f, indent=4)


processed_meeting_end = set()


@app.route("/zoom/webhook", methods=["POST"])
def zoom_webhook():
    data = request.json
    event = data.get("event")

    print(f"\nWebhook Received: {event}")

    # Zoom URL Validation
    if event == "endpoint.url_validation":
        plain_token = data["payload"]["plainToken"]
        return jsonify({"plainToken": plain_token})

    obj = data.get("payload", {}).get("object", {})

    # Participant Joined
    if event == "meeting.participant_joined":
        name = obj["participant"]["user_name"]
        join_time = parse_zoom_time(obj["participant"]["join_time"])

        meeting_info["topic"] = obj.get("topic", "")
        meeting_info["meeting_id"] = obj.get("id", "")
        meeting_info["host_id"] = obj.get("host_id", "")

        if meeting_info["start_time"] is None:
            meeting_info["start_time"] = parse_zoom_time(obj.get("start_time"))

        process_join(name, join_time)

        print(f"✅ Joined: {name}")
        save_live_data()

    # Participant Left
    elif event == "meeting.participant_left":
        name = obj["participant"]["user_name"]
        leave_time = parse_zoom_time(obj["participant"]["leave_time"])

        process_leave(name, leave_time)

        print(f"❌ Left: {name}")
        save_live_data()

    # Meeting Ended
    elif event == "meeting.ended":
        meeting_uuid = obj.get("uuid", "")

        if meeting_uuid in processed_meeting_end:
            print("⚠️ Duplicate meeting.ended webhook ignored.")
            return jsonify({"status": "duplicate"}), 200

        processed_meeting_end.add(meeting_uuid)

        meeting_info["topic"] = obj.get("topic", "")
        meeting_info["meeting_id"] = obj.get("id", "")

        start_time = parse_zoom_time(obj.get("start_time"))
        end_time = parse_zoom_time(obj.get("end_time"))

        meeting_info["start_time"] = start_time
        meeting_info["end_time"] = end_time

        meeting_info["date"] = start_time.strftime("%d-%m-%Y")
        meeting_info["start_time_str"] = start_time.strftime("%H-%M-%S")
        meeting_info["end_time_str"] = end_time.strftime("%H-%M-%S")

        meeting_info["total_minutes"] = (end_time - start_time).total_seconds() / 60

        # If participant still joined, close them
        for name, p in participants.items():
            if p["current_join"] is not None:
                p["total_seconds"] += (end_time - p["current_join"]).total_seconds()
                p["current_join"] = None
                p["last_leave"] = end_time

        csv_file, pdf_file = generate_reports(participants, meeting_info, OUTPUT_FOLDER)

        print(f"✅ CSV Saved Successfully: {os.path.abspath(csv_file)}")
        print(f"✅ PDF Saved Successfully: {os.path.abspath(pdf_file)}")
        print("✅ Attendance Report Generated Successfully!")

        # Save to DB
        save_attendance_to_db(meeting_info, participants)

        # Send to all active members
        members = get_members(active_only=True)

        subject = f"Attendance Report - {meeting_info['topic']}"
        body = f"""Attendance report attached.

Meeting Topic: {meeting_info['topic']}
Meeting ID: {meeting_info['meeting_id']}
Date: {meeting_info['date']}
Start Time: {meeting_info['start_time_str']}
End Time: {meeting_info['end_time_str']}
"""

        for m in members:
            name, email, whatsapp, active = m
            if email:
                send_email_with_attachment(subject, body, email, [csv_file, pdf_file])

        # WhatsApp Summary (exclude host)
        top_list = sorted(participants.items(), key=lambda x: x[1]["total_seconds"], reverse=True)

        msg = f"""📌 Meeting Summary
📅 Date: {meeting_info['date']}
🕒 Start: {meeting_info['start_time_str']}
🕓 End: {meeting_info['end_time_str']}

🏆 Top 5 Attendees (Excluding Host):
"""

        count = 0
        for name, p in top_list:
            if name.lower().strip() == "akshay girase":
                continue
            count += 1
            msg += f"{count}) {name} - {round(p['total_seconds']/60,2)} min\n"
            if count == 5:
                break

        send_whatsapp_text(msg)

        save_live_data()

    return jsonify({"status": "ok"}), 200


if __name__ == "__main__":
    init_db()
    app.run(port=5000)