# Extracted from original app.py during real modular split.
# Do not run this file directly. It registers routes on the shared Flask app.
import sys
_legacy = sys.modules.get("app") or sys.modules.get("__main__")
if _legacy is None:
    raise RuntimeError("This module must be imported by app.py")
globals().update({name: getattr(_legacy, name) for name in dir(_legacy) if not name.startswith("__")})

# ---- analytics ----
@app.route("/analytics")
@login_required

def analytics():
    maybe_finalize_stale_live_meetings()

    filters = {
        "period_mode": request.args.get("period_mode", "custom"),
        "from_date": request.args.get("from_date", ""),
        "to_date": request.args.get("to_date", ""),
        "meeting_uuid": request.args.get("meeting_uuid", ""),
        "member_ids": request.args.getlist("member_ids"),
        "person_name": request.args.get("person_name", ""),
        "participant_type": request.args.get("participant_type", "all"),
    }

    data = analytics_data(filters)
    trend = data["trend"]
    member_chart = data["member_duration_chart"]
    export_query = build_filter_query(data["filters"])
    export_csv_url = url_for("export_analytics_csv") + (f"?{export_query}" if export_query else "")
    export_pdf_url = url_for("export_analytics_pdf") + (f"?{export_query}" if export_query else "")
    latest_meeting = data.get("latest_meeting_summary")
    previous_meeting = data.get("previous_meeting_summary")
    comparison_delta = data.get("comparison_delta")
    graph_options = graph_analytics_options()

    body = render_template_string(
        """
        <div class="hero">
            <div class="hero-grid">
                <div>
                    <div class="badge info" style="margin-bottom:12px">Analytics Studio</div>
                    <h1 class="hero-title">Advanced Attendance Intelligence</h1>
                    <div class="hero-copy">
                        Explore attendance health, trend movement, member engagement, risk indicators, and exportable filtered views without changing your backend workflow.
                    </div>
                </div>
                <div class="hero-stats">
                    <div class="hero-chip">
                        <div class="small">Rows</div>
                        <div class="big">{{ data.summary.total_rows }}</div>
                    </div>
                    <div class="hero-chip">
                        <div class="small">Health</div>
                        <div class="big">{{ data.summary.current_meeting_health }} / 100</div>
                    </div>
                    <div class="hero-chip">
                        <div class="small">Predicted Next</div>
                        <div class="big">{{ data.summary.current_meeting_health }}</div>
                    </div>
                </div>
            </div>
        </div>

        <div class="card">
            <div class="section-title">
                <div>
                    <h3 style="margin:0">Analytics Filters</h3>
                    <p>Slice attendance by period, person, member, meeting, and participant type.</p>
                </div>
            </div>
            <form method="get">
                <div class="grid" style="grid-template-columns:1.1fr 1fr 1fr 1.15fr 1.1fr 1.1fr 1fr;">
                    <div>
                        <label>Period Mode</label>
                        <select name="period_mode">
                            <option value="day" {% if filters.period_mode == 'day' %}selected{% endif %}>Day</option>
                            <option value="week" {% if filters.period_mode == 'week' %}selected{% endif %}>Week</option>
                            <option value="month" {% if filters.period_mode == 'month' %}selected{% endif %}>Month</option>
                            <option value="year" {% if filters.period_mode == 'year' %}selected{% endif %}>Year</option>
                            <option value="custom" {% if filters.period_mode == 'custom' %}selected{% endif %}>Custom</option>
                        </select>
                    </div>
                    <div>
                        <label>From Date</label>
                        <input type="date" name="from_date" value="{{ filters.from_date }}">
                    </div>
                    <div>
                        <label>To Date</label>
                        <input type="date" name="to_date" value="{{ filters.to_date }}">
                    </div>
                    <div>
                        <label>Meeting</label>
                        <select name="meeting_uuid">
                            <option value="">All meetings</option>
                            {% for m in data.meetings %}
                            <option value="{{ m.meeting_uuid }}" {% if filters.meeting_uuid == m.meeting_uuid %}selected{% endif %}>
                                {{ m.topic or 'Untitled Meeting' }} - {{ fmt_dt(m.start_time) }}
                            </option>
                            {% endfor %}
                        </select>
                    </div>
                    <div>
                        <label>Members</label>
                        <select name="member_ids" multiple style="min-height:132px">
                            {% for m in data.members %}
                            <option value="{{ m.id }}" {% if m.id|string in filters.member_ids %}selected{% endif %}>{{ m.display_name or member_display_name(m) }}</option>
                            {% endfor %}
                        </select>
                    </div>
                    <div>
                        <label>Person Search</label>
                        <input type="text" name="person_name" value="{{ filters.person_name }}" placeholder="type participant name">
                    </div>
                    <div>
                        <label>Participant Type</label>
                        <select name="participant_type">
                            <option value="all" {% if filters.participant_type == 'all' %}selected{% endif %}>All</option>
                            <option value="member" {% if filters.participant_type == 'member' %}selected{% endif %}>Member</option>
                            <option value="unknown" {% if filters.participant_type == 'unknown' %}selected{% endif %}>Unknown</option>
                            <option value="host" {% if filters.participant_type == 'host' %}selected{% endif %}>Host</option>
                        </select>
                    </div>
                </div>
                <div class="toolbar" style="margin-top:8px">
                    <button type="submit">Apply Filters</button>
                    <a class="btn success" href="{{ export_csv_url }}">Export CSV</a>
                    <a class="btn secondary" href="{{ export_pdf_url }}">Export PDF</a>
                </div>
            </form>
        </div>

        <style>
        .dash-showcase{display:grid;grid-template-columns:180px minmax(0,1fr) 310px;gap:14px;margin-top:16px;align-items:start}
        .dash-mini-sidebar{background:#0f172a;color:#e5e7eb;border-radius:16px;padding:14px;box-shadow:0 14px 35px rgba(15,23,42,.18);position:sticky;top:92px}
        .dash-mini-brand{font-weight:950;font-size:15px;line-height:1.25;margin-bottom:14px;display:flex;gap:8px;align-items:center}
        .dash-mini-nav{display:grid;gap:8px}.dash-mini-nav a,.dash-note{border-radius:12px;padding:10px 11px;text-decoration:none;color:#e5e7eb;font-weight:800;font-size:13px;background:rgba(255,255,255,.04)}
        .dash-mini-nav a.active{background:linear-gradient(135deg,#6d28d9,#7c3aed);box-shadow:0 12px 26px rgba(109,40,217,.30)}
        .dash-note{margin-top:14px;background:#fff8d6;color:#28334a;border:1px solid #f1d976;font-size:12px;line-height:1.5}
        .dash-main-title{display:flex;justify-content:space-between;gap:10px;align-items:center;margin-bottom:12px}
        .dash-title-pill{margin:auto;background:#0b274b;color:#fff;border-radius:11px;padding:10px 26px;font-size:24px;font-weight:950;letter-spacing:.5px;text-align:center;box-shadow:0 10px 25px rgba(2,6,23,.18)}
        .dash-actions{display:flex;gap:16px;align-items:center;white-space:nowrap;color:#0f172a;font-weight:850}
        body.dark .dash-actions{color:#e5e7eb}
        .dash-card{background:rgba(255,255,255,.92);border:1px solid rgba(15,23,42,.10);border-radius:14px;box-shadow:0 8px 24px rgba(15,23,42,.10);padding:16px;color:#172033}
        body.dark .dash-card{background:rgba(15,23,42,.76);border-color:rgba(148,163,184,.20);color:#e5e7eb}
        .analytics-layout{display:grid;grid-template-columns:minmax(0,1fr) 240px;gap:14px}
        .chart-title{font-weight:950;font-size:16px;margin-bottom:8px}.chart-sub{font-size:12px;color:#64748b;margin-top:-4px;margin-bottom:8px}
        body.dark .chart-sub{color:#94a3b8}.chart-big{height:310px}.chart-small{height:260px}
        .control-stack{border-left:1px solid rgba(148,163,184,.25);padding-left:14px;display:grid;gap:10px}.control-title{font-weight:950;color:#1e3a8a;margin-bottom:4px}
        body.dark .control-title{color:#bfdbfe}.control-stack label,.side-control label{font-size:12px;font-weight:900;color:#334155;margin-bottom:4px;display:block}body.dark .control-stack label,body.dark .side-control label{color:#cbd5e1}
        .control-stack input,.control-stack select,.side-control input,.side-control select{height:34px;padding:6px 9px;border-radius:8px;font-size:13px}
        .apply-wide{width:100%;justify-content:center;margin-top:6px;border-radius:8px}
        .bottom-grid{display:grid;grid-template-columns:minmax(0,1.15fr) minmax(240px,.75fr);gap:14px;margin-top:14px}.participant-chart-grid{display:grid;grid-template-columns:minmax(0,1fr) 220px;gap:14px}
        .side-help{background:#fff7df;border:1px solid #e7cb8a;border-radius:16px;padding:14px;color:#2f3142;position:sticky;top:92px}.side-help h3{font-size:14px;margin:0 0 8px;color:#0f172a}.side-help ul{margin:0;padding-left:18px;line-height:1.65;font-size:13px}
        .checkbox-select{position:relative}.checkbox-select-btn{width:100%;text-align:left;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;padding:8px 10px;border-radius:8px;font-size:13px;min-height:34px}.checkbox-select-menu{display:none;position:absolute;z-index:80;top:calc(100% + 6px);left:0;right:0;max-height:230px;overflow:auto;background:rgba(255,255,255,.98);color:#172033;border:1px solid rgba(148,163,184,.35);border-radius:12px;box-shadow:0 20px 45px rgba(0,0,0,.22);padding:8px}.checkbox-select.open .checkbox-select-menu{display:block}.checkbox-select-menu label{display:flex;gap:8px;align-items:center;padding:7px;border-radius:9px;cursor:pointer;font-size:13px}.checkbox-select-menu label:hover{background:rgba(99,102,241,.12)}.checkbox-select-menu input{width:auto;height:auto}body.dark .checkbox-select-menu{background:#0f172a;color:#e5e7eb}
        .month-year-box{background:#fff8df;border:1px solid #e4c779;border-radius:10px;padding:10px;display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:8px}.month-year-box h4{margin:0 0 6px;font-size:12px}.month-year-box label{display:flex;gap:7px;align-items:center;font-size:12px;margin:4px 0}.month-year-box input{height:auto}.register-table-wrap{max-height:72vh;overflow:auto;border-radius:18px;border:1px solid rgba(148,163,184,.22)}.register-table{border-collapse:separate;border-spacing:0;width:max-content;min-width:100%}.register-table th,.register-table td{min-width:44px;text-align:center;padding:9px 10px;border-bottom:1px solid rgba(148,163,184,.16);border-right:1px solid rgba(148,163,184,.12)}.register-table th{position:sticky;top:0;z-index:4;background:#111827}.register-table .sticky-member{position:sticky;left:0;z-index:5;min-width:230px;text-align:left;background:#111827}.register-table td.sticky-member{z-index:3;background:rgba(15,23,42,.98);font-weight:800;cursor:pointer}.reg-cell{font-weight:900;border-radius:9px;color:#08111f}.reg-p{background:#22c55e}.reg-l{background:#facc15}.reg-a{background:#ef4444;color:#fff}.reg-u{background:#94a3b8}.reg-empty{color:#94a3b8}.modal-backdrop{display:none;position:fixed;inset:0;background:rgba(2,6,23,.68);z-index:999;align-items:center;justify-content:center;padding:18px}.modal-backdrop.show{display:flex}.modal-card{max-width:460px;width:100%;background:#0f172a;border:1px solid rgba(148,163,184,.3);border-radius:22px;padding:22px;box-shadow:0 30px 80px rgba(0,0,0,.45)}
        @media(max-width:1180px){.dash-showcase{grid-template-columns:1fr}.dash-mini-sidebar,.side-help{position:static}.analytics-layout,.bottom-grid,.participant-chart-grid{grid-template-columns:1fr}.dash-main-title{flex-direction:column}.dash-title-pill{width:100%;font-size:18px}.control-stack{border-left:0;padding-left:0}}
        
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


        <style>
        /* Compact graph analytics: remove helper panels and give charts more space */
        .dash-mini-sidebar,.dash-actions,.side-help{display:none!important;}
        .dash-showcase{grid-template-columns:minmax(0,1fr)!important;}
        .analytics-layout{grid-template-columns:minmax(0,1fr)!important;}
        .bottom-grid{grid-template-columns:minmax(0,1fr)!important;}
        .bottom-grid > .dash-card:nth-child(2){display:none!important;}
        .participant-chart-grid{grid-template-columns:minmax(0,1fr) 260px!important;}
        .chart-big{height:360px!important}.chart-small{height:330px!important;}
        .checkbox-select-menu{z-index:9999!important;}
        /* ANALYTICS_TABS_V3: organized navigation without removing old analytics */
        .analytics-tab-shell{position:sticky;top:78px;z-index:70;margin:16px 0 14px;padding:10px;border-radius:18px;background:rgba(2,6,23,.72);border:1px solid rgba(96,165,250,.22);backdrop-filter:blur(16px);box-shadow:0 18px 45px rgba(0,0,0,.28)}
        .analytics-tab-nav{display:flex;gap:10px;overflow-x:auto;scrollbar-width:thin;padding:2px}
        .analytics-tab-nav a{flex:0 0 auto;text-decoration:none;color:#cbd5e1;background:rgba(15,23,42,.9);border:1px solid rgba(148,163,184,.18);border-radius:14px;padding:11px 14px;font-size:13px;font-weight:950;transition:transform .18s ease,background .18s ease,border-color .18s ease,box-shadow .18s ease,color .18s ease}
        .analytics-tab-nav a:hover,.analytics-tab-nav a.active{transform:translateY(-2px);color:#fff;background:linear-gradient(135deg,#2563eb,#7c3aed);border-color:rgba(191,219,254,.55);box-shadow:0 14px 30px rgba(37,99,235,.28)}
        .analytics-anchor-section{scroll-margin-top:154px;animation:analyticsFadeIn .24s ease both}
        @keyframes analyticsFadeIn{from{opacity:.72;transform:translateY(6px)}to{opacity:1;transform:none}}
        html{scroll-behavior:smooth}
        
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
        <div class="analytics-tab-shell" id="analyticsTabsV3">
            <nav class="analytics-tab-nav" aria-label="Analytics sections">
                <a class="active" href="#analyticsOverview">Overview</a>
                <a href="#graphAnalyticsSection">Graph Analytics</a>
                <a href="{{ url_for('attendance_register') }}">Register</a>
                <a href="#analyticsMembers">Members</a>
                <a href="#analyticsRisk">Risk</a>
                <a href="#analyticsTrends">Trends</a>
                <a href="#analyticsReports">Reports</a>
            </nav>
        </div>
        <div class="dash-showcase analytics-anchor-section" id="graphAnalyticsSection">
            <aside class="dash-mini-sidebar">
                <div class="dash-mini-brand">📊 Analytical<br>Dashboard</div>
                <nav class="dash-mini-nav">
                    <a class="active" href="#graphAnalyticsSection">Overview</a>
                    <a href="#gaTrendChart">Attendance Graphs</a>
                    <a href="{{ url_for('attendance_register') }}">Register View</a>
                    <a href="#analyticsRows">Participants</a>
                    <a href="{{ export_pdf_url }}">Reports</a>
                </nav>
                <div class="dash-note"><b>GRAPH 1: PARTICIPATION OVER TIME</b><br>Line graph with 4 lines: Present, Late, Absent and Unknown.</div>
                <div class="dash-note" style="background:#eaf4ff;border-color:#93c5fd"><b>GRAPH 2: TIME SPENT</b><br>Multiple members → members on X-axis.<br>Single member → date vs duration.</div>
            </aside>

            <main>
                <div class="dash-main-title">
                    <div class="dash-title-pill">1. ANALYTICAL DASHBOARD (GRAPHS & INSIGHTS)</div>
                    <div class="dash-actions"><span>⬇ Export</span><span>⟳ Refresh</span><span>⚿ Filters</span></div>
                </div>

                <div class="analytics-layout">
                    <div class="dash-card">
                        <div class="analytics-layout" style="grid-template-columns:minmax(0,1fr) 230px">
                            <div>
                                <div class="chart-title">Participants Over Time</div>
                                <div class="chart-big"><canvas id="gaTrendChart"></canvas></div>
                            </div>
                            <div class="control-stack">
                                <div class="control-title">Graph 1 Controls</div>
                                <div><label>X-Axis</label><select id="gaXAxis"><option value="date">Date</option><option value="month">Month</option><option value="year">Year</option></select></div>
                                <div><label>Y-Axis</label><select id="gaYAxis"><option value="count">Number of Participants</option><option value="percentage">Percentage</option></select></div>
                                <div class="ga-date-filter"><label>From Date</label><input type="date" id="gaFromDate"></div>
                                <div class="ga-date-filter"><label>To Date</label><input type="date" id="gaToDate"></div>
                                <button type="button" class="apply-wide" id="gaApplyBtn">Apply</button>
                            </div>
                        </div>
                    </div>

                    <aside class="side-help">
                        <h3>AXIS & DATE SELECTION OPTIONS</h3>
                        <ul>
                            <li>Choose Date / Month / Year for X-axis</li>
                            <li>Choose Count or Percentage for Y-axis</li>
                            <li>Select date range for Date mode</li>
                            <li>Select multiple months or years with checkboxes</li>
                        </ul>
                        <div class="month-year-box">
                            <div class="ga-month-filter" style="display:none">
                                <h4>If X-Axis = Month</h4>
                                <div class="checkbox-select" data-target="gaMonths">
                                    <button type="button" class="checkbox-select-btn">All months</button>
                                    <div class="checkbox-select-menu">
                                        <label><input type="checkbox" value="__all__" checked> All Months</label>
                                        {% for month in graph_options.months %}<label><input type="checkbox" value="{{ month.value }}"> {{ month.label }}</label>{% endfor %}
                                    </div>
                                </div>
                            </div>
                            <div class="ga-year-filter" style="display:none">
                                <h4>If X-Axis = Year</h4>
                                <div class="checkbox-select" data-target="gaYears">
                                    <button type="button" class="checkbox-select-btn">All years</button>
                                    <div class="checkbox-select-menu">
                                        <label><input type="checkbox" value="__all__" checked> All Years</label>
                                        {% for year in graph_options.years %}<label><input type="checkbox" value="{{ year }}"> {{ year }}</label>{% endfor %}
                                    </div>
                                </div>
                            </div>
                        </div>
                    </aside>
                </div>

                <div class="bottom-grid">
                    <div class="dash-card">
                        <div class="participant-chart-grid">
                            <div>
                                <div class="chart-title">Time Spent by Participants (In Minutes)</div>
                                <div class="chart-sub" id="gaDurationHint">All selected members total duration in minutes.</div>
                                <div class="chart-small"><canvas id="gaDurationChart"></canvas></div>
                            </div>
                            <div class="side-control">
                                <div class="control-title">Graph 2 Controls</div>
                                <label>Select Participants</label>
                                <div class="checkbox-select" data-target="gaMembers">
                                    <button type="button" class="checkbox-select-btn">All members</button>
                                    <div class="checkbox-select-menu">
                                        <label><input type="checkbox" value="__all__" checked> All Members</label>
                                        {% for member in graph_options.members %}<label><input type="checkbox" value="{{ member.id }}"> {{ member.name }}</label>{% endfor %}
                                    </div>
                                </div>
                                <div style="margin-top:10px"><label>From Date</label><input type="date" id="gaDurationFromDate"></div>
                                <div style="margin-top:10px"><label>To Date</label><input type="date" id="gaDurationToDate"></div>
                                <button type="button" class="apply-wide" onclick="document.getElementById('gaApplyBtn').click()">Apply</button>
                            </div>
                        </div>
                    </div>
                    <div class="dash-card" style="border:1px solid #93c5fd">
                        <div class="chart-sub" style="font-weight:900;color:#1d4ed8">If Single Participant Selected</div>
                        <div class="chart-title" id="gaTrendHint">Time Over Time</div>
                        <p style="margin:0;color:#64748b;font-size:13px">When only one member is selected, Graph 2 automatically changes to date vs duration.</p>
                    </div>
                </div>
            </main>
        </div>

        <div class="grid" style="margin-top:16px">
            <div class="card kpi-card">
                <div class="kpi-icon">🧾</div>
                <h4>Total Rows</h4>
                <div class="metric">{{ data.summary.total_rows }}</div>
                <div class="metric-sub">Attendance records matching the current filter state.</div>
            </div>
            <div class="card kpi-card">
                <div class="kpi-icon">✅</div>
                <h4>Present</h4>
                <div class="metric">{{ data.summary.present_rows }}</div>
                <div class="metric-sub">Participants who met the present threshold.</div>
            </div>
            <div class="card kpi-card">
                <div class="kpi-icon">⏳</div>
                <h4>Late</h4>
                <div class="metric">{{ data.summary.late_rows }}</div>
                <div class="metric-sub">Attended but below the required present duration.</div>
            </div>
            <div class="card kpi-card">
                <div class="kpi-icon">🚫</div>
                <h4>Absent</h4>
                <div class="metric">{{ data.summary.absent_rows }}</div>
                <div class="metric-sub">Rows classified as absent in the filtered dataset.</div>
            </div>
            <div class="card kpi-card">
                <div class="kpi-icon">❓</div>
                <h4>Unknown</h4>
                <div class="metric">{{ data.summary.unknown_rows }}</div>
                <div class="metric-sub">Participants not matched to a registered member.</div>
            </div>
            <div class="card kpi-card">
                <div class="kpi-icon">🔮</div>
                <h4>Meeting Health</h4>
                <div class="metric">{{ data.summary.current_meeting_health }}</div>
                <div class="metric-sub">Weighted score from attendance, duration and participation.</div>
            </div>
        </div>

        <div class="grid-2 analytics-anchor-section" id="analyticsTrends" style="margin-top:16px">
            <div class="card">
                <div class="section-title">
                    <div>
                        <h3 style="margin:0">Attendance Trend</h3>
                        <p>Present, late and absent distribution over the selected period.</p>
                    </div>
                </div>
                <div class="chart-wrap tall"><canvas id="trendChart"></canvas></div>
            </div>
            <div class="card">
                <div class="section-title">
                    <div>
                        <h3 style="margin:0">Status Mix</h3>
                        <p>How the current filtered rows are distributed by classification.</p>
                    </div>
                </div>
                <div class="chart-wrap"><canvas id="statusMixChart"></canvas></div>
            </div>
        </div>

        <div class="grid-2" style="margin-top:16px">
            <div class="card">
                <div class="section-title">
                    <div>
                        <h3 style="margin:0">Member Duration</h3>
                        <p>{{ member_chart.subtitle }}</p>
                    </div>
                </div>
                {% if member_chart.empty %}
                    <div class="empty-state" style="padding:24px 18px">
                        <div class="empty-icon" style="width:58px;height:58px;font-size:22px">📊</div>
                        <div style="font-weight:900;margin-bottom:6px">No member duration data</div>
                        <div class="muted">Adjust filters or wait for tracked member attendance to appear.</div>
                    </div>
                {% else %}
                    <div class="chart-wrap"><canvas id="memberDurationChart"></canvas></div>
                {% endif %}
            </div>
            <div class="card">
                <div class="section-title">
                    <div>
                        <h3 style="margin:0">Health Snapshot</h3>
                        <p>Latest meeting comparison and summary performance indicators.</p>
                    </div>
                </div>
                <div class="stack">
                    <div class="grid-2">
                        <div class="mini-kpi">
                            <div class="label">Attendance Health</div>
                            <div class="value">{{ data.summary.attendance_health }}%</div>
                        </div>
                        <div class="mini-kpi">
                            <div class="label">Health Delta</div>
                            <div class="value">
                                {% if comparison_delta is not none %}
                                    {{ '+' if comparison_delta >= 0 else '' }}{{ comparison_delta }}
                                {% else %}
                                    -
                                {% endif %}
                            </div>
                        </div>
                    </div>
                    <div class="mini-list">
                        <div class="mini-item">
                            <div class="muted">Latest meeting</div>
                            <div style="font-weight:900;margin-top:4px">{{ latest_meeting.topic if latest_meeting else 'No meeting yet' }}</div>
                            <div class="muted" style="margin-top:4px">{{ fmt_dt(latest_meeting.start_time) if latest_meeting else '-' }}</div>
                        </div>
                        <div class="mini-item">
                            <div class="muted">Average attendance score</div>
                            <div style="font-weight:900;margin-top:4px">{{ data.summary.avg_attendance_score }}</div>
                        </div>
                        <div class="mini-item">
                            <div class="muted">Average engagement score</div>
                            <div style="font-weight:900;margin-top:4px">{{ data.summary.avg_engagement_score }}</div>
                        </div>
                    </div>
                </div>
            </div>
        </div>

        <div class="grid-3" style="margin-top:16px">
            <div class="card">
                <div class="section-title">
                    <div>
                        <h3 id="analyticsMembers" class="analytics-anchor-section" style="margin:0">Top Members</h3>
                        <p>Top performers ranked by weighted attendance score, consistency and duration.</p>
                    </div>
                </div>
                <div class="list-card">
                    {% for item in data.top_people %}
                    <div class="list-row">
                        <div>
                            <div style="font-weight:900">{{ item.name }}</div>
                            <div class="muted">Attendance {{ item.attendance_score }} · Engagement {{ item.engagement_score }}</div>
                        </div>
                        <span class="badge ok">{{ item.overall_score }}</span>
                    </div>
                    {% else %}
                    <div class="muted">No ranked members available.</div>
                    {% endfor %}
                </div>
            </div>

            <div class="card">
                <div class="section-title">
                    <div>
                        <h3 id="analyticsRisk" class="analytics-anchor-section" style="margin:0">Risk Members</h3>
                        <p>Members in warning or critical risk zone.</p>
                    </div>
                </div>
                <div class="list-card">
                    {% for item in data.risk_table[:8] %}
                    <div class="list-row">
                        <div>
                            <div style="font-weight:900">{{ item.name }}</div>
                            <div class="muted">{{ item.risk.label }} · Overall {{ item.overall_score }}</div>
                        </div>
                        <span class="badge {{ 'danger' if item.risk.short == 'CRITICAL' else 'warn' }}">{{ item.risk.short }}</span>
                    </div>
                    {% else %}
                    <div class="muted">No members are currently in warning or critical state.</div>
                    {% endfor %}
                </div>
            </div>

            <div class="card">
                <div class="section-title">
                    <div>
                        <h3 style="margin:0">Insights</h3>
                        <p>Auto-generated interpretation from the filtered dataset.</p>
                    </div>
                </div>
                <div class="insight-list">
                    {% for line in data.summary.insight_lines %}
                    <div class="insight-item">{{ line }}</div>
                    {% else %}
                    <div class="insight-item">Not enough data yet to generate analytics insights.</div>
                    {% endfor %}
                    {% if data.reminder_suggestion.count %}
                    <div class="insight-item">Reminder suggestion: {{ data.reminder_suggestion.message }}</div>
                    {% endif %}
                </div>
            </div>
        </div>

        <div class="grid-2" style="margin-top:16px">
            <div class="card">
                <div class="section-title">
                    <div>
                        <h3 id="analyticsReports" class="analytics-anchor-section" style="margin:0">Operational Alerts</h3>
                        <p>Auto-detected reminders, unknown spikes, and meeting health warnings.</p>
                    </div>
                    <a class="btn warn small" href="{{ url_for('analytics_reminder', **request.args) }}">Trigger Reminder Suggestion</a>
                </div>
                <div class="insight-list">
                    {% for alert in data.alerts %}
                    <div class="insight-item" style="border-left:4px solid {% if alert.level == 'danger' %}#ef4444{% elif alert.level == 'warn' %}#f59e0b{% elif alert.level == 'ok' %}#22c55e{% else %}#3b82f6{% endif %}">
                        <div style="font-weight:900">{{ alert.title }}</div>
                        <div class="muted" style="margin-top:4px">{{ alert.text }}</div>
                    </div>
                    {% endfor %}
                </div>
            </div>
            <div class="card">
                <div class="section-title">
                    <div>
                        <h3 style="margin:0">Duration Distribution</h3>
                        <p>How attendance durations are distributed across the filtered records.</p>
                    </div>
                </div>
                <div class="mini-list">
                    {% for bucket, count in data.summary.duration_distribution.items() %}
                    <div class="mini-item">
                        <div class="muted">{{ bucket }} minutes</div>
                        <div style="font-weight:900;margin-top:4px">{{ count }} record(s)</div>
                    </div>
                    {% endfor %}
                </div>
            </div>
        </div>

        <div class="grid-2" style="margin-top:16px">
            <div class="card">
                <div class="section-title">
                    <div>
                        <h3 style="margin:0">Auto Actions</h3>
                        <p>Suggested next actions based on risk, live quality, and meeting intelligence.</p>
                    </div>
                </div>
                <div class="insight-list">
                    {% for action in data.auto_actions %}
                    <div class="insight-item">{{ action }}</div>
                    {% endfor %}
                </div>
            </div>
            <div class="card">
                <div class="section-title">
                    <div>
                        <h3 style="margin:0">Attendance Heatmap</h3>
                        <p>Recent participation footprint for the selected member scope.</p>
                    </div>
                </div>
                <div style="display:grid;grid-template-columns:repeat(14,minmax(0,1fr));gap:6px">
                    {% for cell in data.heatmap %}
                    <div title="{{ cell.title }}" style="height:24px;border-radius:7px;display:grid;place-items:center;font-size:10px;
                        background:{% if cell.css == 'heat-good' %}rgba(34,197,94,.35){% elif cell.css == 'heat-warn' %}rgba(245,158,11,.35){% elif cell.css == 'heat-bad' %}rgba(239,68,68,.35){% else %}rgba(148,163,184,.16){% endif %};
                        border:1px solid rgba(255,255,255,.06)">
                        {{ cell.day }}
                    </div>
                    {% endfor %}
                </div>
            </div>
            <div class="card">
                <div class="section-title">
                    <div>
                        <h3 style="margin:0">Unknown Match Suggestions</h3>
                        <p>Potential member matches for unknown participant names.</p>
                    </div>
                </div>
                <div class="list-card">
                    {% for suggestion in data.unknown_match_suggestions %}
                    <div class="list-row">
                        <div>
                            <div style="font-weight:900">{{ suggestion.unknown }}</div>
                            <div class="muted">Possible match: {{ suggestion.member }}</div>
                        </div>
                        <span class="badge info">{{ suggestion.score }}%</span>
                    </div>
                    {% else %}
                    <div class="muted">No likely unknown-to-member match suggestions right now.</div>
                    {% endfor %}
                </div>
            </div>
        </div>


        <script>
        (() => {
            const tabShell = document.getElementById('analyticsTabsV3');
            if (tabShell) {
                const tabLinks = Array.from(tabShell.querySelectorAll('a[href^="#"]'));
                const sections = tabLinks.map(a => document.querySelector(a.getAttribute('href'))).filter(Boolean);
                tabLinks.forEach(link => link.addEventListener('click', () => {
                    tabLinks.forEach(a => a.classList.remove('active'));
                    link.classList.add('active');
                }));
                if ('IntersectionObserver' in window && sections.length) {
                    const observer = new IntersectionObserver(entries => {
                        const visible = entries.filter(e => e.isIntersecting).sort((a,b)=>b.intersectionRatio-a.intersectionRatio)[0];
                        if (!visible) return;
                        const active = tabLinks.find(a => a.getAttribute('href') === '#' + visible.target.id);
                        if (active) { tabLinks.forEach(a => a.classList.remove('active')); active.classList.add('active'); }
                    }, {rootMargin:'-35% 0px -55% 0px', threshold:[.1,.25,.5]});
                    sections.forEach(section => observer.observe(section));
                }
            }
        })();

        (() => {
            const graphSection = document.getElementById('graphAnalyticsSection');
            const gaXAxis = document.getElementById('gaXAxis');
            const gaYAxis = document.getElementById('gaYAxis');
            const gaFromDate = document.getElementById('gaFromDate');
            const gaToDate = document.getElementById('gaToDate');
            const gaDurationFromDate = document.getElementById('gaDurationFromDate');
            const gaDurationToDate = document.getElementById('gaDurationToDate');
            const gaMonths = document.querySelector('[data-target="gaMonths"]');
            const gaYears = document.querySelector('[data-target="gaYears"]');
            const gaMembers = document.querySelector('[data-target="gaMembers"]');
            const gaApplyBtn = document.getElementById('gaApplyBtn');
            const gaTrendHint = document.getElementById('gaTrendHint');
            const gaDurationHint = document.getElementById('gaDurationHint');
            let gaTrendChart = null;
            let gaDurationChart = null;
            let gaLoaded = false;

            const valueLabelPlugin = {
                id: 'valueLabelPlugin',
                afterDatasetsDraw(chart) {
                    if (chart.config.type !== 'bar') return;
                    const {ctx} = chart;
                    ctx.save();
                    ctx.font = '700 11px Inter, Arial';
                    ctx.textAlign = 'center';
                    ctx.textBaseline = 'bottom';
                    ctx.fillStyle = getComputedStyle(document.body).color || '#e5e7eb';
                    chart.data.datasets.forEach((dataset, datasetIndex) => {
                        const meta = chart.getDatasetMeta(datasetIndex);
                        meta.data.forEach((bar, index) => {
                            const value = dataset.data[index];
                            if (value === null || value === undefined) return;
                            ctx.fillText(value, bar.x, bar.y - 6);
                        });
                    });
                    ctx.restore();
                }
            };

            function selectedValues(boxEl) {
                if (!boxEl) return [];
                const checked = Array.from(boxEl.querySelectorAll('input[type="checkbox"]:checked')).map(input => input.value);
                if (!checked.length || checked.includes('__all__')) return [];
                return checked;
            }

            function setupCheckboxSelect(boxEl) {
                if (!boxEl) return;
                const btn = boxEl.querySelector('.checkbox-select-btn');
                const inputs = Array.from(boxEl.querySelectorAll('input[type="checkbox"]'));
                const allInput = inputs.find(input => input.value === '__all__');
                const refreshLabel = () => {
                    const selected = inputs.filter(input => input.checked && input.value !== '__all__');
                    if (!selected.length || (allInput && allInput.checked)) {
                        btn.textContent = allInput ? allInput.parentElement.textContent.trim() : 'All';
                    } else if (selected.length === 1) {
                        btn.textContent = selected[0].parentElement.textContent.trim();
                    } else {
                        btn.textContent = `${selected.length} selected`;
                    }
                };
                btn?.addEventListener('click', (event) => {
                    event.stopPropagation();
                    document.querySelectorAll('.checkbox-select.open').forEach(el => { if (el !== boxEl) el.classList.remove('open'); });
                    boxEl.classList.toggle('open');
                });
                inputs.forEach(input => input.addEventListener('change', () => {
                    if (input.value === '__all__' && input.checked) {
                        inputs.forEach(other => { if (other !== input) other.checked = false; });
                    } else if (input.value !== '__all__' && input.checked && allInput) {
                        allInput.checked = false;
                    }
                    if (!inputs.some(item => item.checked) && allInput) allInput.checked = true;
                    refreshLabel();
                    if (gaLoaded) loadGraphAnalytics();
                }));
                refreshLabel();
            }
            document.addEventListener('click', () => document.querySelectorAll('.checkbox-select.open').forEach(el => el.classList.remove('open')));
            document.querySelectorAll('.checkbox-select').forEach(box => {
                box.addEventListener('click', (event) => event.stopPropagation());
                const menu = box.querySelector('.checkbox-select-menu');
                if (menu) menu.addEventListener('click', (event) => event.stopPropagation());
                setupCheckboxSelect(box);
            });

            function updateGraphFilterVisibility() {
                if (!gaXAxis) return;
                const mode = gaXAxis.value;
                document.querySelectorAll('.ga-date-filter').forEach(el => el.style.display = mode === 'date' ? '' : 'none');
                document.querySelectorAll('.ga-month-filter').forEach(el => el.style.display = mode === 'month' ? '' : 'none');
                document.querySelectorAll('.ga-year-filter').forEach(el => el.style.display = mode === 'year' ? '' : 'none');
            }

            function buildGraphQuery() {
                const params = new URLSearchParams();
                params.set('x_axis', gaXAxis?.value || 'date');
                params.set('y_axis', gaYAxis?.value || 'count');
                if ((gaXAxis?.value || 'date') === 'date') {
                    const fromVal = gaDurationFromDate?.value || gaFromDate?.value;
                    const toVal = gaDurationToDate?.value || gaToDate?.value;
                    if (fromVal) params.set('from_date', fromVal);
                    if (toVal) params.set('to_date', toVal);
                }
                selectedValues(gaMonths).forEach(v => params.append('months', v));
                selectedValues(gaYears).forEach(v => params.append('years', v));
                selectedValues(gaMembers).forEach(v => params.append('member_ids', v));
                return params.toString();
            }

            async function loadGraphAnalytics() {
                if (!graphSection) return;
                graphSection.classList.add('loading');
                try {
                    const response = await fetch(`{{ url_for('analytics_graph_data') }}?${buildGraphQuery()}`, {
                        headers: {'X-Requested-With': 'XMLHttpRequest'}
                    });
                    if (!response.ok) throw new Error('Graph request failed');
                    const payload = await response.json();
                    renderTrendGraph(payload.trend);
                    renderDurationGraph(payload.duration);
                    gaLoaded = true;
                } catch (err) {
                    console.error(err);
                    if (gaTrendHint) gaTrendHint.textContent = 'Unable to load graph analytics. Please check server logs.';
                } finally {
                    graphSection.classList.remove('loading');
                }
            }

            function renderTrendGraph(trend) {
                const canvas = document.getElementById('gaTrendChart');
                if (!canvas || !window.Chart) return;
                if (gaTrendChart) gaTrendChart.destroy();
                const suffix = trend.y_axis === 'percentage' ? '%' : '';
                if (gaTrendHint) gaTrendHint.textContent = `X-axis: ${trend.x_axis}. Y-axis: ${trend.y_axis}.`;
                gaTrendChart = new Chart(canvas, {
                    type: 'line',
                    data: {
                        labels: trend.labels,
                        datasets: [
                            {label: 'Present', data: trend.present, borderColor: '#22c55e', backgroundColor: 'rgba(34,197,94,.10)', fill: false},
                            {label: 'Late', data: trend.late, borderColor: '#f59e0b', backgroundColor: 'rgba(245,158,11,.10)', fill: false},
                            {label: 'Absent', data: trend.absent, borderColor: '#ef4444', backgroundColor: 'rgba(239,68,68,.10)', fill: false},
                            {label: 'Unknown', data: trend.unknown, borderColor: '#38bdf8', backgroundColor: 'rgba(56,189,248,.10)', fill: false}
                        ]
                    },
                    options: {
                        responsive: true,
                        interaction: {mode: 'index', intersect: false},
                        plugins: {
                            legend: {display: true},
                            tooltip: {callbacks: {label: ctx => `${ctx.dataset.label}: ${ctx.parsed.y}${suffix}`}}
                        },
                        scales: {y: {beginAtZero: true, ticks: {callback: value => `${value}${suffix}`}}}
                    }
                });
            }

            function renderDurationGraph(duration) {
                const canvas = document.getElementById('gaDurationChart');
                if (!canvas || !window.Chart) return;
                if (gaDurationChart) gaDurationChart.destroy();
                const single = duration.mode === 'single_member_date_duration';
                if (gaDurationHint) {
                    gaDurationHint.textContent = single
                        ? `${duration.selected_member_name || 'Selected member'}: date vs duration in minutes.`
                        : 'Selected members: total duration in minutes.';
                }
                gaDurationChart = new Chart(canvas, {
                    type: 'bar',
                    plugins: [valueLabelPlugin],
                    data: {
                        labels: duration.labels,
                        datasets: [{
                            label: 'Minutes',
                            data: duration.values,
                            borderRadius: 10,
                            backgroundColor: duration.labels.map((_, i) => `hsla(${(i * 47) % 360}, 72%, 55%, .78)`)
                        }]
                    },
                    options: {
                        responsive: true,
                        plugins: {legend: {display: false}},
                        scales: {
                            x: {grid: {display: false}},
                            y: {beginAtZero: true, title: {display: true, text: 'Minutes'}}
                        }
                    }
                });
            }

            updateGraphFilterVisibility();
            [gaXAxis, gaYAxis].forEach(el => el && el.addEventListener('change', () => {
                updateGraphFilterVisibility();
                if (gaLoaded) loadGraphAnalytics();
            }));
            [gaFromDate, gaToDate, gaDurationFromDate, gaDurationToDate].forEach(el => el && el.addEventListener('change', () => {
                if (gaLoaded) loadGraphAnalytics();
            }));
            gaApplyBtn?.addEventListener('click', loadGraphAnalytics);

            if (graphSection && 'IntersectionObserver' in window) {
                const observer = new IntersectionObserver(entries => {
                    if (entries.some(entry => entry.isIntersecting) && !gaLoaded) {
                        loadGraphAnalytics();
                        observer.disconnect();
                    }
                }, {rootMargin: '200px'});
                observer.observe(graphSection);
            } else {
                loadGraphAnalytics();
            }
        })();

        (() => {
            const trendCanvas = document.getElementById('trendChart');
            if (trendCanvas) {
                new Chart(trendCanvas, {
                    type: 'line',
                    data: {
                        labels: {{ trend.labels|tojson }},
                        datasets: [
                            {
                                label: 'Present',
                                data: {{ trend.present|tojson }},
                                borderColor: '#22c55e',
                                backgroundColor: 'rgba(34,197,94,.12)',
                                fill: true
                            },
                            {
                                label: 'Late',
                                data: {{ trend.late|tojson }},
                                borderColor: '#f59e0b',
                                backgroundColor: 'rgba(245,158,11,.10)',
                                fill: true
                            },
                            {
                                label: 'Absent',
                                data: {{ trend.absent|tojson }},
                                borderColor: '#ef4444',
                                backgroundColor: 'rgba(239,68,68,.08)',
                                fill: true
                            }
                        ]
                    },
                    options: {
                        interaction: {mode: 'index', intersect: false},
                        plugins: {legend: {display: true}}
                    }
                });
            }

            const mixCanvas = document.getElementById('statusMixChart');
            if (mixCanvas) {
                new Chart(mixCanvas, {
                    type: 'doughnut',
                    data: {
                        labels: ['Present', 'Late', 'Absent'],
                        datasets: [{
                            data: [{{ data.summary.present_rows }}, {{ data.summary.late_rows }}, {{ data.summary.absent_rows }}],
                            backgroundColor: ['#22c55e','#f59e0b','#ef4444'],
                            borderWidth: 0,
                            hoverOffset: 8
                        }]
                    },
                    options: {
                        cutout: '68%',
                        plugins: {legend: {display: true}}
                    }
                });
            }

            const memberCanvas = document.getElementById('memberDurationChart');
            if (memberCanvas) {
                new Chart(memberCanvas, {
                    type: 'bar',
                    data: {
                        labels: {{ member_chart.labels|tojson }},
                        datasets: [{
                            label: 'Minutes',
                            data: {{ member_chart.chart_values|tojson }},
                            borderRadius: 10,
                            backgroundColor: ['rgba(37,99,235,.78)','rgba(79,70,229,.78)','rgba(124,58,237,.78)','rgba(34,197,94,.72)','rgba(8,145,178,.72)','rgba(245,158,11,.72)','rgba(239,68,68,.72)']
                        }]
                    },
                    options: {
                        plugins: {legend: {display: false}},
                        scales: {
                            x: {grid: {display: false}},
                            y: {beginAtZero: true}
                        }
                    }
                });
            }
        })();
        </script>
        """,
        filters=data["filters"],
        data=data,
        trend=trend,
        member_chart=member_chart,
        fmt_dt=fmt_dt,
        member_display_name=member_display_name,
        export_csv_url=export_csv_url,
        export_pdf_url=export_pdf_url,
        latest_meeting=latest_meeting,
        previous_meeting=previous_meeting,
        comparison_delta=comparison_delta,
        request=request,
        graph_options=graph_options,
    )
    return page("Analytics", body, "analytics")





