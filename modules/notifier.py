from email_service import send_email_with_attachment

# ✅ Email wrapper (already used)
def send_email_with_attachment_wrapper(subject, body, attachments):
    try:
        send_email_with_attachment(subject, body, attachments)
        print("📧 Email sent successfully!")
    except Exception as e:
        print("❌ Email error:", e)


# ✅ WhatsApp placeholder (for Render)
def send_whatsapp_report(message):
    print("📲 WhatsApp (Render):", message)