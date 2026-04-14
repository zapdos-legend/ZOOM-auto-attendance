import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# IMPORTANT → Persistent DB for Render
DATA_DIR = os.environ.get("DATA_DIR", "/opt/render/project/src/data")

DB_FILE = os.path.join(DATA_DIR, "members.db")
OUTPUT_FOLDER = os.path.join(DATA_DIR, "attendance_reports")

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(OUTPUT_FOLDER, exist_ok=True)

# Core settings
TIMEZONE_NAME = os.environ.get("TIMEZONE_NAME", "Asia/Kolkata")
PRESENT_PERCENTAGE = int(os.environ.get("PRESENT_PERCENTAGE", "75"))

# WhatsApp
WHATSAPP_MODE = os.environ.get("WHATSAPP_MODE", "render_link")