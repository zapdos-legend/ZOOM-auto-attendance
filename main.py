import requests
import os
import csv
from config import ACCOUNT_ID, CLIENT_ID, CLIENT_SECRET


OUTPUT_FOLDER = "attendance_reports"
os.makedirs(OUTPUT_FOLDER, exist_ok=True)


def get_access_token():
    url = "https://zoom.us/oauth/token"

    params = {
        "grant_type": "account_credentials",
        "account_id": ACCOUNT_ID
    }

    response = requests.post(url, params=params, auth=(CLIENT_ID, CLIENT_SECRET))

    if response.status_code != 200:
        print("❌ Error getting access token")
        print(response.text)
        return None

    return response.json().get("access_token")


def get_users(access_token):
    url = "https://api.zoom.us/v2/users"
    headers = {"Authorization": f"Bearer {access_token}"}

    response = requests.get(url, headers=headers)

    if response.status_code != 200:
        print("❌ Error fetching users")
        print(response.text)
        return []

    return response.json().get("users", [])


def get_meetings(access_token, user_id):
    url = f"https://api.zoom.us/v2/users/{user_id}/meetings"
    headers = {"Authorization": f"Bearer {access_token}"}

    params = {
        "type": "past",
        "page_size": 30
    }

    response = requests.get(url, headers=headers, params=params)

    if response.status_code != 200:
        return []

    return response.json().get("meetings", [])


def get_participants(access_token, meeting_id):
    url = f"https://api.zoom.us/v2/report/meetings/{meeting_id}/participants"
    headers = {"Authorization": f"Bearer {access_token}"}

    params = {
        "page_size": 300
    }

    response = requests.get(url, headers=headers, params=params)

    if response.status_code != 200:
        print(f"❌ Error fetching participants for meeting {meeting_id}")
        print(response.text)
        return []

    return response.json().get("participants", [])


def safe_filename(name):
    invalid_chars = ['\\', '/', ':', '*', '?', '"', '<', '>', '|']
    for ch in invalid_chars:
        name = name.replace(ch, "_")
    return name


def save_csv(meeting_topic, meeting_id, participants):
    meeting_topic = safe_filename(meeting_topic)

    filename = os.path.join(OUTPUT_FOLDER, f"{meeting_topic}_{meeting_id}.csv")

    with open(filename, "w", newline="", encoding="utf-8") as file:
        writer = csv.writer(file)

        writer.writerow(["Name", "Email", "Join Time", "Leave Time", "Duration (minutes)"])

        for p in participants:
            writer.writerow([
                p.get("name"),
                p.get("user_email"),
                p.get("join_time"),
                p.get("leave_time"),
                p.get("duration")
            ])

    print(f"✅ Saved: {filename}")


def main():
    print("🔄 Getting access token...")
    access_token = get_access_token()

    if not access_token:
        return

    print("✅ Access token generated successfully!")

    print("🔄 Fetching users list...")
    users = get_users(access_token)

    if not users:
        print("❌ No users found.")
        return

    print(f"✅ Found {len(users)} users")

    for user in users:
        user_id = user.get("id")
        user_email = user.get("email")

        print(f"\n📌 Checking meetings for user: {user_email}")

        meetings = get_meetings(access_token, user_id)

        if not meetings:
            print("⚠️ No past meetings found.")
            continue

        print(f"✅ Found {len(meetings)} past meetings")

        for meeting in meetings:
            meeting_id = meeting.get("id")
            topic = meeting.get("topic", "No_Topic")

            print(f"🔄 Fetching participants for meeting: {topic} ({meeting_id})")

            participants = get_participants(access_token, meeting_id)

            if not participants:
                print("⚠️ No participants found.")
                continue

            save_csv(topic, meeting_id, participants)

    print("\n🎉 Done! All attendance reports saved inside 'attendance_reports' folder.")


if __name__ == "__main__":
    main()