import os
import csv
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet
from config import PRESENT_PERCENTAGE


def generate_reports(participants, meeting_info, output_folder):
    os.makedirs(output_folder, exist_ok=True)

    topic = meeting_info.get("topic", "Meeting").replace(" ", "_")
    meeting_id = meeting_info.get("meeting_id", "Unknown")

    start_time = meeting_info["start_time"]
    end_time = meeting_info["end_time"]

    date_str = start_time.strftime("%d-%m-%Y")
    start_str = start_time.strftime("%H-%M-%S")
    end_str = end_time.strftime("%H-%M-%S")

    filename_base = f"{topic}_{meeting_id}_{date_str}_{start_str}_to_{end_str}"

    csv_file = os.path.join(output_folder, filename_base + ".csv")
    pdf_file = os.path.join(output_folder, filename_base + ".pdf")

    total_meeting_seconds = (end_time - start_time).total_seconds()
    if total_meeting_seconds < 0:
        total_meeting_seconds = 0

    total_meeting_minutes = round(total_meeting_seconds / 60, 2)

    required_seconds = (PRESENT_PERCENTAGE / 100) * total_meeting_seconds
    required_minutes = round(required_seconds / 60, 2)

    # =========================
    # CSV FILE
    # =========================
    with open(csv_file, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["Name", "Join Time", "Leave Time", "Duration (Min)", "Status", "Rejoins"])

        for name, p in participants.items():
            join_time = p["first_join"].strftime("%H:%M:%S") if p["first_join"] else "-"
            leave_time = p["last_leave"].strftime("%H:%M:%S") if p["last_leave"] else "-"

            duration_seconds = min(p["total_seconds"], total_meeting_seconds)
            duration_minutes = round(duration_seconds / 60, 2)

            status = "PRESENT" if duration_seconds >= required_seconds else "ABSENT"

            writer.writerow([name, join_time, leave_time, duration_minutes, status, p.get("rejoin_count", 0)])

    # =========================
    # PDF FILE
    # =========================
    styles = getSampleStyleSheet()
    doc = SimpleDocTemplate(pdf_file, pagesize=letter)

    elements = []
    elements.append(Paragraph("<b><font size=18>Attendance Report</font></b>", styles["Title"]))
    elements.append(Spacer(1, 12))

    elements.append(Paragraph(f"<b>Topic:</b> {meeting_info['topic']}", styles["Normal"]))
    elements.append(Paragraph(f"<b>Meeting ID:</b> {meeting_info['meeting_id']}", styles["Normal"]))
    elements.append(Paragraph(f"<b>Date:</b> {start_time.strftime('%d-%m-%Y')}", styles["Normal"]))
    elements.append(Paragraph(f"<b>Start Time:</b> {start_time.strftime('%H:%M:%S')}", styles["Normal"]))
    elements.append(Paragraph(f"<b>End Time:</b> {end_time.strftime('%H:%M:%S')}", styles["Normal"]))
    elements.append(Spacer(1, 20))

    data = [["Name", "Join Time", "Leave Time", "Duration (Min)", "Status", "Rejoins"]]

    for name, p in participants.items():
        join_time = p["first_join"].strftime("%H:%M:%S") if p["first_join"] else "-"
        leave_time = p["last_leave"].strftime("%H:%M:%S") if p["last_leave"] else "-"

        duration_seconds = min(p["total_seconds"], total_meeting_seconds)
        duration_minutes = round(duration_seconds / 60, 2)

        status = "PRESENT" if duration_seconds >= required_seconds else "ABSENT"

        data.append([name, join_time, leave_time, duration_minutes, status, p.get("rejoin_count", 0)])

    table = Table(data)

    style = TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
        ("GRID", (0, 0), (-1, -1), 1, colors.black),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
    ])

    # Color status column
    for i in range(1, len(data)):
        if data[i][4] == "PRESENT":
            style.add("TEXTCOLOR", (4, i), (4, i), colors.green)
        else:
            style.add("TEXTCOLOR", (4, i), (4, i), colors.red)

    table.setStyle(style)

    elements.append(table)
    elements.append(Spacer(1, 20))

    elements.append(Paragraph(f"<b>Total Meeting Duration:</b> {total_meeting_minutes} minutes", styles["Normal"]))
    elements.append(Spacer(1, 10))

    note_text = f"""
    <b>📌 Attendance Criteria</b><br/>
    ✅ Present = Duration ≥ {PRESENT_PERCENTAGE}% of total meeting duration<br/>
    ❌ Absent = Duration &lt; {PRESENT_PERCENTAGE}% of total meeting duration<br/><br/>
    <b>📊 Today’s {PRESENT_PERCENTAGE}% Criteria:</b> {required_minutes} minutes
    """

    note_table = Table([[Paragraph(note_text, styles["Normal"])]], colWidths=[500])
    note_table.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 2, colors.black),
        ("BACKGROUND", (0, 0), (-1, -1), colors.whitesmoke),
        ("PADDING", (0, 0), (-1, -1), 10),
    ]))

    elements.append(note_table)

    doc.build(elements)

    return csv_file, pdf_file