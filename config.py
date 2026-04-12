import os

# Folder to store reports
OUTPUT_FOLDER = "attendance_reports"

# Live status file for dashboard
LIVE_DATA_FILE = "data/live_status.json"

# Scheduler storage
SCHEDULER_FILE = "data/scheduler.json"

# Database file
DB_FILE = "data/members.db"

# IST Offset
IST_OFFSET_HOURS = 5
IST_OFFSET_MINUTES = 30

# Late marking
LATE_LIMIT_MINUTES = 5

# Attendance Criteria
PRESENT_PERCENTAGE = 75

# EMAIL SETTINGS
EMAIL_ENABLED = True
SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587
EMAIL_SENDER = "akshaygirase0606@gmail.com"
EMAIL_PASSWORD = "shjj bjjx mtpy pnkp"
EMAIL_RECEIVER = "akshaygirase0606@gmail.com"

# WhatsApp automation (works only on local PC)
WHATSAPP_ENABLED = True
WHATSAPP_NUMBER = "+919834698994"

# Cloud mode flag (Render deployment)
# If running on Render, set this to True (WhatsApp auto disabled)
CLOUD_MODE = False