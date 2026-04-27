# Extracted from original app.py during real modular split.
# Do not run this file directly. It registers routes on the shared Flask app.
import sys
_legacy = sys.modules.get("app") or sys.modules.get("__main__")
if _legacy is None:
    raise RuntimeError("This module must be imported by app.py")
globals().update({name: getattr(_legacy, name) for name in dir(_legacy) if not name.startswith("__")})

# ---- users ----
@app.route("/users", methods=["GET", "POST"])
@login_required
@admin_required
def users():
    if request.method == "POST":
        action = request.form.get("action")

        if action == "add":
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            role = request.form.get("role", "viewer")
            if username and password:
                with db() as conn:
                    true_val = db_true_value(conn, "users", "is_active")
                    with conn.cursor() as cur:
                        cur.execute(
                            "INSERT INTO users(username, password_hash, role, is_active) VALUES (%s,%s,%s,%s)",
                            (username, hash_password(password), role, true_val),
                        )
                    conn.commit()
                log_activity("user_add", username)
                flash("User created.", "success")

        elif action == "edit":
            user_id = int(request.form.get("user_id"))
            username = request.form.get("username", "").strip()
            role = request.form.get("role", "viewer")
            with db() as conn:
                with conn.cursor() as cur:
                    cur.execute("UPDATE users SET username=%s, role=%s WHERE id=%s", (username, role, user_id))
                conn.commit()
            log_activity("user_edit", str(user_id))
            flash("User updated.", "success")

        elif action == "toggle":
            user_id = int(request.form.get("user_id"))
            with db() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT is_active, username FROM users WHERE id=%s", (user_id,))
                    row = cur.fetchone()
                    if row:
                        if row["username"] == session.get("username"):
                            flash("You cannot disable your own active session.", "error")
                            return redirect(url_for("users"))
                        next_val = db_false_value(conn, "users", "is_active") if is_truthy(row["is_active"]) else db_true_value(conn, "users", "is_active")
                        cur.execute("UPDATE users SET is_active=%s WHERE id=%s", (next_val, user_id))
                conn.commit()
            log_activity("user_toggle", str(user_id))
            flash("User status updated.", "success")

        elif action == "password":
            user_id = int(request.form.get("user_id"))
            new_password = request.form.get("new_password", "")
            if new_password:
                with db() as conn:
                    with conn.cursor() as cur:
                        cur.execute("UPDATE users SET password_hash=%s WHERE id=%s", (hash_password(new_password), user_id))
                    conn.commit()
                log_activity("user_password", str(user_id))
                flash("Password changed.", "success")

        elif action == "delete":
            user_id = int(request.form.get("user_id"))
            with db() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT username FROM users WHERE id=%s", (user_id,))
                    row = cur.fetchone()
                    if row:
                        if row["username"] == session.get("username"):
                            flash("You cannot delete your own account while logged in.", "error")
                            return redirect(url_for("users"))
                        cur.execute("DELETE FROM users WHERE id=%s", (user_id,))
                conn.commit()
            log_activity("user_delete", str(user_id))
            flash("User deleted.", "success")

        return redirect(url_for("users"))

    edit_id = request.args.get("edit_id", "").strip()
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM users ORDER BY id DESC")
            rows = cur.fetchall()

            edit_user = None
            if edit_id:
                cur.execute("SELECT * FROM users WHERE id=%s", (int(edit_id),))
                edit_user = cur.fetchone()

    body = render_template_string(
        """
        <div class="hero">
            <h2>Users & Roles</h2>
            <div class="muted" style="color:#cbd5e1">Manage admin/viewer access, reset passwords, delete users, and control activity safely.</div>
        </div>

        <div class='grid'>
            <div class='card'>
                <h3>{{ 'Edit User' if edit_user else 'Create User' }}</h3>
                <form method='post'>
                    <input type='hidden' name='action' value='{{ "edit" if edit_user else "add" }}'>
                    {% if edit_user %}<input type='hidden' name='user_id' value='{{ edit_user.id }}'>{% endif %}
                    <label>Username</label>
                    <input name='username' required value='{{ edit_user.username if edit_user else "" }}'>
                    {% if not edit_user %}
                    <label>Password</label>
                    <input name='password' required>
                    {% endif %}
                    <label>Role</label>
                    <select name='role'>
                        <option value='viewer' {% if edit_user and edit_user.role == 'viewer' %}selected{% endif %}>viewer</option>
                        <option value='admin' {% if edit_user and edit_user.role == 'admin' %}selected{% endif %}>admin</option>
                    </select>
                    <button type='submit'>{{ 'Update User' if edit_user else 'Create' }}</button>
                    {% if edit_user %}
                        <a class='btn secondary' href='{{ url_for("users") }}'>Cancel</a>
                    {% endif %}
                </form>
            </div>

            <div class='card'>
                <h3>Role Guide</h3>
                <div class='muted'>
                    Admin can manage members, users, settings, imports, and finalization.
                    Viewer can safely view live dashboard, meetings, analytics, and reports.
                </div>
            </div>
        </div>

        <br>

        <div class='card'>
            <div class="table-wrap">
                <table>
                    <tr><th>Username</th><th>Role</th><th>Status</th><th>Created</th><th>Actions</th></tr>
                    {% for u in rows %}
                    <tr>
                        <td>{{ u.username }}</td>
                        <td>{{ u.role }}</td>
                        <td>
                            {% if u.is_active|string in ['1', 'True', 'true', 't'] %}
                                <span class='badge ok'>Active</span>
                            {% else %}
                                <span class='badge danger'>Disabled</span>
                            {% endif %}
                        </td>
                        <td>{{ fmt_dt(u.created_at) }}</td>
                        <td>
                            <div class='row'>
                                <a class='btn secondary small' href='{{ url_for("users", edit_id=u.id) }}'>Edit</a>
                                <form method='post'>
                                    <input type='hidden' name='action' value='toggle'>
                                    <input type='hidden' name='user_id' value='{{ u.id }}'>
                                    <button type='submit' class='status-toggle-btn {% if u.is_active|string in ['1', 'True', 'true', 't'] %}is-active{% endif %}' aria-label='Toggle user status'>
                                        <span class='status-toggle-label label-inactive'>Inactive</span>
                                        <span class='status-toggle-label label-active'>Active</span>
                                        <span class='status-toggle-knob'></span>
                                    </button>
                                </form>
                                <form method='post'>
                                    <input type='hidden' name='action' value='password'>
                                    <input type='hidden' name='user_id' value='{{ u.id }}'>
                                    <input name='new_password' placeholder='new password' required>
                                    <button class='btn secondary small' type='submit'>Reset Password</button>
                                </form>
                                <form method='post' onsubmit='return confirm("Delete this user?")'>
                                    <input type='hidden' name='action' value='delete'>
                                    <input type='hidden' name='user_id' value='{{ u.id }}'>
                                    <button class='btn danger small' type='submit'>Delete</button>
                                </form>
                            </div>
                        </td>
                    </tr>
                    {% endfor %}
                </table>
            </div>
        </div>
        """,
        rows=rows,
        fmt_dt=fmt_dt,
        edit_user=edit_user,
    )
    return page("Users", body, "users")


