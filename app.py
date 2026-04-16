from flask import Flask, request, jsonify, session, redirect
import psycopg
import os
from dotenv import load_dotenv
from datetime import datetime

load_dotenv()

app = Flask(__name__)
app.secret_key = "secret"

DB_URL = os.getenv("DATABASE_URL")

# ---------------- DB CONNECTION ----------------
def get_db():
    return psycopg.connect(DB_URL)

# ---------------- TEST DB ROUTE (NEW) ----------------
@app.route("/test-db")
def test_db():
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT NOW();")
        result = cur.fetchone()
        return f"DB Connected Successfully: {result}"
    except Exception as e:
        return f"DB Connection Error: {str(e)}"

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

    cur.execute("SELECT * FROM meetings ORDER BY id DESC")
    meetings = cur.fetchall()

    html = "<h1>Dashboard</h1>"

    html += "<h2>Meetings</h2><table border=1>"
    html += "<tr><th>#</th><th>Topic</th><th>Date</th><th>Action</th></tr>"

    for i, m in enumerate(meetings, start=1):
        html += f"<tr><td>{i}</td><td>{m[1]}</td><td>{m[2]}</td>"
        html += f"<td><a href='/report/{m[0]}'>View</a></td></tr>"

    html += "</table>"

    return html

# ---------------- WEBHOOK ----------------
@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json
    event = data.get("event")

    conn = get_db()
    cur = conn.cursor()

    if event == "meeting.participant_joined":
        p = data["payload"]["object"]["participant"]

        cur.execute("""
        INSERT INTO attendance(meeting_id, name, join_time)
        VALUES (%s,%s,%s)
        """, (1, p["user_name"], datetime.now()))

    if event == "meeting.participant_left":
        p = data["payload"]["object"]["participant"]

        cur.execute("""
        UPDATE attendance
        SET leave_time=%s
        WHERE name=%s AND leave_time IS NULL
        """, (datetime.now(), p["user_name"]))

    conn.commit()
    return jsonify({"status":"ok"})

# ---------------- REPORT ----------------
@app.route("/report/<int:meeting_id>")
def report(meeting_id):
    conn = get_db()
    cur = conn.cursor()

    # ONLY COUNT JOINED USERS
    cur.execute("""
    SELECT name FROM attendance
    WHERE meeting_id=%s AND join_time IS NOT NULL
    """, (meeting_id,))

    data = cur.fetchall()
    total = len(data)

    html = f"<h1>Total Participants (Joined Only): {total}</h1>"

    for d in data:
        html += f"<div>{d[0]}</div>"

    html += f"<br><a href='/pdf/{meeting_id}'>Download PDF</a>"

    return html

# ---------------- PDF ----------------
@app.route("/pdf/<int:meeting_id>")
def pdf(meeting_id):
    from reportlab.platypus import SimpleDocTemplate, Table

    conn = get_db()
    cur = conn.cursor()

    cur.execute("SELECT name FROM attendance WHERE meeting_id=%s", (meeting_id,))
    data = cur.fetchall()

    filename = f"report_{meeting_id}.pdf"

    doc = SimpleDocTemplate(filename)
    table = Table([["Name"]] + data)
    doc.build([table])

    return open(filename, "rb").read()

# ---------------- LIVE DASHBOARD ----------------
@app.route("/live")
def live():
    conn = get_db()
    cur = conn.cursor()

    # joined users
    cur.execute("SELECT name FROM attendance WHERE join_time IS NOT NULL")
    joined = [i[0] for i in cur.fetchall()]

    # all members
    cur.execute("SELECT name FROM members")
    all_members = [i[0] for i in cur.fetchall()]

    not_joined = list(set(all_members) - set(joined))

    html = "<h2>Joined Participants</h2>"
    for j in joined:
        html += f"<div style='color:green'>{j}</div>"

    html += "<h2>Not Joined Members</h2>"
    for n in not_joined:
        html += f"<div style='color:red'>{n}</div>"

    return html

# ---------------- RUN ----------------
if __name__ == "__main__":
    app.run(debug=True)