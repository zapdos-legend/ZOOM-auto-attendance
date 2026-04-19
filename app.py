from flask import Flask, render_template, request, redirect, session, jsonify
import psycopg
import os
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.secret_key = "secret123"

DATABASE_URL = os.getenv("DATABASE_URL")

# ================= DB =================

def get_conn():
    return psycopg.connect(DATABASE_URL)

def rows_to_dicts(rows):
    return [dict(r) for r in rows]

def is_pg():
    return True


# ================= LOGIN =================

def login_required(fn):
    def wrapper(*args, **kwargs):
        if "user" not in session:
            return redirect("/login")
        return fn(*args, **kwargs)
    wrapper.__name__ = fn.__name__
    return wrapper


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form["username"]
        password = request.form["password"]

        conn = get_conn()
        cur = conn.cursor()

        cur.execute("SELECT * FROM users WHERE username=%s AND password=%s", (username, password))
        user = cur.fetchone()

        conn.close()

        if user:
            session["user"] = username
            return redirect("/dashboard")

        return "Invalid login"

    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")


# ================= FIXED ANALYTICS FUNCTION =================

def get_analytics():
    conn = get_conn()
    cur = conn.cursor()

    # totals
    cur.execute("SELECT COUNT(*) FROM meetings")
    total_meetings = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM members")
    total_members = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM members WHERE active = 1")
    active_members = cur.fetchone()[0]

    # attendance counts
    cur.execute("SELECT COUNT(*) FROM attendance WHERE status='PRESENT'")
    present_count = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM attendance WHERE status='LATE'")
    late_count = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM attendance WHERE status='ABSENT'")
    absent_count = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM attendance WHERE is_unknown=1")
    unknown_count = cur.fetchone()[0]

    total_records = present_count + late_count + absent_count

    attendance_rate = 0
    if total_records > 0:
        attendance_rate = round(((present_count + late_count) / total_records) * 100, 2)

    # top attendees
    cur.execute("""
        SELECT name, SUM(duration_minutes)
        FROM attendance
        GROUP BY name
        ORDER BY SUM(duration_minutes) DESC
        LIMIT 5
    """)
    top_attendees = cur.fetchall()

    conn.close()

    return {
        "total_meetings": total_meetings,
        "total_members": total_members,
        "active_members": active_members,
        "present_count": present_count,
        "late_count": late_count,
        "absent_count": absent_count,
        "unknown_count": unknown_count,
        "attendance_rate": attendance_rate,
        "top_attendees": top_attendees
    }


# ================= DASHBOARD =================

@app.route("/dashboard")
@login_required
def dashboard_home():
    analytics = get_analytics()

    return render_template("dashboard.html", data=analytics)