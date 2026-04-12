print("APP STARTED")

from flask import Flask, request, jsonify
from datetime import datetime, timedelta
import os, json, hashlib, hmac

from modules.attendance import process_join, process_leave, participants, meeting_info
from modules.report_generator import generate_reports
from modules.notifier import send_email_with_attachment_wrapper, send_whatsapp_report
from modules.db import init_db, save_attendance_to_db

from config import LIVE_DATA_FILE, IST_OFFSET_HOURS, IST_OFFSET_MINUTES, OUTPUT_FOLDER

app = Flask(__name__)

print("SERVER STARTING...")

init_db()

IST_OFFSET = timedelta(hours=IST_OFFSET_HOURS, minutes=IST_OFFSET_MINUTES)

# 🔐 IMPORTANT: Zoom Secret Token (copy from Zoom UI)
ZOOM_SECRET_TOKEN = "6zW15yRhThaIzj3A18unYg"


def parse_zoom_time(timestr):
    utc_time = datetime.strptime(timestr, "%Y-%m-%dT%H:%M:%SZ")
    return utc_time + IST_OFFSET


@app.route("/")
def home():
    return "Zoom Attendance System Running ✅", 200


@app.route("/zoom/webhook", methods=["POST"])
def zoom_webhook():
    data = request.json
    event = data.get("event")

    print("Webhook Event:", event)

    # ✅ FINAL VALIDATION FIX (HMAC)
    if event == "endpoint.url_validation":
        plain_token = data["payload"]["plainToken"]

        encrypted_token = hmac.new(
            ZOOM_SECRET_TOKEN.encode(),
            plain_token.encode(),
            hashlib.sha256
        ).hexdigest()

        return jsonify({
            "plainToken": plain_token,
            "encryptedToken": encrypted_token
        })

    return jsonify({"status": "ok"}), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)