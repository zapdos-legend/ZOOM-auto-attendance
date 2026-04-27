# Zoom Attendance Platform — Exact Structure Map

This ZIP has the exact folder layout you requested.

IMPORTANT SAFETY NOTE:
- Your latest working app.py is kept as the running source of truth.
- The new files/folders exist exactly as the final architecture map.
- Logic is NOT forcibly moved yet because moving a huge live production app in one step can break routes, templates, DB helpers, webhook handling, or Render deployment.
- Next, we migrate one feature at a time into these files while preserving behavior.

## Exact Structure

```text
zoom-attendance/
│
├── app.py                     Main entry point
│
├── routes/                    Page logic
│   ├── live.py                Live dashboard
│   ├── members.py             Members page
│   ├── users.py               Users page
│   ├── analytics.py           Analytics
│   ├── activity.py            Activity logs
│   ├── auth.py                Login / logout
│
├── services/                  Core logic
│   ├── zoom_webhook.py        Zoom webhook
│   ├── attendance.py          Join/leave logic
│   ├── analytics_service.py   Calculations
│
├── core/                      Shared system
│   ├── db.py                  Database connection
│   ├── utils.py               Helper functions
│   ├── config.py              Env variables
│
├── templates/                 UI HTML
│   ├── base.html
│   ├── live.html
│   ├── members.html
│   ├── users.html
│   ├── analytics.html
│
├── static/                    Frontend
│   ├── css/style.css
│   ├── js/app.js
│
├── requirements.txt
├── Procfile
```

## How to migrate safely next

Recommended sequence:
1. Move CSS into static/css/style.css
2. Move JS into static/js/app.js
3. Move live page into routes/live.py + templates/live.html
4. Move members page into routes/members.py + templates/members.html
5. Move users page
6. Move analytics page
7. Move activity page
8. Move Zoom webhook and attendance services

## How you should ask for future fixes

- Live dashboard issue → say "Live issue"
- Members page issue → say "Members issue"
- Users page issue → say "Users issue"
- Analytics issue → say "Analytics issue"
- Webhook issue → say "Webhook issue"

I will tell you exactly which file(s) to upload or update.
