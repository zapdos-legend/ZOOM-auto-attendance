print("APP STARTED")

import os
import json
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from flask import Flask, jsonify, request, render_template_string, send_from_directory

from config import *
from modules.attendance import *
from modules.db import *
from modules.report_generator import *
from modules.notifier import *

app = Flask(__name__)

print("SERVER STARTING...")

# ✅ FIX 1: Ensure folders exist
os.makedirs("data", exist_ok=True)
os.makedirs("attendance_reports", exist_ok=True)

# ✅ FIX 2: Safe DB init
try:
    init_db()
    print("✅ DB Initialized")
except Exception as e:
    print("❌ DB Error:", e)

IST = ZoneInfo("Asia/Kolkata")
state_lock = threading.Lock()


# ---------------- HOME ----------------
@app.route("/")
def home():
    return "<h2>✅ Zoom Attendance System Running</h2><a href='/dashboard'>Open Dashboard</a>"


# ---------------- DASHBOARD ----------------
@app.route("/dashboard")
def dashboard():
    try:
        # SAFE FETCH
        live_rows = get_live_rows_for_dashboard() if 'get_live_rows_for_dashboard' in globals() else []
        members = get_members() if 'get_members' in globals() else []
        meetings = get_recent_meetings() if 'get_recent_meetings' in globals() else []

        analytics = {}
        try:
            analytics = get_dashboard_analytics()
        except:
            analytics = {
                "total_meetings": 0,
                "active_members": 0,
                "status_counts": {},
                "daily_present_rows": [],
                "top_rows": []
            }

        html = """
        <h1>📊 Dashboard</h1>

        <h2>👥 Members</h2>
        <p>Total Members: {{ members|length }}</p>

        <h2>📂 Meetings</h2>
        <p>Total Meetings: {{ meetings|length }}</p>

        <h2>📈 Analytics</h2>
        <p>Total Meetings: {{ analytics.total_meetings }}</p>
        <p>Active Members: {{ analytics.active_members }}</p>

        <h2>🟢 Live</h2>
        <table border="1">
        <tr><th>Name</th><th>Status</th><th>Duration</th></tr>
        {% for r in live_rows %}
        <tr>
            <td>{{ r.name }}</td>
            <td>{{ r.status }}</td>
            <td>{{ r.duration_minutes }}</td>
        </tr>
        {% endfor %}
        </table>
        """

        return render_template_string(
            html,
            live_rows=live_rows,
            members=members,
            meetings=meetings,
            analytics=analytics
        )

    except Exception as e:
        # 🔥 KEY FIX: show error instead of 500
        return f"<h2>Dashboard Error:</h2><pre>{str(e)}</pre>"


# ---------------- WEBHOOK ----------------
@app.route("/zoom/webhook", methods=["POST"])
def zoom_webhook():
    data = request.get_json(silent=True) or {}
    event = data.get("event")

    print("Webhook Event:", event)

    if event == "endpoint.url_validation":
        plain_token = data.get("payload", {}).get("plainToken", "")
        encrypted_token = plain_token  # simplified safe response

        return jsonify({
            "plainToken": plain_token,
            "encryptedToken": encrypted_token,
        }), 200

    return jsonify({"status": "ok"}), 200


# ---------------- RUN ----------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)