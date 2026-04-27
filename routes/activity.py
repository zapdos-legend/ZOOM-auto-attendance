# Extracted from original app.py during real modular split.
# Do not run this file directly. It registers routes on the shared Flask app.
import sys
_legacy = sys.modules.get("app") or sys.modules.get("__main__")
if _legacy is None:
    raise RuntimeError("This module must be imported by app.py")
globals().update({name: getattr(_legacy, name) for name in dir(_legacy) if not name.startswith("__")})

# ---- activity ----
@app.route("/activity")
@login_required
def activity():
    """Clean production activity dashboard: login/logout sessions + Zoom join/leave activity."""
    def _positive_int(value, default, minimum=1, maximum=500):
        try:
            number = int(value)
        except Exception:
            number = default
        return max(minimum, min(maximum, number))

    def _activity_kind(action):
        text = str(action or "").lower()
        if "login" in text:
            return "login"
        if "logout" in text:
            return "logout"
        if "participant" in text or "zoom" in text or "join" in text or "leave" in text or "meeting" in text:
            return "zoom"
        return "other"

    page_no = _positive_int(request.args.get("page", 1), 1, 1, 100000)
    per_page = _positive_int(request.args.get("per_page", 50), 50, 10, 100)
    offset = (page_no - 1) * per_page

    allowed_actions = [
        "login", "logout", "zoom_participant_event", "zoom_started", "zoom_meeting_ended",
        "member_add", "member_edit", "member_toggle", "user_add", "user_edit", "user_toggle"
    ]
    action_filter = str(request.args.get("action", "") or "").strip()
    username_filter = str(request.args.get("username", "") or "").strip()

    where = []
    params = []
    if action_filter:
        where.append("action=%s")
        params.append(action_filter)
    else:
        where.append("(action ILIKE %s OR action ILIKE %s OR action ILIKE %s OR action ILIKE %s OR action ILIKE %s)")
        params.extend(["%login%", "%logout%", "%zoom%", "%join%", "%leave%"])
    if username_filter:
        where.append("username=%s")
        params.append(username_filter)
    where_sql = " WHERE " + " AND ".join(where) if where else ""

    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) AS total FROM activity_log{where_sql}", params)
            total_rows = int((cur.fetchone() or {}).get("total") or 0)

            cur.execute(
                f"""
                SELECT id, username, action, details, created_at
                FROM activity_log
                {where_sql}
                ORDER BY created_at::timestamp DESC NULLS LAST, id DESC
                LIMIT %s OFFSET %s
                """,
                params + [per_page, offset],
            )
            rows = cur.fetchall()

            cur.execute("SELECT DISTINCT username FROM activity_log WHERE username IS NOT NULL AND username<>'' ORDER BY username LIMIT 80")
            users = [r.get("username") for r in cur.fetchall() if r.get("username")]

            cur.execute(
                """
                SELECT
                    COUNT(*) AS total,
                    COUNT(*) FILTER (WHERE action ILIKE '%login%') AS logins,
                    COUNT(*) FILTER (WHERE action ILIKE '%logout%') AS logouts,
                    COUNT(*) FILTER (WHERE action ILIKE '%zoom%' OR action ILIKE '%join%' OR action ILIKE '%leave%') AS zoom_events
                FROM activity_log
                """
            )
            summary = cur.fetchone() or {}

    total_pages = max((total_rows + per_page - 1) // per_page, 1)
    page_no = min(page_no, total_pages)

    body = render_template_string(
        """
        <div class="hero">
            <div class="hero-grid">
                <div>
                    <div class="badge info" style="margin-bottom:12px">Clean Activity Dashboard</div>
                    <h1 class="hero-title">Activity Timeline</h1>
                    <div class="hero-copy">Shows important production activity only: user login/logout sessions and Zoom meeting join/leave events.</div>
                </div>
                <div class="hero-stats">
                    <div class="hero-chip"><div class="small">Total Logs</div><div class="big">{{ summary.total or 0 }}</div></div>
                    <div class="hero-chip"><div class="small">Logins</div><div class="big">{{ summary.logins or 0 }}</div></div>
                    <div class="hero-chip"><div class="small">Logouts</div><div class="big">{{ summary.logouts or 0 }}</div></div>
                    <div class="hero-chip"><div class="small">Zoom Events</div><div class="big">{{ summary.zoom_events or 0 }}</div></div>
                </div>
            </div>
        </div>

        <div class="card glass-panel" style="margin-bottom:16px">
            <form method="get" class="audit-filter-grid">
                <div><label>Activity Type</label><select name="action"><option value="">Login / Logout / Zoom only</option>{% for act in allowed_actions %}<option value="{{ act }}" {% if action_filter==act %}selected{% endif %}>{{ act }}</option>{% endfor %}</select></div>
                <div><label>User</label><select name="username"><option value="">All users</option>{% for u in users %}<option value="{{ u }}" {% if username_filter==u %}selected{% endif %}>{{ u }}</option>{% endfor %}</select></div>
                <div><label>Rows</label><select name="per_page"><option value="25" {% if per_page==25 %}selected{% endif %}>25</option><option value="50" {% if per_page==50 %}selected{% endif %}>50</option><option value="100" {% if per_page==100 %}selected{% endif %}>100</option></select></div>
                <div style="display:flex;gap:8px;align-items:end"><button type="submit">Apply</button><a class="ghost-link" href="{{ url_for('activity') }}">Reset</a></div>
            </form>
        </div>

        <div class="card glass-panel">
            <div class="section-title"><div><h3 style="margin:0">Important Activity</h3><p>User role/session activity and Zoom meeting presence activity.</p></div></div>
            <div class="table-wrap">
                <table class="activity-table">
                    <tr><th>Time</th><th>User / Type</th><th>Activity</th><th>Details</th></tr>
                    {% for a in rows %}
                    {% set kind = activity_kind(a.action) %}
                    <tr>
                        <td>{{ fmt_dt(a.created_at) }}</td>
                        <td><strong>{{ a.username or 'system' }}</strong><br><span class="muted">{{ 'admin/viewer/system' if not a.username else '' }}</span></td>
                        <td><span class="activity-type {{ kind }}">{{ kind|upper }}</span><br><span class="muted">{{ a.action }}</span></td>
                        <td class="activity-details">{{ a.details or '-' }}</td>
                    </tr>
                    {% else %}
                    <tr><td colspan="4"><div class="empty-state">No login/logout/Zoom activity found yet.</div></td></tr>
                    {% endfor %}
                </table>
            </div>
            <div class="pagination-bar">
                <div class="muted">Page {{ page_no }} of {{ total_pages }}</div>
                <div style="display:flex;gap:8px;flex-wrap:wrap">
                    <a class="page-btn {% if page_no <= 1 %}disabled{% endif %}" href="{{ page_url(page_no-1) }}">← Previous</a>
                    <a class="page-btn {% if page_no >= total_pages %}disabled{% endif %}" href="{{ page_url(page_no+1) }}">Next →</a>
                </div>
            </div>
        </div>
        """,
        rows=rows,
        total_rows=total_rows,
        page_no=page_no,
        per_page=per_page,
        total_pages=total_pages,
        users=users,
        allowed_actions=allowed_actions,
        action_filter=action_filter,
        username_filter=username_filter,
        summary=summary,
        fmt_dt=fmt_dt,
        activity_kind=_activity_kind,
        page_url=lambda p: url_for('activity', **{**request.args.to_dict(), 'page': max(1, p)}),
    )
    return page("Activity", body, "activity")


