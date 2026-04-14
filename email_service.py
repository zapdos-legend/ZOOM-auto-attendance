import base64
import os
import smtplib
from email.message import EmailMessage

import requests

from config import (
    EMAIL_ENABLED,
    EMAIL_FROM,
    EMAIL_PASSWORD,
    EMAIL_PROVIDER,
    EMAIL_RECEIVER,
    EMAIL_SENDER,
    RESEND_API_KEY,
    SMTP_PORT,
    SMTP_SERVER,
)


def _collect_attachments(attachments):
    attachments = attachments or []
    payload_items = []
    attached_files = []

    for file_path in attachments:
        if not file_path or not os.path.exists(file_path):
            continue

        with open(file_path, 'rb') as f:
            raw = f.read()

        file_name = os.path.basename(file_path)
        attached_files.append(file_name)
        payload_items.append(
            {
                'filename': file_name,
                'content': base64.b64encode(raw).decode('utf-8'),
            }
        )

    return payload_items, attached_files


def _send_via_resend(subject, body, attachments=None, to_email=None):
    receiver = (to_email or EMAIL_RECEIVER or '').strip()
    resend_from = (EMAIL_FROM or EMAIL_SENDER or '').strip()
    resend_attachments, attached_files = _collect_attachments(attachments)

    if not RESEND_API_KEY:
        return {
            'success': False,
            'receiver': receiver,
            'error': 'Missing RESEND_API_KEY environment variable.',
            'attachments': attached_files,
        }

    if not resend_from:
        return {
            'success': False,
            'receiver': receiver,
            'error': 'Missing EMAIL_FROM environment variable.',
            'attachments': attached_files,
        }

    if not receiver:
        return {
            'success': False,
            'receiver': receiver,
            'error': 'Receiver email is missing.',
            'attachments': attached_files,
        }

    safe_body = (
        body.replace('&', '&amp;')
        .replace('<', '&lt;')
        .replace('>', '&gt;')
    )

    payload = {
        'from': resend_from,
        'to': [receiver],
        'subject': subject,
        'text': body,
        'html': f'<pre style="font-family: Arial, sans-serif; white-space: pre-wrap;">{safe_body}</pre>',
    }

    if resend_attachments:
        payload['attachments'] = resend_attachments

    try:
        response = requests.post(
            'https://api.resend.com/emails',
            headers={
                'Authorization': f'Bearer {RESEND_API_KEY}',
                'Content-Type': 'application/json',
                'User-Agent': 'zoom-auto-attendance/1.0',
            },
            json=payload,
            timeout=20,
        )

        if 200 <= response.status_code < 300:
            data = response.json() if response.content else {}
            print(f"📧 ✅ Resend email sent to {receiver} | id={data.get('id', '')}")
            return {
                'success': True,
                'receiver': receiver,
                'error': '',
                'attachments': attached_files,
                'provider': 'resend',
                'message_id': data.get('id', ''),
            }

        try:
            error_json = response.json()
            error_message = (
                error_json.get('message')
                or error_json.get('error')
                or str(error_json)
            )
        except Exception:
            error_message = response.text or f'HTTP {response.status_code}'

        print(f'📧 ❌ Resend email failed for {receiver}: {error_message}')
        return {
            'success': False,
            'receiver': receiver,
            'error': error_message,
            'attachments': attached_files,
            'provider': 'resend',
            'status_code': response.status_code,
        }

    except Exception as e:
        print(f'📧 ❌ Resend request failed for {receiver}: {e}')
        return {
            'success': False,
            'receiver': receiver,
            'error': str(e),
            'attachments': attached_files,
            'provider': 'resend',
        }


def _send_via_smtp(subject, body, attachments=None, to_email=None):
    receiver = (to_email or EMAIL_RECEIVER or '').strip()
    attachments = attachments or []

    if not EMAIL_SENDER or not EMAIL_PASSWORD:
        return {
            'success': False,
            'receiver': receiver,
            'error': 'Missing EMAIL_SENDER or EMAIL_PASSWORD environment variables.',
        }

    if not receiver:
        return {
            'success': False,
            'receiver': receiver,
            'error': 'Receiver email is missing.',
        }

    msg = EmailMessage()
    msg['Subject'] = subject
    msg['From'] = EMAIL_SENDER
    msg['To'] = receiver
    msg.set_content(body)

    attached_files = []
    for file_path in attachments:
        if not file_path or not os.path.exists(file_path):
            continue
        with open(file_path, 'rb') as f:
            file_data = f.read()
        file_name = os.path.basename(file_path)
        msg.add_attachment(
            file_data,
            maintype='application',
            subtype='octet-stream',
            filename=file_name,
        )
        attached_files.append(file_name)

    try:
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=10) as server:
            server.starttls()
            server.login(EMAIL_SENDER, EMAIL_PASSWORD)
            server.send_message(msg)

        print(f'📧 ✅ SMTP email sent to {receiver}')
        return {
            'success': True,
            'receiver': receiver,
            'error': '',
            'attachments': attached_files,
            'provider': 'smtp',
        }
    except Exception as e:
        print(f'📧 ❌ SMTP email failed for {receiver}: {e}')
        return {
            'success': False,
            'receiver': receiver,
            'error': str(e),
            'attachments': attached_files,
            'provider': 'smtp',
        }


def send_email_with_attachment(subject, body, attachments=None, to_email=None):
    receiver = (to_email or EMAIL_RECEIVER or '').strip()

    if not EMAIL_ENABLED:
        return {
            'success': False,
            'receiver': receiver,
            'error': 'Email is disabled in config.',
            'provider': EMAIL_PROVIDER,
        }

    provider = (EMAIL_PROVIDER or 'resend').lower()

    if provider == 'smtp':
        return _send_via_smtp(subject, body, attachments, to_email)

    if provider == 'resend':
        return _send_via_resend(subject, body, attachments, to_email)

    return {
        'success': False,
        'receiver': receiver,
        'error': f'Unsupported EMAIL_PROVIDER: {provider}',
        'provider': provider,
    }