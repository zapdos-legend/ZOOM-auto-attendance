from datetime import datetime

participants = {}

meeting_info = {
    "topic": "",
    "meeting_id": "",
    "start_time": None,
    "end_time": None,
    "end_time_candidate": None,
    "host_id": "",
    "host_name": "",
    "current_uuid": "",
    "pending_end": False,
    "report_generated": False,
    "last_activity_time": None,
}


def process_join(name, join_time, participant_user_id="", email="", is_host=False):
    if name not in participants:
        participants[name] = {
            "first_join": join_time,
            "last_leave": None,
            "current_join": join_time,
            "total_seconds": 0.0,
            "rejoin_count": 0,
            "email": email or "",
            "participant_user_id": participant_user_id or "",
            "status": "LIVE",
            "is_host": is_host,
        }
    else:
        if participants[name]["current_join"] is None:
            participants[name]["current_join"] = join_time
            if participants[name]["last_leave"] is not None:
                participants[name]["rejoin_count"] += 1

        if email:
            participants[name]["email"] = email
        if participant_user_id:
            participants[name]["participant_user_id"] = participant_user_id
        if is_host:
            participants[name]["is_host"] = True

    if is_host:
        meeting_info["host_name"] = name


def process_leave(name, leave_time):
    if name not in participants:
        return

    current_join = participants[name]["current_join"]
    if current_join is None:
        return

    session_seconds = (leave_time - current_join).total_seconds()
    if session_seconds < 0:
        session_seconds = 0

    participants[name]["total_seconds"] += session_seconds
    participants[name]["last_leave"] = leave_time
    participants[name]["current_join"] = None
    participants[name]["status"] = "LEFT"


def close_open_sessions(end_time):
    for name, p in participants.items():
        current_join = p.get("current_join")
        if current_join is not None:
            session_seconds = (end_time - current_join).total_seconds()
            if session_seconds > 0:
                p["total_seconds"] += session_seconds
            p["last_leave"] = end_time
            p["current_join"] = None
            p["status"] = "LEFT"


def build_final_rows(total_meeting_minutes, present_percentage, member_lookup, host_name_hint=""):
    threshold = (present_percentage / 100.0) * total_meeting_minutes
    rows = []

    for name, p in participants.items():
        duration_minutes = round(p["total_seconds"] / 60.0, 2)
        join_str = p["first_join"].strftime("%H:%M:%S") if p["first_join"] else "-"
        leave_str = p["last_leave"].strftime("%H:%M:%S") if p["last_leave"] else "-"

        is_member = name.strip().lower() in member_lookup
        is_host = p.get("is_host", False) or (
            host_name_hint and name.strip().lower() == host_name_hint.strip().lower()
        )

        status = "PRESENT" if duration_minutes >= threshold else "ABSENT"

        rows.append({
            "name": name,
            "email": p.get("email", ""),
            "join_time_str": join_str,
            "leave_time_str": leave_str,
            "duration_minutes": duration_minutes,
            "rejoins": p.get("rejoin_count", 0),
            "status": status,
            "is_member": 1 if is_member else 0,
            "is_host": 1 if is_host else 0,
        })

    rows.sort(key=lambda x: x["duration_minutes"], reverse=True)
    return rows


def reset_runtime_state():
    participants.clear()

    meeting_info["topic"] = ""
    meeting_info["meeting_id"] = ""
    meeting_info["start_time"] = None
    meeting_info["end_time"] = None
    meeting_info["end_time_candidate"] = None
    meeting_info["host_id"] = ""
    meeting_info["host_name"] = ""
    meeting_info["current_uuid"] = ""
    meeting_info["pending_end"] = False
    meeting_info["report_generated"] = False
    meeting_info["last_activity_time"] = None