# ---- attendance_register ----
@app.route("/attendance-register")
@login_required
def attendance_register():
    today = today_local()
    data = attendance_register_payload(
        request.args.get("year", today.year),
        request.args.get("month", today.month),
        request.args.get("search", ""),
        request.args.get("page", 1),
        request.args.get("per_page", 25),
    )
    body = render_template_string(
        """
        <style>
        .reg-dashboard-shell{display:grid;grid-template-columns:180px minmax(0,1fr) 210px;gap:14px;align-items:start;margin-top:8px}
        .reg-side-note{background:#eefdf0;border:1px solid #8bd49a;border-radius:14px;padding:14px;font-size:12px;line-height:1.55;color:#12351d;position:sticky;top:92px}
        .reg-side-note b{display:block;margin-bottom:7px;color:#14532d}.reg-feature-box{background:#f5f0ff;border:1px solid #bca7f5;border-radius:14px;padding:16px;color:#3b2a73;line-height:1.7;position:sticky;top:92px}.reg-feature-box h3{margin:0 0 8px;font-size:16px}.reg-feature-box ul{margin:0;padding-left:18px;font-size:13px}
        .register-book{background:linear-gradient(135deg,#7c4a22,#4b2d16);padding:12px;border-radius:20px;box-shadow:0 18px 40px rgba(77,45,22,.35), inset 0 0 0 3px rgba(255,255,255,.12)}
        .register-paper{background:#fffdf4;color:#1f2937;border-radius:13px;padding:14px;box-shadow:inset 0 0 0 1px #d7c9a5}
        .register-heading{display:flex;justify-content:center;margin:-28px 0 10px}.register-heading span{background:#14532d;color:#fff;border-radius:8px;padding:8px 36px;font-weight:950;font-size:22px;box-shadow:0 7px 20px rgba(20,83,45,.28)}
        .reg-topbar{display:flex;justify-content:space-between;align-items:center;gap:10px;margin-bottom:8px;flex-wrap:wrap}.reg-month-nav{display:flex;align-items:center;gap:8px}.reg-month-pill{background:#f8fafc;border:1px solid #cbd5e1;border-radius:7px;padding:6px 14px;font-weight:900}.reg-controls{display:flex;gap:8px;align-items:end;flex-wrap:wrap}.reg-controls input,.reg-controls select{height:34px;border-radius:8px;border:1px solid #cbd5e1;padding:6px 10px}.reg-controls label{font-size:11px;font-weight:900;color:#475569;display:block;margin-bottom:2px}.reg-controls .btn,.reg-controls button{height:34px;padding:7px 10px;border-radius:8px;font-size:12px}
        .register-table-wrap{max-height:72vh;overflow:auto;border-radius:10px;border:1px solid #cfc2a4;background:#fffdf4}.register-table{border-collapse:separate;border-spacing:0;width:max-content;min-width:100%;font-size:13px}.register-table th,.register-table td{min-width:38px;text-align:center;padding:8px;border-bottom:1px solid #d8cdb5;border-right:1px solid #d8cdb5}.register-table th{position:sticky;top:0;z-index:4;background:#f3ebd8;color:#111827}.register-table .sticky-member{position:sticky;left:0;z-index:5;min-width:180px;text-align:left;background:#f3ebd8}.register-table td.sticky-member{z-index:3;background:#fff8df;font-weight:900;cursor:pointer}.register-table td.sticky-member:hover{outline:2px solid #22c55e;border-radius:8px}.reg-cell{font-weight:950;border-radius:6px}.reg-p{color:#15803d}.reg-l{color:#ea580c}.reg-a{color:#dc2626}.reg-u{color:#64748b}.reg-empty{color:#cbd5e1}.modal-backdrop{display:none;position:fixed;inset:0;background:rgba(2,6,23,.68);z-index:999;align-items:center;justify-content:center;padding:18px}.modal-backdrop.show{display:flex}.modal-card{max-width:460px;width:100%;background:#0f172a;color:#e5e7eb;border:1px solid rgba(148,163,184,.3);border-radius:22px;padding:22px;box-shadow:0 30px 80px rgba(0,0,0,.45)}
        @media print{.sidebar,.topbar,.reg-side-note,.reg-feature-box,.reg-controls,.reg-month-nav{display:none!important}.main{margin:0!important}.register-book{box-shadow:none;background:#fff;padding:0}.register-heading span{color:#000;background:#fff;border:1px solid #000}.register-table-wrap{max-height:none;overflow:visible}.register-table th{position:static}.register-table .sticky-member{position:static}}
        @media(max-width:1180px){.reg-dashboard-shell{grid-template-columns:1fr}.reg-side-note,.reg-feature-box{position:static}.register-heading span{font-size:17px;padding:8px 14px}}
        
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


        <style>
        /* Premium readable register theme: keeps book structure, fixes color clarity and spacing */
        .reg-dashboard-shell{grid-template-columns:minmax(0,1fr)!important;}
        .reg-side-note,.reg-feature-box{display:none!important;}
        .register-book{background:linear-gradient(135deg,#3a2418,#6b4428 45%,#2b1b12)!important;border:1px solid rgba(255,232,180,.18)!important;box-shadow:0 24px 70px rgba(0,0,0,.42), inset 0 0 0 3px rgba(255,255,255,.08)!important;}
        .register-paper{background:linear-gradient(180deg,#fffaf0,#fff7df)!important;border-color:#d6bd8b!important;color:#172033!important;}
        /* DARK_REGISTER_THEME_V3: darker register with colorful P/L/A/U cells */
        .reg-dashboard-shell{background:radial-gradient(circle at top,#13213b 0%,#07111f 46%,#030712 100%)!important;color:#e5e7eb!important;}
        .register-book{background:linear-gradient(135deg,rgba(15,23,42,.96),rgba(2,6,23,.98))!important;border:1px solid rgba(59,130,246,.32)!important;box-shadow:0 28px 70px rgba(0,0,0,.55)!important;}
        .register-paper{background:rgba(8,13,27,.96)!important;border:1px solid rgba(148,163,184,.20)!important;box-shadow:inset 0 0 0 1px rgba(255,255,255,.03)!important;}
        .register-heading span{background:linear-gradient(90deg,#0f766e,#2563eb,#7c3aed)!important;color:white!important;letter-spacing:.3px;box-shadow:0 14px 34px rgba(37,99,235,.35)!important;}
        .register-table-wrap{background:#07111f!important;border-color:rgba(59,130,246,.35)!important;box-shadow:0 18px 45px rgba(0,0,0,.42)!important;}
        .register-table{border-spacing:4px!important;background:#07111f!important;}
        .register-table th{background:linear-gradient(180deg,#10223f,#0b162b)!important;color:#eaf2ff!important;border:1px solid rgba(96,165,250,.34)!important;font-weight:950;}
        .register-table th.reg-total-head{background:linear-gradient(180deg,#1e3a8a,#172554)!important;color:#dbeafe!important;}
        .register-table td{background:#111827!important;color:#e5e7eb!important;border:1px solid rgba(148,163,184,.20)!important;}
        .register-table .sticky-member{background:#0b1220!important;color:#f8fafc!important;box-shadow:4px 0 16px rgba(0,0,0,.35)!important;}
        .register-table td.sticky-member{background:#0f172a!important;color:#f8fafc!important;}
        .register-table td.reg-total-cell{background:#1e293b!important;color:#bfdbfe!important;font-weight:950!important;}
        .register-table td.reg-p{background:linear-gradient(135deg,#064e3b,#16a34a)!important;color:#ecfdf5!important;font-weight:1000!important;text-shadow:0 1px 2px rgba(0,0,0,.35);}
        .register-table td.reg-l{background:linear-gradient(135deg,#78350f,#f59e0b)!important;color:#fff7ed!important;font-weight:1000!important;text-shadow:0 1px 2px rgba(0,0,0,.30);}
        .register-table td.reg-a{background:linear-gradient(135deg,#7f1d1d,#ef4444)!important;color:#fff1f2!important;font-weight:1000!important;text-shadow:0 1px 2px rgba(0,0,0,.30);}
        .register-table td.reg-u{background:linear-gradient(135deg,#334155,#94a3b8)!important;color:#f8fafc!important;font-weight:1000!important;text-shadow:0 1px 2px rgba(0,0,0,.35);}
        .register-table td.reg-empty{background:#101827!important;color:#334155!important;}
        .register-table td.reg-p,.register-table td.reg-l,.register-table td.reg-a,.register-table td.reg-u{border-radius:8px!important;box-shadow:0 4px 12px rgba(0,0,0,.20),inset 0 0 0 1px rgba(255,255,255,.12)!important;}
        .reg-month-pill{background:#0f172a!important;color:#dbeafe!important;border-color:rgba(96,165,250,.45)!important;}
        .reg-controls input,.reg-controls select{background:#0b1220!important;color:#e5e7eb!important;border-color:rgba(96,165,250,.35)!important;}
        .reg-controls label{color:#bfdbfe!important;}
        .reg-side-note,.reg-feature-box{background:rgba(15,23,42,.86)!important;color:#dbeafe!important;border-color:rgba(96,165,250,.25)!important;box-shadow:0 18px 40px rgba(0,0,0,.38)!important;}
        .register-book.reg-light{background:linear-gradient(135deg,#7c4a22,#4b2d16)!important;border-color:rgba(255,232,180,.25)!important;}
        .register-book.reg-light .register-paper{background:linear-gradient(180deg,#fffaf0,#fff7df)!important;color:#172033!important;border-color:#d6bd8b!important;}
        .register-book.reg-light .register-table-wrap{background:#fffdf4!important;border-color:#cfc2a4!important;box-shadow:none!important;}
        .register-book.reg-light .register-table{background:#fffdf4!important;border-spacing:2px!important;}
        .register-book.reg-light .register-table th{background:#064e3b!important;color:#fff!important;border-color:#d8cdb5!important;}
        .register-book.reg-light .register-table td{background:#fffaf0!important;color:#1f2937!important;border-color:#d8cdb5!important;}
        .register-book.reg-light .register-table .sticky-member,.register-book.reg-light .register-table td.sticky-member{background:#fff0c7!important;color:#111827!important;}
        .register-book.reg-light .register-table td.reg-total-cell{background:#e0f2fe!important;color:#0f172a!important;}
        .register-book.reg-light .register-table td.reg-p{background:#bbf7d0!important;color:#15803d!important;text-shadow:none!important;}
        .register-book.reg-light .register-table td.reg-l{background:#fed7aa!important;color:#c2410c!important;text-shadow:none!important;}
        .register-book.reg-light .register-table td.reg-a{background:#fecaca!important;color:#b91c1c!important;text-shadow:none!important;}
        .register-book.reg-light .register-table td.reg-u{background:#e5e7eb!important;color:#475569!important;text-shadow:none!important;}
        .register-book.reg-light .register-table td.reg-empty{background:#fffaf0!important;color:#d6bd8b!important;}
        .register-book.reg-light .reg-month-pill{background:#f8fafc!important;color:#0f172a!important;border-color:#cbd5e1!important;}
        .register-book.reg-light .reg-controls input,.register-book.reg-light .reg-controls select{background:#fff!important;color:#111827!important;border-color:#cbd5e1!important;}
        .register-book.reg-light .reg-controls label{color:#475569!important;}
        .reg-pagination{display:flex;gap:8px;align-items:center;justify-content:flex-end;margin-top:10px;flex-wrap:wrap;color:#cbd5e1;font-weight:800}
        .reg-pagination a,.reg-pagination span{padding:7px 10px;border-radius:8px;background:#0f172a;border:1px solid rgba(96,165,250,.35);color:#dbeafe;text-decoration:none}
        .reg-pagination .disabled{opacity:.45}
        
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
        <div class="reg-dashboard-shell">
            <aside class="reg-side-note">
                <b>MONTHLY REGISTER VIEW</b>
                Each page represents a month.<br><br>
                <b>Cells</b>
                <span style="color:#15803d;font-weight:900">P</span> Present - Green<br>
                <span style="color:#ea580c;font-weight:900">L</span> Late - Orange<br>
                <span style="color:#dc2626;font-weight:900">A</span> Absent - Red<br>
                <span style="color:#64748b;font-weight:900">U</span> Unknown - Gray<br><br>
                Click on participant name to view summary.
            </aside>

            <main class="register-book">
                <div class="register-heading"><span>2. ATTENDANCE REGISTER (MONTHLY VIEW)</span></div>
                <div style="display:flex;justify-content:flex-end;margin:-6px 0 8px"><button type="button" id="registerThemeToggle" class="btn secondary small">🌙 Dark Register</button></div>
                <div class="register-paper">
                    <form method="get" class="reg-topbar">
                        <div class="reg-month-nav">
                            {% set prev_month = 12 if data.month == 1 else data.month - 1 %}
                            {% set prev_year = data.year - 1 if data.month == 1 else data.year %}
                            {% set next_month = 1 if data.month == 12 else data.month + 1 %}
                            {% set next_year = data.year + 1 if data.month == 12 else data.year %}
                            <a class="btn secondary small" href="{{ url_for('attendance_register', month=prev_month, year=prev_year, search=request.args.get('search','')) }}">‹</a>
                            <span class="reg-month-pill">{{ data.month_name }} {{ data.year }}</span>
                            <a class="btn secondary small" href="{{ url_for('attendance_register', month=next_month, year=next_year, search=request.args.get('search','')) }}">›</a>
                        </div>
                        <div class="reg-controls">
                            <div><label>Month</label><select name="month" id="regMonth">{% for i in range(1, 13) %}<option value="{{ i }}" {% if i == data.month %}selected{% endif %}>{{ month_names[i-1] }}</option>{% endfor %}</select></div>
                            <div><label>Year</label><select name="year" id="regYear">{% for y in data.years %}<option value="{{ y }}" {% if y|string == data.year|string %}selected{% endif %}>{{ y }}</option>{% endfor %}</select></div>
                            <div><label>Search member</label><input type="text" name="search" id="regSearch" value="{{ request.args.get('search','') }}" placeholder="member name"></div>
                            <button type="submit">Apply</button>
                            <button type="button" onclick="window.print()">Print</button>
                            <a class="btn secondary" href="{{ url_for('attendance_register_export_pdf', month=data.month, year=data.year, search=request.args.get('search','')) }}">PDF</a>
                            <a class="btn success" href="{{ url_for('attendance_register_export_excel', month=data.month, year=data.year, search=request.args.get('search','')) }}">Excel</a>
                        </div>
                    </form>

                    <div class="register-table-wrap">
                        <table class="register-table" id="attendanceRegisterTable">
                            <thead>
                                <tr>
                                    <th class="sticky-member">Name</th>
                                    <th class="reg-total-head">Total</th>
                                    {% for d in data.days %}<th>{{ d }}</th>{% endfor %}
                                    <th>P</th><th>L</th><th>A</th><th>U</th><th>%</th>
                                </tr>
                            </thead>
                            <tbody>
                                {% for row in data.rows %}
                                <tr>
                                    <td class="sticky-member reg-member" data-name="{{ row.name }}" data-present="{{ row.totals.P }}" data-late="{{ row.totals.L }}" data-absent="{{ row.totals.A }}" data-unknown="{{ row.totals.U }}" data-total="{{ row.total_meetings }}" data-percent="{{ row.attendance_pct }}">{{ row.name }}</td>
                                    <td class="reg-total-cell">{{ row.total_meetings }}</td>
                                    {% for cell in row.cells %}<td class="reg-cell {% if cell == 'P' %}reg-p{% elif cell == 'L' %}reg-l{% elif cell == 'A' %}reg-a{% elif cell == 'U' %}reg-u{% else %}reg-empty{% endif %}">{{ cell or '' }}</td>{% endfor %}
                                    <td>{{ row.totals.P }}</td><td>{{ row.totals.L }}</td><td>{{ row.totals.A }}</td><td>{{ row.totals.U }}</td><td>{{ row.attendance_pct }}%</td>
                                </tr>
                                {% endfor %}
                            </tbody>
                        </table>
                    </div>
                    <div class="reg-pagination">
                        {% set pg = data.pagination %}
                        {% if pg.has_prev %}
                            <a href="{{ url_for('attendance_register', month=data.month, year=data.year, search=request.args.get('search',''), page=pg.page-1, per_page=pg.per_page) }}">‹ Previous</a>
                        {% else %}
                            <span class="disabled">‹ Previous</span>
                        {% endif %}
                        <span>Page {{ pg.page }} / {{ pg.pages }} · {{ pg.total }} members</span>
                        {% if pg.has_next %}
                            <a href="{{ url_for('attendance_register', month=data.month, year=data.year, search=request.args.get('search',''), page=pg.page+1, per_page=pg.per_page) }}">Next ›</a>
                        {% else %}
                            <span class="disabled">Next ›</span>
                        {% endif %}
                    </div>
                </div>
            </main>

            <aside class="reg-feature-box">
                <h3>FEATURES</h3>
                <ul>
                    <li>Book-style monthly pages</li>
                    <li>Auto adjust days 28/29/30/31</li>
                    <li>Color coded attendance</li>
                    <li>Click name → View summary</li>
                    <li>Easy month navigation</li>
                    <li>PDF, Excel and Print</li>
                </ul>
            </aside>
        </div>

        <div class="modal-backdrop" id="regModal">
            <div class="modal-card">
                <div class="section-title"><h3 id="regModalName" style="margin:0">Member</h3><button type="button" id="regModalClose">Close</button></div>
                <div class="grid-2">
                    <div class="mini-kpi"><div class="label">Total Meetings</div><div class="value" id="regModalTotal">0</div></div>
                    <div class="mini-kpi"><div class="label">Total Present</div><div class="value" id="regModalP">0</div></div>
                    <div class="mini-kpi"><div class="label">Total Late</div><div class="value" id="regModalL">0</div></div>
                    <div class="mini-kpi"><div class="label">Total Absent</div><div class="value" id="regModalA">0</div></div>
                    <div class="mini-kpi"><div class="label">Attendance %</div><div class="value" id="regModalPct">0%</div></div>
                </div>
            </div>
        </div>

        <script>
        (() => {
            const modal = document.getElementById('regModal');
            const closeBtn = document.getElementById('regModalClose');
            document.querySelectorAll('.reg-member').forEach(cell => {
                cell.addEventListener('click', () => {
                    document.getElementById('regModalName').textContent = cell.dataset.name || 'Member';
                    document.getElementById('regModalTotal').textContent = cell.dataset.total || '0';
                    document.getElementById('regModalP').textContent = cell.dataset.present || '0';
                    document.getElementById('regModalL').textContent = cell.dataset.late || '0';
                    document.getElementById('regModalA').textContent = cell.dataset.absent || '0';
                    document.getElementById('regModalPct').textContent = (cell.dataset.percent || '0') + '%';
                    modal.classList.add('show');
                });
            });
            closeBtn?.addEventListener('click', () => modal.classList.remove('show'));
            modal?.addEventListener('click', e => { if (e.target === modal) modal.classList.remove('show'); });
            const book = document.querySelector('.register-book');
            const themeBtn = document.getElementById('registerThemeToggle');
            function applyRegisterTheme(mode){
                if(!book || !themeBtn) return;
                const light = mode === 'light';
                book.classList.toggle('reg-light', light);
                themeBtn.textContent = light ? '☀️ Light Register' : '🌙 Dark Register';
                localStorage.setItem('registerThemeMode', light ? 'light' : 'dark');
            }
            applyRegisterTheme(localStorage.getItem('registerThemeMode') || 'dark');
            themeBtn?.addEventListener('click', () => applyRegisterTheme(book.classList.contains('reg-light') ? 'dark' : 'light'));
        })();
        </script>
        """,
        data=data,
        month_names=["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"],
        request=request,
    )
    return page("Attendance Register", body, "attendance_register")



