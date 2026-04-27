# Extracted from original app.py during real modular split.
# Do not run this file directly. It registers routes on the shared Flask app.
import sys
_legacy = sys.modules.get("app") or sys.modules.get("__main__")
if _legacy is None:
    raise RuntimeError("This module must be imported by app.py")
globals().update({name: getattr(_legacy, name) for name in dir(_legacy) if not name.startswith("__")})

# ---- api_member_intelligence_level3 ----
@app.route('/api/member-intelligence/<int:member_id>')
@login_required
def api_member_intelligence_level3(member_id):
    matches=[m for m in _ai_member_stats() if int(m.get('id') or 0)==int(member_id)]
    return (jsonify(matches[0]) if matches else (jsonify({'error':'Member not found'}),404))

