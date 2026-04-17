# ZOOM-auto-attendance
# Zoom Attendance Platform

A real-time Zoom attendance tracking and analytics platform built with Flask, Neon/PostgreSQL, and Zoom webhooks.

## Main features

- Zoom webhook based join/leave tracking
- Live dashboard for current meeting
- Active members not joined yet
- Meeting-wise attendance reports
- PDF and CSV report generation
- Old PDF/CSV fallback from database after restart
- Analytics dashboard
- Member search and management
- Date and topic based meeting filters
- Login system
- Role-based access (admin / viewer)
- CSV import for members
- Configurable attendance rules

---

## Tech stack

- Flask
- PostgreSQL / Neon
- Render
- ReportLab
- Chart.js
- HTML/CSS

---

## Project structure

- `app.py` → main application
- `requirements.txt` → Python dependencies
- `runtime.txt` → Python version for Render
- `Procfile` → Render start command
- `.env.example` → environment variables example

---

## Environment variables

Set these in Render:

- `DATABASE_URL`
- `FLASK_SECRET_KEY`
- `TIMEZONE_NAME`
- `ADMIN_USERNAME`
- `ADMIN_PASSWORD`
- `VIEWER_USERNAME`
- `VIEWER_PASSWORD`
- `PRESENT_PERCENTAGE`
- `LATE_COUNT_AS_PRESENT_PERCENTAGE`
- `LATE_THRESHOLD_MINUTES`
- `INACTIVITY_CONFIRM_SECONDS`
- `ZOOM_SECRET_TOKEN`
- `HOST_NAME_HINT`

---

## Local run

Install dependencies:

```bash
pip install -r requirements.txt