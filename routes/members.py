# Extracted from original app.py during real modular split.
# Do not run this file directly. It registers routes on the shared Flask app.
import sys
_legacy = sys.modules.get("app") or sys.modules.get("__main__")
if _legacy is None:
    raise RuntimeError("This module must be imported by app.py")
globals().update({name: getattr(_legacy, name) for name in dir(_legacy) if not name.startswith("__")})

# ---- members ----
@app.route("/members", methods=["GET", "POST"])
@login_required
def members():
    if request.method == "POST":
        action = request.form.get("action")

        if action == "add" and can_edit_users():
            full_name = request.form.get("full_name", "").strip()
            email = request.form.get("email", "").strip() or None
            phone = request.form.get("phone", "").strip() or None

            if full_name:
                with db() as conn:
                    true_val = db_true_value(conn, "members", "active")
                    with conn.cursor() as cur:
                        insert_member_record(cur, conn, full_name, email, phone, true_val)
                    conn.commit()
                log_activity("member_add", full_name)
                flash("Member added successfully.", "success")

        elif action == "edit" and can_edit_users():
            member_id = int(request.form.get("member_id"))
            full_name = request.form.get("full_name", "").strip()
            email = request.form.get("email", "").strip() or None
            phone = request.form.get("phone", "").strip() or None
            with db() as conn:
                with conn.cursor() as cur:
                    update_member_record(cur, conn, member_id, full_name, email, phone)
                conn.commit()
            log_activity("member_edit", str(member_id))
            flash("Member updated successfully.", "success")

        elif action == "toggle" and can_edit_users():
            member_id = int(request.form.get("member_id"))
            with db() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT active FROM members WHERE id=%s", (member_id,))
                    row = cur.fetchone()
                    if row:
                        next_val = db_false_value(conn, "members", "active") if is_truthy(row["active"]) else db_true_value(conn, "members", "active")
                        cur.execute("UPDATE members SET active=%s WHERE id=%s", (next_val, member_id))
                conn.commit()
            log_activity("member_toggle", str(member_id))
            flash("Member status updated.", "success")

        elif action == "delete" and can_edit_users():
            member_id = int(request.form.get("member_id"))
            with db() as conn:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM members WHERE id=%s", (member_id,))
                conn.commit()
            log_activity("member_delete", str(member_id))
            flash("Member deleted successfully.", "success")

        elif action == "import_csv" and can_edit_users():
            file = request.files.get("csv_file")
            imported = 0
            if file:
                stream = io.StringIO(file.stream.read().decode("utf-8"))
                reader = csv.DictReader(stream)
                with db() as conn:
                    true_val = db_true_value(conn, "members", "active")
                    with conn.cursor() as cur:
                        for row in reader:
                            name = (row.get("full_name") or row.get("name") or "").strip()
                            if not name:
                                continue
                            email = (row.get("email") or "").strip() or None
                            phone = (row.get("phone") or "").strip() or None
                            insert_member_record(cur, conn, name, email, phone, true_val)
                            imported += 1
                    conn.commit()
                log_activity("member_import", f"Imported {imported} members")
                flash(f"Imported {imported} members.", "success")

        return redirect(url_for("members"))

    q = request.args.get("q", "").strip().lower()
    edit_id = request.args.get("edit_id", "").strip()

    with db() as conn:
        with conn.cursor() as cur:
            member_name_field = member_name_sql(conn)
            cur.execute("SELECT COUNT(*) AS c FROM members")
            total_members_count = cur.fetchone()["c"]
            cur.execute(f"SELECT COUNT(*) AS c FROM members WHERE {ACTIVE_MEMBER_SQL}")
            active_members_count = cur.fetchone()["c"]
            inactive_members_count = total_members_count - active_members_count

            if q:
                cur.execute(
                    f"SELECT * FROM members WHERE (lower(COALESCE({member_name_field}, '')) LIKE %s OR lower(COALESCE(email,'')) LIKE %s OR lower(COALESCE(phone,'')) LIKE %s) ORDER BY id DESC",
                    (f"%{q}%", f"%{q}%", f"%{q}%"),
                )
            else:
                cur.execute("SELECT * FROM members ORDER BY id DESC")
            rows = cur.fetchall()

            edit_member = None
            if edit_id:
                cur.execute("SELECT * FROM members WHERE id=%s", (int(edit_id),))
                edit_member = cur.fetchone()

    body = render_template_string(
        """
        <div class="hero">
            <h2>Members</h2>
            <div class="muted" style="color:#cbd5e1">Manage members, import CSV, and maintain clean member vs non-member distinction.</div>
        </div>

        <div class='grid'>
            <div class='card stat-card'><h4>Total Members</h4><div class='metric'>{{ total_members_count }}</div></div>
            <div class='card stat-card'><h4>Active Members</h4><div class='metric'>{{ active_members_count }}</div></div>
            <div class='card stat-card'><h4>Inactive Members</h4><div class='metric'>{{ inactive_members_count }}</div></div>
        </div>

        <br>

        <div class='grid'>
            <div class='card'>
                <h3>{{ 'Edit Member' if edit_member else 'Add Member' }}</h3>
                {% if session.get('role') == 'admin' %}
                <form method='post'>
                    <input type='hidden' name='action' value='{{ "edit" if edit_member else "add" }}'>
                    {% if edit_member %}<input type='hidden' name='member_id' value='{{ edit_member.id }}'>{% endif %}
                    <label>Full Name</label>
                    <input name='full_name' required value='{{ member_display_name(edit_member) if edit_member else "" }}'>
                    <label>Email</label>
                    <input name='email' type='email' placeholder='member@example.com' value='{{ edit_member.email if edit_member else "" }}'>
                    <label>Phone</label>
                    <input name='phone' value='{{ edit_member.phone if edit_member else "" }}'>
                    <button type='submit'>{{ 'Update Member' if edit_member else 'Save Member' }}</button>
                    {% if edit_member %}
                        <a class='btn secondary' href='{{ url_for("members") }}'>Cancel</a>
                    {% endif %}
                </form>
                {% else %}
                <div class='muted'>Viewer can only view members.</div>
                {% endif %}
            </div>

            <div class='card'>
                <h3>CSV Import</h3>
                <div class='muted'>Expected columns: full_name, email, phone</div>
                {% if session.get('role') == 'admin' %}
                <form method='post' enctype='multipart/form-data'>
                    <input type='hidden' name='action' value='import_csv'>
                    <input type='file' name='csv_file' accept='.csv' required>
                    <button type='submit' class='btn success'>Import CSV</button>
                </form>
                {% endif %}
            </div>
        </div>

        <br>

        <div class='card'>
            <h3>Search Members</h3>
            <form method='get'>
                <input name='q' value='{{ q }}' placeholder='Search by name, email or phone'>
                <button type='submit'>Search</button>
            </form>

            <br>

            <div class="table-wrap">
                <table>
                    <tr>
                        <th>Name</th><th>Email</th><th>Phone</th><th>Status</th><th>Insights</th>
                        {% if session.get('role') == 'admin' %}<th>Actions</th>{% endif %}
                    </tr>
                    {% for m in rows %}
                        <tr>
                            <td>{{ member_display_name(m) }}</td>
                            <td>{{ m.email or '-' }}</td>
                            <td>{{ m.phone or '-' }}</td>
                            <td>
                                {% if m.active|string in ['1', 'True', 'true', 't'] %}
                                    <span class='badge ok'>Active</span>
                                {% else %}
                                    <span class='badge danger'>Inactive</span>
                                {% endif %}
                            </td>
                            <td><a class='btn secondary small' href='{{ url_for("member_profile", member_id=m.id) }}'>View Profile</a></td>
                            {% if session.get('role') == 'admin' %}
                            <td>
                                <div class='row'>
                                    <a class='btn secondary small' href='{{ url_for("members", edit_id=m.id) }}'>Edit</a>
                                    <form method='post' class='toggle-form'>
                                        <input type='hidden' name='action' value='toggle'>
                                        <input type='hidden' name='member_id' value='{{ m.id }}'>
                                        <button type='submit' class='status-toggle-btn {% if m.active|string in ['1', 'True', 'true', 't'] %}is-active{% endif %}' aria-label='Toggle member status'>
                                            <span class='status-toggle-label label-inactive'>Inactive</span>
                                            <span class='status-toggle-label label-active'>Active</span>
                                            <span class='status-toggle-knob'></span>
                                        </button>
                                    </form>
                                    <form method='post' onsubmit='return confirm("Delete this member?")'>
                                        <input type='hidden' name='action' value='delete'>
                                        <input type='hidden' name='member_id' value='{{ m.id }}'>
                                        <button type='submit' class='btn danger small'>Delete</button>
                                    </form>
                                </div>
                            </td>
                            {% endif %}
                        </tr>
                    {% endfor %}
                </table>
            </div>
        </div>
        """,
        rows=rows,
        q=q,
        edit_member=edit_member,
        member_display_name=member_display_name,
        total_members_count=total_members_count,
        active_members_count=active_members_count,
        inactive_members_count=inactive_members_count,
        session=session,
    )
    return page("Members", body, "members")



