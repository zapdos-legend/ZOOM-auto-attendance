"""Configuration compatibility module.
Real env values are still initialized in app.py during this safe split.
"""
import sys
_app = sys.modules.get('app') or sys.modules.get('__main__')
if _app:
    DATABASE_URL = getattr(_app, 'DATABASE_URL', '')
    TIMEZONE_NAME = getattr(_app, 'TIMEZONE_NAME', 'Asia/Kolkata')
    ZOOM_SECRET_TOKEN = getattr(_app, 'ZOOM_SECRET_TOKEN', '')
    DEFAULT_SETTINGS = getattr(_app, 'DEFAULT_SETTINGS', {})
