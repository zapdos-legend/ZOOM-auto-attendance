import os
import csv
from datetime import datetime
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet
from config import PRESENT_PERCENTAGE


def generate_reports(participants, meeting_info, output_folder):
    os.makedirs(output_folder, exist_ok=True)

    topic = meeting_info["topic"].replace(" ", "_")
    meeting_id = meeting_info["meeting_id"]

    date_str = meeting_info["date"]
    start_str = meeting_info["start_time_str"]
    end_str = meeting_info["end_time_str"]

    base_name = f"{topic}_{meeting_id}_{date_str}_{start_str}_to_{end_str}"

    csv_path = os.path.join(output_folder, base_name + ".csv")
    pdf_path = os.path.join(output_folder, base_name + ".pdf")

    total_meeting_minutes = meeting_info["total_minutes"]
    threshold_minutes = (PRESENT_PERCENTAGE / 100) * total_meeting_minutes

    # CSV
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["Name", "Join Time", "Leave Time", "Duration (Min)", "Rejoins", "Status"])

        for name, p in participants.items():
            join_time = p["first_join"].strftime("%H:%M:%S") if p["first_join"] else ""
            leave_time = p["last_leave"].strftime("%H:%M:%S") if p["last_leave"] else ""

            duration_min = round(p["total_seconds"] / 60, 2)

            status = "Present" if duration_min >= threshold_minutes else "Absent"
            p["status"] = status

            writer.writerow([name, join_time, leave_time, duration_min, p["rejoin_count"], status])

    # PDF
    styles = getSampleStyleSheet()
    doc = SimpleDocTemplate(pdf_path, pagesize=A4)
    elements = []

    elements.append(Paragraph(f"📌 Zoom Attendance Report", styles["Title"]))
    elements.append(Spacer(1, 10))

    elements.append(Paragraph(f"<b>Topic:</b> {meeting_info['topic']}", styles["Normal"]))
    elements.append(Paragraph(f"<b>Meeting ID:</b> {meeting_id}", styles["Normal"]))
    elements.append(Paragraph(f"<b>Date:</b> {date_str}", styles["Normal"]))
    elements.append(Paragraph(f"<b>Start Time:</b> {start_str}", styles["Normal"]))
    elements.append(Paragraph(f"<b>End Time:</b> {end_str}", styles["Normal"]))
    elements.append(Spacer(1, 10))

    elements.append(Paragraph(f"⏳ Total Meeting Duration: {round(total_meeting_minutes,2)} minutes", styles["Normal"]))
    elements.append(Spacer(1, 10))

    note_text = f"""
    <b>📌 Attendance Criteria:</b><br/>
    ✅ Present = Duration ≥ {PRESENT_PERCENTAGE}% of total meeting duration<br/>
    ❌ Absent = Duration < {PRESENT_PERCENTAGE}% of total meeting duration<br/><br/>
    <b>🎯 Today’s Present Threshold:</b> {round(threshold_minutes,2)} minutes
    """
    elements.append(Paragraph(note_text, styles["Normal"]))
    elements.append(Spacer(1, 15))

    table_data = [["Name", "Join", "Leave", "Duration (Min)", "Rejoins", "Status"]]

    for name, p in participants.items():
        join_time = p["first_join"].strftime("%H:%M:%S") if p["first_join"] else ""
        leave_time = p["last_leave"].strftime("%H:%M:%S") if p["last_leave"] else ""

        duration_min = round(p["total_seconds"] / 60, 2)
        status = p["status"]

        table_data.append([name, join_time, leave_time, duration_min, p["rejoin_count"], status])

    table = Table(table_data)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
        ("GRID", (0, 0), (-1, -1), 1, colors.black),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
    ]))

    elements.append(table)
    doc.build(elements)

    return csv_path, pdf_path