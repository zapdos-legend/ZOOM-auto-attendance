from flask import Flask, request, session, redirect, url_for
import psycopg
import os
from functools import wraps

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "12345")

DATABASE_URL = os.environ.get("DATABASE_URL")


def get_conn():
    return psycopg.connect(DATABASE_URL)


def init_users():
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS users(
            id SERIAL PRIMARY KEY,
            username TEXT UNIQUE,
            password TEXT,
            role TEXT
        )
    """)
    conn.commit()

    cur.execute("SELECT * FROM users WHERE username=%s", ("admin",))
    if not cur.fetchone():
        cur.execute(
            "INSERT INTO users(username,password,role) VALUES(%s,%s,%s)",
            ("admin", "admin123", "admin")
        )
        conn.commit()

    conn.close()


init_users()


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()

        conn = get_conn()
        cur = conn.cursor()
        cur.execute(
            "SELECT role FROM users WHERE username=%s AND password=%s",
            (username, password)
        )
        user = cur.fetchone()
        conn.close()

        if user:
            session["user"] = username
            session["role"] = user[0]
            return redirect("/dashboard")

        return "Invalid login"

    return """
    <h2>Login</h2>
    <form method='post'>
        <input name='username' placeholder='username'><br><br>
        <input name='password' type='password' placeholder='password'><br><br>
        <button type='submit'>Login</button>
    </form>
    """


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")


def login_required(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        if "user" not in session:
            return redirect("/login")
        return func(*args, **kwargs)
    return wrapper


@app.route("/")
def home():
    return redirect("/login")


@app.route("/dashboard")
@login_required
def dashboard():
    return """
    <h1>Dashboard</h1>
    <a href='/analytics'>Analytics</a><br><br>
    <a href='/users'>Manage Users</a><br><br>
    <a href='/logout'>Logout</a>
    """


@app.route("/users")
@login_required
def users():
    if session.get("role") != "admin":
        return "Access Denied"

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT username, role FROM users ORDER BY username")
    users_data = cur.fetchall()
    conn.close()

    rows = ""
    for u in users_data:
        rows += f"<tr><td>{u[0]}</td><td>{u[1]}</td></tr>"

    return f"""
    <h2>Users</h2>
    <table border='1' cellpadding='8'>
        <tr><th>Username</th><th>Role</th></tr>
        {rows}
    </table>

    <h3>Add User</h3>
    <form method='post' action='/add-user'>
        <input name='username' placeholder='username' required><br><br>
        <input name='password' placeholder='password' required><br><br>
        <select name='role'>
            <option value='admin'>admin</option>
            <option value='viewer'>viewer</option>
        </select><br><br>
        <button type='submit'>Add</button>
    </form>

    <br>
    <a href='/dashboard'>Back</a>
    """


@app.route("/add-user", methods=["POST"])
@login_required
def add_user():
    if session.get("role") != "admin":
        return "Access Denied"

    username = request.form.get("username", "").strip()
    password = request.form.get("password", "").strip()
    role = request.form.get("role", "").strip()

    if not username or not password or role not in ("admin", "viewer"):
        return "Invalid input"

    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            "INSERT INTO users(username,password,role) VALUES(%s,%s,%s)",
            (username, password, role)
        )
        conn.commit()
    except Exception as e:
        conn.rollback()
        conn.close()
        return f"Error adding user: {e}"

    conn.close()
    return redirect("/users")


@app.route("/analytics")
@login_required
def analytics():
    present = 10
    late = 3
    absent = 2

    return f"""
    <h1>Analytics</h1>
    <canvas id="pieChart" width="400" height="400"></canvas>

    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <script>
    new Chart(document.getElementById('pieChart'), {{
        type: 'pie',
        data: {{
            labels: ['Present','Late','Absent'],
            datasets: [{{
                data: [{present}, {late}, {absent}],
                backgroundColor: ['green','orange','red']
            }}]
        }}
    }});
    </script>

    <br>
    <a href='/dashboard'>Back</a>
    """


if __name__ == "__main__":
    app.run(debug=True)