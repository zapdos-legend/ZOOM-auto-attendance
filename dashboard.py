import streamlit as st
import json, os
import pandas as pd
from datetime import datetime

from config import LIVE_DATA_FILE, OUTPUT_FOLDER
from modules.db import init_db, add_member, remove_member, get_members, fetch_attendance_logs


st.set_page_config(page_title="Zoom Attendance Dashboard", layout="wide")

st.title("📊 Zoom Attendance Dashboard (LIVE + Reports)")
st.caption("Auto refresh every 2 seconds")

st_autorefresh = st.empty()


def load_live_data():
    if not os.path.exists(LIVE_DATA_FILE):
        return None

    with open(LIVE_DATA_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


# Auto refresh
st.markdown("<meta http-equiv='refresh' content='2'>", unsafe_allow_html=True)

init_db()

live_data = load_live_data()

# ---------------- LIVE SECTION ----------------
st.header("🟢 LIVE ATTENDANCE")

if live_data:
    meeting = live_data.get("meeting", {})
    participants = live_data.get("participants", {})

    col1, col2, col3, col4 = st.columns(4)

    topic = meeting.get("topic", "N/A")
    meeting_id = meeting.get("meeting_id", "N/A")

    total_joined = len(participants)
    present_now = len([p for p in participants.values() if p["current_join"] is not None])
    left_count = total_joined - present_now

    col1.metric("📌 Topic", topic)
    col2.metric("🆔 Meeting ID", meeting_id)
    col3.metric("👥 Total Joined", total_joined)
    col4.metric("🟢 Present Now", present_now)

    st.metric("🔴 Left", left_count)

    rows = []
    for name, p in participants.items():
        duration_min = round(p["total_seconds"] / 60, 2)
        rows.append({
            "Name": name,
            "Status": "🟢 Present" if p["current_join"] else "🔴 Left",
            "Duration (Min)": duration_min,
            "Rejoins": p.get("rejoin_count", 0)
        })

    df = pd.DataFrame(rows)
    df = df.sort_values(by="Duration (Min)", ascending=False)

    st.subheader("📋 Live Participant List")
    st.dataframe(df, use_container_width=True)

else:
    st.warning("⚠️ No live meeting data found yet.")

st.divider()

# ---------------- MEMBERS MANAGEMENT ----------------
st.header("👥 MEMBERS MANAGEMENT")

colA, colB = st.columns(2)

with colA:
    st.subheader("➕ Add Member")
    name = st.text_input("Name")
    email = st.text_input("Email")
    whatsapp = st.text_input("WhatsApp Number (+91...)")

    if st.button("Add Member"):
        if name.strip():
            add_member(name.strip(), email.strip(), whatsapp.strip())
            st.success("✅ Member Added Successfully")

with colB:
    st.subheader("🗑 Remove Member")
    members = get_members(active_only=False)
    member_names = [m[0] for m in members]

    selected = st.selectbox("Select Member", [""] + member_names)

    if st.button("Remove Member"):
        if selected:
            remove_member(selected)
            st.success("✅ Member Removed")

st.divider()

# ---------------- REPORT ANALYTICS ----------------
st.header("📂 REPORT ANALYTICS (Weekly / Monthly / Yearly)")

logs = fetch_attendance_logs()

if logs:
    df_logs = pd.DataFrame(logs, columns=[
        "ID", "Meeting ID", "Topic", "Date", "Start", "End",
        "Participant", "Duration (Min)", "Status", "Rejoins"
    ])

    st.subheader("📌 Full Attendance History")
    st.dataframe(df_logs, use_container_width=True)

    st.subheader("📊 Present vs Absent Pie Chart")
    pie_data = df_logs["Status"].value_counts()
    st.pyplot(pie_data.plot.pie(autopct="%1.1f%%", figsize=(4, 4)).figure)

else:
    st.warning("⚠️ No past attendance logs found in database.")