# ---- attendance_register_data ----
@app.route("/attendance-register/data")
@login_required
def attendance_register_data():
    return jsonify(attendance_register_payload(
        request.args.get("year"),
        request.args.get("month"),
        request.args.get("search", ""),
        request.args.get("page", 1),
        request.args.get("per_page", 25),
    ))



# ---- attendance_register_export_excel ----
@app.route("/attendance-register/export/excel")
@login_required
def attendance_register_export_excel():
    data = attendance_register_payload(request.args.get("year"), request.args.get("month"), request.args.get("search", ""), all_rows=True)
    output = io.StringIO()
    output.write("<html><head><meta charset='utf-8'></head><body><table border='1'>")
    output.write(f"<tr><th colspan='{len(data['days']) + 7}'>Attendance Register - {data['month_name']} {data['year']}</th></tr>")
    output.write("<tr><th>Member</th><th>Total</th>" + "".join(f"<th>{d}</th>" for d in data["days"]) + "<th>P</th><th>L</th><th>A</th><th>U</th><th>%</th></tr>")
    for row in data["rows"]:
        output.write(f"<tr><td>{row['name']}</td><td>{row['total_meetings']}</td>" + "".join(f"<td>{c or '-'}</td>" for c in row["cells"]) + f"<td>{row['totals']['P']}</td><td>{row['totals']['L']}</td><td>{row['totals']['A']}</td><td>{row['totals']['U']}</td><td>{row['attendance_pct']}%</td></tr>")
    output.write("</table></body></html>")
    filename = f"attendance_register_{data['year']}_{data['month']:02d}.xls"
    return Response(output.getvalue(), mimetype="application/vnd.ms-excel", headers={"Content-Disposition": f"attachment; filename={filename}"})



