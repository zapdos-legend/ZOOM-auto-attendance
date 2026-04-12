# email_service.py

import smtplib
from email.message import EmailMessage
from config import EMAIL_ADDRESS, EMAIL_PASSWORD


def send_email(to_email, subject, body, attachments=[]):

    msg = EmailMessage()
    msg["From"] = EMAIL_ADDRESS
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.set_content(body)

    for file in attachments:
        with open(file, "rb") as f:
            data = f.read()
            name = file.split("/")[-1]

        msg.add_attachment(data, maintype="application", subtype="octet-stream", filename=name)

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(EMAIL_ADDRESS, EMAIL_PASSWORD)
        smtp.send_message(msg)