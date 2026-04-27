# Zoom Attendance Platform — Real Modular Split

This version moves actual route code out of app.py into route/service modules while preserving the same Flask app object, URLs, DB helpers, Render start command, and Zoom webhook path.

## Running entry point

`app.py` is still the Gunicorn entry point:

```bash
gunicorn app:app
```

## Files now containing actual extracted route code

- routes/auth.py — login/logout/profile/root/theme
- routes/home.py — home dashboard
- routes/live.py — live dashboard APIs and /live
- routes/members.py — members and member profile
- routes/users.py — users dashboard
- routes/analytics.py — analytics, attendance register, AI analytics pages
- routes/meetings.py — meetings and report routes
- routes/settings.py — settings and appearance
- routes/activity.py — activity dashboard
- routes/system.py — favicon/health
- routes/alerts.py — alert run API
- services/zoom_webhook.py — Zoom webhook route
- services/notifications.py — push/email notification routes

## Shared helpers

For safety, shared helper functions and DB/schema utilities remain in app.py and are imported by modules at runtime. This avoids breaking the existing production logic while still putting route code in the correct places.

## Next migration stage

Move shared helpers from app.py into core/db.py, core/utils.py and services/attendance.py one group at a time.
