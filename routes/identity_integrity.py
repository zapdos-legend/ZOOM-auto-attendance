# Identity Integrity Scanner route module.
# Read-only trust analysis over historical attendance rows.
import csv
import io
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime

_legacy = sys.modules.get("app") or sys.modules.get("__main__")
if _legacy is None:
    raise RuntimeError("This module must be imported by app.py")
globals().update({name: getattr(_legacy, name) for name in dir(_legacy) if not name.startswith("__")})

IDENTITY_SCAN_CACHE_KEY = "identity_integrity:scan:v2"
IDENTITY_RENDER_LIMIT = 1000
IDENTITY_TOP_LIMIT = 20
IDENTITY_WORST_LIMIT = 75


def _identity_norm_text(value):
    return " ".join(str(value or "").strip().lower().split())


def _identity_norm_email(value):
    return str(value or "").strip().lower()


def _identity_name_tokens(value):
    return {token for token in _identity_norm_text(value).replace(".", " ").split() if len(token) > 1}


def _identity_token_overlap(left, right):
    left_tokens = _identity_name_tokens(left)
    right_tokens = _identity_name_tokens(right)
    if not left_tokens or not right_tokens:
        return 0
    return len(left_tokens & right_tokens) / max(len(left_tokens | right_tokens), 1)


def _identity_category(score):
    if score >= 95:
        return "VERIFIED"
    if score >= 70:
        return "POSSIBLE MATCH"
    if score >= 30:
        return "SUSPICIOUS"
    return "BROKEN"


def _identity_badge_class(category):
    return {
        "VERIFIED": "ok",
        "POSSIBLE MATCH": "info",
        "SUSPICIOUS": "warn",
        "BROKEN": "danger",
    }.get(category, "info")


def _identity_row_signature(row):
    email = _identity_norm_email(row.get("participant_email"))
    if email:
        return "email:" + email
    name = _identity_norm_text(row.get("participant_name"))
    if name:
        return "name:" + name
    return "attendance:" + str(row.get("attendance_id") or "unknown")


def _identity_member_name_select_sql(conn, alias=""):
    prefix = f"{alias}." if alias else ""
    has_full_name = column_exists(conn, "members", "full_name")
    has_name = column_exists(conn, "members", "name")
    if has_full_name and has_name:
        return f"COALESCE(NULLIF({prefix}full_name, ''), NULLIF({prefix}name, ''))"
    if has_full_name:
        return f"{prefix}full_name"
    if has_name:
        return f"{prefix}name"
    return "NULL"


def _identity_member_name(member):
    return member.get("member_name") or member.get("full_name") or member.get("name") or ""


def _identity_candidate_strength(row, member, signature_counts=None):
    participant_email = _identity_norm_email(row.get("participant_email"))
    participant_name = _identity_norm_text(row.get("participant_name"))
    member_email = _identity_norm_email(member.get("email"))
    member_name = _identity_norm_text(_identity_member_name(member))
    strength = 0
    if participant_email and member_email and participant_email == member_email:
        strength += 75
    if participant_name and member_name and participant_name == member_name:
        strength += 55
    elif participant_name and member_name:
        overlap = _identity_token_overlap(participant_name, member_name)
        if overlap >= 0.66:
            strength += 30
        elif overlap >= 0.34:
            strength += 15
    if signature_counts:
        sig = _identity_row_signature(row)
        total = sum(signature_counts.get(sig, {}).values())
        member_hits = signature_counts.get(sig, {}).get(member.get("id"), 0)
        if total and member_hits:
            strength += min(25, round((member_hits / total) * 25))
    return min(100, strength)


def _identity_build_member_indexes(members):
    by_id = {}
    index_by_id = {}
    by_email = defaultdict(list)
    token_index = defaultdict(set)
    for idx, member in enumerate(members):
        member_id = member.get("id")
        by_id[member_id] = member
        index_by_id[member_id] = idx
        email = _identity_norm_email(member.get("email"))
        if email:
            by_email[email].append(member)
        for token in _identity_name_tokens(_identity_member_name(member)):
            token_index[token].add(idx)
    return by_id, index_by_id, by_email, token_index


def _identity_candidate_pool(row, members, by_email, token_index, by_id, index_by_id):
    candidate_indexes = set()
    participant_email = _identity_norm_email(row.get("participant_email"))
    if participant_email:
        for member in by_email.get(participant_email, []):
            member_idx = index_by_id.get(member.get("id"))
            if member_idx is not None:
                candidate_indexes.add(member_idx)
    for token in _identity_name_tokens(row.get("participant_name")):
        candidate_indexes.update(token_index.get(token, set()))
    linked_idx = index_by_id.get(row.get("member_id"))
    if linked_idx is not None:
        candidate_indexes.add(linked_idx)
    if not candidate_indexes:
        return members
    return [members[idx] for idx in candidate_indexes]


def _identity_best_member(row, candidates, signature_counts):
    best_member = None
    best_strength = 0
    for member in candidates:
        strength = _identity_candidate_strength(row, member, signature_counts)
        if strength > best_strength:
            best_member = member
            best_strength = strength
    return best_member, best_strength


