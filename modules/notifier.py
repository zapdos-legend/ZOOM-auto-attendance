def generate_whatsapp_link(number, message):
    number = number.replace("+", "").strip()
    msg = message.replace(" ", "%20")
    return f"https://wa.me/{number}?text={msg}"