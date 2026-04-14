import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# FREE PLAN SAFE STORAGE
DATA_DIR = os.path.join(BASE_DIR, "data")

DB_FILE = os.path.join(DATA_DIR, "members.db")
OUTPUT_FOLDER = os.path.join(DATA_DIR, "attendance_reports")

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(OUTPUT_FOLDER, exist_ok=True)

TIMEZONE_NAME = "Asia/Kolkata"
PRESENT_PERCENTAGE = 75

WHATSAPP_MODE = "render_link"