import pywhatkit
from datetime import datetime, timedelta


def send_whatsapp_message(phone, message):
    now = datetime.now() + timedelta(minutes=2)

    hour = now.hour
    minute = now.minute

    pywhatkit.sendwhatmsg(phone, message, hour, minute, wait_time=15, tab_close=True, close_time=3)