# ---- attendance_register_export_pdf ----
@app.route("/attendance-register/export/pdf")
@login_required
def attendance_register_export_pdf():
    data = attendance_register_payload(request.args.get("year"), request.args.get("month"), request.args.get("search", ""), all_rows=True)
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter)
    styles = getSampleStyleSheet()
    story = [Paragraph(f"Attendance Register - {data['month_name']} {data['year']}", styles["Title"]), Spacer(1, 10)]
    table_data = [["Member", "Total"] + [str(d) for d in data["days"]] + ["P", "L", "A", "U", "%"]]
    for row in data["rows"][:80]:
        table_data.append([row["name"][:24], row["total_meetings"]] + [c or "-" for c in row["cells"]] + [row["totals"]["P"], row["totals"]["L"], row["totals"]["A"], row["totals"]["U"], f"{row['attendance_pct']}%"])
    tbl = Table(table_data, repeatRows=1)
    tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#111827")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
        ("FONTSIZE", (0, 0), (-1, -1), 6),
        ("BACKGROUND", (1, 1), (-6, -1), colors.whitesmoke),
    ]))
    story.append(tbl)
    doc.build(story)
    buffer.seek(0)
    return send_file(buffer, as_attachment=True, download_name=f"attendance_register_{data['year']}_{data['month']:02d}.pdf", mimetype="application/pdf")