def _identity_explain_confidence(row, linked_member, best_member, best_strength, signature_counts):
    """
    Row confidence is intentionally read-only and decomposed for investigation:
    - Email Score: up to +45 for exact participant/member email evidence.
    - Name Score: up to +25 for exact or strong name overlap evidence.
    - Member Match Score: up to +10 for rows already classified/linked as member rows.
    - Historical Consistency Score: up to +20 for repeated identity signatures pointing to the same member_id.
    - Penalty Score: negative cap pressure when another member is a stronger fit or the row is unlinked/missing data.
    Final confidence is the bounded 0-100 sum and is never written back to attendance.
    """
    reasons = []
    root_causes = []
    participant_email = _identity_norm_email(row.get("participant_email"))
    participant_name = _identity_norm_text(row.get("participant_name"))

    explanation = {
        "email_score": 0,
        "name_score": 0,
        "member_match_score": 0,
        "historical_consistency_score": 0,
        "penalty_score": 0,
        "final_confidence": 0,
        "linked_strength": 0,
        "history_percent": None,
        "reason_lines": [],
        "root_causes": [],
        "possible_false_positive": False,
        "alternate": None,
    }

    if linked_member:
        member_email = _identity_norm_email(linked_member.get("email"))
        member_name = _identity_norm_text(_identity_member_name(linked_member))
        linked_id = linked_member.get("id")

        if participant_email and member_email and participant_email == member_email:
            explanation["email_score"] = 45
            reasons.append("Participant email exactly matches the linked member email.")
        elif not participant_email:
            explanation["email_score"] = 20
            root_causes.append("Missing Email")
            reasons.append("Participant email is missing, so only neutral partial email credit was awarded.")
        elif not member_email:
            explanation["email_score"] = 18
            root_causes.append("Missing Email")
            reasons.append("Linked member email is missing, so exact email verification is not possible.")
        else:
            root_causes.append("Email Mismatch")
            reasons.append("Participant email does not match the linked member email.")

        if participant_name and member_name and participant_name == member_name:
            explanation["name_score"] = 25
            reasons.append("Participant name exactly matches the linked member name.")
        elif participant_name and member_name:
            overlap = _identity_token_overlap(participant_name, member_name)
            if overlap >= 0.66:
                explanation["name_score"] = 18
                reasons.append("Participant and member names have strong token overlap.")
            elif overlap >= 0.34:
                explanation["name_score"] = 9
                reasons.append("Participant and member names have partial token overlap.")
            else:
                root_causes.append("Name Mismatch")
                reasons.append("Participant name does not match the linked member name.")
        else:
            root_causes.append("Name Mismatch")
            reasons.append("Name comparison could not be fully verified.")

        if row.get("is_member") or row.get("member_id"):
            explanation["member_match_score"] = 10
            reasons.append("Attendance row is linked/marked as a known member participant.")
        else:
            root_causes.append("Unknown Participant")
            reasons.append("Attendance row is not marked as a known member participant.")

        sig = _identity_row_signature(row)
        total_for_sig = sum(signature_counts.get(sig, {}).values())
        linked_hits = signature_counts.get(sig, {}).get(linked_id, 0)
        if total_for_sig:
            consistency = linked_hits / total_for_sig
            explanation["history_percent"] = round(consistency * 100, 1)
            explanation["historical_consistency_score"] = round(consistency * 20)
            reasons.append(f"Historical consistency is {round(consistency * 100, 1)}% for this participant signature.")
            if consistency < 0.60:
                root_causes.append("Historical Inconsistency")
        else:
            reasons.append("No historical consistency sample exists for this participant signature.")

        linked_strength = _identity_candidate_strength(row, linked_member, signature_counts)
        explanation["linked_strength"] = linked_strength
        base_score = sum(explanation[key] for key in ("email_score", "name_score", "member_match_score", "historical_consistency_score"))
        if best_member and best_member.get("id") != linked_id:
            alternate_reason = _identity_alternate_reason(row, linked_member, best_member, linked_strength, best_strength)
            alternate = {
                "member_id": best_member.get("id"),
                "name": _identity_member_name(best_member) or f"Member #{best_member.get('id')}",
                "email": best_member.get("email") or "—",
                "confidence": best_strength,
                "reason": alternate_reason,
            }
            if best_strength >= max(70, linked_strength + 20):
                explanation["penalty_score"] = min(0, 29 - base_score)
                root_causes.append("Wrong Member Assignment")
                reasons.append(f"A stronger alternate member match was found: {alternate['name']}.")
                explanation["alternate"] = alternate
            elif best_strength >= max(45, linked_strength + 10):
                explanation["penalty_score"] = min(0, 69 - base_score)
                root_causes.append("Wrong Member Assignment")
                reasons.append(f"Another member is a plausible stronger match: {alternate['name']}.")
                explanation["alternate"] = alternate
            elif best_strength > linked_strength:
                explanation["alternate"] = alternate
    else:
        root_causes.append("Missing Member")
        if not row.get("is_member"):
            root_causes.append("Unknown Participant")
        if not participant_email:
            root_causes.append("Missing Email")
        if best_member and best_strength >= 70:
            explanation["penalty_score"] = -71
            explanation["alternate"] = {
                "member_id": best_member.get("id"),
                "name": _identity_member_name(best_member) or f"Member #{best_member.get('id')}",
                "email": best_member.get("email") or "—",
                "confidence": best_strength,
                "reason": "Participant identity strongly matches this existing member, but attendance has no linked member.",
            }
            root_causes.append("Wrong Member Assignment")
            reasons.append("No linked member exists, while an existing member is a strong alternate match.")
            base_score = best_strength
            explanation["final_confidence"] = min(29, best_strength)
        elif best_member and best_strength >= 45:
            explanation["alternate"] = {
                "member_id": best_member.get("id"),
                "name": _identity_member_name(best_member) or f"Member #{best_member.get('id')}",
                "email": best_member.get("email") or "—",
                "confidence": best_strength,
                "reason": "Participant may match this existing member, but member_id is missing.",
            }
            reasons.append("No linked member exists, but a possible member match was found.")
            base_score = 45
            explanation["final_confidence"] = 45
        else:
            reasons.append("Unknown participant row has no linked member to verify.")
            base_score = 55
            explanation["final_confidence"] = 55
        explanation["penalty_score"] = explanation["final_confidence"] - base_score

    if linked_member:
        base_score = sum(explanation[key] for key in ("email_score", "name_score", "member_match_score", "historical_consistency_score"))
        explanation["final_confidence"] = max(0, min(100, int(round(base_score + explanation["penalty_score"]))))

    explanation["root_causes"] = sorted(set(root_causes)) or ["No Major Identity Issue"]
    explanation["reason_lines"] = reasons
    explanation["reason"] = "; ".join(reasons) + "."
    explanation["possible_false_positive"] = _identity_is_possible_false_positive(explanation)
    return explanation


