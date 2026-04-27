# Extracted from original app.py during real modular split.
# Do not run this file directly. It registers routes on the shared Flask app.
import sys
_legacy = sys.modules.get("app") or sys.modules.get("__main__")
if _legacy is None:
    raise RuntimeError("This module must be imported by app.py")
globals().update({name: getattr(_legacy, name) for name in dir(_legacy) if not name.startswith("__")})

# ---- favicon ----
@app.route("/favicon.ico")
def favicon():
    return Response(status=204)



# ---- health ----
@app.route("/health")
def health():
    maybe_finalize_stale_live_meetings(force=True)
    return jsonify({"ok": True, "time": fmt_dt(now_local())})