# ---- analytics_graph_data ----
@app.route("/analytics/graph-data")
@login_required
def analytics_graph_data():
    maybe_finalize_stale_live_meetings()
    return jsonify(graph_analytics_payload())



# ---- analytics_reminder ----
@app.route("/analytics/reminder")
@login_required
def analytics_reminder():
    filters = {
        "period_mode": request.args.get("period_mode", "custom"),
        "from_date": request.args.get("from_date", ""),
        "to_date": request.args.get("to_date", ""),
        "meeting_uuid": request.args.get("meeting_uuid", ""),
        "member_ids": request.args.getlist("member_ids"),
        "person_name": request.args.get("person_name", ""),
        "participant_type": request.args.get("participant_type", "all"),
    }
    data = analytics_data(filters)
    names = data["reminder_suggestion"].get("names") or []
    if names:
        flash("Reminder suggestion prepared for: " + ", ".join(names), "success")
    else:
        flash("No urgent reminder targets found in the current filtered view.", "success")
    query = build_filter_query(data["filters"])
    return redirect(url_for("analytics") + (f"?{query}" if query else ""))


# ---- export_analytics_csv ----
@app.route("/analytics/export.csv")
@login_required
def export_analytics_csv():
    filters = dict(request.args)
    filters["member_ids"] = request.args.getlist("member_ids")
    data = analytics_data(filters)
    content = export_csv_bytes(data["rows"])
    filename = f"analytics_{slugify(now_local().strftime('%Y%m%d_%H%M%S'))}.csv"
    return Response(content, mimetype="text/csv", headers={"Content-Disposition": f"attachment; filename={filename}"})



