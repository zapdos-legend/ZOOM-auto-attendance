from email_service import send_email_with_attachment


def send_email_with_attachment_wrapper(subject, body, attachments=None, to_email=None):
    if attachments is None:
        attachments = []
    return send_email_with_attachment(subject, body, attachments, to_email)


def send_whatsapp_report(message):
    # Render/cloud-safe placeholder
    print("📲 WhatsApp (Render placeholder):")
    print(message)
    return True