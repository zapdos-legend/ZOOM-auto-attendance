# ================================
# NEW IMPORTS ADDED (CHART + LOGIN)
# ================================
from flask import Flask, request, jsonify, session, redirect, url_for
import sqlite3
import psycopg
import os

# ================================
# EXISTING APP
# ================================
app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "12345")

DATABASE_URL = os.environ.get("DATABASE_URL")

# ================================
# DB CONNECTION
# ================================
def get_conn():
    return psycopg.connect(DATABASE_URL)

# ================================
# 🆕 USERS TABLE (NEW FEATURE)
# ================================
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

    # default admin
    cur.execute("SELECT * FROM users WHERE username='admin'")
    if not cur.fetchone():
        cur.execute("INSERT INTO users(username,password,role) VALUES('admin','admin123','admin')")
        conn.commit()

    conn.close()

init_users()

# ================================
# 🆕 LOGIN SYSTEM (DB BASED)
# ================================
@app.route("/login", methods=["GET","POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username")
        password = request.form.get("password")

        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT role FROM users WHERE username=%s AND password=%s",(username,password))
        user = cur.fetchone()
        conn.close()

        if user:
            session["user"] = username
            session["role"] = user[0]
            return redirect("/dashboard")
        else:
            return "Invalid login"

    return """
    <h2>Login</h2>
    <form method='post'>
        <input name='username' placeholder='username'><br>
        <input name='password' type='password'><br>
        <button>Login</button>
    </form>
    """

# ================================
# AUTH DECORATOR
# ================================
def login_required(func):
    def wrapper(*args, **kwargs):
        if "user" not in session:
            return redirect("/login")
        return func(*args, **kwargs)
    return wrapper

# ================================
# DASHBOARD
# ================================
@app.route("/dashboard")
@login_required
def dashboard():
    return """
    <h1>Dashboard</h1>
    <a href='/analytics'>Analytics</a><br>
    <a href='/users'>Manage Users</a><br>
    """

# ================================
# 🆕 USER MANAGEMENT (ADMIN)
# ================================
@app.route("/users")
@login_required
def users():
    if session.get("role") != "admin":
        return "Access Denied"

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT username,role FROM users")
    users = cur.fetchall()
    conn.close()

    rows = ""
    for u in users:
        rows += f"<tr><td>{u[0]}</td><td>{u[1]}</td></tr>"

    return f"""
    <h2>Users</h2>
    <table border=1>
    <tr><th>Username</th><th>Role</th></tr>
    {rows}
    </table>

    <h3>Add User</h3>
    <form method='post' action='/add-user'>
    <input name='username'>
    <input name='password'>
    <select name='role'>
        <option>admin</option>
        <option>viewer</option>
    </select>
    <button>Add</button>
    </form>
    """

@app.route("/add-user", methods=["POST"])
@login_required
def add_user():
    if session.get("role") != "admin":
        return "Access Denied"

    u = request.form["username"]
    p = request.form["password"]
    r = request.form["role"]

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("INSERT INTO users(username,password,role) VALUES(%s,%s,%s)",(u,p,r))
    conn.commit()
    conn.close()

    return redirect("/users")

# ================================
# 🆕 ANALYTICS WITH REAL CHARTS
# ================================
@app.route("/analytics")
@login_required
def analytics():
    # dummy values (replace with real DB later)
    present = 10
    late = 3
    absent = 2

    return f"""
    <h1>Analytics</h1>

    <canvas id="pieChart"></canvas>

    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <script>
    new Chart(document.getElementById('pieChart'), {{
        type: 'pie',
        data: {{
            labels: ['Present','Late','Absent'],
            datasets: [{{
                data: [{present},{late},{absent}],
                backgroundColor: ['green','orange','red']
            }}]
        }}
    }});
    </script>
    """

# ================================
# START
# ================================
if __name__ == "__main__":
    app.run(debug=True)