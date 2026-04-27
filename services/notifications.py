# Extracted from original app.py during real modular split.
# Do not run this file directly. It registers routes on the shared Flask app.
import sys
_legacy = sys.modules.get("app") or sys.modules.get("__main__")
if _legacy is None:
    raise RuntimeError("This module must be imported by app.py")
globals().update({name: getattr(_legacy, name) for name in dir(_legacy) if not name.startswith("__")})

# ---- push_vapid_key ----
@app.route("/push/vapid-key")
@login_required
def push_vapid_key():
    if not is_web_push_configured():
        return jsonify({"ok": False, "error": "Web Push not configured"}), 503
    return jsonify({"ok": True, "publicKey": VAPID_PUBLIC_KEY})



# ---- push_subscribe ----
@app.route("/push/subscribe", methods=["POST"])
@login_required
def push_subscribe():
    if not is_web_push_configured():
        return jsonify({"ok": False, "error": "Web Push not configured"}), 503

    data = request.get_json(silent=True) or {}
    ok, message = save_push_subscription(data, session.get("username"))
    if ok:
        log_activity("push_subscription_saved", session.get("username") or "anonymous")
        return jsonify({"ok": True, "message": message})
    return jsonify({"ok": False, "error": message}), 400



# ---- service_worker_js ----
@app.route("/service-worker.js")
def service_worker_js():
    js = """
self.addEventListener('push', function(event) {
  let data = {};
  try {
    data = event.data ? event.data.json() : {};
  } catch (e) {
    data = { title: 'Notification', body: event.data ? event.data.text() : '' };
  }

  const title = data.title || 'Zoom Attendance Platform';
  const options = {
    body: data.body || '',
    icon: data.icon || '/static/icon.png',
    badge: data.badge || '/static/icon.png',
    data: { url: data.url || '/' }
  };

  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener('notificationclick', function(event) {
  event.notification.close();
  const targetUrl = (event.notification.data && event.notification.data.url) || '/';
  event.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true }).then(function(clientList) {
      for (const client of clientList) {
        if ('focus' in client) {
          client.navigate(targetUrl);
          return client.focus();
        }
      }
      if (clients.openWindow) {
        return clients.openWindow(targetUrl);
      }
    })
  );
});
"""
    return Response(js, mimetype="application/javascript")



