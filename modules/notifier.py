import smtplib
from email.message import EmailMessage
from config import EMAIL_ENABLED, SMTP_SERVER, SMTP_PORT, EMAIL_SENDER, EMAIL_PASSWORD
from config import WHATSAPP_ENABLED, WHATSAPP_NUMBER, CLOUD_MODE

import time
from datetime import datetime, timedelta


def send_email(subject, body, receiver):
    if not EMAIL_ENABLED:
        return False

    try:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = EMAIL_SENDER
        msg["To"] = receiver
        msg.set_content(body)

        server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT)
        server.starttls()
        server.login(EMAIL_SENDER, EMAIL_PASSWORD)
        server.send_message(msg)
        server.quit()
        return True
    except:
        return False


def send_email_with_attachment(subject, body, receiver, attachments=[]):
    if not EMAIL_ENABLED:
        return False

    try:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = EMAIL_SENDER
        msg["To"] = receiver
        msg.set_content(body)

        for file_path in attachments:
            with open(file_path, "rb") as f:
                file_data = f.read()
                file_name = file_path.split("\\")[-1].split("/")[-1]

            msg.add_attachment(file_data, maintype="application", subtype="octet-stream", filename=file_name)

        server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT)
        server.starttls()
        server.login(EMAIL_SENDER, EMAIL_PASSWORD)
        server.send_message(msg)
        server.quit()

        print("📧 ✅ Email sent successfully!")
        return True

    except Exception as e:
        print("📧 ❌ Email failed:", e)
        return False


def send_whatsapp_text(message):
    if CLOUD_MODE:
        print("⚠️ WhatsApp disabled in cloud mode.")
        return False

    if not WHATSAPP_ENABLED:
        return False

    try:
        import pywhatkit

        now = datetime.now() + timedelta(minutes=2)
        hour = now.hour
        minute = now.minute

        print("📲 WhatsApp sending scheduled...")
        pywhatkit.sendwhatmsg(WHATSAPP_NUMBER, message, hour, minute, wait_time=15, tab_close=True)

        print("📲 ✅ WhatsApp message sent successfully!")
        return True

    except Exception as e:
        print("📲 ❌ WhatsApp failed:", e)
        return False