from flask import Flask, request, jsonify, session, redirect, url_for
import psycopg
import os
from dotenv import load_dotenv
from datetime import datetime

load_dotenv()

app = Flask(__name__)
app.secret_key = "secret"

DB_URL = os.getenv("DATABASE_URL")


# ---------------- DB ----------------
def get_db():
    return psycopg.connect(DB_URL)


# ---------------- TEST DB ----------------
@app.route("/test-db")
def test_db():
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT NOW();")
        result = cur.fetchone()
        return f"DB Connected Successfully: {result}"
    except Exception as e:
        return f"Error: {str(e)}"


# ---------------- LOGIN ----------------
@app.route("/", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if request.form["username"] == os.getenv("ADMIN_USERNAME") and request.form["password"] == os.getenv("ADMIN_PASSWORD"):
            session["user"] = "admin"
            return redirect("/dashboard")
        return "Login Failed"

    return '''
    <h2>Login</h2>
    <form method="post">
    <input name="username" placeholder="username"/>
    <input name="password" type="password"/>
    <button>Login</button>
    </form>
    '''


# ---------------- DASHBOARD ----------------
@app.route("/dashboard")
def dashboard():
    if "user" not in session:
        return redirect("/")

    conn = get_db()
    cur = conn.cursor()

    cur.execute("SELECT id, topic, date FROM meetings ORDER BY id DESC")
    meetings = cur.fetchall()

    html = "<h1>Dashboard</h1>"
    html += "<a href='/live'>Live</a> | <a href='/test-db'>Test DB</a><br><br>"

    html += "<table border=1>"
    html += "<tr><th>#</th><th>Topic</th><th>Date</th><th>Action</th></tr>"

    for i, m in enumerate(meetings, start=1):
        html += f"<tr><td>{i}</td><td>{m[1]}</td><td>{m[2]}</td>"
        html += f"<td><a href='/report/{m[0]}'>View</a></td></tr>"

    html += "</table>"

    return html


# ---------------- WEBHOOK ----------------
@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.json

        event = data.get("event")
        obj = data["payload"]["object"]
        participant = obj.get("participant", {})

        name = participant.get("user_name", "Unknown")
        topic = obj.get("topic", "Meeting")

        conn = get_db()
        cur = conn.cursor()

        # create/find meeting
        cur.execute("SELECT id FROM meetings WHERE topic=%s ORDER BY id DESC LIMIT 1", (topic,))
        row = cur.fetchone()

        if row:
            meeting_id = row[0]
        else:
            cur.execute("INSERT INTO meetings(topic, date) VALUES (%s,%s) RETURNING id",
                        (topic, datetime.now()))
            meeting_id = cur.fetchone()[0]

        # JOIN
        if event == "meeting.participant_joined":
            cur.execute("""
                INSERT INTO attendance(meeting_id, name, join_time)
                VALUES (%s,%s,%s)
            """, (meeting_id, name, datetime.now()))

        # LEAVE
        if event == "meeting.participant_left":
            cur.execute("""
                UPDATE attendance
                SET leave_time=%s
                WHERE id = (
                    SELECT id FROM attendance
                    WHERE meeting_id=%s AND name=%s AND leave_time IS NULL
                    ORDER BY id DESC LIMIT 1
                )
            """, (datetime.now(), meeting_id, name))

        conn.commit()
        return jsonify({"status": "ok"})

    except Exception as e:
        return jsonify({"error": str(e)})


# ---------------- REPORT ----------------
@app.route("/report/<int:meeting_id>")
def report(meeting_id):
    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
        SELECT DISTINCT name
        FROM attendance
        WHERE meeting_id=%s AND join_time IS NOT NULL
    """, (meeting_id,))

    users = cur.fetchall()
    total = len(users)

    html = f"<h2>Total Participants: {total}</h2>"

    for u in users:
        html += f"<div>{u[0]}</div>"

    return html


# ---------------- LIVE (FINAL FIXED) ----------------
@app.route("/live")
def live():
    try:
        conn = get_db()
        cur = conn.cursor()

        # JOINED USERS
        cur.execute("""
            SELECT DISTINCT name
            FROM attendance
            WHERE join_time IS NOT NULL
        """)
        joined = [r[0] for r in cur.fetchall()]

        # SAFE MEMBERS FETCH (NO CRASH)
        try:
            cur.execute("SELECT name FROM members")
            members = [r[0] for r in cur.fetchall()]
        except:
            members = []

        not_joined = [m for m in members if m not in joined]

        html = "<h1>Live Dashboard</h1>"

        html += "<h2>Joined</h2>"
        if joined:
            for j in joined:
                html += f"<div style='color:green'>{j}</div>"
        else:
            html += "<div>No one joined</div>"

        html += "<h2>Not Joined</h2>"
        if not_joined:
            for n in not_joined:
                html += f"<div style='color:red'>{n}</div>"
        else:
            html += "<div>No members found</div>"

        return html

    except Exception as e:
        return f"<h2>ERROR:</h2><pre>{str(e)}</pre>"


# ---------------- RUN ----------------
if __name__ == "__main__":
    app.run(debug=True)