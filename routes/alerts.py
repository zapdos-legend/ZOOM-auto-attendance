# Extracted from original app.py during real modular split.
# Do not run this file directly. It registers routes on the shared Flask app.
import sys
_legacy = sys.modules.get("app") or sys.modules.get("__main__")
if _legacy is None:
    raise RuntimeError("This module must be imported by app.py")
globals().update({name: getattr(_legacy, name) for name in dir(_legacy) if not name.startswith("__")})

# ---- api_alerts_run_now ----
@app.route("/api/alerts/run", methods=["POST", "GET"])
@login_required
def api_alerts_run_now():
    if session.get("role") != "admin":
        return jsonify({"ok": False, "error": "Admin access required"}), 403
    result = run_smart_scheduler(force=True)
    return jsonify({"ok": True, **result})