def _identity_alternate_reason(row, linked_member, best_member, linked_strength, best_strength):
    participant_email = _identity_norm_email(row.get("participant_email"))
    best_email = _identity_norm_email(best_member.get("email"))
    if participant_email and best_email and participant_email == best_email:
        return "Email belongs to alternate member."
    participant_name = _identity_norm_text(row.get("participant_name"))
    best_name = _identity_norm_text(_identity_member_name(best_member))
    linked_name = _identity_norm_text(_identity_member_name(linked_member or {}))
    if participant_name and best_name and participant_name == best_name and participant_name != linked_name:
        return "Participant name exactly matches alternate member."
    if best_strength > linked_strength:
        return "Alternate member has stronger combined email/name/history evidence."
    return "Alternate member is the closest available candidate."


def _identity_is_possible_false_positive(explanation):
    return bool(
        explanation.get("final_confidence", 0) < 70
        and explanation.get("email_score", 0) >= 40
        and explanation.get("name_score", 0) >= 18
        and explanation.get("historical_consistency_score", 0) >= 15
    )


def _identity_pct(count, total):
    return round((count / total) * 100, 1) if total else 0.0


def _identity_load_scan(force=False):
    cached = None if force else _cache_get(IDENTITY_SCAN_CACHE_KEY)
    if cached:
        return cached

    started = time.time()
    with db() as conn:
        with conn.cursor() as cur:
            member_name_select = _identity_member_name_select_sql(conn)
            linked_member_name_select = _identity_member_name_select_sql(conn, "lm")
            cur.execute(
                f"""
                SELECT id, COALESCE({member_name_select}, '') AS member_name, email
                FROM members
                ORDER BY id
                """
            )
            members = cur.fetchall()

            cur.execute(
                f"""
                SELECT a.id AS attendance_id,
                       a.meeting_uuid,
                       a.participant_name,
                       a.participant_email,
                       a.is_member,
                       a.member_id,
                       a.final_status,
                       a.created_at AS attendance_created_at,
                       COALESCE(m.start_time, a.first_join, a.created_at) AS meeting_date,
                       COALESCE(m.topic, 'Untitled Meeting') AS meeting_topic,
                       lm.id AS linked_member_id,
                       COALESCE({linked_member_name_select}, '') AS linked_member_name,
                       lm.email AS linked_member_email
                FROM attendance a
                LEFT JOIN meetings m ON m.meeting_uuid = a.meeting_uuid
                LEFT JOIN members lm ON lm.id = a.member_id
                ORDER BY COALESCE(m.start_time, a.first_join, a.created_at) DESC NULLS LAST, a.id DESC
                """
            )
            attendance_rows = cur.fetchall()

    member_by_id, member_index_by_id, members_by_email, member_token_index = _identity_build_member_indexes(members)
    signature_counts = defaultdict(Counter)
    for row in attendance_rows:
        if row.get("member_id"):
            signature_counts[_identity_row_signature(row)][row.get("member_id")] += 1

    records = []
    counts = Counter()
    root_cause_counts = Counter()
    member_rollup = defaultdict(lambda: {"issue_count": 0, "broken_count": 0, "suspicious_count": 0, "confidence_total": 0, "row_count": 0})
    meeting_rollup = defaultdict(lambda: {"issue_count": 0, "broken_count": 0, "suspicious_count": 0, "confidence_total": 0, "row_count": 0, "topic": "Untitled Meeting", "date": None})
    issue_count = 0
    confidence_total = 0
    false_positive_count = 0

    for row in attendance_rows:
        linked_member = None
        if row.get("member_id"):
            linked_member = {
                "id": row.get("linked_member_id") or row.get("member_id"),
                "member_name": row.get("linked_member_name") or "Missing member record",
                "email": row.get("linked_member_email") or "",
            }
        candidates = _identity_candidate_pool(row, members, members_by_email, member_token_index, member_by_id, member_index_by_id)
        best_member, best_strength = _identity_best_member(row, candidates, signature_counts)
        explanation = _identity_explain_confidence(row, linked_member, best_member, best_strength, signature_counts)
        confidence = explanation["final_confidence"]
        category = _identity_category(confidence)
        counts[category] += 1
        is_issue = category in ("SUSPICIOUS", "BROKEN")
        if is_issue:
            issue_count += 1
            for cause in explanation["root_causes"]:
                if cause != "No Major Identity Issue":
                    root_cause_counts[cause] += 1
        if explanation["possible_false_positive"]:
            false_positive_count += 1
        confidence_total += confidence

        linked_member_label = row.get("linked_member_name") or ("Member #" + str(row.get("member_id")) if row.get("member_id") else "Unlinked / Unknown")
        record = {
            "attendance_id": row.get("attendance_id"),
            "meeting_uuid": row.get("meeting_uuid"),
            "meeting_topic": row.get("meeting_topic") or "Untitled Meeting",
            "meeting_date": row.get("meeting_date"),
            "participant_name": row.get("participant_name") or "Unknown",
            "participant_email": row.get("participant_email") or "—",
            "linked_member": linked_member_label,
            "member_email": row.get("linked_member_email") or "—",
            "confidence": confidence,
            "category": category,
            "badge_class": _identity_badge_class(category),
            "reason": explanation["reason"],
            "member_id": row.get("member_id"),
            "alternate": explanation.get("alternate"),
            "explanation": explanation,
            "root_causes": explanation["root_causes"],
            "possible_false_positive": explanation["possible_false_positive"],
        }
        records.append(record)

        member_key = row.get("member_id") or "unlinked"
        member_bucket = member_rollup[member_key]
        member_bucket["member_name"] = linked_member_label
        member_bucket["member_email"] = row.get("linked_member_email") or "—"
        member_bucket["confidence_total"] += confidence
        member_bucket["row_count"] += 1
        if category == "BROKEN":
            member_bucket["broken_count"] += 1
        if category == "SUSPICIOUS":
            member_bucket["suspicious_count"] += 1
        if is_issue:
            member_bucket["issue_count"] += 1

        meeting_key = row.get("meeting_uuid") or f"attendance:{row.get('attendance_id')}"
        meeting_bucket = meeting_rollup[meeting_key]
        meeting_bucket["topic"] = row.get("meeting_topic") or "Untitled Meeting"
        meeting_bucket["date"] = row.get("meeting_date")
        meeting_bucket["confidence_total"] += confidence
        meeting_bucket["row_count"] += 1
        if category == "BROKEN":
            meeting_bucket["broken_count"] += 1
        if category == "SUSPICIOUS":
            meeting_bucket["suspicious_count"] += 1
        if is_issue:
            meeting_bucket["issue_count"] += 1

    total_rows = len(attendance_rows)
    trust_score = round((confidence_total / total_rows), 2) if total_rows else 100.0
    adjusted_confidence_total = confidence_total + sum(max(0, 70 - r["confidence"]) for r in records if r["possible_false_positive"])
    adjusted_trust_score = round((adjusted_confidence_total / total_rows), 2) if total_rows else 100.0
    true_suspicious = max(0, counts["SUSPICIOUS"] - false_positive_count)
    true_broken = counts["BROKEN"]

    top_members = _identity_top_member_rollup(member_rollup)
    top_meetings = _identity_top_meeting_rollup(meeting_rollup)
    root_cause_breakdown = [
        {"cause": cause, "count": count, "percent": _identity_pct(count, issue_count)}
        for cause, count in root_cause_counts.most_common()
    ]
    worst_cases = sorted([r for r in records if r["category"] in ("SUSPICIOUS", "BROKEN")], key=lambda r: (r["confidence"], r["attendance_id"] or 0))[:IDENTITY_WORST_LIMIT]

    scan = {
        "records": records,
        "summary": {
            "total_rows": total_rows,
            "verified": counts["VERIFIED"],
            "possible": counts["POSSIBLE MATCH"],
            "suspicious": counts["SUSPICIOUS"],
            "broken": counts["BROKEN"],
            "issues": issue_count,
            "trust_score": trust_score,
            "duration_ms": int((time.time() - started) * 1000),
        },
        "diagnostics": {
            "rows_examined": total_rows,
            "verified_rows": counts["VERIFIED"],
            "likely_false_positives": false_positive_count,
            "true_suspicious_rows": true_suspicious,
            "true_broken_rows": true_broken,
            "adjusted_trust_score": adjusted_trust_score,
        },
        "top_members": top_members,
        "top_meetings": top_meetings,
        "root_cause_breakdown": root_cause_breakdown,
        "worst_cases": worst_cases,
        "scanned_at": datetime.utcnow(),
    }
    try:
        log_activity(
            "identity_scan_run",
            f"Identity Scan Run | rows scanned={total_rows} | issues found={issue_count} | trust score={trust_score}%",
        )
    except Exception:
        pass
    return _cache_set(IDENTITY_SCAN_CACHE_KEY, scan)


