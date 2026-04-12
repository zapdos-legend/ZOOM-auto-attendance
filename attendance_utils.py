# attendance_utils.py
import os
import csv
from datetime import datetime, timedelta
from fpdf import FPDF
import smtplib
from email.message import EmailMessage

from config import OUTPUT_FOLDER, EMAIL_ADDRESS, EMAIL_PASSWORD, LATE_THRESHOLD_MINUTES

os.makedirs(OUTPUT_FOLDER, exist_ok=True)

def generate_report(meeting_topic, meeting_id, start_time, end_time, participants):
    date_str = start_time.strftime("%d-%m-%Y")
    start_str = start_time.strftime("%H:%M:%S")
    end_str = end_time.strftime("%H:%M:%S")
    filename_base = f"{meeting_topic}_{meeting_id}_{date_str}_{start_str}_to_{end_str}"

    csv_file = os.path.join(OUTPUT_FOLDER, filename_base + ".csv")
    pdf_file = os.path.join(OUTPUT_FOLDER, filename_base + ".pdf")

    # CSV
    with open(csv_file, 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(["Name", "Date", "Join Time", "Leave Time", "Duration", "Status"])
        for p in participants:
            writer.writerow([
                p['name'],
                date_str,
                p['join_time'].strftime("%H:%M:%S"),
                p['leave_time'].strftime("%H:%M:%S"),
                str(p['duration']),
                p['status']
            ])
        writer.writerow([])
        writer.writerow(["Meeting Topic", meeting_topic])
        writer.writerow(["Meeting Start", start_str])
        writer.writerow(["Meeting End", end_str])
        writer.writerow(["Total Participants", len(participants)])
        avg_duration = sum([p['duration'].total_seconds() for p in participants])/len(participants)
        writer.writerow(["Average Attendance Duration", str(datetime.utcfromtimestamp(avg_duration).strftime("%H:%M:%S"))])

    # PDF
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Arial", 'B', 16)
    pdf.cell(0, 10, f"Attendance Report: {meeting_topic}", ln=True)
    pdf.set_font("Arial", '', 12)
    pdf.cell(0, 8, f"Date: {date_str}", ln=True)
    pdf.cell(0, 8, f"Start: {start_str} | End: {end_str}", ln=True)
    pdf.ln(5)

    pdf.set_font("Arial", 'B', 12)
    pdf.cell(40, 8, "Name", 1)
    pdf.cell(30, 8, "Join", 1)
    pdf.cell(30, 8, "Leave", 1)
    pdf.cell(30, 8, "Duration", 1)
    pdf.cell(30, 8, "Status", 1)
    pdf.ln()

    pdf.set_font("Arial", '', 12)
    for p in participants:
        pdf.cell(40, 8, p['name'], 1)
        pdf.cell(30, 8, p['join_time'].strftime("%H:%M:%S"), 1)
        pdf.cell(30, 8, p['leave_time'].strftime("%H:%M:%S"), 1)
        pdf.cell(30, 8, str(p['duration']), 1)
        pdf.cell(30, 8, p['status'], 1)
        pdf.ln()

    pdf.ln(5)
    pdf.cell(0, 8, f"Total Participants: {len(participants)}", ln=True)
    pdf.cell(0, 8, f"Average Duration: {str(datetime.utcfromtimestamp(avg_duration).strftime('%H:%M:%S'))}", ln=True)

    pdf.output(pdf_file)
    return csv_file, pdf_file

def send_email_with_attachments(to_email, subject, body, attachments=[]):
    msg = EmailMessage()
    msg['From'] = EMAIL_ADDRESS
    msg['To'] = to_email
    msg['Subject'] = subject
    msg.set_content(body)
    for file in attachments:
        with open(file, 'rb') as f:
            file_data = f.read()
            file_name = os.path.basename(file)
        msg.add_attachment(file_data, maintype='application', subtype='octet-stream', filename=file_name)
    with smtplib.SMTP_SSL('smtp.gmail.com', 465) as smtp:
        smtp.login(EMAIL_ADDRESS, EMAIL_PASSWORD)
        smtp.send_message(msg)