"""Utility compatibility module for the modular split.
Shared helpers are still owned by app.py during this safe migration.
"""
import sys
_app = sys.modules.get('app') or sys.modules.get('__main__')
if _app:
    now_local = getattr(_app, 'now_local')
    today_local = getattr(_app, 'today_local')
    parse_dt = getattr(_app, 'parse_dt')
    fmt_dt = getattr(_app, 'fmt_dt')
    fmt_date = getattr(_app, 'fmt_date')
    fmt_time = getattr(_app, 'fmt_time')
    fmt_time_ampm = getattr(_app, 'fmt_time_ampm')
    slugify = getattr(_app, 'slugify')
