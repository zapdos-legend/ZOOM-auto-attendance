import os
import smtplib
from email.message import EmailMessage

from config import EMAIL_ENABLED, EMAIL_SENDER, EMAIL_PASSWORD, EMAIL_RECEIVER


def send_email_with_attachment(subject, body, attachments=None, to_email=None):
    if not EMAIL_ENABLED:
        print("📧 Email disabled in config.")
        return False

    attachments = attachments or []
    receiver = to_email or EMAIL_RECEIVER

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = EMAIL_SENDER
    msg["To"] = receiver
    msg.set_content(body)

    for file_path in attachments:
        if not file_path:
            continue
        if not os.path.exists(file_path):
            print(f"⚠️ Attachment not found: {file_path}")
            continue

        with open(file_path, "rb") as f:
            file_data = f.read()
            file_name = os.path.basename(file_path)

        msg.add_attachment(
            file_data,
            maintype="application",
            subtype="octet-stream",
            filename=file_name,
        )

    try:
        with smtplib.SMTP("smtp.gmail.com", 587) as server:
            server.starttls()
            server.login(EMAIL_SENDER, EMAIL_PASSWORD)
            server.send_message(msg)

        print(f"📧 ✅ Email sent to {receiver}")
        return True

    except Exception as e:
        print(f"📧 ❌ Email failed for {receiver}: {e}")
        return False