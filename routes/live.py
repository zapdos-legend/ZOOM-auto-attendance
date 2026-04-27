# Extracted from original app.py during real modular split.
# Do not run this file directly. It registers routes on the shared Flask app.
import sys
_legacy = sys.modules.get("app") or sys.modules.get("__main__")
if _legacy is None:
    raise RuntimeError("This module must be imported by app.py")
globals().update({name: getattr(_legacy, name) for name in dir(_legacy) if not name.startswith("__")})

# ---- api_live_snapshot ----
@app.route("/api/live-snapshot")
@login_required
def api_live_snapshot():
    return jsonify(build_live_snapshot_payload(include_feed=True))



# ---- api_live_summary ----
@app.route("/api/live-summary")
@login_required
def api_live_summary():
    payload = build_live_snapshot_payload(include_feed=False)
    return jsonify({
        "ok": payload.get("ok"),
        "has_live": payload.get("has_live"),
        "server_now": payload.get("server_now"),
        "meeting": payload.get("meeting"),
        "summary": payload.get("summary"),
    })



# ---- api_live_feed ----
@app.route("/api/live-feed")
@login_required
def api_live_feed():
    payload = build_live_snapshot_payload(include_feed=True)
    return jsonify({
        "ok": payload.get("ok"),
        "has_live": payload.get("has_live"),
        "server_now": payload.get("server_now"),
        "feed": payload.get("feed", []),
    })



