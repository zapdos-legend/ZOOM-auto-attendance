import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Folders and files
DATA_DIR = os.path.join(BASE_DIR, "data")
OUTPUT_FOLDER = os.path.join(BASE_DIR, "attendance_reports")
LIVE_DATA_FILE = os.path.join(DATA_DIR, "live_status.json")
SCHEDULER_FILE = os.path.join(DATA_DIR, "scheduler.json")
DB_FILE = os.path.join(DATA_DIR, "members.db")

# Timezone
TIMEZONE_NAME = "Asia/Kolkata"

# Old IST settings kept for compatibility
IST_OFFSET_HOURS = 5
IST_OFFSET_MINUTES = 30

# Attendance settings
PRESENT_PERCENTAGE = 75
LATE_LIMIT_MINUTES = 5
INACTIVITY_CONFIRM_SECONDS = 120

# Zoom
ZOOM_SECRET_TOKEN = "6zW15yRhThaIzj3A18unYg"

# Optional host name hint for analytics exclusion
HOST_NAME_HINT = "Akshay Girase"

# Email settings
EMAIL_ENABLED = True
SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587
EMAIL_SENDER = "akshaygirase0606@gmail.com"
EMAIL_PASSWORD = "shjj bjjx mtpy pnkp"
EMAIL_RECEIVER = "akshaygirase0606@gmail.com"

# WhatsApp settings
WHATSAPP_ENABLED = True
WHATSAPP_NUMBER = "+919834698994"

# Render / cloud mode
CLOUD_MODE = True