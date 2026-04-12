import requests
from config import WHATSAPP_PHONE_NUMBER_ID, WHATSAPP_ACCESS_TOKEN


def send_whatsapp_text(to_number, message):
    url = f"https://graph.facebook.com/v18.0/{WHATSAPP_PHONE_NUMBER_ID}/messages"

    headers = {
        "Authorization": f"Bearer {WHATSAPP_ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }

    payload = {
        "messaging_product": "whatsapp",
        "to": to_number,
        "type": "text",
        "text": {"body": message}
    }

    r = requests.post(url, headers=headers, json=payload)
    return r.status_code, r.text