# ---- live ----
@app.route("/live")
@login_required
def live():
    initial_payload = build_live_snapshot_payload(include_feed=True)
    body = render_template_string(
        """
        <style>
            .live-fix-hero{border:1px solid rgba(99,102,241,.25);background:linear-gradient(135deg,rgba(15,23,42,.92),rgba(30,41,59,.78));border-radius:26px;padding:22px;box-shadow:0 24px 70px rgba(0,0,0,.35)}
            .live-fix-top{display:flex;justify-content:space-between;gap:16px;align-items:flex-start;flex-wrap:wrap}
            .live-fix-badge{display:inline-flex;align-items:center;gap:9px;border-radius:999px;padding:8px 13px;background:rgba(239,68,68,.14);border:1px solid rgba(239,68,68,.34);color:#fecaca;font-size:12px;font-weight:900;letter-spacing:.08em;text-transform:uppercase}
            .live-fix-dot{width:10px;height:10px;border-radius:999px;background:#ef4444;box-shadow:0 0 0 rgba(239,68,68,.7);animation:liveFixPulse 1.2s infinite}@keyframes liveFixPulse{0%{box-shadow:0 0 0 0 rgba(239,68,68,.7)}70%{box-shadow:0 0 0 12px rgba(239,68,68,0)}100%{box-shadow:0 0 0 0 rgba(239,68,68,0)}}
            .live-fix-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:14px;margin-top:16px}.live-fix-stat{border-radius:20px;border:1px solid rgba(148,163,184,.18);background:rgba(255,255,255,.055);padding:16px}.live-fix-label{font-size:12px;text-transform:uppercase;letter-spacing:.07em;color:#94a3b8;font-weight:900}.live-fix-value{font-size:30px;font-weight:950;margin-top:7px}.live-fix-table td,.live-fix-table th{vertical-align:middle}.live-fix-duration{font-variant-numeric:tabular-nums;font-weight:900}.live-fix-left{opacity:.62}.live-fix-empty{text-align:center;padding:36px 16px}.live-fix-conn{font-size:12px;font-weight:900;border-radius:999px;padding:8px 12px;border:1px solid rgba(148,163,184,.24)}.live-fix-conn.ok{color:#86efac;border-color:rgba(34,197,94,.35)}.live-fix-conn.bad{color:#fecaca;border-color:rgba(239,68,68,.35)} .live-fix-badge.is-live{background:rgba(34,197,94,.16);border-color:rgba(34,197,94,.42);color:#bbf7d0}.live-fix-badge.is-live .live-fix-dot{background:#22c55e;animation:liveFixPulseGreen 1.2s infinite}@keyframes liveFixPulseGreen{0%{box-shadow:0 0 0 0 rgba(34,197,94,.7)}70%{box-shadow:0 0 0 12px rgba(34,197,94,0)}100%{box-shadow:0 0 0 0 rgba(34,197,94,0)}}.live-nav-live{background:linear-gradient(135deg,#16a34a,#22c55e)!important;box-shadow:0 12px 30px rgba(34,197,94,.35)!important}.live-nav-idle{background:linear-gradient(135deg,#dc2626,#ef4444)!important;box-shadow:0 12px 30px rgba(239,68,68,.35)!important}
        
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

        <div class="live-fix-hero">
            <div class="live-fix-top">
                <div>
                    <div class="live-fix-badge {{ 'is-live' if data.has_live else '' }}" id="lfBadgeWrap"><span class="live-fix-dot"></span><span id="lfBadge">{{ 'LIVE MEETING RUNNING' if data.has_live else 'LIVE DASHBOARD IDLE' }}</span></div>
                    <h1 class="hero-title" id="lfTopic" style="margin-top:14px">{{ data.meeting.topic if data.has_live else 'Waiting for Zoom meeting' }}</h1>
                    <div class="hero-copy" id="lfCopy">This page now renders live attendance from server immediately and then refreshes every 2 seconds.</div>
                    <div class="row" id="lfMetaRow" style="margin-top:14px;gap:10px;flex-wrap:wrap;display:{{ 'flex' if data.has_live else 'none' }}">
                        <span class="badge info" id="lfMeetingId">Meeting ID {{ data.meeting.id if data.has_live else '-' }}</span>
                        <span class="badge gray" id="lfStarted">Started {{ data.meeting.start_time if data.has_live else '-' }}</span>
                        <span class="badge gray" id="lfDuration">Duration {{ fmt_seconds(data.summary.meeting_duration_seconds) }}</span>
                    </div>
                </div>
                <div class="live-fix-conn ok" id="lfConn">● Server rendered</div>
            </div>
            <div class="live-fix-grid">
                <div class="live-fix-stat"><div class="live-fix-label">Live Participants</div><div class="live-fix-value" id="lfActive">{{ data.summary.active_now }}</div></div>
                <div class="live-fix-stat"><div class="live-fix-label">Members Live</div><div class="live-fix-value" id="lfKnown">{{ data.summary.known_count }}</div></div>
                <div class="live-fix-stat"><div class="live-fix-label">Unknown Live</div><div class="live-fix-value" id="lfUnknown">{{ data.summary.unknown_count }}</div></div>
                <div class="live-fix-stat"><div class="live-fix-label">Host</div><div class="live-fix-value" id="lfHost">{{ 'Present' if data.summary.host_present else 'Absent' }}</div></div>
                <div class="live-fix-stat"><div class="live-fix-label">Not Joined</div><div class="live-fix-value" id="lfNotJoined">{{ data.summary.not_joined_count }}</div></div>
            </div>
        </div>

        <div class="grid-2" style="margin-top:16px;grid-template-columns:minmax(0,1.45fr) minmax(320px,.55fr)">
            <div class="card">
                <div class="section-title"><div><h3 style="margin:0">Live Attendance</h3><p>Sorted by duration. Status changes to LIVE while inside meeting and LEFT after leaving.</p></div><span class="badge ok">Realtime</span></div>
                <div id="lfEmpty" class="live-fix-empty" style="display:{{ 'none' if data.has_live and data.participants else 'block' }}">
                    <div class="empty-icon">📡</div><h3 style="margin:0 0 8px 0">No live participant rows yet</h3><div class="muted">Webhook meeting start may be received before participant join. This will auto-update.</div>
                </div>
                <div class="table-wrap" id="lfTableWrap" style="display:{{ 'block' if data.has_live and data.participants else 'none' }}">
                    <table class="live-fix-table">
                        <thead><tr><th>Name</th><th>Category</th><th>Join</th><th>Leave</th><th>Duration</th><th>Rejoins</th><th>Status</th></tr></thead>
                        <tbody id="lfRows">
                            {% for p in data.participants %}
                            <tr class="{{ '' if p.is_active else 'live-fix-left' }}">
                                <td><b>{{ p.name }}</b>{% if p.is_host %} <span class="badge info">HOST</span>{% endif %}</td>
                                <td><span class="badge {{ 'info' if p.type == 'HOST' else ('ok' if p.type == 'MEMBER' else 'warn') }}">{{ p.type }}</span></td>
                                <td>{{ p.first_join }}</td>
                                <td>{{ p.last_leave }}</td>
                                <td><span class="live-fix-duration" data-base="{{ p.stored_seconds if p.is_active else p.duration_seconds }}" data-active="{{ 1 if p.is_active else 0 }}" data-current-join-ms="{{ p.current_join_epoch_ms if p.is_active else 0 }}">{{ fmt_seconds(p.duration_seconds) }}</span></td>
                                <td>{{ p.rejoins }}</td>
                                <td><span class="badge {{ 'ok' if p.status == 'LIVE' else 'gray' }}">{{ p.status }}</span></td>
                            </tr>
                            {% endfor %}
                        </tbody>
                    </table>
                </div>
            </div>
            <div class="stack">
                <div class="card"><div class="section-title"><div><h3 style="margin:0">Join / Leave Feed</h3><p>Latest activity from current session.</p></div></div><div class="list-card" id="lfFeed"></div></div>
                <div class="card"><div class="section-title"><div><h3 style="margin:0">Members Not Yet Joined</h3><p>Active registered members missing from live session.</p></div></div><div class="list-card" id="lfMissing"></div></div>
            </div>
        </div>

        <script>
        (function(){
            let lastPayload = {{ data|tojson }};
            let tickBase = Date.now();
            function esc(v){return String(v ?? '').replace(/[&<>\"']/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',"'":'&#39;'}[c];});}
            function fmt(sec){sec=Math.max(0,parseInt(sec||0,10));let h=String(Math.floor(sec/3600)).padStart(2,'0'),m=String(Math.floor((sec%3600)/60)).padStart(2,'0'),s=String(sec%60).padStart(2,'0');return h+':'+m+':'+s;}
            function cls(type){return type==='HOST'?'info':(type==='MEMBER'?'ok':'warn');}
            function render(data){
                lastPayload=data; tickBase=Date.now();
                document.getElementById('lfBadge').textContent=data.has_live?'LIVE MEETING RUNNING':'NO LIVE MEETING';
                document.getElementById('lfBadgeWrap').classList.toggle('is-live', !!data.has_live);
                document.getElementById('lfMetaRow').style.display=data.has_live?'flex':'none';
                const liveNav=[...document.querySelectorAll('.sidebar a')].find(a=>a.getAttribute('href')&&a.getAttribute('href').includes('/live'));
                if(liveNav){liveNav.classList.toggle('live-nav-live',!!data.has_live); liveNav.classList.toggle('live-nav-idle',!data.has_live);}
                document.getElementById('lfTopic').textContent=data.has_live?(data.meeting.topic||'Untitled Meeting'):'Waiting for Zoom meeting';
                document.getElementById('lfMeetingId').textContent='Meeting ID '+(data.has_live?(data.meeting.id||'-'):'-');
                document.getElementById('lfStarted').textContent='Started '+(data.has_live?(data.meeting.start_time||'-'):'-');
                document.getElementById('lfDuration').textContent='Duration '+fmt((data.summary||{}).meeting_duration_seconds||0);
                document.getElementById('lfActive').textContent=(data.summary||{}).active_now||0;
                document.getElementById('lfKnown').textContent=(data.summary||{}).known_count||0;
                document.getElementById('lfUnknown').textContent=(data.summary||{}).unknown_count||0;
                document.getElementById('lfHost').textContent=(data.summary||{}).host_present?'Present':'Absent';
                document.getElementById('lfNotJoined').textContent=(data.summary||{}).not_joined_count||0;
                const rows=data.participants||[];
                document.getElementById('lfEmpty').style.display=rows.length?'none':'block';
                document.getElementById('lfTableWrap').style.display=rows.length?'block':'none';
                document.getElementById('lfRows').innerHTML=rows.map(p=>`<tr class="${p.is_active?'':'live-fix-left'}"><td><b>${esc(p.name)}</b>${p.is_host?' <span class="badge info">HOST</span>':''}</td><td><span class="badge ${cls(p.type)}">${esc(p.type)}</span></td><td>${esc(p.first_join)}</td><td>${esc(p.last_leave)}</td><td><span class="live-fix-duration" data-base="${parseInt((p.is_active?p.stored_seconds:p.duration_seconds)||0,10)}" data-active="${p.is_active?1:0}" data-current-join-ms="${p.is_active?parseInt(p.current_join_epoch_ms||0,10):0}">${fmt(p.duration_seconds)}</span></td><td>${esc(p.rejoins)}</td><td><span class="badge ${p.status==='LIVE'?'ok':'gray'}">${esc(p.status)}</span></td></tr>`).join('');
                document.getElementById('lfFeed').innerHTML=(data.feed||[]).length?(data.feed||[]).map(i=>`<div class="list-row"><div><div style="font-weight:900">${esc(i.name)}</div><div class="muted">${esc(i.label)} · ${esc(i.time)}</div></div><span class="badge ${i.kind==='join'?'ok':'gray'}">${esc(i.tag)}</span></div>`).join(''):'<div class="muted">No join/leave events yet.</div>';
                document.getElementById('lfMissing').innerHTML=(data.not_joined||[]).length?(data.not_joined||[]).map(m=>`<div class="list-row"><div><div style="font-weight:900">${esc(m.name)}</div><div class="muted">${esc(m.contact)}</div></div><span class="badge danger">Not joined</span></div>`).join(''):'<div class="muted">No pending registered member.</div>';
            }
            async function poll(){
                try{let r=await fetch('{{ url_for("api_live_snapshot") }}?t='+Date.now(),{cache:'no-store',credentials:'same-origin'});let d=await r.json();document.getElementById('lfConn').className='live-fix-conn ok';document.getElementById('lfConn').textContent='● Updated '+new Date().toLocaleTimeString();render(d);}catch(e){document.getElementById('lfConn').className='live-fix-conn bad';document.getElementById('lfConn').textContent='● Poll retrying';}
                setTimeout(poll,2000);
            }
            setInterval(function(){
                const nowMs=Date.now();
                document.querySelectorAll('.live-fix-duration').forEach(function(el){
                    let base=parseInt(el.getAttribute('data-base')||'0',10);
                    let active=el.getAttribute('data-active')==='1';
                    let joinMs=parseInt(el.getAttribute('data-current-join-ms')||'0',10);
                    let extra=(active&&joinMs>0)?Math.max(0,Math.floor((nowMs-joinMs)/1000)):0;
                    el.textContent=fmt(base+extra);
                });
                if(lastPayload && lastPayload.meeting && lastPayload.meeting.start_iso){
                    let startMs=Date.parse(lastPayload.meeting.start_iso);
                    let sec=isNaN(startMs)?((lastPayload.summary||{}).meeting_duration_seconds||0):Math.max(0,Math.floor((nowMs-startMs)/1000));
                    document.getElementById('lfDuration').textContent='Duration '+fmt(sec);
                }
            },1000);
            render(lastPayload); poll();
        })();
        </script>
        """,
        data=initial_payload,
        fmt_seconds=lambda sec: f"{max(int(sec or 0),0)//3600:02d}:{(max(int(sec or 0),0)%3600)//60:02d}:{max(int(sec or 0),0)%60:02d}",
    )
    return page("Live", body, "live")