# ---- export_analytics_pdf ----
@app.route("/analytics/export.pdf")
@login_required
def export_analytics_pdf():
    filters = dict(request.args)
    filters["member_ids"] = request.args.getlist("member_ids")
    data = analytics_data(filters)
    pdf = export_pdf_bytes("Filtered Analytics Report", data["rows"], data["summary"])
    return send_file(io.BytesIO(pdf), download_name="analytics_report.pdf", mimetype="application/pdf", as_attachment=True)




# ---- api_ai_assistant_level3 ----
@app.route('/api/ai-assistant-level3', methods=['POST'])
@login_required
def api_ai_assistant_level3():
    payload=request.get_json(silent=True) or {}
    return jsonify(_ai_bot_answer(payload.get('query','')))


# ---- api_ai_insights_level3 ----
@app.route('/api/ai-insights-level3')
@login_required
def api_ai_insights_level3():
    return jsonify({'insights':generate_ai_level3_insights(),'members':_ai_member_stats(),'meetings':[dict(m) for m in _ai_recent_meetings(8)]})


# ---- ai_export_low_attendance_csv ----
@app.route('/ai/export/low-attendance.csv')
@login_required
def ai_export_low_attendance_csv():
    output=io.StringIO(); writer=csv.writer(output); writer.writerow(['Name','Email','Attendance %','Risk','Trend','Suggestion'])
    for m in _ai_low_attendance_members('below 75'): writer.writerow([m['name'],m['email'],m['attendance_pct'],m['risk'],m['trend'],m['suggestion']])
    return Response(output.getvalue(), mimetype='text/csv', headers={'Content-Disposition':'attachment; filename=ai_low_attendance_report.csv'})