# ---- member_profile ----
@app.route("/members/<int:member_id>/profile")
@login_required
def member_profile(member_id):
    profile_data = build_member_profile_insights(member_id)
    if not profile_data:
        flash("Member not found.", "error")
        return redirect(url_for("members"))

    body = render_template_string(
        """
        <style>
            .member-profile-hero{display:grid;grid-template-columns:minmax(0,1.4fr) minmax(260px,.6fr);gap:16px;align-items:stretch}
            .profile-title{font-size:30px;font-weight:950;margin:0 0 8px}.profile-sub{color:#cbd5e1;font-weight:700}.profile-kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;margin:14px 0}.profile-kpi{border:1px solid rgba(148,163,184,.18);border-radius:18px;padding:14px;background:linear-gradient(180deg,rgba(255,255,255,.08),rgba(255,255,255,.025));box-shadow:0 16px 38px rgba(2,6,23,.18)}.profile-kpi small{display:block;color:#94a3b8;font-weight:900;text-transform:uppercase;letter-spacing:.08em}.profile-kpi strong{display:block;font-size:28px;margin-top:6px}.profile-chart-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}.profile-chart{height:320px;position:relative}.profile-chart.small{height:280px}.risk-pill{display:inline-flex;gap:8px;align-items:center;border-radius:999px;padding:8px 12px;font-weight:950;background:rgba(15,23,42,.55);border:1px solid rgba(148,163,184,.20)}.timeline-list{display:grid;gap:8px;max-height:360px;overflow:auto}.timeline-item{border:1px solid rgba(148,163,184,.16);border-radius:14px;padding:10px;background:rgba(255,255,255,.04)}.profile-actions{display:flex;gap:10px;flex-wrap:wrap;margin-top:10px}@media(max-width:1050px){.member-profile-hero,.profile-chart-grid{grid-template-columns:1fr}}
        
/* TOGGLE SWITCH */
.toggle-switch {
    position: relative;
    width: 120px;
    height: 36px;
    background: linear-gradient(90deg,#9333ea,#6366f1);
    border-radius: 999px;
    display: flex;
    align-items: center;
    cursor: pointer;
    padding: 4px;
}
.toggle-circle {
    width: 50%;
    height: 100%;
    background: white;
    border-radius: 999px;
    transition: all 0.3s ease;
}
.toggle-active .toggle-circle {
    transform: translateX(100%);
}
.toggle-label {
    position: absolute;
    width: 100%;
    text-align: center;
    font-size: 12px;
    font-weight: bold;
    color: white;
}

</style>
        <div class="member-profile-hero">
            <div class="hero">
                <div class="profile-sub">Member Profile / Deep Insights</div>
                <h2 class="profile-title">{{ member_display_name(data.member) }}</h2>
                <div class="profile-sub">Last seen: {{ data.summary.last_seen }} · Meetings tracked: {{ data.summary.meetings }}</div>
                <div class="profile-actions">
                    <a class="btn secondary" href="{{ url_for('members') }}">← Back to Members</a>
                    <a class="btn secondary" href="{{ url_for('analytics', member_ids=data.member.id) }}">Open in Analytics</a>
                </div>
            </div>
            <div class="card">
                <h3>Current Risk & Trend</h3>
                <div class="risk-pill">{{ data.summary.risk.emoji }} {{ data.summary.risk.label }}</div>
                <div style="height:12px"></div>
                <div class="risk-pill">{{ data.summary.trend.emoji }} {{ data.summary.trend.label }} {% if data.summary.trend.delta %}({{ data.summary.trend.delta }}){% endif %}</div>
                <p class="muted">Score combines attendance, consistency, duration participation, rejoins, and recent activity. Existing attendance logic is not changed.</p>
            </div>
        </div>

        <div class="profile-kpis">
            <div class="profile-kpi"><small>Attendance %</small><strong>{{ data.summary.attendance_percent }}%</strong></div>
            <div class="profile-kpi"><small>Overall Score</small><strong>{{ data.summary.overall_score }}</strong></div>
            <div class="profile-kpi"><small>Attendance Score</small><strong>{{ data.summary.attendance_score }}</strong></div>
            <div class="profile-kpi"><small>Engagement Score</small><strong>{{ data.summary.engagement_score }}</strong></div>
            <div class="profile-kpi"><small>Total Duration</small><strong>{{ data.summary.total_minutes }}m</strong></div>
            <div class="profile-kpi"><small>Average Duration</small><strong>{{ data.summary.avg_minutes }}m</strong></div>
            <div class="profile-kpi"><small>Late Count</small><strong>{{ data.summary.late }}</strong></div>
            <div class="profile-kpi"><small>Rejoins</small><strong>{{ data.summary.rejoins }}</strong></div>
        </div>

        <div class="profile-chart-grid">
            <div class="card profile-chart"><h3>Score Over Time</h3><canvas id="memberScoreChart"></canvas></div>
            <div class="card profile-chart"><h3>Duration Over Time</h3><canvas id="memberDurationChart"></canvas></div>
            <div class="card profile-chart small"><h3>Status Distribution</h3><canvas id="memberStatusChart"></canvas></div>
            <div class="card profile-chart small"><h3>Late Pattern</h3><canvas id="memberLateChart"></canvas></div>
        </div>

        <br>
        <div class="grid">
            <div class="card">
                <h3>Risk History</h3>
                <div class="timeline-list">
                    {% for label in data.charts.risk_labels|reverse %}
                    <div class="timeline-item"><b>{{ label }}</b> · Risk score {{ data.charts.risk_values[loop.revindex0] }}</div>
                    {% else %}<div class="muted">No risk history yet.</div>{% endfor %}
                </div>
            </div>
            <div class="card">
                <h3>Alert History</h3>
                <div class="timeline-list">
                    {% for alert in data.alerts %}
                    <div class="timeline-item"><b>{{ alert.title }}</b><br><span class="muted">{{ fmt_dt(alert.created_at) }} · {{ alert.current_state }}</span><br>{{ alert.message }}</div>
                    {% else %}<div class="muted">No smart alerts recorded for this member yet.</div>{% endfor %}
                </div>
            </div>
        </div>

        <br>
        <div class="card">
            <h3>Meeting-wise Member History</h3>
            <div class="table-wrap">
                <table>
                    <tr><th>Meeting</th><th>Date</th><th>Join</th><th>Leave</th><th>Duration</th><th>Rejoins</th><th>Status</th></tr>
                    {% for r in data.rows %}
                    <tr><td>{{ r.topic }}</td><td>{{ r.date }}</td><td>{{ r.join }}</td><td>{{ r.leave }}</td><td>{{ r.duration }} min</td><td>{{ r.rejoins }}</td><td><span class="badge {% if r.status == 'PRESENT' %}ok{% elif r.status == 'LATE' %}warn{% elif r.status == 'ABSENT' %}danger{% else %}info{% endif %}">{{ r.status }}</span></td></tr>
                    {% else %}<tr><td colspan="7">No attendance records found for this member.</td></tr>{% endfor %}
                </table>
            </div>
        </div>

        <script>
        const memberProfileData = {{ data.charts|tojson }};
        function memberProfilePalette(){return (window.getThemePalette?window.getThemePalette():{ok:'#22c55e',warn:'#f59e0b',danger:'#ef4444',a:'#6366f1',b:'#22d3ee',c:'#a855f7',text:'#cbd5e1',grid:'rgba(148,163,184,.18)'});}
        function makeMemberProfileCharts(){
            if(!window.Chart) return;
            const p=memberProfilePalette();
            new Chart(document.getElementById('memberScoreChart'),{type:'line',data:{labels:memberProfileData.labels,datasets:[{label:'Score',data:memberProfileData.score,borderColor:p.a,backgroundColor:p.a,fill:false,tension:.42}]},options:{responsive:true,maintainAspectRatio:false,scales:{y:{beginAtZero:true,max:100,grid:{color:p.grid},ticks:{color:p.text}},x:{grid:{color:p.grid},ticks:{color:p.text}}}}});
            new Chart(document.getElementById('memberDurationChart'),{type:'bar',data:{labels:memberProfileData.labels,datasets:[{label:'Duration minutes',data:memberProfileData.duration,backgroundColor:p.b,borderColor:p.b}]},options:{responsive:true,maintainAspectRatio:false,scales:{y:{beginAtZero:true,grid:{color:p.grid},ticks:{color:p.text}},x:{grid:{color:p.grid},ticks:{color:p.text}}}}});
            new Chart(document.getElementById('memberStatusChart'),{type:'doughnut',data:{labels:memberProfileData.status_distribution.labels,datasets:[{data:memberProfileData.status_distribution.values,backgroundColor:[p.ok,p.warn,p.danger,p.c]}]},options:{responsive:true,maintainAspectRatio:false}});
            new Chart(document.getElementById('memberLateChart'),{type:'bar',data:{labels:memberProfileData.late_pattern.map(x=>x.label),datasets:[{label:'Late count',data:memberProfileData.late_pattern.map(x=>x.count),backgroundColor:p.warn,borderColor:p.warn}]},options:{responsive:true,maintainAspectRatio:false,scales:{y:{beginAtZero:true,grid:{color:p.grid},ticks:{color:p.text}},x:{grid:{color:p.grid},ticks:{color:p.text}}}}});
        }
        document.addEventListener('DOMContentLoaded',()=>setTimeout(makeMemberProfileCharts,100));
        </script>
        """,
        data=profile_data,
        member_display_name=member_display_name,
        fmt_dt=fmt_dt,
    )
    return page("Member Profile", body, "members")


