from email_service import send_email_with_attachment


def send_email_with_attachment_wrapper(subject, body, attachments=None, to_email=None):
    attachments = attachments or []
    return send_email_with_attachment(subject, body, attachments, to_email)


def send_whatsapp_report(message):
    # Cloud-safe placeholder
    print("📲 WhatsApp placeholder:")
    print(message)
    return True