# ---- ai_export_low_attendance_pdf ----
@app.route('/ai/export/low-attendance.pdf')
@login_required
def ai_export_low_attendance_pdf():
    buf=io.BytesIO(); doc=SimpleDocTemplate(buf,pagesize=letter); styles=getSampleStyleSheet(); story=[Paragraph('AI Low Attendance Report',styles['Title']),Spacer(1,12)]
    data=[['Name','Attendance %','Risk','Suggestion']]+[[m['name'],str(m['attendance_pct']),m['risk'],m['suggestion'][:60]] for m in _ai_low_attendance_members('below 75')[:50]]
    table=Table(data, repeatRows=1); table.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,0),colors.HexColor('#111827')),('TEXTCOLOR',(0,0),(-1,0),colors.white),('GRID',(0,0),(-1,-1),0.25,colors.grey),('FONTSIZE',(0,0),(-1,-1),8)])); story.append(table); doc.build(story); buf.seek(0)
    return send_file(buf, as_attachment=True, download_name='ai_low_attendance_report.pdf', mimetype='application/pdf')


# ---- ai_intelligence ----
@app.route('/ai-intelligence')
@login_required
def ai_intelligence():
    insights=generate_ai_level3_insights(); members=_ai_member_stats(); meetings=_ai_recent_meetings(8)
    critical=len([m for m in members if m['risk']=='Critical']); warning=len([m for m in members if m['risk']=='Warning'])
    latest_score=_ai_meeting_health_score(meetings[0]) if meetings else 0
    avg_duration=round(sum([m.get('duration_minutes',0) for m in members])/max(len(members),1),2)
    basis=(members[0].get('basis') if members else 'Current month')
    logs=[]
    try:
        with db() as conn:
            with conn.cursor() as cur:
                if table_exists(conn,'smart_alert_logs'):
                    cur.execute('SELECT title, message, current_state, created_at FROM smart_alert_logs ORDER BY created_at DESC LIMIT 8'); logs=cur.fetchall()
    except Exception: logs=[]
    heat_members=members[:20]; heat_meetings=list(reversed(meetings[:12])); heat=[]
    try:
        heat_member_ids=[m.get('id') for m in heat_members if m.get('id') is not None]; heat_meeting_uuids=[mt.get('meeting_uuid') for mt in heat_meetings if mt.get('meeting_uuid')]; status_map={}
        if heat_member_ids and heat_meeting_uuids:
            with db() as conn:
                with conn.cursor() as cur:
                    cur.execute('SELECT member_id, meeting_uuid, final_status FROM attendance WHERE member_id = ANY(%s) AND meeting_uuid = ANY(%s)', (heat_member_ids, heat_meeting_uuids))
                    for r in cur.fetchall(): status_map[(r.get('member_id'), r.get('meeting_uuid'))] = r.get('final_status') or 'NO_DATA'
        for mem in heat_members:
            row={'name':mem.get('name') or 'Member','cells':[]}
            for mt in heat_meetings: row['cells'].append(status_map.get((mem.get('id'), mt.get('meeting_uuid')), 'NO_DATA'))
            heat.append(row)
    except Exception as exc:
        print(f"AI heatmap skipped safely: {exc}"); heat=[]
    try:
        preds=generate_ai_level4_predictions(); recs=generate_ai_level4_recommendations()
    except Exception as exc:
        print(f"AI Level 4 section skipped safely: {exc}"); preds=[]; recs=[]
    high=len([p for p in preds if p.get('absence_probability',0)>=70]); med=len([p for p in preds if 45<=p.get('absence_probability',0)<70]); consistent=len([p for p in preds if p.get('behavior_tag')=='Consistent']); risky=len([p for p in preds if p.get('behavior_tag')=='Risky'])
    body=render_template_string("""
    <style>.ai-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:16px}.ai-card{background:rgba(15,23,42,.78);border:1px solid rgba(148,163,184,.18);border-radius:22px;padding:18px;box-shadow:0 18px 60px rgba(0,0,0,.28)}.ai-big{font-size:30px;font-weight:950}.ai-chat{display:grid;grid-template-columns:minmax(0,1fr) 390px;gap:18px}.ai-msg{white-space:pre-wrap;background:rgba(15,23,42,.85);border:1px solid rgba(148,163,184,.16);padding:12px;border-radius:16px;margin:10px 0}.ai-input{width:100%;border-radius:14px;border:1px solid rgba(99,102,241,.3);background:#020617;color:#e5e7eb;padding:13px}.ai-suggest{display:flex;flex-wrap:wrap;gap:8px;margin:10px 0}.ai-suggest button{border:0;border-radius:999px;padding:9px 12px;background:rgba(99,102,241,.2);color:#c7d2fe;font-weight:800}.risk-critical{color:#fecaca}.risk-warning{color:#fde68a}.risk-healthy{color:#bbf7d0}.heat{overflow:auto}.heat table{border-collapse:separate;border-spacing:4px;width:100%}.heat td,.heat th{font-size:12px;padding:8px;border-radius:8px;text-align:center}.h-PRESENT,.h-HOST{background:#166534;color:#dcfce7}.h-LATE{background:#92400e;color:#fef3c7}.h-ABSENT{background:#7f1d1d;color:#fee2e2}.h-NO_DATA{background:#334155;color:#cbd5e1}.l4-pill{display:inline-flex;border-radius:999px;padding:6px 10px;font-weight:900;font-size:12px}.l4-high{background:rgba(239,68,68,.18);color:#fecaca;border:1px solid rgba(239,68,68,.35)}.l4-med{background:rgba(245,158,11,.18);color:#fde68a;border:1px solid rgba(245,158,11,.35)}.l4-low{background:rgba(34,197,94,.14);color:#bbf7d0;border:1px solid rgba(34,197,94,.28)}.l4-actions{display:flex;gap:10px;flex-wrap:wrap;margin:14px 0}.l4-actions button,.l4-actions a{border:0;border-radius:12px;padding:11px 14px;font-weight:900;color:white;background:linear-gradient(90deg,#2563eb,#7c3aed);text-decoration:none}.l4-actions .danger{background:linear-gradient(90deg,#dc2626,#f97316)}@media(max-width:1100px){.ai-grid{grid-template-columns:1fr 1fr}.ai-chat{grid-template-columns:1fr}}@media(max-width:700px){.ai-grid{grid-template-columns:1fr}}
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
    <div class="hero"><div class="hero-grid"><div><div class="badge info">AI Intelligence Center</div><h1 class="hero-title">🧠 AI Intelligence + Level 4</h1><div class="hero-copy">Smart assistant, current-month member intelligence, risk heatmap, prediction engine, behavioral tags, auto-actions, and smart reports — merged into one dashboard.</div></div><div class="hero-stats"><div class="hero-chip"><div class="small">Health Score</div><div class="big">{{ latest_score }}/100</div></div><div class="hero-chip"><div class="small">Basis</div><div class="big" style="font-size:18px">{{ basis }}</div></div></div></div></div>
    <div class="ai-grid"><div class="ai-card"><div class="small">Critical Members</div><div class="ai-big risk-critical">{{ critical }}</div></div><div class="ai-card"><div class="small">Warning Members</div><div class="ai-big risk-warning">{{ warning }}</div></div><div class="ai-card"><div class="small">High Absence Risk</div><div class="ai-big risk-critical">{{ high }}</div></div><div class="ai-card"><div class="small">Latest Meeting Health</div><div class="ai-big">{{ latest_score }}/100</div></div></div>
    <div class="ai-chat" style="margin-top:18px"><div class="ai-card"><h2>🤖 Smart Assistant</h2><div id="aiGreeting" class="ai-msg">Analyzing your latest attendance data...</div><div class="ai-suggest"><button type="button" onclick="aiAsk('Who is at risk?')">At-risk members</button><button type="button" onclick="aiAsk('List all members below 50% attendance')">Below 50%</button><button type="button" onclick="aiAsk('Show top performers')">Top performers</button><button type="button" onclick="aiAsk('Why attendance dropped?')">Why dropped?</button><button type="button" onclick="aiAsk('Summarize last meeting')">Last meeting</button><button type="button" onclick="aiAsk('Show predictions')">Predictions</button><button type="button" onclick="aiAsk('Show behavioral tags')">Behavior tags</button><button type="button" onclick="aiAsk('Send reminder to them')">Remind them</button><button type="button" onclick="location.href='/ai-level4/report.pdf'">Smart PDF</button><button type="button" onclick="location.href='/ai-level4/report.csv'">Smart CSV</button></div><input id="aiLevel3Input" class="ai-input" placeholder="Ask attendance question..." onkeydown="if(event.key==='Enter'){aiAsk(this.value)}"><div style="margin-top:10px"><button type="button" onclick="aiAsk(document.getElementById('aiLevel3Input').value)">Ask AI</button></div><div id="aiLevel3Answer" class="ai-msg">Ready.</div></div><div class="ai-card"><h2>💡 Insights</h2>{% for i in insights %}<div class="ai-msg"><b>{{ i.title }}</b><br><span class="small">{{ i.category }} · {{ i.severity }}</span><br>{{ i.message }}<br><b>Recommendation:</b> {{ i.recommendation }}</div>{% endfor %}</div></div>
    <div class="ai-card" style="margin-top:18px"><h2>👤 Member Intelligence <span class="small">({{ basis }}, same formula as Attendance Register)</span></h2><div class="table-wrap"><table><thead><tr><th>Name</th><th>Total</th><th>P</th><th>L</th><th>A</th><th>U</th><th>Attendance %</th><th>Trend</th><th>Risk</th><th>Tag</th><th>Suggestion</th></tr></thead><tbody>{% for m in members %}<tr><td>{{ m.name }}</td><td>{{ m.total }}</td><td>{{ m.present }}</td><td>{{ m.late }}</td><td>{{ m.absent }}</td><td>{{ m.unknown or 0 }}</td><td>{{ m.attendance_pct }}%</td><td>{{ m.trend }}</td><td class="risk-{{ m.risk|lower }}">{{ m.risk }}</td><td>{{ m.tag }}</td><td>{{ m.suggestion }}</td></tr>{% endfor %}</tbody></table></div></div>
    <div class="ai-card" style="margin-top:18px"><h2>🔮 Prediction + Behavioral Intelligence</h2><div class="l4-actions"><button type="button" onclick="l4Run(false)">Preview Auto Actions</button><button type="button" class="danger" onclick="if(confirm('Send reminders to high-risk members?'))l4Run(true)">Execute Reminders</button><a href="/ai-level4/report.pdf">Smart PDF</a><a href="/ai-level4/report.csv">Smart CSV</a></div><div id="l4ActionResult" class="ai-msg">Ready.</div><div class="table-wrap"><table><thead><tr><th>Name</th><th>Attendance %</th><th>Absence Risk</th><th>Prediction</th><th>Behavior Tag</th><th>Recommendation</th></tr></thead><tbody>{% for p in preds %}<tr><td>{{ p.name }}</td><td>{{ p.attendance_pct }}%</td><td><span class="l4-pill {{ 'l4-high' if p.absence_probability >= 70 else 'l4-med' if p.absence_probability >= 45 else 'l4-low' }}">{{ p.absence_probability }}%</span></td><td>{{ p.prediction }}</td><td>{{ p.behavior_tag }}</td><td>{{ p.recommendation }}</td></tr>{% endfor %}</tbody></table></div></div>
    <div class="ai-card heat" style="margin-top:18px"><h2>🧠 Risk Heatmap</h2><table><thead><tr><th>Member</th>{% for mt in heat_meetings %}<th>{{ fmt_date(mt.start_time) }}</th>{% endfor %}</tr></thead><tbody>{% for row in heat %}<tr><th>{{ row.name }}</th>{% for c in row.cells %}<td class="h-{{ c }}">{{ 'P' if c in ['PRESENT','HOST'] else 'L' if c=='LATE' else 'A' if c=='ABSENT' else '-' }}</td>{% endfor %}</tr>{% endfor %}</tbody></table></div>
    <div class="ai-card" style="margin-top:18px"><h2>🔥 Smart Alert Panel</h2>{% if logs %}{% for l in logs %}<div class="ai-msg"><b>{{ l.title }}</b><br>{{ l.message }}<br><span class="small">{{ fmt_dt(l.created_at) }} · {{ l.current_state }}</span></div>{% endfor %}{% else %}<div class="muted">No smart alert logs yet.</div>{% endif %}</div>
    <script>function aiAsk(q){if(!q||!q.trim())return;const box=document.getElementById('aiLevel3Answer');box.innerText='Thinking...';fetch('/api/ai-assistant-level3',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({query:q})}).then(async r=>{let d=await r.json().catch(()=>({response:'AI response parse failed.'}));if(!r.ok){throw new Error(d.response||('HTTP '+r.status));}return d;}).then(d=>{box.innerText=d.response||'No answer found.';}).catch(err=>{box.innerText='AI assistant error: '+(err.message||err)+'. Please check Render logs if this repeats.';});}function l4Run(execute){const box=document.getElementById('l4ActionResult');box.innerText=execute?'Executing safe reminders...':'Checking preview...';fetch('/api/ai-level4/auto-actions',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({execute:execute,max_members:20})}).then(r=>r.json()).then(d=>{box.innerText=`Mode: ${d.mode}\nTargets: ${d.target_count}\nSent: ${d.sent||0}\nSkipped: ${d.skipped||0}\nFailed: ${(d.failed||[]).join(', ')||'None'}`;}).catch(()=>{box.innerText='Auto action failed. Check logs.';});}fetch('/api/ai-insights-level3').then(r=>r.json()).then(d=>{let ins=(d.insights||[]).slice(0,2).map(x=>'• '+x.message).join('\n');document.getElementById('aiGreeting').innerText=ins||'No critical insight right now.';}).catch(()=>{});</script>
    """, insights=insights, members=members, critical=critical, warning=warning, latest_score=latest_score, avg_duration=avg_duration, logs=logs, heat=heat, heat_meetings=heat_meetings, fmt_date=fmt_date, fmt_dt=fmt_dt, basis=basis, preds=preds, recs=recs, high=high, med=med, consistent=consistent, risky=risky)
    return page('AI Intelligence', body, 'ai_intelligence')

