# Extracted from original app.py during real modular split.
# Do not run this file directly. It registers routes on the shared Flask app.
import sys
_legacy = sys.modules.get("app") or sys.modules.get("__main__")
if _legacy is None:
    raise RuntimeError("This module must be imported by app.py")
globals().update({name: getattr(_legacy, name) for name in dir(_legacy) if not name.startswith("__")})

# ---- toggle_theme ----
@app.route("/toggle-theme")
@login_required
def toggle_theme():
    session["theme"] = "light" if session.get("theme") == "dark" else "dark"
    return redirect(request.referrer or url_for("home"))



# ---- index ----
@app.route("/", methods=["GET", "HEAD"])
def index():
    if session.get("user_id"):
        return redirect(url_for("home"))
    return redirect(url_for("login"))



# ---- login ----
@app.route("/login", methods=["GET", "POST"])
def login():
    login_error = None

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        with db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT * FROM users WHERE username=%s AND {ACTIVE_USER_SQL}",
                    (username,),
                )
                user = cur.fetchone()

        if user and user["password_hash"] == hash_password(password):
            session["user_id"] = user["id"]
            session["username"] = user["username"]
            session["role"] = user["role"]
            if "theme" not in session:
                session["theme"] = "light"
            session["login_time"] = now_local().isoformat()
            log_activity("login", f"{username} logged in | role={user['role']}")
            return redirect(url_for("home"))

        login_error = "Invalid username or password"
        flash("Invalid username or password", "error")

    body = render_template_string(
        """
        <div class="login-box">
            <div class="login-side">
                <h1 style="margin:0 0 14px 0">Zoom Attendance Platform</h1>
                <p style="color:#dbeafe;line-height:1.7">
                    Track Zoom meeting attendance with live monitoring, member vs non-member distinction,
                    strong analytics, exportable reports, role-based login, and professional dashboard UI.
                </p>
                <div class="row" style="margin-top:20px">
                    <span class="badge info">Live Tracking</span>
                    <span class="badge ok">Reports</span>
                    <span class="badge warn">Analytics</span>
                    <span class="badge gray">Role Based Access</span>
                </div>
            </div>
            <div class="login-card">
                <h2 style="margin-top:0">Welcome Back</h2>
                <p class="muted">Login to continue to your attendance dashboard.</p>

                {% if login_error %}
                    <div class='login-error'>{{ login_error }}</div>\n                    <div class='app-note'>Use your assigned role credentials. The UI is mobile-friendly and tuned for dark SaaS mode.</div>
                {% endif %}

                <form method='post'>
                    <label>Username</label>
                    <input name='username' required value='{{ request.form.get("username", "") if request.method == "POST" else "" }}'>
                    <label>Password</label>
                    <input type='password' name='password' required>
                    <button type='submit' style="width:100%">Login</button>
                </form>
            </div>
        </div>
        """,
        login_error=login_error,
        request=request,
    )
    return render_template_string(BASE_HTML, title="Login", body=body, nav=[], active="")



# ---- logout ----
@app.route("/logout")
def logout():
    login_time = parse_dt(session.get("login_time"))
    logout_time = now_local()
    duration_seconds = int((logout_time - login_time).total_seconds()) if login_time else 0
    log_activity("logout", f"{session.get('username')} logged out | role={session.get('role')} | duration_seconds={duration_seconds}")
    session.clear()
    return redirect(url_for("login"))



# ---- profile ----
@app.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    if request.method == "POST":
        current_password = request.form.get("current_password", "")
        new_password = request.form.get("new_password", "")
        confirm_password = request.form.get("confirm_password", "")

        with db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM users WHERE id=%s", (session["user_id"],))
                user = cur.fetchone()

                if not user or user["password_hash"] != hash_password(current_password):
                    flash("Current password is incorrect.", "error")
                    return redirect(url_for("profile"))

                if not new_password or len(new_password) < 4:
                    flash("New password must be at least 4 characters.", "error")
                    return redirect(url_for("profile"))

                if new_password != confirm_password:
                    flash("New password and confirm password do not match.", "error")
                    return redirect(url_for("profile"))

                cur.execute(
                    "UPDATE users SET password_hash=%s WHERE id=%s",
                    (hash_password(new_password), session["user_id"]),
                )
            conn.commit()

        log_activity("profile_password_change", session.get("username"))
        flash("Password updated successfully.", "success")
        return redirect(url_for("profile"))

    body = render_template_string(
        """
        <div class="hero">
            <div class="hero-grid">
                <div>
                    <div class="badge info" style="margin-bottom:12px">Identity Center</div>
                    <h1 class="hero-title">My Profile & Security</h1>
                    <div class="hero-copy">Manage account identity, password security, and access posture from one polished control area.</div>
                </div>
                <div class="hero-stats">
                    <div class="hero-chip"><div class="small">Username</div><div class="big">{{ session.get('username') }}</div></div>
                    <div class="hero-chip"><div class="small">Role</div><div class="big">{{ session.get('role') }}</div></div>
                </div>
            </div>
        </div>
        <div class="stat-strip">
            <div class="compact-kpi"><div class="k">Account status</div><div class="v">Active</div></div>
            <div class="compact-kpi"><div class="k">Session theme</div><div class="v">{{ session.get('theme', 'light')|title }}</div></div>
            <div class="compact-kpi"><div class="k">Security mode</div><div class="v">Protected</div></div>
        </div>
        <div class="profile-shell" style="margin-top:16px">
            <div class="card glass-panel">
                <div class="section-title">
                    <div><h3 style="margin:0">Account Snapshot</h3><p>Profile identity and quick recovery guidance.</p></div>
                    <span class="badge ok">Stable</span>
                </div>
                <div class="mini-list">
                    <div class="mini-item"><div class="muted">Username</div><div style="font-weight:900;margin-top:4px">{{ session.get('username') }}</div></div>
                    <div class="mini-item"><div class="muted">Role</div><div style="font-weight:900;margin-top:4px">{{ session.get('role') }}</div></div>
                    <div class="mini-item"><div class="muted">Recommendation</div><div style="font-weight:900;margin-top:4px">Change your password regularly and avoid sharing admin credentials.</div></div>
                </div>
            </div>
            <div class="card">
                <div class="section-title"><div><h3 style="margin:0">Change Password</h3><p>Apply a new password without affecting current project data.</p></div></div>
                <form method="post">
                    <label>Current Password</label>
                    <input type="password" name="current_password" required>
                    <label>New Password</label>
                    <input type="password" name="new_password" required>
                    <label>Confirm New Password</label>
                    <input type="password" name="confirm_password" required>
                    <div class="app-note" style="margin:10px 0 14px 0">Use at least 4 characters. Longer passwords are safer for admin roles.</div>
                    <button type="submit">Update Password</button>
                </form>
            </div>
        </div>
        """
    )
    return page("Profile", body, "profile")


