from flask import Flask, request, redirect, url_for, session, jsonify
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import psycopg
from psycopg.rows import dict_row
from dotenv import load_dotenv
from reportlab.platypus import SimpleDocTemplate, Table
from reportlab.lib.pagesizes import letter

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "secret")

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
APP_TZ = ZoneInfo(os.getenv("TIMEZONE_NAME", "Asia/Kolkata"))
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123")


def get_db():
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


def escape_html(text):
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def render_page(title, body):
    return f"""
    <!doctype html>
    <html>
    <head>
        <title>{escape_html(title)}</title>
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <style>
            body {{
                font-family: Arial, sans-serif;
                background: #f4f7fb;
                margin: 0;
                color: #111827;
            }}
            .top {{
                background: #0b1530;
                color: white;
                padding: 18px 24px;
            }}
            .top a {{
                color: white;
                text-decoration: none;
                margin-right: 18px;
                font-weight: 600;
            }}
            .wrap {{
                max-width: 1200px;
                margin: 24px auto;
                padding: 0 16px;
            }}
            .card {{
                background: white;
                border-radius: 14px;
                box-shadow: 0 6px 24px rgba(0,0,0,0.08);
                padding: 18px;
                margin-bottom: 20px;
            }}
            .grid {{
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
                gap: 14px;
                margin-bottom: 20px;
            }}
            .metric-label {{
                color: #6b7280;
                font-size: 13px;
            }}
            .metric-value {{
                font-size: 28px;
                font-weight: 700;
                margin-top: 6px;
            }}
            table {{
                width: 100%;
                border-collapse: collapse;
                background: white;
            }}
            th, td {{
                border-bottom: 1px solid #e5e7eb;
                padding: 10px;
                text-align: left;
                font-size: 14px;
            }}
            th {{
                background: #f9fafb;
            }}
            .btn {{
                display: inline-block;
                padding: 7px 12px;
                border-radius: 10px;
                text-decoration: none;
                color: white;
                font-size: 13px;
                font-weight: 700;
            }}
            .btn-blue {{
                background: #2563eb;
            }}
            .btn-gray {{
                background: #6b7280;
            }}
            .badge {{
                padding: 4px 8px;
                border-radius: 999px;
                font-size: 12px;
                font-weight: 700;
                display: inline-block;
            }}
            .green {{
                background: #dcfce7;
                color: #166534;
            }}
            .red {{
                background: #fee2e2;
                color: #991b1b;
            }}
            .orange {{
                background: #ffedd5;
                color: #9a3412;
            }}
            .blue {{
                background: #dbeafe;
                color: #1d4ed8;
            }}
            input {{
                padding: 10px;
                border: 1px solid #d1d5db;
                border-radius: 10px;
            }}
            button {{
                padding: 10px 14px;
                border: none;
                border-radius: 10px;
                background: #2563eb;
                color: white;
                font-weight: 700;
                cursor: pointer;
            }}
        </style>
    </head>
    <body>
        <div class="top">
            <div style="font-size:18px;font-weight:700;margin-bottom:8px;">Zoom Attendance Platform</div>
            <a href="/dashboard">Meetings</a>
            <a href="/live">Live</a>
            <a href="/members">Members</a>
            <a href="/test-db">Test DB</a>
            <a href="/logout">Logout</a>
        </div>
        <div class="wrap">{body}</div>
    </body>
    </html>
    """


@app.route("/test-db")
def test_db():
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT NOW() AS now")
                row = cur.fetchone()
        return f"DB Connected Successfully: {row['now']}"
    except Exception as e:
        return f"DB Connection Error: {escape_html(str(e))}", 500


