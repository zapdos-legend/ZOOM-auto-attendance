import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

DATA_DIR = os.path.join(BASE_DIR, "data")
DB_FILE = os.path.join(DATA_DIR, "members.db")
OUTPUT_FOLDER = os.path.join(DATA_DIR, "attendance_reports")

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(OUTPUT_FOLDER, exist_ok=True)

TIMEZONE_NAME = os.environ.get("TIMEZONE_NAME", "Asia/Kolkata")

# Main attendance rule
PRESENT_PERCENTAGE = int(os.environ.get("PRESENT_PERCENTAGE", "75"))

# Late-grace minutes for optional future logic / compatibility
LATE_THRESHOLD_MINUTES = int(os.environ.get("LATE_THRESHOLD_MINUTES", "10"))

# Summary rule:
# if someone is Late but duration > this % of meeting time,
# count them as present in summary
LATE_COUNT_AS_PRESENT_PERCENTAGE = int(
    os.environ.get("LATE_COUNT_AS_PRESENT_PERCENTAGE", "30")
)

WHATSAPP_MODE = os.environ.get("WHATSAPP_MODE", "render_link")