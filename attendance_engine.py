# attendance_engine.py

from datetime import datetime, timedelta
from collections import defaultdict
from config import LATE_THRESHOLD_MINUTES

meetings = defaultdict(dict)
meeting_meta = {}

def process_join(meeting_id, name, join_time, start_time):
    if name not in meetings[meeting_id]:
        meetings[meeting_id][name] = {
            "name": name,
            "join_time": join_time,
            "leave_time": join_time,
            "total_duration": timedelta(0),
            "status": "On Time"
        }

        diff = (join_time - start_time).total_seconds()
        if diff > LATE_THRESHOLD_MINUTES * 60:
            meetings[meeting_id][name]["status"] = "Late"


def process_leave(meeting_id, name, leave_time):
    if name in meetings[meeting_id]:
        user = meetings[meeting_id][name]

        user["leave_time"] = leave_time

        # total duration logic
        user["total_duration"] = user["leave_time"] - user["join_time"]


def get_participants(meeting_id):
    return list(meetings[meeting_id].values())


def clear_meeting(meeting_id):
    if meeting_id in meetings:
        del meetings[meeting_id]