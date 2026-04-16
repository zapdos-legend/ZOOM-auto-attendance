from flask import Flask, request, jsonify, session, redirect, url_for
import psycopg
import os
from dotenv import load_dotenv
from datetime import datetime
from reportlab.platypus import SimpleDocTemplate, Table
from reportlab.lib.pagesizes import letter

load_dotenv()

app = Flask(__name__)
app.secret_key = "secret-key-change-later"

DB_URL = os.getenv("DATABASE_URL")


# ---------------- DB CONNECTION ----------------
def get_db():
    return psycopg.connect(DB_URL)


# ---------------- TEST DB ROUTE ----------------
@app.route("/test-db")
def test_db():
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT NOW();")
        result = cur.fetchone()
        cur.close()
        conn.close()
        return f"DB Connected Successfully: {result}"
    except Exception as e:
        return f"DB Connection Error: {str(e)}", 500


# ---------------- LOGIN ----------------
@app.route("/", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()

        admin_username = os.getenv("ADMIN_USERNAME", "admin")
        admin_password = os.getenv("ADMIN_PASSWORD", "admin123")

        if username == admin_username and password == admin_password:
            session["user"] = username
            return redirect(url_for("dashboard"))
        return """
        <h2>Login Failed</h2>
        <a href="/">Back to Login</a>
        """

    return """
    <h2>Login</h2>
    <form method="post">
        <input name="username" placeholder="username" />
        <input name="password" type="password" placeholder="password" />
        <button type="submit">Login</button>
    </form>
    """


# ---------------- LOGOUT ----------------
@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------------- DASHBOARD ----------------
@app.route("/dashboard")
def dashboard():
    if "user" not in session:
        return redirect(url_for("login"))

    try:
        conn = get_db()
        cur = conn.cursor()

        cur.execute("""
            SELECT id, topic, date
            FROM meetings
            ORDER BY id DESC
        """)
        meetings = cur.fetchall()

        cur.close()
        conn.close()

        html = """
        <h1>Dashboard</h1>
        <p>
            <a href="/live">Live Dashboard</a> |
            <a href="/test-db">Test DB</a> |
            <a href="/logout">Logout</a>
        </p>
        <h2>Meetings</h2>
        <table border="1" cellpadding="6" cellspacing="0">
            <tr>
                <th>#</th>
                <th>Meeting ID</th>
                <th>Topic</th>
                <th>Date</th>
                <th>Action</th>
            </tr>
        """

        if meetings:
            for display_index, meeting in enumerate(meetings, start=1):
                meeting_pk = meeting[0]
                topic = meeting[1] if meeting[1] else "No Topic"
                date = meeting[2] if meeting[2] else ""

                html += f"""
                <tr>
                    <td>{display_index}</td>
                    <td>{meeting_pk}</td>
                    <td>{topic}</td>
                    <td>{date}</td>
                    <td><a href="/report/{meeting_pk}">View</a></td>
                </tr>
                """
        else:
            html += """
            <tr>
                <td colspan="5">No meetings found</td>
            </tr>
            """

        html += "</table>"
        return html

    except Exception as e:
        return f"<h1>Dashboard Error</h1><pre>{str(e)}</pre>", 500


# ---------------- WEBHOOK ----------------
@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.get_json(silent=True) or {}
        event = data.get("event", "")

        payload = data.get("payload", {})
        obj = payload.get("object", {})
        participant = obj.get("participant", {})

        meeting_topic = obj.get("topic", "Untitled Meeting")
        external_meeting_id = str(obj.get("id", "0"))
        participant_name = participant.get("user_name", "Unknown Participant")

        conn = get_db()
        cur = conn.cursor()

        # Ensure meeting exists
        cur.execute("""
            SELECT id FROM meetings
            WHERE topic = %s
            ORDER BY id DESC
            LIMIT 1
        """, (meeting_topic,))
        existing_meeting = cur.fetchone()

        if existing_meeting:
            meeting_id = existing_meeting[0]
        else:
            cur.execute("""
                INSERT INTO meetings (topic, date)
                VALUES (%s, %s)
                RETURNING id
            """, (meeting_topic, datetime.now()))
            meeting_id = cur.fetchone()[0]

        if event == "meeting.participant_joined":
            cur.execute("""
                INSERT INTO attendance (meeting_id, name, join_time, leave_time)
                VALUES (%s, %s, %s, NULL)
            """, (meeting_id, participant_name, datetime.now()))

        elif event == "meeting.participant_left":
            cur.execute("""
                UPDATE attendance
                SET leave_time = %s
                WHERE id = (
                    SELECT id FROM attendance
                    WHERE meeting_id = %s
                      AND name = %s
                      AND leave_time IS NULL
                    ORDER BY id DESC
                    LIMIT 1
                )
            """, (datetime.now(), meeting_id, participant_name))

        conn.commit()
        cur.close()
        conn.close()

        return jsonify({
            "status": "ok",
            "event": event,
            "meeting_id": external_meeting_id,
            "participant": participant_name
        })

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


# ---------------- REPORT ----------------
@app.route("/report/<int:meeting_id>")
def report(meeting_id):
    try:
        conn = get_db()
        cur = conn.cursor()

        # Count only actual joined participants
        cur.execute("""
            SELECT name, join_time, leave_time
            FROM attendance
            WHERE meeting_id = %s
              AND join_time IS NOT NULL
            ORDER BY id ASC
        """, (meeting_id,))
        rows = cur.fetchall()

        cur.execute("""
            SELECT topic, date
            FROM meetings
            WHERE id = %s
        """, (meeting_id,))
        meeting = cur.fetchone()

        cur.close()
        conn.close()

        topic = meeting[0] if meeting else "Unknown Topic"
        date = meeting[1] if meeting else ""

        unique_joined = []
        seen = set()
        for row in rows:
            name = row[0]
            if name not in seen:
                seen.add(name)
                unique_joined.append(row)

        total_joined = len(seen)

        html = f"""
        <h1>Attendance Report</h1>
        <p><strong>Topic:</strong> {topic}</p>
        <p><strong>Date:</strong> {date}</p>
        <p><strong>Total Participants (Joined Only):</strong> {total_joined}</p>
        <p>
            <a href="/pdf/{meeting_id}">Download PDF</a> |
            <a href="/dashboard">Back to Dashboard</a>
        </p>
        <table border="1" cellpadding="6" cellspacing="0">
            <tr>
                <th>#</th>
                <th>Name</th>
                <th>Join Time</th>
                <th>Leave Time</th>
            </tr>
        """

        if rows:
            for i, row in enumerate(rows, start=1):
                name = row[0]
                join_time = row[1] if row[1] else ""
                leave_time = row[2] if row[2] else ""
                html += f"""
                <tr>
                    <td>{i}</td>
                    <td>{name}</td>
                    <td>{join_time}</td>
                    <td>{leave_time}</td>
                </tr>
                """
        else:
            html += """
            <tr>
                <td colspan="4">No attendance data found</td>
            </tr>
            """

        html += "</table>"
        return html

    except Exception as e:
        return f"<h1>Report Error</h1><pre>{str(e)}</pre>", 500


# ---------------- PDF ----------------
@app.route("/pdf/<int:meeting_id>")
def pdf(meeting_id):
    try:
        conn = get_db()
        cur = conn.cursor()

        cur.execute("""
            SELECT name, join_time, leave_time
            FROM attendance
            WHERE meeting_id = %s
              AND join_time IS NOT NULL
            ORDER BY id ASC
        """, (meeting_id,))
        rows = cur.fetchall()

        cur.close()
        conn.close()

        filename = f"report_{meeting_id}.pdf"
        pdf_path = os.path.join("/tmp", filename)

        table_data = [["Name", "Join Time", "Leave Time"]]
        for row in rows:
            table_data.append([
                str(row[0]) if row[0] else "",
                str(row[1]) if row[1] else "",
                str(row[2]) if row[2] else ""
            ])

        doc = SimpleDocTemplate(pdf_path, pagesize=letter)
        table = Table(table_data)
        doc.build([table])

        with open(pdf_path, "rb") as f:
            pdf_bytes = f.read()

        return (
            pdf_bytes,
            200,
            {
                "Content-Type": "application/pdf",
                "Content-Disposition": f"inline; filename={filename}"
            }
        )

    except Exception as e:
        return f"<h1>PDF Error</h1><pre>{str(e)}</pre>", 500


# ---------------- LIVE DASHBOARD ----------------
@app.route("/live")
def live():
    try:
        conn = get_db()
        cur = conn.cursor()

        # Joined participants
        cur.execute("""
            SELECT DISTINCT name
            FROM attendance
            WHERE join_time IS NOT NULL
            ORDER BY name ASC
        """)
        joined = [row[0] for row in cur.fetchall()]

        # Members table may be empty, but route must not crash
        cur.execute("""
            SELECT name
            FROM members
            ORDER BY name ASC
        """)
        all_members = [row[0] for row in cur.fetchall()]

        cur.close()
        conn.close()

        not_joined = [name for name in all_members if name not in joined]

        html = """
        <h1>Live Dashboard</h1>
        <p><a href="/dashboard">Back to Dashboard</a></p>
        <h2>Joined Participants</h2>
        """

        if joined:
            html += "<ul>"
            for name in joined:
                html += f"<li style='color:green;'>{name}</li>"
            html += "</ul>"
        else:
            html += "<p>No one joined yet</p>"

        html += "<h2>Not Joined Members</h2>"

        if not_joined:
            html += "<ul>"
            for name in not_joined:
                html += f"<li style='color:red;'>{name}</li>"
            html += "</ul>"
        else:
            html += "<p>No members found or all members already joined</p>"

        return html

    except Exception as e:
        return f"<h1>Live Dashboard Error</h1><pre>{str(e)}</pre>", 500


# ---------------- HEALTH ----------------
@app.route("/health")
def health():
    return "OK"


# ---------------- RUN ----------------
if __name__ == "__main__":
    app.run(debug=True)