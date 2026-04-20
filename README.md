# Final Zoom Attendance Platform

This is the cleaned single-codebase version of your Zoom attendance platform.

## What this version solves

- one final stable Flask app
- no missing template dependency
- one database schema only
- Zoom webhook join/leave/end handling
- member vs unknown participant distinction
- joined-only participant counting
- live page with active members not yet joined
- login + role-based access
- CSV member import
- configurable attendance rules
- meeting list + persistent report export from database
- analytics filters + CSV/PDF export
- activity log

## Files

- `app.py` -> full application
- `requirements.txt` -> dependencies
- `Procfile` -> Render start command
- `runtime.txt` -> Python version
- `.env.example` -> environment variable sample

## Render start command

Already covered by Procfile:

```bash
gunicorn app:app
```

## Important route

Set your Zoom webhook URL to:

```text
https://YOUR-RENDER-APP.onrender.com/zoom/webhook
```

## Local run

```bash
pip install -r requirements.txt
python app.py
```

## Notes

Reports persist because meetings and attendance stay in PostgreSQL. CSV and PDF are generated from stored data whenever you open/export them, so restart does not remove report capability.