# ---- push_setup ----
@app.route("/push-setup")
@login_required
def push_setup():
    html = f"""
    <!doctype html>
    <html>
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>Web Push Setup</title>
        {DARK_THEME_CSS}
        <style>
            .push-wrap {{ max-width: 760px; margin: 40px auto; padding: 24px; }}
            .push-card {{ background: rgba(255,255,255,0.05); border: 1px solid rgba(255,255,255,0.12); border-radius: 18px; padding: 24px; box-shadow: 0 8px 30px rgba(0,0,0,0.35); }}
            .push-muted {{ color: #9ca3af; }}
            .push-row {{ display: flex; gap: 12px; flex-wrap: wrap; margin-top: 18px; }}
            .push-btn {{ cursor: pointer; }}
            .push-status {{ margin-top: 16px; padding: 12px 14px; border-radius: 12px; background: rgba(255,255,255,0.04); white-space: pre-wrap; }}
            a.push-link {{ color: #c4b5fd; text-decoration: none; }}
        
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
    </head>
    <body>
        <div class="push-wrap">
            <div class="push-card">
                <h1 style="margin-top:0;">🔔 Browser Push Setup</h1>
                <p class="push-muted">Enable browser notifications for your account. This safely stores your browser subscription in the database for future smart alerts.</p>
                <div class="push-row">
                    <button class="push-btn" onclick="enablePush()">Enable Notifications</button>
                    <button class="push-btn" onclick="sendTestPush()">Send Test Push</button>
                    <a class="push-link" href="{url_for('home')}">← Back to Dashboard</a>
                </div>
                <div id="pushStatus" class="push-status">Status: Ready</div>
            </div>
        </div>
        <script>
        function urlBase64ToUint8Array(base64String) {{
            const padding = '='.repeat((4 - base64String.length % 4) % 4);
            const base64 = (base64String + padding).replace(/-/g, '+').replace(/_/g, '/');
            const rawData = atob(base64);
            return Uint8Array.from([...rawData].map(c => c.charCodeAt(0)));
        }}

        function setStatus(message) {{
            document.getElementById('pushStatus').textContent = message;
        }}

        async function enablePush() {{
            try {{
                if (!('serviceWorker' in navigator)) {{
                    setStatus('Service Worker is not supported in this browser.');
                    return;
                }}
                if (!('PushManager' in window)) {{
                    setStatus('Push notifications are not supported in this browser.');
                    return;
                }}

                const permission = await Notification.requestPermission();
                if (permission !== 'granted') {{
                    setStatus('Notification permission was not granted.');
                    return;
                }}

                const vapidResp = await fetch('{url_for('push_vapid_key')}');
                const vapidData = await vapidResp.json();
                if (!vapidData.ok) {{
                    setStatus('Unable to load VAPID key: ' + (vapidData.error || 'Unknown error'));
                    return;
                }}

                const registration = await navigator.serviceWorker.register('{url_for('service_worker_js')}');
                let subscription = await registration.pushManager.getSubscription();
                if (!subscription) {{
                    subscription = await registration.pushManager.subscribe({{
                        userVisibleOnly: true,
                        applicationServerKey: urlBase64ToUint8Array(vapidData.publicKey)
                    }});
                }}

                const saveResp = await fetch('{url_for('push_subscribe')}', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify(subscription)
                }});
                const saveData = await saveResp.json();
                if (saveData.ok) {{
                    setStatus('Notifications enabled successfully for this browser.');
                }} else {{
                    setStatus('Subscription save failed: ' + (saveData.error || 'Unknown error'));
                }}
            }} catch (err) {{
                setStatus('Push setup failed: ' + err);
            }}
        }}

        async function sendTestPush() {{
            try {{
                const resp = await fetch('{url_for('test_push')}');
                const data = await resp.json();
                setStatus('Test push result: ' + JSON.stringify(data));
            }} catch (err) {{
                setStatus('Test push failed: ' + err);
            }}
        }}
        </script>
    </body>
    </html>
    """
    return render_template_string(html)



# ---- test_push ----
@app.route("/test-push")
@login_required
def test_push():
    results = send_push_notification(
        title="Test Push from Zoom Attendance Platform",
        body="Browser push setup is working successfully.",
        target_username=session.get("username"),
        click_url=url_for("home", _external=True),
    )
    if results.get("sent", 0) > 0:
        log_activity("test_push_sent", session.get("username") or "unknown")
    return jsonify({"ok": results.get("sent", 0) > 0, **results})



# ---- test_email ----
@app.route("/test-email")
def test_email():
    target_email = (request.args.get("to") or "").strip()
    if not target_email:
        return "Please pass email like /test-email?to=yourgmail@gmail.com", 400

    ok, message = send_email(
        to_email=target_email,
        subject="Test Email from Zoom Attendance Platform",
        body="Hello,\n\nYour Gmail SMTP setup is working successfully.\n\nRegards,\nZoom Attendance Platform"
    )

    if ok:
        return f"✅ {message} -> {target_email}"
    return f"❌ {message}", 500




