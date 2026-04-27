# Extracted from original app.py during real modular split.
# Do not run this file directly. It registers routes on the shared Flask app.
import sys
_legacy = sys.modules.get("app") or sys.modules.get("__main__")
if _legacy is None:
    raise RuntimeError("This module must be imported by app.py")
globals().update({name: getattr(_legacy, name) for name in dir(_legacy) if not name.startswith("__")})

# ---- settings ----
@app.route("/settings", methods=["GET", "POST"])
@login_required
@admin_required
def settings():
    if request.method == "POST":
        for key in ["present_percentage", "late_count_as_present_percentage", "late_threshold_minutes", "meeting_finalize_seconds"]:
            set_setting(key, request.form.get(key, DEFAULT_SETTINGS[key]))
        log_activity("settings_update", "Attendance rules changed")
        flash("Settings updated.", "success")
        return redirect(url_for("settings"))

    settings_map = {k: get_setting(k, str) for k in DEFAULT_SETTINGS.keys()}

    body = render_template_string(
        """
        <div class="hero">
            <div class="hero-grid">
                <div>
                    <div class="badge info" style="margin-bottom:12px">System Controls</div>
                    <h1 class="hero-title">Attendance Settings & Reliability Controls</h1>
                    <div class="hero-copy">Tune thresholds, stale-meeting finalization, and attendance rules with a clearer production-safe settings experience.</div>
                </div>
                <div class="hero-stats">
                    <div class="hero-chip"><div class="small">Present %</div><div class="big">{{ s.present_percentage }}</div></div>
                    <div class="hero-chip"><div class="small">Late %</div><div class="big">{{ s.late_count_as_present_percentage }}</div></div>
                </div>
            </div>
        </div>
        <div class="stat-strip">
            <div class="compact-kpi"><div class="k">Finalize seconds</div><div class="v">{{ s.meeting_finalize_seconds }}</div></div>
            <div class="compact-kpi"><div class="k">Late threshold</div><div class="v">{{ s.late_threshold_minutes }}m</div></div>
            <div class="compact-kpi"><div class="k">Fallback cache</div><div class="v">Enabled</div></div>
        </div>
        <div class="two-col" style="margin-top:16px">
            <div class="card glass-panel">
                <div class="section-title"><div><h3 style="margin:0">Rule Configuration</h3><p>All values continue to use your existing settings table and logic.</p></div></div>
                <form method='post'>
                    <div class="setting-grid">
                        <div class="setting-tile"><label>Present Percentage</label><input name='present_percentage' value='{{ s.present_percentage }}'></div>
                        <div class="setting-tile"><label>Late Count As Present Percentage</label><input name='late_count_as_present_percentage' value='{{ s.late_count_as_present_percentage }}'></div>
                        <div class="setting-tile"><label>Late Threshold Minutes</label><input name='late_threshold_minutes' value='{{ s.late_threshold_minutes }}'></div>
                        <div class="setting-tile"><label>Meeting Finalize Seconds</label><input name='meeting_finalize_seconds' value='{{ s.meeting_finalize_seconds }}'></div>
                    </div>
                    <div class="mobile-actions" style="margin-top:14px"><button type='submit'>Save Settings</button></div>
                </form>
            </div>
            <div class="stack">
                <div class="card">
                    <h3 style="margin-top:0">Reliability Notes</h3>
                    <div class="mini-list">
                        <div class="mini-item"><div class="muted">Startup flow</div><div style="font-weight:900;margin-top:4px">Initialization now fails gracefully instead of crashing the whole app.</div></div>
                        <div class="mini-item"><div class="muted">Settings cache</div><div style="font-weight:900;margin-top:4px">Cached defaults keep the app usable even during temporary DB issues.</div></div>
                        <div class="mini-item"><div class="muted">Legacy rows</div><div style="font-weight:900;margin-top:4px">Existing data stays compatible with old and new database values.</div></div>
                    </div>
                </div>
            </div>
        </div>
        """,
        s=settings_map,
    )
    return page("Settings", body, "settings")



# ---- appearance ----
@app.route("/appearance")
@login_required
def appearance():
    themes = [("default-saas-dark","Default SaaS Dark","Premium dark dashboard with blue-purple SaaS glow.","#0b1020","#6366f1","#22d3ee"),("notion-clean","Notion Clean","Clean white workspace style for focused admin work.","#f7f6f3","#111827","#64748b"),("stripe-glow","Stripe Glow","High-end product dashboard style with gradient glow.","#070b1a","#635bff","#00d4ff"),("vercel-minimal","Vercel Minimal","Black and white minimal engineering console.","#000000","#ffffff","#737373"),("netflix-dark","Netflix Dark","Deep cinematic dark mode with red highlights.","#080808","#e50914","#f97316"),("college-formal","College Formal","Formal cream and navy palette for academic presentations.","#f3efe4","#1e3a8a","#92400e"),("purple-neon","Purple Neon","Futuristic neon purple interface for live dashboards.","#070014","#a855f7","#ec4899"),("light-professional","Light Professional","Modern light business dashboard with clean blue accents.","#eef2f7","#2563eb","#0ea5e9")]
    body = render_template_string("""
        <div class="hero"><div class="hero-grid"><div><div class="badge info" style="margin-bottom:12px">Appearance Engine</div><h1 class="hero-title">🎨 Appearance Studio</h1><div class="hero-copy">One-click full system theme switching with premium skeleton loading, animation control, glow effects, and Chart.js theme sync.</div></div><div class="hero-stats"><div class="hero-chip"><div class="small">Themes</div><div class="big">8</div></div><div class="hero-chip"><div class="small">Storage</div><div class="big">Local</div></div></div></div></div>
        <div class="appearance-controls"><div class="appearance-control"><label>Animation Level</label><select id="animationLevelSelect"><option value="off">Off</option><option value="minimal">Minimal</option><option value="smooth">Smooth</option><option value="full">Full</option></select><div class="muted" style="margin-top:8px">Saved in browser using localStorage. Affects transitions, hover motion, and loading polish.</div></div><div class="appearance-control"><label>Premium Skeleton Preview</label><div class="premium-skeleton-grid" style="margin-top:10px"><div class="premium-skeleton premium-skeleton-card"></div><div><div class="premium-skeleton premium-skeleton-line long"></div><div class="premium-skeleton premium-skeleton-line medium"></div><div class="premium-skeleton premium-skeleton-line short"></div></div></div></div></div>
        <div class="appearance-studio-grid">{% for key, name, desc, p1, p2, p3 in themes %}<div class="appearance-card" data-theme-apply="{{ key }}" style="--p1:{{ p1 }};--p2:{{ p2 }};--p3:{{ p3 }}"><div class="preview-band"></div><h3>{{ name }}</h3><p>{{ desc }}</p><div class="row" style="margin-top:14px"><span class="badge info">Click to Apply</span></div></div>{% endfor %}</div>
        <script>document.addEventListener('DOMContentLoaded',function(){if(window.setupAppearanceEngineV8){window.setupAppearanceEngineV8();}});</script>
    """, themes=themes)
    return page("Appearance Studio", body, "appearance")


# =========================
# UI_UPDATE_V10_AI_LEVEL3_SMART_ENGINE_APPLIED = True
# UI_UPDATE_V10_1_AI_LEVEL3_PERFORMANCE_FIX_APPLIED = True
# =========================

AI_LEVEL3_LOW_ATTENDANCE_DEFAULT = 50.0

