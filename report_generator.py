# report_generator.py

import os
import csv
from datetime import datetime
from fpdf import FPDF
from config import OUTPUT_FOLDER

os.makedirs(OUTPUT_FOLDER, exist_ok=True)


def generate_reports(topic, meeting_id, start_time, end_time, participants):

    date = start_time.strftime("%d-%m-%Y")
    start_str = start_time.strftime("%H-%M-%S")
    end_str = end_time.strftime("%H-%M-%S")

    filename = f"{topic}_{meeting_id}_{date}_{start_str}_to_{end_str}"

    csv_path = os.path.join(OUTPUT_FOLDER, filename + ".csv")
    pdf_path = os.path.join(OUTPUT_FOLDER, filename + ".pdf")

    # CSV
    with open(csv_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["Name", "Date", "Join", "Leave", "Duration", "Status"])

        for p in participants:
            writer.writerow([
                p["name"],
                date,
                p["join_time"].strftime("%H:%M:%S"),
                p["leave_time"].strftime("%H:%M:%S"),
                str(p["total_duration"]),
                p["status"]
            ])

        writer.writerow([])
        writer.writerow(["Total Participants", len(participants)])

    # PDF
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Arial", size=12)

    pdf.cell(200, 10, f"Attendance Report: {topic}", ln=True)
    pdf.cell(200, 10, f"Date: {date}", ln=True)
    pdf.cell(200, 10, f"Start: {start_str} End: {end_str}", ln=True)

    pdf.ln(5)

    for p in participants:
        pdf.cell(200, 10,
                 f"{p['name']} | {p['status']} | {p['total_duration']}",
                 ln=True)

    pdf.output(pdf_path)

    return csv_path, pdf_path