def _identity_top_member_rollup(member_rollup):
    rows = []
    for bucket in member_rollup.values():
        if not bucket["issue_count"]:
            continue
        rows.append(
            {
                "member_name": bucket.get("member_name") or "Unlinked / Unknown",
                "member_email": bucket.get("member_email") or "—",
                "issue_count": bucket["issue_count"],
                "broken_count": bucket["broken_count"],
                "suspicious_count": bucket["suspicious_count"],
                "trust_rating": round(bucket["confidence_total"] / bucket["row_count"], 2) if bucket["row_count"] else 100.0,
            }
        )
    return sorted(rows, key=lambda item: (-item["issue_count"], item["trust_rating"], item["member_name"]))[:IDENTITY_TOP_LIMIT]


def _identity_top_meeting_rollup(meeting_rollup):
    rows = []
    for bucket in meeting_rollup.values():
        if not bucket["issue_count"]:
            continue
        rows.append(
            {
                "topic": bucket.get("topic") or "Untitled Meeting",
                "date": bucket.get("date"),
                "issue_count": bucket["issue_count"],
                "broken_count": bucket["broken_count"],
                "trust_rating": round(bucket["confidence_total"] / bucket["row_count"], 2) if bucket["row_count"] else 100.0,
            }
        )
    return sorted(rows, key=lambda item: (-item["issue_count"], item["trust_rating"], item["topic"]))[:IDENTITY_TOP_LIMIT]


