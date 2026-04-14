import csv
import os

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from config import OUTPUT_FOLDER, PRESENT_PERCENTAGE


def generate_reports(rows, meeting_meta):
    os.makedirs(OUTPUT_FOLDER, exist_ok=True)

    safe_topic = meeting_meta["topic"].replace(" ", "_").replace("/", "_")
    base_name = (
        f'{safe_topic}_{meeting_meta["zoom_meeting_id"]}_'
        f'{meeting_meta["meeting_date"]}_{meeting_meta["start_time"].replace(":", "-")}_to_{meeting_meta["end_time"].replace(":", "-")}'
    )

    csv_file = os.path.join(OUTPUT_FOLDER, f"{base_name}.csv")
    pdf_file = os.path.join(OUTPUT_FOLDER, f"{base_name}.pdf")

    with open(csv_file, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["Name", "Join Time", "Leave Time", "Duration (Min)", "Rejoins", "Status", "Is Member", "Is Host"])
        for row in rows:
            writer.writerow([
                row["name"],
                row["join_time_str"],
                row["leave_time_str"],
                row["duration_minutes"],
                row["rejoins"],
                row["status"],
                row["is_member"],
                row["is_host"],
            ])

    styles = getSampleStyleSheet()
    doc = SimpleDocTemplate(pdf_file, pagesize=A4)
    elements = []

    elements.append(Paragraph("Attendance Report", styles["Title"]))
    elements.append(Spacer(1, 10))

    elements.append(Paragraph(f"<b>Topic:</b> {meeting_meta['topic']}", styles["Normal"]))
    elements.append(Paragraph(f"<b>Meeting ID:</b> {meeting_meta['zoom_meeting_id']}", styles["Normal"]))
    elements.append(Paragraph(f"<b>Date:</b> {meeting_meta['meeting_date']}", styles["Normal"]))
    elements.append(Paragraph(f"<b>Start Time:</b> {meeting_meta['start_time']}", styles["Normal"]))
    elements.append(Paragraph(f"<b>End Time:</b> {meeting_meta['end_time']}", styles["Normal"]))
    elements.append(Spacer(1, 10))

    total_meeting_minutes = meeting_meta["total_minutes"]
    threshold = round((PRESENT_PERCENTAGE / 100.0) * total_meeting_minutes, 2)

    elements.append(Paragraph(f"<b>Total Meeting Duration:</b> {round(total_meeting_minutes, 2)} minutes", styles["Normal"]))
    elements.append(Spacer(1, 12))

    table_data = [["Name", "Join", "Leave", "Duration", "Rejoins", "Status"]]
    for row in rows:
        table_data.append([
            row["name"],
            row["join_time_str"],
            row["leave_time_str"],
            row["duration_minutes"],
            row["rejoins"],
            row["status"],
        ])

    table = Table(table_data, repeatRows=1)
    style = TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
        ("GRID", (0, 0), (-1, -1), 1, colors.black),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
    ])

    for i in range(1, len(table_data)):
        if table_data[i][5] == "PRESENT":
            style.add("TEXTCOLOR", (5, i), (5, i), colors.green)
        else:
            style.add("TEXTCOLOR", (5, i), (5, i), colors.red)

    table.setStyle(style)
    elements.append(table)
    elements.append(Spacer(1, 12))

    note = f"""
    <b>📌 Attendance Criteria</b><br/>
    ✅ Present = Duration ≥ {PRESENT_PERCENTAGE}% of total meeting duration<br/>
    ❌ Absent = Duration &lt; {PRESENT_PERCENTAGE}% of total meeting duration<br/><br/>
    <b>🎯 Present Threshold For This Meeting:</b> {threshold} minutes
    """

    note_table = Table([[Paragraph(note, styles["Normal"])]], colWidths=[500])
    note_table.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 1.5, colors.black),
        ("BACKGROUND", (0, 0), (-1, -1), colors.whitesmoke),
        ("PADDING", (0, 0), (-1, -1), 8),
    ]))
    elements.append(note_table)

    doc.build(elements)
    return csv_file, pdf_file