# ---- notification_control ----
@app.route("/notification-control", methods=["GET", "POST"])
@login_required
def notification_control():
    result_message = None
    result_type = "ok"
    if request.method == "POST":
        action = request.form.get("action", "save")
        if action == "save":
            save_notification_settings(request.form)
            log_activity("notification_settings_saved", session.get("username") or "unknown")
            result_message = "Notification settings saved successfully."
        elif action == "test_email":
            target = (request.form.get("test_email_to") or get_notification_settings().get("test_email_to") or SMART_ALERT_EMAIL_TO).strip()
            if target:
                ok, msg = send_email(target, "Test Email from Zoom Attendance Platform", "Your Notification Control Center email test is working successfully.", "<h2>Notification Control Center</h2><p>Your email test is working successfully.</p>")
                result_message = ("Test email sent to " + target) if ok else ("Test email failed: " + str(msg))
                result_type = "ok" if ok else "danger"
            else:
                result_message = "Please enter a test email address first."
                result_type = "danger"
        elif action == "test_push":
            push_result = send_push_notification("Test Push from Zoom Attendance Platform", "Your Notification Control Center push test is working successfully.", target_username=session.get("username"), click_url=url_for("notification_control", _external=True))
            result_message = f"Push test result: sent={push_result.get('sent', 0)}, failed={push_result.get('failed', 0)}"
            result_type = "ok" if push_result.get("sent", 0) > 0 else "danger"
    settings_data = get_notification_settings()
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT alert_type, entity_type, entity_id, previous_state, current_state, title, message, email_sent, push_sent, created_at
                FROM smart_alert_logs
                ORDER BY created_at DESC
                LIMIT 80
            """)
            logs = cur.fetchall()
    body = render_template_string("""
        <style>
        .notif-shell{display:grid;grid-template-columns:minmax(0,1fr) 420px;gap:18px;align-items:start}.notif-card{background:linear-gradient(145deg,rgba(15,23,42,.96),rgba(2,6,23,.98));border:1px solid rgba(99,102,241,.28);border-radius:24px;padding:22px;box-shadow:0 24px 70px rgba(0,0,0,.42)}.notif-title{display:flex;justify-content:space-between;gap:12px;align-items:center;margin-bottom:18px}.notif-title h2{margin:0;font-size:24px}.notif-title p{margin:5px 0 0;color:#94a3b8}.notif-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}.notif-box{background:rgba(15,23,42,.9);border:1px solid rgba(148,163,184,.18);border-radius:18px;padding:16px}.notif-box h3{margin:0 0 12px;font-size:16px}.toggle-row,.check-row{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:10px 0;border-bottom:1px solid rgba(148,163,184,.10)}.toggle-row:last-child,.check-row:last-child{border-bottom:0}.notif-input,.notif-textarea{width:100%;border-radius:12px;border:1px solid rgba(96,165,250,.28);background:#08111f;color:#e5e7eb;padding:11px 12px}.notif-textarea{min-height:120px;resize:vertical}.switch{position:relative;width:52px;height:28px}.switch input{display:none}.slider{position:absolute;inset:0;background:#334155;border-radius:999px;cursor:pointer;transition:.2s}.slider:before{content:"";position:absolute;width:22px;height:22px;left:3px;top:3px;background:white;border-radius:50%;transition:.2s}.switch input:checked + .slider{background:linear-gradient(90deg,#2563eb,#7c3aed)}.switch input:checked + .slider:before{transform:translateX(24px)}.notif-actions{display:flex;gap:10px;flex-wrap:wrap;margin-top:16px}.notif-actions button{border:0;border-radius:12px;padding:11px 14px;font-weight:900;color:white;background:linear-gradient(90deg,#2563eb,#7c3aed)}.notif-actions .secondary{background:#1e293b}.notif-actions .success{background:#16a34a}.notif-log{max-height:620px;overflow:auto}.log-item{border-bottom:1px solid rgba(148,163,184,.12);padding:12px 0}.log-title{font-weight:950;color:#f8fafc}.log-meta{font-size:12px;color:#94a3b8;margin-top:4px}.log-msg{font-size:13px;color:#cbd5e1;margin-top:6px;line-height:1.45}.pill-ok{background:rgba(34,197,94,.14);color:#86efac;border:1px solid rgba(34,197,94,.28);padding:5px 8px;border-radius:999px;font-size:12px;font-weight:900}@media(max-width:1100px){.notif-shell{grid-template-columns:1fr}.notif-grid{grid-template-columns:1fr}}
        
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
        <div class="hero"><div class="hero-grid"><div><div class="badge">Notification Control Center</div><h1 class="hero-title">Smart alert delivery controls</h1><div class="hero-copy">Enable or disable Email/Push, select alert types, customize messages, test delivery, and review alert logs.</div></div><div class="hero-stats"><div class="hero-chip"><div class="small">Email</div><div class="big">{{ 'ON' if settings.email_enabled else 'OFF' }}</div></div><div class="hero-chip"><div class="small">Push</div><div class="big">{{ 'ON' if settings.push_enabled else 'OFF' }}</div></div></div></div></div>
        {% if result_message %}<div class="card" style="margin-bottom:16px">{{ result_message }}</div>{% endif %}
        <div class="notif-shell"><form method="post" class="notif-card"><div class="notif-title"><div><h2>Controls</h2><p>Connected with your existing smart alert system.</p></div><span class="pill-ok">No spam: state-change only</span></div><div class="notif-grid"><div class="notif-box"><h3>Delivery Channels</h3><label class="toggle-row"><span>Email alerts</span><span class="switch"><input type="checkbox" name="email_enabled" {% if settings.email_enabled %}checked{% endif %}><span class="slider"></span></span></label><label class="toggle-row"><span>Push alerts</span><span class="switch"><input type="checkbox" name="push_enabled" {% if settings.push_enabled %}checked{% endif %}><span class="slider"></span></span></label><div style="margin-top:12px"><label class="small">Test email receiver</label><input class="notif-input" name="test_email_to" value="{{ settings.test_email_to }}" placeholder="your@email.com"></div></div><div class="notif-box"><h3>Alert Types</h3>{% for key,label in alert_labels.items() %}<label class="check-row"><span>{{ label }}</span><input type="checkbox" name="alert_types" value="{{ key }}" {% if key in settings.alert_types %}checked{% endif %}></label>{% endfor %}</div><div class="notif-box"><h3>Timing Control</h3>{% for key,label in [('before','Before meeting'),('during','During meeting'),('after','After meeting')] %}<label class="check-row"><span>{{ label }}</span><input type="checkbox" name="timings" value="{{ key }}" {% if key in settings.timings %}checked{% endif %}></label>{% endfor %}</div><div class="notif-box"><h3>Message Template</h3><textarea class="notif-textarea" name="message_template">{{ settings.message_template }}</textarea><div class="muted" style="font-size:12px;margin-top:8px">Available: {title}, {message}, {state}, {alert_type}, {member_name}, {meeting_topic}</div></div></div><div class="notif-actions"><button type="submit" name="action" value="save">Save Controls</button><button type="submit" class="success" name="action" value="test_email">Test Email</button><button type="submit" class="secondary" name="action" value="test_push">Test Push</button></div></form><div class="notif-card notif-log"><div class="notif-title"><div><h2>Alert Logs</h2><p>Latest smart alert state-change records.</p></div></div>{% if logs %}{% for log in logs %}<div class="log-item"><div class="log-title">{{ log.title }}</div><div class="log-meta">{{ fmt_dt(log.created_at) }} · {{ log.alert_type }} · {{ log.previous_state or '-' }} → {{ log.current_state }} · Email {{ '✓' if log.email_sent else '×' }} · Push {{ log.push_sent }}</div><div class="log-msg">{{ log.message }}</div></div>{% endfor %}{% else %}<div class="muted">No alert logs yet.</div>{% endif %}</div></div>
    """, settings=settings_data, alert_labels=NOTIFICATION_ALERT_TYPE_LABELS, logs=logs, result_message=result_message, result_type=result_type, fmt_dt=fmt_dt)
    return page("Notification Control", body, "notification_control")