def _identity_parse_date(value):
    value = str(value or "").strip()
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except Exception:
        return None


def _identity_filter_records(records, category, start_date, end_date, member_search):
    filtered = []
    member_search = _identity_norm_text(member_search)
    for record in records:
        if category != "ALL" and record.get("category") != category:
            continue
        meeting_date = record.get("meeting_date")
        meeting_day = meeting_date.date() if hasattr(meeting_date, "date") else None
        if start_date and (not meeting_day or meeting_day < start_date):
            continue
        if end_date and (not meeting_day or meeting_day > end_date):
            continue
        if member_search:
            haystack = _identity_norm_text(
                " ".join(
                    [
                        record.get("participant_name", ""),
                        record.get("participant_email", ""),
                        record.get("linked_member", ""),
                        record.get("member_email", ""),
                        record.get("meeting_topic", ""),
                    ]
                )
            )
            if member_search not in haystack:
                continue
        filtered.append(record)
    return filtered


def _identity_csv_response(filename, records):
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "Attendance ID",
            "Meeting Topic",
            "Meeting Date",
            "Participant Name",
            "Participant Email",
            "Current Member",
            "Current Member Email",
            "Suggested Member",
            "Suggested Member Email",
            "Alternate Confidence",
            "Confidence",
            "Category",
            "Root Causes",
            "Reason",
            "Possible False Positive",
            "Email Score",
            "Name Score",
            "Member Match Score",
            "Historical Consistency Score",
            "Penalty Score",
            "Final Confidence",
        ]
    )
    for record in records:
        explanation = record.get("explanation") or {}
        alternate = record.get("alternate") or {}
        writer.writerow(
            [
                record.get("attendance_id"),
                record.get("meeting_topic"),
                record.get("meeting_date"),
                record.get("participant_name"),
                record.get("participant_email"),
                record.get("linked_member"),
                record.get("member_email"),
                alternate.get("name", ""),
                alternate.get("email", ""),
                alternate.get("confidence", ""),
                record.get("confidence"),
                record.get("category"),
                "; ".join(record.get("root_causes") or []),
                record.get("reason"),
                "YES" if record.get("possible_false_positive") else "NO",
                explanation.get("email_score", 0),
                explanation.get("name_score", 0),
                explanation.get("member_match_score", 0),
                explanation.get("historical_consistency_score", 0),
                explanation.get("penalty_score", 0),
                explanation.get("final_confidence", record.get("confidence")),
            ]
        )
    response = Response(output.getvalue(), mimetype="text/csv")
    response.headers["Content-Disposition"] = f"attachment; filename={filename}"
    return response


@app.route("/identity-integrity/export/<export_type>")
@login_required
@admin_required
def identity_integrity_export(export_type):
    scan = _identity_load_scan(force=request.args.get("refresh") == "1")
    records = scan["records"]
    if export_type == "suspicious":
        export_records = [r for r in records if r["category"] == "SUSPICIOUS"]
        return _identity_csv_response("identity_suspicious_rows.csv", export_records)
    if export_type == "broken":
        export_records = [r for r in records if r["category"] == "BROKEN"]
        return _identity_csv_response("identity_broken_rows.csv", export_records)
    if export_type == "investigation":
        export_records = [r for r in records if r["category"] in ("SUSPICIOUS", "BROKEN") or r.get("possible_false_positive")]
        return _identity_csv_response("identity_investigation_report.csv", export_records)
    return Response("Unknown identity export type", status=404)