# =========================
# END UI_UPDATE_V10_AI_LEVEL3_SMART_ENGINE_APPLIED
# =========================

# UI_UPDATE_V11_AI_LEVEL4_CORE_APPLIED = True
# UI_UPDATE_V11_1_AI_MERGED_DASHBOARD_ASSISTANT_FIX_APPLIED = True
# AI LEVEL 4 CORE: Prediction + Behavioral Tagging + Auto Actions + Smart Reports
AI_LEVEL4_LOW_THRESHOLD = float(os.getenv("AI_LEVEL4_LOW_THRESHOLD", "75") or "75")
AI_LEVEL4_CRITICAL_THRESHOLD = float(os.getenv("AI_LEVEL4_CRITICAL_THRESHOLD", "50") or "50")


# ---- api_ai_level4_predictions ----
@app.route('/api/ai-level4/predictions')
@login_required
def api_ai_level4_predictions(): return jsonify({'predictions':generate_ai_level4_predictions(),'recommendations':generate_ai_level4_recommendations()})


# ---- api_ai_level4_auto_actions ----
@app.route('/api/ai-level4/auto-actions', methods=['POST'])
@login_required
@admin_required
def api_ai_level4_auto_actions():
    payload=request.get_json(silent=True) or {}; execute=str(payload.get('execute','false')).lower() in ('1','true','yes','on')
    return jsonify(run_ai_level4_auto_actions(execute=execute, max_members=int(payload.get('max_members',20) or 20)))


# ---- ai_level4_report_csv ----
@app.route('/ai-level4/report.csv')
@login_required
def ai_level4_report_csv():
    output=io.StringIO(); w=csv.writer(output); w.writerow(['AI Level 4 Smart Report']); w.writerow([]); w.writerow(['Recommendations']); w.writerow(['Severity','Title','Message','Action'])
    for r in generate_ai_level4_recommendations(): w.writerow([r.get('severity'),r.get('title'),r.get('message'),r.get('action')])
    w.writerow([]); w.writerow(['Member Predictions']); w.writerow(['Name','Attendance %','Absence Probability','Prediction','Behavior Tag','Recommendation'])
    for p in generate_ai_level4_predictions(): w.writerow([p.get('name'),p.get('attendance_pct'),p.get('absence_probability'),p.get('prediction'),p.get('behavior_tag'),p.get('recommendation')])
    return Response(output.getvalue(), mimetype='text/csv', headers={'Content-Disposition':'attachment; filename=ai_level4_smart_report.csv'})


# ---- ai_level4_report_pdf ----
@app.route('/ai-level4/report.pdf')
@login_required
def ai_level4_report_pdf():
    preds=generate_ai_level4_predictions(); recs=generate_ai_level4_recommendations(); buf=io.BytesIO(); doc=SimpleDocTemplate(buf,pagesize=letter); styles=getSampleStyleSheet(); story=[Paragraph('AI Level 4 Smart Report',styles['Title']),Spacer(1,12),Paragraph('Recommendations',styles['Heading2'])]
    t=Table([['Severity','Title','Action']]+[[r.get('severity'),r.get('title'),r.get('action')] for r in recs], repeatRows=1); t.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,0),colors.HexColor('#111827')),('TEXTCOLOR',(0,0),(-1,0),colors.white),('GRID',(0,0),(-1,-1),0.25,colors.grey),('FONTSIZE',(0,0),(-1,-1),8)])); story.append(t); story.append(Spacer(1,14)); story.append(Paragraph('Top Absence Risk Predictions',styles['Heading2']))
    t2=Table([['Name','Attendance %','Absence Risk','Tag','Prediction']]+[[p.get('name'),str(p.get('attendance_pct')),str(p.get('absence_probability')),p.get('behavior_tag'),p.get('prediction')] for p in preds[:25]], repeatRows=1); t2.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,0),colors.HexColor('#111827')),('TEXTCOLOR',(0,0),(-1,0),colors.white),('GRID',(0,0),(-1,-1),0.25,colors.grey),('FONTSIZE',(0,0),(-1,-1),7)])); story.append(t2); doc.build(story); buf.seek(0)
    return send_file(buf, as_attachment=True, download_name='ai_level4_smart_report.pdf', mimetype='application/pdf')

try: _ai_level3_original_bot_answer = _ai_bot_answer
except Exception: _ai_level3_original_bot_answer = None


# ---- ai_level4_dashboard ----
@app.route('/ai-level4')
@login_required
def ai_level4_dashboard():
    return redirect(url_for('ai_intelligence'))

# END UI_UPDATE_V11_AI_LEVEL4_CORE_APPLIED



# UI_UPDATE_V11_2_AI_ASSISTANT_COMMAND_ENGINE_FIX_APPLIED = True

# ---- AI Command Engine v11.2: broad offline attendance/project assistant ----
