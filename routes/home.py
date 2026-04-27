# Extracted from original app.py during real modular split.
# Do not run this file directly. It registers routes on the shared Flask app.
import sys
_legacy = sys.modules.get("app") or sys.modules.get("__main__")
if _legacy is None:
    raise RuntimeError("This module must be imported by app.py")
globals().update({name: getattr(_legacy, name) for name in dir(_legacy) if not name.startswith("__")})

# ---- home ----
@app.route("/home")
@login_required

def home():
    try:
        maybe_finalize_stale_live_meetings()
    except Exception as e:
        print(f"⚠️ home stale finalization skipped: {e}")

    live_info = read_live_snapshot()

    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS c FROM meetings")
            total_meetings = cur.fetchone()["c"]

            cur.execute("SELECT COUNT(*) AS c FROM members")
            total_members = cur.fetchone()["c"]

            cur.execute(f"SELECT COUNT(*) AS c FROM members WHERE {ACTIVE_MEMBER_SQL}")
            active_members = cur.fetchone()["c"]

            cur.execute("SELECT COUNT(*) AS c FROM attendance WHERE final_status='PRESENT'")
            present = cur.fetchone()["c"]

            cur.execute("SELECT COUNT(*) AS c FROM attendance WHERE final_status='LATE'")
            late = cur.fetchone()["c"]

            cur.execute("SELECT COUNT(*) AS c FROM attendance WHERE final_status='ABSENT'")
            absent = cur.fetchone()["c"]

            cur.execute("SELECT * FROM meetings ORDER BY id DESC LIMIT 8")
            recent_meetings = cur.fetchall()

            cur.execute("SELECT * FROM activity_log ORDER BY id DESC LIMIT 12")
            recent_activity = cur.fetchall()

    total_classified = present + late + absent
    health = round(((present + late) / total_classified) * 100, 2) if total_classified else 0
    latest_meeting = recent_meetings[0] if recent_meetings else None

    host_now = "No"
    unknown_live_count = 0
    if live_info and live_info.get("participants"):
        for participant_row in live_info.get("participants") or []:
            if participant_row.get("is_host") and participant_row.get("current_join") is not None:
                host_now = "Yes"
            if participant_row.get("current_join") is not None and not participant_row.get("is_member"):
                unknown_live_count += 1

    home_data = {
        "phase3_alerts": [
            {
                "level": "ok" if live_info else "info",
                "title": "Live monitoring active" if live_info else "System standing by",
                "text": "Webhook stream is tracking a current live meeting." if live_info else "No live session is open right now, but the control center is healthy.",
            },
            {
                "level": "warn" if latest_meeting and (latest_meeting.get("unknown_participants") or 0) > 0 else "ok",
                "title": "Unknown participant watch",
                "text": f"{(latest_meeting.get('unknown_participants') or 0) if latest_meeting else 0} unknown participant(s) detected in the latest meeting snapshot.",
            },
            {
                "level": "danger" if health < 75 else "ok",
                "title": "Attendance health signal",
                "text": "Attention is needed because attendance quality is below target." if health < 75 else "Attendance health is currently in a comfortable zone.",
            },
        ]
    }

    body = render_template_string(
        """
        <div class="hero">
            <div class="hero-grid">
                <div>
                    <div class="badge info" style="margin-bottom:12px">Control Center</div>
                    <h1 class="hero-title">Zoom Attendance Command Dashboard</h1>
                    <div class="hero-copy">
                        Monitor meetings, member participation, finalization quality, and reporting health from one <strong>premium control layer</strong> with richer signals, smoother interactions, and a stronger live-ops feel.
                    </div>
                    <div class="row" style="margin-top:16px">
                        <span class="badge ok">Stable tracking</span>
                        <span class="badge info">Reports ready</span>
                        <span class="badge warn">Analytics enabled</span>
                        <span class="badge gray">{{ 'Live meeting detected' if live_info else 'Waiting for next live session' }}</span>
                    </div>
                </div>
                <div class="hero-stats">
                    <div class="hero-chip">
                        <div class="small">System Health</div>
                        <div class="big">{{ health }}%</div>
                    </div>
                    <div class="hero-chip">
                        <div class="small">Current State</div>
                        <div class="big">{{ 'LIVE' if live_info else 'IDLE' }}</div>
                    </div>
                    <div class="hero-chip">
                        <div class="small">Members</div>
                        <div class="big">{{ active_members }}/{{ total_members }}</div>
                    </div>
                </div>
            </div>
        </div>

        <div class="alert-rail">
            <div class="alert-chip {{ 'ok' if live_info else 'info' }}">
                <strong>{{ 'Live monitoring active' if live_info else 'System standing by' }}</strong>
                <div class="muted">{{ 'Webhook stream is tracking a current live meeting.' if live_info else 'No live session is open right now, but the control center is healthy.' }}</div>
            </div>
            <div class="alert-chip {{ 'warn' if latest_meeting and (latest_meeting.unknown_participants or 0) > 0 else 'ok' }}">
                <strong>Unknown participant watch</strong>
                <div class="muted">{{ ((latest_meeting.unknown_participants or 0)|string) if latest_meeting else '0' }} unknown participant(s) detected in the latest meeting snapshot.</div>
            </div>
            <div class="alert-chip {{ 'danger' if health < 75 else 'ok' }}">
                <strong>Attendance health signal</strong>
                <div class="muted">{{ 'Attention is needed because attendance quality is below target.' if health < 75 else 'Attendance health is currently in a comfortable zone.' }}</div>
            </div>
        </div>

        <div class="alert-rail">
            <div class="alert-chip ok"><strong>Live engine status</strong><div class="muted">Participants are being recalculated in real time every refresh cycle.</div></div>
            <div class="alert-chip {{ 'warn' if host_now != 'Yes' else 'ok' }}"><strong>Host presence</strong><div class="muted">{{ 'Host is not currently active in the meeting.' if host_now != 'Yes' else 'Host presence has been detected successfully.' }}</div></div>
            <div class="alert-chip {{ 'danger' if unknown_live_count >= 3 else 'info' }}"><strong>Unknown participant watch</strong><div class="muted">{{ unknown_live_count }} unknown participant(s) are currently part of this session.</div></div>
        </div>

        <div class="grid">
            <div class="card kpi-card">
                <div class="kpi-icon">📂</div>
                <h4>Total Meetings</h4>
                <div class="metric">{{ total_meetings }}</div>
                <div class="metric-sub">Completed and live meetings recorded in PostgreSQL.</div>
            </div>
            <div class="card kpi-card">
                <div class="kpi-icon">👥</div>
                <h4>Active Members</h4>
                <div class="metric">{{ active_members }}</div>
                <div class="metric-sub">Total members in directory: {{ total_members }}</div>
            </div>
            <div class="card kpi-card">
                <div class="kpi-icon">🩺</div>
                <h4>Attendance Health</h4>
                <div class="metric">{{ health }}%</div>
                <div class="metric-sub">Present plus late records across finalized attendance rows.</div>
            </div>
            <div class="card kpi-card">
                <div class="kpi-icon">📡</div>
                <h4>Live Status</h4>
                <div class="metric">{{ 'LIVE' if live_info else 'IDLE' }}</div>
                <div class="metric-sub">Webhook monitoring status for current Zoom traffic.</div>
            </div>
        </div>

        <div class="alert-rail">
            {% for alert in data.phase3_alerts %}
            <div class="alert-chip {{ alert.level }}">
                <strong>{{ alert.title }}</strong>
                <div class="muted">{{ alert.text }}</div>
            </div>
            {% endfor %}
        </div>

        <div class="grid-2" style="margin-top:16px">
            <div class="card">
                <div class="section-title">
                    <div>
                        <h3 style="margin:0">Latest Meeting Spotlight</h3>
                        <p>Quick summary of the most recent tracked meeting.</p>
                    </div>
                    {% if latest_meeting %}
                    <span class="badge gray">{{ fmt_dt(latest_meeting.start_time) }}</span>
                    {% endif %}
                </div>
                {% if latest_meeting %}
                    <div class="split-head" style="margin-bottom:14px">
                        <div>
                            <div style="font-size:22px;font-weight:900;letter-spacing:-.03em">{{ latest_meeting.topic or 'Untitled Meeting' }}</div>
                            <div class="muted" style="margin-top:6px">Meeting ID: {{ latest_meeting.meeting_id or '-' }}</div>
                        </div>
                        <div class="row">
                            <span class="badge ok">Present {{ latest_meeting.present_count or 0 }}</span>
                            <span class="badge warn">Late {{ latest_meeting.late_count or 0 }}</span>
                            <span class="badge danger">Absent {{ latest_meeting.absent_count or 0 }}</span>
                            <span class="badge info">Unknown {{ latest_meeting.unknown_participants or 0 }}</span>
                        </div>
                    </div>
                    <div class="stack">
                        <div class="mini-kpi">
                            <div class="label">Command spotlight progress</div>
                            <div class="value">{{ latest_meeting.present_count or 0 }} + {{ latest_meeting.late_count or 0 }}</div>
                            {% set spotlight_total = (latest_meeting.present_count or 0) + (latest_meeting.late_count or 0) + (latest_meeting.absent_count or 0) %}
                            <div class="spotlight-bar" style="margin-top:10px">
                                <span style="width: {{ ((latest_meeting.present_count or 0) + (latest_meeting.late_count or 0)) / spotlight_total * 100 if spotlight_total else 0 }}%"></span>
                            </div>
                        </div>
                        <div class="toolbar">
                            <a class="btn" href="{{ url_for('meetings') }}">Open Meetings</a>
                            <a class="btn secondary" href="{{ url_for('analytics') }}">Open Analytics</a>
                            <a class="btn success" href="{{ url_for('live') }}">Open Live</a>
                        </div>
                    </div>
                {% else %}
                    <div class="empty-state">
                        <div class="empty-icon">📭</div>
                        <h3 style="margin-bottom:8px">No meeting summary available</h3>
                        <div class="muted">Once Zoom meetings are tracked and finalized, the latest meeting snapshot will appear here.</div>
                    </div>
                {% endif %}
            </div>

            <div class="card">
                <div class="section-title">
                    <div>
                        <h3 style="margin:0">Quick Actions</h3>
                        <p>Fast navigation into your most-used platform flows.</p>
                    </div>
                    <span class="badge {{ 'ok' if live_info else 'gray' }}">
                        <span class="{{ 'status-pulse' if live_info else 'status-off' }}"></span>
                        {{ 'Live now' if live_info else 'Idle now' }}
                    </span>
                </div>
                <div class="grid" style="grid-template-columns:repeat(2,minmax(0,1fr));gap:12px">
                    <a class="card card-tight" href="{{ url_for('live') }}" style="text-decoration:none;color:inherit">
                        <div class="kpi-icon">🟢</div>
                        <h4 style="margin:0">Live Monitor</h4>
                        <div class="muted">Track active participants, duration and live status.</div>
                    </a>
                    <a class="card card-tight" href="{{ url_for('analytics') }}" style="text-decoration:none;color:inherit">
                        <div class="kpi-icon">📈</div>
                        <h4 style="margin:0">Analytics</h4>
                        <div class="muted">Open charts, health view, risk members and exports.</div>
                    </a>
                    <a class="card card-tight" href="{{ url_for('members') }}" style="text-decoration:none;color:inherit">
                        <div class="kpi-icon">👥</div>
                        <h4 style="margin:0">Members</h4>
                        <div class="muted">Manage active members and import new people safely.</div>
                    </a>
                    <a class="card card-tight" href="{{ url_for('settings') }}" style="text-decoration:none;color:inherit">
                        <div class="kpi-icon">⚙️</div>
                        <h4 style="margin:0">Settings</h4>
                        <div class="muted">Tune thresholds and finalization behavior.</div>
                    </a>
                </div>
            </div>
        </div>

        <div class="grid-2" style="margin-top:16px">
            <div class="card">
                <div class="section-title">
                    <div>
                        <h3 style="margin:0">Recent Meetings</h3>
                        <p>Latest meeting sessions with participant counts and status.</p>
                    </div>
                    <a class="btn small secondary" href="{{ url_for('meetings') }}">See All</a>
                </div>
                <div class="table-wrap">
                    <table>
                        <tr>
                            <th>Date</th>
                            <th>Topic</th>
                            <th>Status</th>
                            <th>Participants</th>
                            <th>Health</th>
                        </tr>
                        {% for m in recent_meetings %}
                        {% set total_rows = (m.present_count or 0) + (m.late_count or 0) + (m.absent_count or 0) %}
                        {% set meeting_health = (((m.present_count or 0) + (m.late_count or 0)) / total_rows * 100) if total_rows else 0 %}
                        <tr>
                            <td>{{ fmt_dt(m.start_time) }}</td>
                            <td>{{ m.topic or 'Untitled Meeting' }}</td>
                            <td>
                                <span class="badge {{ 'ok' if m.status == 'live' else 'gray' }}">{{ m.status or '-' }}</span>
                            </td>
                            <td>{{ m.unique_participants or 0 }}</td>
                            <td>{{ '%.1f'|format(meeting_health) }}%</td>
                        </tr>
                        {% endfor %}
                    </table>
                </div>
            </div>

            <div class="card">
                <div class="section-title">
                    <div>
                        <h3 style="margin:0">Recent Activity</h3>
                        <p>Most recent system actions and webhook events.</p>
                    </div>
                    <a class="btn small secondary" href="{{ url_for('activity') }}">Open Log</a>
                </div>
                <div class="list-card">
                    {% for item in recent_activity %}
                    <div class="list-row">
                        <div>
                            <div style="font-weight:800">{{ item.action or '-' }}</div>
                            <div class="muted">{{ item.username or 'system' }}</div>
                        </div>
                        <div style="text-align:right;max-width:58%">
                            <div class="muted">{{ fmt_dt(item.created_at) }}</div>
                            <div style="margin-top:4px;font-size:12px">{{ item.details or '-' }}</div>
                        </div>
                    </div>
                    {% endfor %}
                </div>
            </div>
        </div>
        """,
        total_meetings=total_meetings,
        total_members=total_members,
        active_members=active_members,
        present=present,
        late=late,
        absent=absent,
        recent_meetings=recent_meetings,
        recent_activity=recent_activity,
        health=health,
        live_info=live_info,
        latest_meeting=latest_meeting,
        host_now=host_now,
        unknown_live_count=unknown_live_count,
        data=home_data,
        fmt_dt=fmt_dt,
    )
    return page("Home", body, "home")


# UI_UPDATE_V7_REALTIME_LIVE_DASHBOARD_APPLIED = True


