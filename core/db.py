"""Database compatibility module for the modular split.
The real db helpers are currently owned by app.py and imported here safely.
"""
import sys
_app = sys.modules.get('app') or sys.modules.get('__main__')
if _app:
    db = getattr(_app, 'db')
    init_db = getattr(_app, 'init_db')
    table_exists = getattr(_app, 'table_exists')
    column_exists = getattr(_app, 'column_exists')
    ensure_column = getattr(_app, 'ensure_column')
    ensure_index = getattr(_app, 'ensure_index')