@app.route("/", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()

        if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
            session["user"] = username
            return redirect(url_for("dashboard"))

        return "<h2>Login Failed</h2><a href='/'>Back</a>"

    return """
    <h2>Login</h2>
    <form method="post">
        <input name="username" placeholder="username">
        <input name="password" type="password" placeholder="password">
        <button type="submit">Login</button>
    </form>
    """


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


def require_login():
    return "user" in session


@app.route("/dashboard")
def dashboard():
    if not require_login():
        return redirect(url_for("login"))

    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT id, zoom_meeting_id, topic, meeting_date, start_time, end_time, total_minutes, csv_file, pdf_file
                    FROM meetings
                    ORDER BY id DESC
                """)
                meetings = cur.fetchall()

        rows = []
        for idx, m in enumerate(meetings, start=1):
            csv_link = f"<a class='btn btn-blue' href='/download/csv/{m['id']}'>CSV</a>" if m.get("csv_file") else "-"
            pdf_link = f"<a class='btn btn-gray' href='/download/pdf/{m['id']}'>PDF</a>" if m.get("pdf_file") else "-"

            rows.append(f"""
            <tr>
                <td>{idx}</td>
                <td>{escape_html(m.get('topic') or 'No Topic')}</td>
                <td>{escape_html(m.get('meeting_date') or '-')}</td>
                <td>{escape_html(m.get('start_time') or '-')}</td>
                <td>{escape_html(m.get('end_time') or '-')}</td>
                <td>{m.get('total_minutes') or 0}</td>
                <td>{csv_link} {pdf_link}</td>
                <td><a class='btn btn-blue' href='/report/{m['id']}'>Open</a></td>
            </tr>
            """)

        body = f"""
        <div class="card">
            <h2>Recent Meetings</h2>
            <table>
                <tr>
                    <th>#</th>
                    <th>Topic</th>
                    <th>Date</th>
                    <th>Start</th>
                    <th>End</th>
                    <th>Total Minutes</th>
                    <th>Reports</th>
                    <th>Action</th>
                </tr>
                {''.join(rows) if rows else '<tr><td colspan="8">No meetings found</td></tr>'}
            </table>
        </div>
        """
        return render_page("Meetings Dashboard", body)

    except Exception as e:
        return f"<h1>Dashboard Error</h1><pre>{escape_html(str(e))}</pre>", 500


@app.route("/live")
@app.route("/dashboard/live")
def live():
    if not require_login():
        return redirect(url_for("login"))

    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT zoom_meeting_id, topic
                    FROM meetings
                    ORDER BY id DESC
                    LIMIT 1
                """)
                meeting = cur.fetchone()

                cur.execute("""
                    SELECT participant_name, status, COALESCE(duration_minutes, 0) AS duration_minutes,
                           COALESCE(rejoins, 0) AS rejoins, COALESCE(is_host, 0) AS is_host
                    FROM attendance
                    WHERE meeting_pk = COALESCE((SELECT id FROM meetings ORDER BY id DESC LIMIT 1), 0)
                    ORDER BY duration_minutes DESC, participant_name ASC
                """)
                participants = cur.fetchall()

                cur.execute("""
                    SELECT name
                    FROM members
                    WHERE active = 1
                    ORDER BY name ASC
                """)
                members = [r["name"] for r in cur.fetchall()]

        joined_names = []
        participant_rows = []
        live_count = 0
        left_count = 0
        host_name = "-"
        max_minutes = 0.0

        for p in participants:
            name = p.get("participant_name") or "Unknown"
            status = (p.get("status") or "LEFT").upper()
            minutes = float(p.get("duration_minutes") or 0)
            rejoins = int(p.get("rejoins") or 0)
            is_host = int(p.get("is_host") or 0) == 1

            joined_names.append(name)
            max_minutes = max(max_minutes, minutes)

            if status == "LIVE":
                live_count += 1
            else:
                left_count += 1

            if is_host:
                host_name = name

            participant_rows.append(f"""
            <tr>
                <td>{escape_html(name)}</td>
                <td><span class="badge {'green' if status == 'LIVE' else 'orange'}">{escape_html(status)}</span></td>
                <td>{minutes}</td>
                <td>{rejoins}</td>
                <td>{'Yes' if is_host else 'No'}</td>
            </tr>
            """)

        not_joined = [m for m in members if m not in set(joined_names)]
        not_joined_rows = "".join(f"<tr><td>{escape_html(n)}</td></tr>" for n in not_joined)

        meeting_topic = meeting["topic"] if meeting else "No live meeting"
        meeting_zoom_id = meeting["zoom_meeting_id"] if meeting else "-"

        body = f"""
        <div class="grid">
            <div class="card"><div class="metric-label">Live Topic</div><div class="metric-value">{escape_html(meeting_topic)}</div></div>
            <div class="card"><div class="metric-label">Meeting ID</div><div class="metric-value">{escape_html(meeting_zoom_id)}</div></div>
            <div class="card"><div class="metric-label">Live Count</div><div class="metric-value">{live_count}</div></div>
            <div class="card"><div class="metric-label">Left Count</div><div class="metric-value">{left_count}</div></div>
            <div class="card"><div class="metric-label">Detected Host</div><div class="metric-value">{escape_html(host_name)}</div></div>
            <div class="card"><div class="metric-label">Top Duration So Far</div><div class="metric-value">{max_minutes} min</div></div>
        </div>

        <div class="card">
            <h2>Live Participants</h2>
            <table>
                <tr>
                    <th>Name</th>
                    <th>Status</th>
                    <th>Duration (Min)</th>
                    <th>Rejoins</th>
                    <th>Host</th>
                </tr>
                {''.join(participant_rows) if participant_rows else '<tr><td colspan="5">No participant data yet.</td></tr>'}
            </table>
        </div>

        <div class="card">
            <h2>Active Members Not Joined Yet</h2>
            <table>
                <tr><th>Name</th></tr>
                {not_joined_rows if not_joined_rows else '<tr><td>All active members joined or no active members found.</td></tr>'}
            </table>
        </div>
        """
        return render_page("Live Dashboard", body)

    except Exception as e:
        return f"<h1>Live Dashboard Error</h1><pre>{escape_html(str(e))}</pre>", 500


