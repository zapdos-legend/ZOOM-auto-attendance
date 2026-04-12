import requests
import os
import csv
from config import ACCOUNT_ID, CLIENT_ID, CLIENT_SECRET, OUTPUT_FOLDER


def get_access_token():
    url = "https://zoom.us/oauth/token"

    params = {
        "grant_type": "account_credentials",
        "account_id": ACCOUNT_ID
    }

    response = requests.post(url, params=params, auth=(CLIENT_ID, CLIENT_SECRET))

    if response.status_code != 200:
        print("❌ Failed to get access token:", response.text)
        return None

    return response.json().get("access_token")


def safe_filename(name):
    invalid_chars = ['\\', '/', ':', '*', '?', '"', '<', '>', '|']
    for ch in invalid_chars:
        name = name.replace(ch, "_")
    return name


def get_meeting_participants(meeting_uuid, access_token):
    url = f"https://api.zoom.us/v2/report/meetings/{meeting_uuid}/participants"
    headers = {"Authorization": f"Bearer {access_token}"}

    participants = []
    next_page_token = ""

    while True:
        params = {"page_size": 300}
        if next_page_token:
            params["next_page_token"] = next_page_token

        response = requests.get(url, headers=headers, params=params)

        if response.status_code != 200:
            print("❌ Error fetching participants:", response.text)
            break

        data = response.json()
        participants.extend(data.get("participants", []))

        next_page_token = data.get("next_page_token")
        if not next_page_token:
            break

    return participants


def save_attendance_csv(topic, meeting_uuid, participants):
    os.makedirs(OUTPUT_FOLDER, exist_ok=True)

    topic = safe_filename(topic)
    filename = os.path.join(OUTPUT_FOLDER, f"{topic}_{meeting_uuid}.csv")

    with open(filename, "w", newline="", encoding="utf-8") as file:
        writer = csv.writer(file)
        writer.writerow(["Name", "Email", "Join Time", "Leave Time", "Duration (Minutes)"])

        for p in participants:
            writer.writerow([
                p.get("name"),
                p.get("user_email"),
                p.get("join_time"),
                p.get("leave_time"),
                p.get("duration")
            ])

    return filename