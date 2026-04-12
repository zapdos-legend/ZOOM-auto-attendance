import json
import os
from datetime import datetime, timedelta
from config import SCHEDULER_FILE


def save_meeting_schedule(meeting_time_str):
    os.makedirs("data", exist_ok=True)

    data = {
        "meeting_time": meeting_time_str
    }

    with open(SCHEDULER_FILE, "w") as f:
        json.dump(data, f)


def load_meeting_schedule():
    if not os.path.exists(SCHEDULER_FILE):
        return None

    with open(SCHEDULER_FILE, "r") as f:
        data = json.load(f)

    return data.get("meeting_time")