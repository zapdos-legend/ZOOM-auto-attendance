# Extracted from original app.py during real modular split.
# Do not run this file directly. It registers routes on the shared Flask app.
import sys
_legacy = sys.modules.get("app") or sys.modules.get("__main__")
if _legacy is None:
    raise RuntimeError("This module must be imported by app.py")
globals().update({name: getattr(_legacy, name) for name in dir(_legacy) if not name.startswith("__")})

# ---- zoom_webhook ----
@app.route("/zoom/webhook", methods=["POST"])
def zoom_webhook():
    try:
        payload = request.get_json(force=True, silent=True) or {}
        print("🔥 FULL ZOOM DATA:", payload)

        if payload.get("event") == "endpoint.url_validation":
            plain = payload.get("payload", {}).get("plainToken", "")
            encrypted = hmac.new(
                ZOOM_SECRET_TOKEN.encode("utf-8"),
                plain.encode("utf-8"),
                hashlib.sha256,
            ).hexdigest() if ZOOM_SECRET_TOKEN else ""
            print("✅ URL VALIDATION:", {"plainToken": plain, "encryptedToken": encrypted})
            return jsonify({"plainToken": plain, "encryptedToken": encrypted})

        if not verify_zoom_signature(request):
            print("❌ INVALID SIGNATURE")
            return jsonify({"message": "invalid signature"}), 401

        event = (payload.get("event") or "").strip()
        payload_root = payload.get("payload", {}) or {}
        obj = payload_root.get("object", {}) or {}

        print("📌 EVENT:", event)
        print("📌 OBJECT:", obj)

        participant = obj.get("participant") or payload_root.get("participant") or {}
        if not participant and isinstance(obj.get("participants"), list) and obj.get("participants"):
            participant = obj.get("participants")[0] or {}
        if not participant and any(k in obj for k in ("user_name", "participant_user_name", "name", "email", "user_email")):
            participant = obj

        print("📌 PARTICIPANT:", participant)

        if event == "meeting.started":
            meeting = ensure_meeting(obj)
            print("✅ MEETING STARTED RESOLVED:", meeting)
            print("📝 WEBHOOK LOG zoom_started:", meeting["meeting_uuid"] if meeting else "unknown")
            return jsonify({"ok": True})

        if "participant_joined" in event or "participant_left" in event:
            meeting = ensure_meeting(obj)
            print("✅ PARTICIPANT EVENT MEETING:", meeting)

            if not meeting:
                print("❌ meeting not resolved")
                return jsonify({"ok": False, "reason": "meeting not resolved"}), 200

            meeting_uuid = meeting["meeting_uuid"]
            event_type = "join" if "participant_joined" in event else "leave"

            event_raw = (
                participant.get("join_time")
                or participant.get("leave_time")
                or obj.get("join_time")
                or obj.get("leave_time")
                or (
                    datetime.fromtimestamp(payload.get("event_ts") / 1000, tz=ZoneInfo(TIMEZONE_NAME)).isoformat()
                    if isinstance(payload.get("event_ts"), (int, float))
                    else None
                )
            )
            event_time = parse_dt(event_raw) or now_local()

            participant_name = (
                participant.get("user_name")
                or participant.get("participant_user_name")
                or participant.get("display_name")
                or participant.get("name")
                or participant.get("participant_name")
                or participant.get("screen_name")
                or "Unknown Participant"
            )
            participant_email = (
                participant.get("email")
                or participant.get("user_email")
                or participant.get("participant_email")
                or None
            )

            print("📌 PARSED PARTICIPANT:", {
                "meeting_uuid": meeting_uuid,
                "event_type": event_type,
                "participant_name": participant_name,
                "participant_email": participant_email,
                "event_raw": event_raw,
                "event_time": str(event_time),
            })

            zoom_host_id = str(obj.get("host_id") or "").strip()
            participant_ids = {
                str(participant.get("id") or "").strip(),
                str(participant.get("participant_user_id") or "").strip(),
                str(participant.get("user_id") or "").strip(),
            }
            is_host_override = bool(zoom_host_id and zoom_host_id in participant_ids) or (
                bool(obj.get("host_email")) and str(obj.get("host_email")).strip().lower() == str(participant_email or "").strip().lower()
            )

            update_participant(
                meeting_uuid,
                participant_name,
                participant_email,
                event_time,
                event_type,
                is_host_override=is_host_override,
            )
            print("📝 WEBHOOK LOG zoom_participant_event:", f"{event} :: {meeting_uuid} :: {participant_name}")
            return jsonify({"ok": True})

        if event in ("meeting.ended", "meeting.end"):
            meeting = ensure_meeting(obj)
            print("✅ MEETING ENDED RESOLVED:", meeting)

            if not meeting:
                print("❌ meeting not resolved")
                return jsonify({"ok": False, "reason": "meeting not resolved"}), 200

            finalized = finalize_meeting(meeting["meeting_uuid"], parse_dt(obj.get("end_time")) or now_local(), run_post_tasks=False)
            print("✅ FINALIZED:", finalized)
            print("📝 WEBHOOK LOG zoom_meeting_ended:", meeting["meeting_uuid"])
            return jsonify({"ok": True, "finalized": bool(finalized)})

        print("ℹ️ IGNORED EVENT:", event)
        return jsonify({"ok": True, "ignored": event})

    except Exception as e:
        print("❌ WEBHOOK ERROR:", str(e))
        return jsonify({"ok": False, "error": str(e)}), 200