@app.route("/members")
@app.route("/dashboard/members")
def members_page():
    if not require_login():
        return redirect(url_for("login"))

    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT id, name, email, whatsapp, active
                    FROM members
                    ORDER BY name ASC
                """)
                members = cur.fetchall()

        rows = []
        for m in members:
            rows.append(
                f"<tr><td>{m['id']}</td><td>{escape_html(m['name'])}</td><td>{escape_html(m.get('email') or '')}</td><td>{escape_html(m.get('whatsapp') or '')}</td><td>{'Yes' if m.get('active') else 'No'}</td></tr>"
            )

        body = f"""
        <div class="card">
            <h2>Members</h2>
            <table>
                <tr>
                    <th>ID</th>
                    <th>Name</th>
                    <th>Email</th>
                    <th>WhatsApp</th>
                    <th>Active</th>
                </tr>
                {''.join(rows) if rows else '<tr><td colspan="5">No members found</td></tr>'}
            </table>
        </div>
        """
        return render_page("Members", body)

    except Exception as e:
        return f"<h1>Members Error</h1><pre>{escape_html(str(e))}</pre>", 500


@app.route("/report/<int:meeting_id>")
def report(meeting_id):
    if not require_login():
        return redirect(url_for("login"))

    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT topic, meeting_date, start_time, end_time, total_minutes
                    FROM meetings
                    WHERE id = %s
                """, (meeting_id,))
                meeting = cur.fetchone()

                cur.execute("""
                    SELECT participant_name, join_time, leave_time, duration_minutes, rejoins, status, is_host
                    FROM attendance
                    WHERE meeting_pk = %s
                    ORDER BY COALESCE(duration_minutes, 0) DESC, participant_name ASC
                """, (meeting_id,))
                rows = cur.fetchall()

        joined_names = {
            r["participant_name"]
            for r in rows
            if r.get("join_time") not in (None, "", "-")
        }
        total_participants = len(joined_names)

        tr = []
        for i, r in enumerate(rows, start=1):
            status = (r.get("status") or "-").upper()
            badge = "green" if status == "PRESENT" else ("red" if status == "ABSENT" else ("orange" if status == "LATE" else "blue"))

            tr.append(f"""
            <tr>
                <td>{i}</td>
                <td>{escape_html(r.get('participant_name') or '')}</td>
                <td>{escape_html(str(r.get('join_time') or '-'))}</td>
                <td>{escape_html(str(r.get('leave_time') or '-'))}</td>
                <td>{r.get('duration_minutes') or 0}</td>
                <td>{r.get('rejoins') or 0}</td>
                <td><span class="badge {badge}">{escape_html(status)}</span></td>
            </tr>
            """)

        body = f"""
        <div class="grid">
            <div class="card"><div class="metric-label">Topic</div><div class="metric-value">{escape_html(meeting['topic'] if meeting else '-')}</div></div>
            <div class="card"><div class="metric-label">Date</div><div class="metric-value">{escape_html(meeting['meeting_date'] if meeting else '-')}</div></div>
            <div class="card"><div class="metric-label">Start</div><div class="metric-value">{escape_html(meeting['start_time'] if meeting else '-')}</div></div>
            <div class="card"><div class="metric-label">End</div><div class="metric-value">{escape_html(meeting['end_time'] if meeting else '-')}</div></div>
            <div class="card"><div class="metric-label">Meeting Minutes</div><div class="metric-value">{meeting['total_minutes'] if meeting and meeting['total_minutes'] is not None else 0}</div></div>
            <div class="card"><div class="metric-label">Joined Participants</div><div class="metric-value">{total_participants}</div></div>
        </div>

        <div class="card">
            <p>
                <a class="btn btn-blue" href="/download/pdf/{meeting_id}">PDF</a>
                <a class="btn btn-gray" href="/download/csv/{meeting_id}">CSV</a>
            </p>
            <table>
                <tr>
                    <th>#</th>
                    <th>Name</th>
                    <th>Join</th>
                    <th>Leave</th>
                    <th>Duration</th>
                    <th>Rejoins</th>
                    <th>Status</th>
                </tr>
                {''.join(tr) if tr else '<tr><td colspan="7">No attendance data found.</td></tr>'}
            </table>
        </div>
        """
        return render_page("Attendance Report", body)

    except Exception as e:
        return f"<h1>Report Error</h1><pre>{escape_html(str(e))}</pre>", 500


