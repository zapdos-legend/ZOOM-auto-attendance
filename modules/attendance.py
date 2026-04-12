from datetime import datetime
from config import PRESENT_PERCENTAGE


participants = {}
meeting_info = {
    "topic": "",
    "meeting_id": "",
    "start_time": None,
    "end_time": None,
    "host_id": "",
}


def process_join(name, join_time):
    if name not in participants:
        participants[name] = {
            "first_join": join_time,
            "last_leave": None,
            "current_join": join_time,
            "total_seconds": 0,
            "status": "Present",
            "rejoin_count": 0
        }
    else:
        # Rejoin detected
        if participants[name]["current_join"] is None:
            participants[name]["current_join"] = join_time
            participants[name]["rejoin_count"] += 1

    participants[name]["status"] = "Present"


def process_leave(name, leave_time):
    if name in participants and participants[name]["current_join"] is not None:
        join_time = participants[name]["current_join"]
        participants[name]["total_seconds"] += (leave_time - join_time).total_seconds()
        participants[name]["last_leave"] = leave_time
        participants[name]["current_join"] = None
        participants[name]["status"] = "Left"