@app.route("/identity-integrity")
@login_required
@admin_required
def identity_integrity():
    category = str(request.args.get("category") or "ALL").upper()
    category_map = {
        "ALL": "ALL",
        "VERIFIED": "VERIFIED",
        "POSSIBLE MATCH": "POSSIBLE MATCH",
        "POSSIBLE": "POSSIBLE MATCH",
        "SUSPICIOUS": "SUSPICIOUS",
        "BROKEN": "BROKEN",
    }
    category = category_map.get(category, "ALL")
    start_date_raw = request.args.get("start_date", "")
    end_date_raw = request.args.get("end_date", "")
    member_search = request.args.get("member_search", "")
    scan = _identity_load_scan(force=request.args.get("refresh") == "1")
    filtered_records = _identity_filter_records(
        scan["records"],
        category,
        _identity_parse_date(start_date_raw),
        _identity_parse_date(end_date_raw),
        member_search,
    )
    filtered_counts = Counter(r["category"] for r in filtered_records)
    filtered_trust = round(sum(r["confidence"] for r in filtered_records) / len(filtered_records), 2) if filtered_records else 100.0
    limited_records = filtered_records[:IDENTITY_RENDER_LIMIT]
    filters_active = bool(category != "ALL" or start_date_raw or end_date_raw or member_search)

    body = render_template_string(
        """
        <style>
          .identity-hero{position:relative;overflow:hidden;border-radius:28px;padding:24px;border:1px solid rgba(125,211,252,.25);background:radial-gradient(circle at 85% 0%,rgba(34,197,94,.20),transparent 32%),radial-gradient(circle at 15% 20%,rgba(99,102,241,.24),transparent 30%),linear-gradient(135deg,rgba(15,23,42,.94),rgba(30,41,59,.78));box-shadow:0 28px 80px rgba(2,6,23,.38);margin-bottom:18px}.identity-kicker{color:#7dd3fc;font-weight:1000;text-transform:uppercase;letter-spacing:.14em;font-size:12px}.identity-title{margin:8px 0;color:#f8fafc;font-size:34px;line-height:1.05;font-weight:1000}.identity-copy{color:#cbd5e1;max-width:980px;line-height:1.55}.identity-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:14px;margin:18px 0}.identity-card{border-radius:20px;padding:16px;background:rgba(15,23,42,.72);border:1px solid rgba(148,163,184,.18);box-shadow:0 18px 42px rgba(0,0,0,.22)}.identity-card small{display:block;color:#94a3b8;text-transform:uppercase;letter-spacing:.09em;font-weight:950;font-size:11px}.identity-card strong{display:block;margin-top:8px;font-size:30px;color:#f8fafc}.identity-card.trust{background:linear-gradient(135deg,rgba(14,165,233,.18),rgba(34,197,94,.12));border-color:rgba(125,211,252,.30)}.identity-card.broken{background:linear-gradient(135deg,rgba(239,68,68,.18),rgba(124,58,237,.10));border-color:rgba(248,113,113,.28)}.identity-card.warn{background:linear-gradient(135deg,rgba(245,158,11,.18),rgba(14,165,233,.08));border-color:rgba(251,191,36,.25)}.identity-filters{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;align-items:end;margin:12px 0 18px}.identity-filters label{color:#94a3b8;font-size:11px;font-weight:950;text-transform:uppercase;letter-spacing:.08em}.identity-filters input,.identity-filters select{width:100%;border-radius:12px;border:1px solid rgba(148,163,184,.22);background:rgba(15,23,42,.74);color:#e5e7eb;padding:10px}.identity-table-wrap{overflow:auto;border-radius:20px;border:1px solid rgba(148,163,184,.16);background:rgba(2,6,23,.25);margin:16px 0}.identity-table{width:100%;border-collapse:collapse;min-width:1050px}.identity-table th,.identity-table td{padding:12px;border-bottom:1px solid rgba(148,163,184,.12);text-align:left;vertical-align:top}.identity-table th{color:#cbd5e1;font-size:11px;text-transform:uppercase;letter-spacing:.08em}.identity-table td{color:#e5e7eb}.identity-pill{display:inline-flex;border-radius:999px;padding:6px 10px;font-size:11px;font-weight:1000;border:1px solid rgba(255,255,255,.12);white-space:nowrap}.identity-pill.ok{background:rgba(34,197,94,.15);color:#bbf7d0}.identity-pill.info{background:rgba(56,189,248,.15);color:#bae6fd}.identity-pill.warn{background:rgba(245,158,11,.15);color:#fde68a}.identity-pill.danger{background:rgba(239,68,68,.16);color:#fecaca}.identity-reason{max-width:440px;color:#cbd5e1;line-height:1.45}.identity-muted{color:#94a3b8}.identity-actions{display:flex;gap:10px;flex-wrap:wrap}.identity-actions .btn,.identity-actions button{display:inline-flex;align-items:center;justify-content:center;text-decoration:none;border-radius:12px;padding:10px 14px;font-weight:900;border:1px solid rgba(148,163,184,.18)}.identity-actions .secondary{background:rgba(148,163,184,.12);color:#e5e7eb}.identity-actions button{background:linear-gradient(135deg,#0ea5e9,#22c55e);color:#082f49}.identity-section-title{margin:26px 0 10px;color:#f8fafc;font-size:22px}.identity-details{margin-top:10px}.identity-details summary{cursor:pointer;color:#7dd3fc;font-weight:1000}.identity-score-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:8px;margin-top:10px}.identity-score{border-radius:14px;padding:10px;background:rgba(15,23,42,.62);border:1px solid rgba(148,163,184,.16)}.identity-score span{display:block;color:#94a3b8;font-size:10px;text-transform:uppercase;letter-spacing:.08em}.identity-score b{display:block;color:#f8fafc;margin-top:4px}.identity-alt{margin-top:10px;padding:10px;border-radius:14px;background:rgba(245,158,11,.10);border:1px solid rgba(245,158,11,.22)}.identity-cause-list{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:10px}.identity-cause{display:flex;justify-content:space-between;gap:8px;border-radius:14px;padding:12px;background:rgba(15,23,42,.62);border:1px solid rgba(148,163,184,.14)}
        </style>
        <section class="identity-hero">
          <div class="identity-kicker">Trust & Intelligence Center</div>
          <h1 class="identity-title">Identity Validation Center</h1>
          <p class="identity-copy">Read-only investigation layer for the Identity Integrity Scanner. It explains every confidence decision, highlights alternate member matches, surfaces affected members and meetings, and exports investigation CSVs without remapping or editing attendance data.</p>
        </section>

        <div class="identity-grid">
          <div class="identity-card"><small>Total Attendance Rows</small><strong>{{ summary.total_rows }}</strong></div>
          <div class="identity-card trust"><small>Identity Trust Score</small><strong>{{ '%.2f'|format(summary.trust_score) }}%</strong></div>
          <div class="identity-card"><small>Verified</small><strong>{{ summary.verified }}</strong></div>
          <div class="identity-card"><small>Possible Match</small><strong>{{ summary.possible }}</strong></div>
          <div class="identity-card warn"><small>Suspicious</small><strong>{{ summary.suspicious }}</strong></div>
          <div class="identity-card broken"><small>Broken</small><strong>{{ summary.broken }}</strong></div>
        </div>

        <h2 class="identity-section-title">Trust Score Validation</h2>
        <div class="identity-grid">
          <div class="identity-card"><small>Rows Examined</small><strong>{{ diagnostics.rows_examined }}</strong></div>
          <div class="identity-card"><small>Verified Rows</small><strong>{{ diagnostics.verified_rows }}</strong></div>
          <div class="identity-card warn"><small>Likely False Positives</small><strong>{{ diagnostics.likely_false_positives }}</strong></div>
          <div class="identity-card warn"><small>True Suspicious Rows</small><strong>{{ diagnostics.true_suspicious_rows }}</strong></div>
          <div class="identity-card broken"><small>True Broken Rows</small><strong>{{ diagnostics.true_broken_rows }}</strong></div>
          <div class="identity-card trust"><small>Adjusted Diagnostic Trust</small><strong>{{ '%.2f'|format(diagnostics.adjusted_trust_score) }}%</strong></div>
        </div>

        <div class="identity-grid">
          <div class="identity-card"><small>Filtered Rows</small><strong>{{ filtered_total }}</strong></div>
          <div class="identity-card trust"><small>Filtered Trust</small><strong>{{ '%.2f'|format(filtered_trust) }}%</strong></div>
          <div class="identity-card broken"><small>Filtered Issues</small><strong>{{ filtered_counts.get('SUSPICIOUS', 0) + filtered_counts.get('BROKEN', 0) }}</strong></div>
          <div class="identity-card"><small>Scan Runtime</small><strong>{{ summary.duration_ms }}ms</strong></div>
        </div>

        <form class="identity-card" method="get">
          <div class="identity-filters">
            <div><label>Category</label><select name="category">
              {% for value, label in categories %}<option value="{{ value }}" {% if value == category %}selected{% endif %}>{{ label }}</option>{% endfor %}
            </select></div>
            <div><label>Date From</label><input type="date" name="start_date" value="{{ start_date_raw }}"></div>
            <div><label>Date To</label><input type="date" name="end_date" value="{{ end_date_raw }}"></div>
            <div><label>Member / Meeting Search</label><input type="search" name="member_search" placeholder="Name, email, or meeting" value="{{ member_search }}"></div>
            <div class="identity-actions"><button type="submit">Apply Filters</button><a class="btn secondary" href="{{ url_for('identity_integrity') }}">Reset</a><a class="btn secondary" href="{{ url_for('identity_integrity', refresh=1) }}">Refresh Scan</a></div>
          </div>
          <div class="identity-actions">
            <a class="btn secondary" href="{{ url_for('identity_integrity_export', export_type='suspicious') }}">Export Suspicious Rows CSV</a>
            <a class="btn secondary" href="{{ url_for('identity_integrity_export', export_type='broken') }}">Export Broken Rows CSV</a>
            <a class="btn secondary" href="{{ url_for('identity_integrity_export', export_type='investigation') }}">Export Investigation Report CSV</a>
          </div>
          <div class="identity-muted" style="margin-top:10px">Showing {{ records|length }} of {{ filtered_total }} matching rows{% if records|length < filtered_total %} (render limit {{ render_limit }}){% endif %}. Scan cached briefly for performance; refresh forces a new read-only scan.</div>
        </form>

        <h2 class="identity-section-title">Root Cause Breakdown</h2>
        <div class="identity-cause-list">
          {% for item in root_cause_breakdown %}
          <div class="identity-cause"><span>{{ item.cause }}</span><strong>{{ item.count }} ({{ item.percent }}%)</strong></div>
          {% else %}<div class="identity-card identity-muted">No suspicious or broken root causes found.</div>{% endfor %}
        </div>

        <h2 class="identity-section-title">Top Affected Members</h2>
        <div class="identity-table-wrap"><table class="identity-table"><thead><tr><th>Member Name</th><th>Issue Count</th><th>Broken Count</th><th>Suspicious Count</th><th>Trust Rating</th></tr></thead><tbody>
          {% for member in top_members %}<tr><td><strong>{{ member.member_name }}</strong><div class="identity-muted">{{ member.member_email }}</div></td><td>{{ member.issue_count }}</td><td>{{ member.broken_count }}</td><td>{{ member.suspicious_count }}</td><td><strong>{{ member.trust_rating }}%</strong></td></tr>{% else %}<tr><td colspan="5" class="identity-muted">No affected members found.</td></tr>{% endfor %}
        </tbody></table></div>

        <h2 class="identity-section-title">Top Affected Meetings</h2>
        <div class="identity-table-wrap"><table class="identity-table"><thead><tr><th>Meeting Topic</th><th>Meeting Date</th><th>Issue Count</th><th>Broken Count</th><th>Trust Rating</th></tr></thead><tbody>
          {% for meeting in top_meetings %}<tr><td><strong>{{ meeting.topic }}</strong></td><td>{{ meeting.date.strftime('%Y-%m-%d %H:%M') if meeting.date else '—' }}</td><td>{{ meeting.issue_count }}</td><td>{{ meeting.broken_count }}</td><td><strong>{{ meeting.trust_rating }}%</strong></td></tr>{% else %}<tr><td colspan="5" class="identity-muted">No affected meetings found.</td></tr>{% endfor %}
        </tbody></table></div>

        <h2 class="identity-section-title">Top Worst Cases</h2>
        <div class="identity-table-wrap"><table class="identity-table"><thead><tr><th>Attendance ID</th><th>Participant</th><th>Current Member</th><th>Suggested Member</th><th>Confidence</th><th>Reason</th></tr></thead><tbody>
          {% for row in worst_cases %}<tr><td>{{ row.attendance_id }}</td><td><strong>{{ row.participant_name }}</strong><div class="identity-muted">{{ row.participant_email }}</div></td><td>{{ row.linked_member }}</td><td>{% if row.alternate %}<strong>{{ row.alternate.name }}</strong><div class="identity-muted">{{ row.alternate.email }} · {{ row.alternate.confidence }}%</div>{% else %}<span class="identity-muted">No stronger alternate</span>{% endif %}</td><td><strong>{{ row.confidence }}%</strong><div><span class="identity-pill {{ row.badge_class }}">{{ row.category }}</span></div></td><td class="identity-reason">{{ row.reason }}</td></tr>{% else %}<tr><td colspan="6" class="identity-muted">No suspicious or broken rows found.</td></tr>{% endfor %}
        </tbody></table></div>

        <h2 class="identity-section-title">Row-Level Confidence Explainer</h2>
        <div class="identity-table-wrap">
          <table class="identity-table">
            <thead><tr><th>Meeting Date</th><th>Participant</th><th>Current Member</th><th>Confidence</th><th>Category</th><th>Why Flagged / Details</th></tr></thead>
            <tbody>
              {% for row in records %}
              <tr>
                <td><strong>{{ row.meeting_date.strftime('%Y-%m-%d %H:%M') if row.meeting_date else '—' }}</strong><div class="identity-muted">{{ row.meeting_topic }}</div></td>
                <td><strong>{{ row.participant_name }}</strong><div class="identity-muted">{{ row.participant_email }}</div></td>
                <td><strong>{{ row.linked_member }}</strong><div class="identity-muted">{{ row.member_email }}</div></td>
                <td><strong>{{ row.confidence }}%</strong>{% if row.possible_false_positive %}<div><span class="identity-pill warn">Possible False Positive</span></div>{% endif %}</td>
                <td><span class="identity-pill {{ row.badge_class }}">{{ row.category }}</span></td>
                <td class="identity-reason">
                  {{ row.reason }}
                  <details class="identity-details">
                    <summary>Confidence decision breakdown</summary>
                    <div class="identity-score-grid">
                      <div class="identity-score"><span>Email Score</span><b>{{ row.explanation.email_score }}</b></div>
                      <div class="identity-score"><span>Name Score</span><b>{{ row.explanation.name_score }}</b></div>
                      <div class="identity-score"><span>Member Match Score</span><b>{{ row.explanation.member_match_score }}</b></div>
                      <div class="identity-score"><span>Historical Consistency</span><b>{{ row.explanation.historical_consistency_score }}</b></div>
                      <div class="identity-score"><span>Penalty Score</span><b>{{ row.explanation.penalty_score }}</b></div>
                      <div class="identity-score"><span>Final Confidence</span><b>{{ row.explanation.final_confidence }}%</b></div>
                    </div>
                    <div class="identity-muted" style="margin-top:8px">Root causes: {{ row.root_causes|join(', ') }}</div>
                    {% if row.alternate %}
                    <div class="identity-alt">
                      <strong>Alternate Member Detection</strong><br>
                      Current Member: {{ row.linked_member }}<br>
                      Best Alternate Member: {{ row.alternate.name }}<br>
                      Alternate Member Email: {{ row.alternate.email }}<br>
                      Alternate Confidence: {{ row.alternate.confidence }}%<br>
                      Reason: {{ row.alternate.reason }}
                    </div>
                    {% endif %}
                  </details>
                </td>
              </tr>
              {% else %}
              <tr><td colspan="6" class="identity-muted">No attendance rows match these filters.</td></tr>
              {% endfor %}
            </tbody>
          </table>
        </div>
        """,
        summary=scan["summary"],
        diagnostics=scan["diagnostics"],
        records=limited_records,
        filtered_total=len(filtered_records),
        filtered_trust=filtered_trust,
        filtered_counts=filtered_counts,
        categories=[("ALL", "All"), ("VERIFIED", "Verified"), ("POSSIBLE MATCH", "Possible Match"), ("SUSPICIOUS", "Suspicious"), ("BROKEN", "Broken")],
        category=category,
        start_date_raw=start_date_raw,
        end_date_raw=end_date_raw,
        member_search=member_search,
        filters_active=filters_active,
        render_limit=IDENTITY_RENDER_LIMIT,
        top_members=scan["top_members"],
        top_meetings=scan["top_meetings"],
        root_cause_breakdown=scan["root_cause_breakdown"],
        worst_cases=scan["worst_cases"],
    )
    return page("Identity Validation Center", body, active="identity_integrity")