@app.route("/download/pdf/<int:meeting_id>")
def download_pdf(meeting_id):
    if not require_login():
        return redirect(url_for("login"))

    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT participant_name, join_time, leave_time, duration_minutes, status
                    FROM attendance
                    WHERE meeting_pk = %s
                    ORDER BY COALESCE(duration_minutes, 0) DESC, participant_name ASC
                """, (meeting_id,))
                rows = cur.fetchall()

        file_name = f"meeting_{meeting_id}.pdf"
        pdf_path = f"/tmp/{file_name}"

        data = [["Name", "Join", "Leave", "Duration", "Status"]]
        for r in rows:
            data.append([
                str(r.get("participant_name") or ""),
                str(r.get("join_time") or "-"),
                str(r.get("leave_time") or "-"),
                str(r.get("duration_minutes") or 0),
                str(r.get("status") or ""),
            ])

        doc = SimpleDocTemplate(pdf_path, pagesize=letter)
        doc.build([Table(data)])

        with open(pdf_path, "rb") as f:
            content = f.read()

        return content, 200, {
            "Content-Type": "application/pdf",
            "Content-Disposition": f"inline; filename={file_name}"
        }

    except Exception as e:
        return f"<h1>PDF Error</h1><pre>{escape_html(str(e))}</pre>", 500


@app.route("/download/csv/<int:meeting_id>")
def download_csv(meeting_id):
    if not require_login():
        return redirect(url_for("login"))

    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT csv_file
                    FROM meetings
                    WHERE id = %s
                """, (meeting_id,))
                meeting = cur.fetchone()

        csv_file = meeting["csv_file"] if meeting else None

        if not csv_file or not os.path.exists(csv_file):
            return "CSV file not found on server for this meeting.", 404

        with open(csv_file, "rb") as f:
            content = f.read()

        return content, 200, {
            "Content-Type": "text/csv",
            "Content-Disposition": f"attachment; filename={os.path.basename(csv_file)}"
        }

    except Exception as e:
        return f"<h1>CSV Error</h1><pre>{escape_html(str(e))}</pre>", 500


@app.route("/health")
def health():
    return "OK"


if __name__ == "__main__":
    app.run(debug=True)