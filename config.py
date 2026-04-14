import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Folders and files
DATA_DIR = os.path.join(BASE_DIR, 'data')
OUTPUT_FOLDER = os.path.join(BASE_DIR, 'attendance_reports')
LIVE_DATA_FILE = os.path.join(DATA_DIR, 'live_status.json')
SCHEDULER_FILE = os.path.join(DATA_DIR, 'scheduler.json')
DB_FILE = os.path.join(DATA_DIR, 'members.db')

# Timezone
TIMEZONE_NAME = os.environ.get('TIMEZONE_NAME', 'Asia/Kolkata')
IST_OFFSET_HOURS = 5
IST_OFFSET_MINUTES = 30

# Attendance settings
PRESENT_PERCENTAGE = int(os.environ.get('PRESENT_PERCENTAGE', '75'))
LATE_LIMIT_MINUTES = int(os.environ.get('LATE_LIMIT_MINUTES', '5'))
INACTIVITY_CONFIRM_SECONDS = int(os.environ.get('INACTIVITY_CONFIRM_SECONDS', '120'))

# Zoom
ZOOM_SECRET_TOKEN = os.environ.get('ZOOM_SECRET_TOKEN', 'CHANGE_ME')
HOST_NAME_HINT = os.environ.get('HOST_NAME_HINT', 'Akshay Girase')

# Email settings
EMAIL_ENABLED = os.environ.get('EMAIL_ENABLED', 'true').lower() == 'true'
EMAIL_PROVIDER = os.environ.get('EMAIL_PROVIDER', 'resend').strip().lower()

# Generic sender settings
EMAIL_SENDER = os.environ.get('EMAIL_SENDER', '').strip()
EMAIL_RECEIVER = os.environ.get('EMAIL_RECEIVER', EMAIL_SENDER).strip()

# SMTP settings (kept for fallback / local use)
SMTP_SERVER = os.environ.get('SMTP_SERVER', 'smtp.gmail.com').strip()
SMTP_PORT = int(os.environ.get('SMTP_PORT', '587'))
EMAIL_PASSWORD = os.environ.get('EMAIL_PASSWORD', '').strip()

# Resend API settings
RESEND_API_KEY = os.environ.get('RESEND_API_KEY', '').strip()
# For first testing you can use:
# EMAIL_FROM = "Zoom Attendance <onboarding@resend.dev>"
# In production use a verified domain sender.
EMAIL_FROM = os.environ.get('EMAIL_FROM', EMAIL_SENDER).strip()

# WhatsApp settings
WHATSAPP_ENABLED = os.environ.get('WHATSAPP_ENABLED', 'false').lower() == 'true'
WHATSAPP_NUMBER = os.environ.get('WHATSAPP_NUMBER', '')

# Render / cloud mode
CLOUD_MODE = os.environ.get('CLOUD_MODE', 'true').lower() == 'true'

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(OUTPUT_FOLDER, exist_ok=True)