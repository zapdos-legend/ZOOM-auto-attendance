# Identity Integrity Scanner route module.
# Read-only trust analysis over historical attendance rows.
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime

_legacy = sys.modules.get("app") or sys.modules.get("__main__")
if _legacy is None:
    raise RuntimeError("This module must be imported by app.py")
globals().update({name: getattr(_legacy, name) for name in dir(_legacy) if not name.startswith("__")})

IDENTITY_SCAN_CACHE_KEY = "identity_integrity:scan:v1"
IDENTITY_RENDER_LIMIT = 1000


def _identity_norm_text(value):
    return " ".join(str(value or "").strip().lower().split())


def _identity_norm_email(value):
    return str(value or "").strip().lower()


def _identity_name_tokens(value):
    return {token for token in _identity_norm_text(value).replace(".", " ").split() if len(token) > 1}


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
        p_tokens = _identity_name_tokens(participant_name)
        m_tokens = _identity_name_tokens(member_name)
        if p_tokens and m_tokens:
            overlap = len(p_tokens & m_tokens) / max(len(p_tokens | m_tokens), 1)
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


def _identity_best_member(row, members, signature_counts):
    best_member = None
    best_strength = 0
    for member in members:
        strength = _identity_candidate_strength(row, member, signature_counts)
        if strength > best_strength:
            best_member = member
            best_strength = strength
    return best_member, best_strength


def _identity_confidence_for_row(row, linked_member, best_member, best_strength, signature_counts):
    """
    Identity Trust Score formula (row level):
    - 45 points: participant email exactly matches the linked member email.
      Missing email gets a neutral partial allowance so exact-name rows land in POSSIBLE MATCH; mismatched email gets 0.
    - 25 points: participant name exactly matches the linked member name; strong token
      overlap receives partial credit.
    - 10 points: row is marked as a known/member participant instead of unknown.
    - 20 points: historical consistency, calculated as the percentage of rows with
      the same participant identity signature that point to this same member_id.
    - Safety cap: if the participant matches another member more strongly than the
      linked member, confidence is capped to BROKEN/SUSPICIOUS ranges.
    Overall dashboard trust score is the average row confidence across scanned rows.
    """
    reasons = []
    participant_email = _identity_norm_email(row.get("participant_email"))
    participant_name = _identity_norm_text(row.get("participant_name"))

    if not linked_member:
        if best_member and best_strength >= 70:
            return min(29, best_strength), "Broken: participant identity strongly matches an existing member but this attendance row has no linked member."
        if best_member and best_strength >= 45:
            return 45, "Suspicious: participant may match an existing member, but member_id is missing."
        return 55, "Suspicious: unknown participant row has no linked member to verify."

    member_email = _identity_norm_email(linked_member.get("email"))
    member_name = _identity_norm_text(_identity_member_name(linked_member))
    score = 0

    if participant_email and member_email and participant_email == member_email:
        score += 45
        reasons.append("email matches linked member")
    elif not participant_email:
        score += 20
        reasons.append("participant email missing")
    elif not member_email:
        score += 18
        reasons.append("member email missing")
    else:
        reasons.append("email mismatch")

    if participant_name and member_name and participant_name == member_name:
        score += 25
        reasons.append("exact name match")
    elif participant_name and member_name:
        p_tokens = _identity_name_tokens(participant_name)
        m_tokens = _identity_name_tokens(member_name)
        overlap = len(p_tokens & m_tokens) / max(len(p_tokens | m_tokens), 1) if p_tokens and m_tokens else 0
        if overlap >= 0.66:
            score += 18
            reasons.append("strong name overlap")
        elif overlap >= 0.34:
            score += 9
            reasons.append("partial name overlap")
        else:
            reasons.append("name mismatch")
    else:
        reasons.append("name unavailable for exact comparison")

    if row.get("is_member") or row.get("member_id"):
        score += 10
        reasons.append("known/member participant status")
    else:
        reasons.append("unknown participant status")

    sig = _identity_row_signature(row)
    linked_id = linked_member.get("id")
    total_for_sig = sum(signature_counts.get(sig, {}).values())
    linked_hits = signature_counts.get(sig, {}).get(linked_id, 0)
    if total_for_sig:
        consistency = linked_hits / total_for_sig
        score += round(consistency * 20)
        reasons.append(f"historical consistency {round(consistency * 100, 1)}%")
    else:
        reasons.append("no historical consistency sample")

    linked_strength = _identity_candidate_strength(row, linked_member, signature_counts)
    if best_member and best_member.get("id") != linked_id and best_strength >= max(70, linked_strength + 20):
        score = min(score, 29)
        reasons.append(f"stronger match appears to be {html_escape(_identity_member_name(best_member))}")
    elif best_member and best_member.get("id") != linked_id and best_strength >= max(45, linked_strength + 10):
        score = min(score, 69)
        reasons.append(f"another member is a plausible stronger match: {html_escape(_identity_member_name(best_member))}")

    score = max(0, min(100, int(round(score))))
    return score, "; ".join(reasons) + "."


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

    signature_counts = defaultdict(Counter)
    for row in attendance_rows:
        if row.get("member_id"):
            signature_counts[_identity_row_signature(row)][row.get("member_id")] += 1

    records = []
    counts = Counter()
    issue_count = 0
    confidence_total = 0
    for row in attendance_rows:
        linked_member = None
        if row.get("member_id"):
            linked_member = {
                "id": row.get("linked_member_id") or row.get("member_id"),
                "member_name": row.get("linked_member_name") or "Missing member record",
                "email": row.get("linked_member_email") or "",
            }
        best_member, best_strength = _identity_best_member(row, members, signature_counts)
        confidence, reason = _identity_confidence_for_row(row, linked_member, best_member, best_strength, signature_counts)
        category = _identity_category(confidence)
        counts[category] += 1
        if category in ("SUSPICIOUS", "BROKEN"):
            issue_count += 1
        confidence_total += confidence
        records.append(
            {
                "meeting_date": row.get("meeting_date"),
                "participant_name": row.get("participant_name") or "Unknown",
                "participant_email": row.get("participant_email") or "—",
                "linked_member": row.get("linked_member_name") or ("Member #" + str(row.get("member_id")) if row.get("member_id") else "Unlinked / Unknown"),
                "member_email": row.get("linked_member_email") or "—",
                "confidence": confidence,
                "category": category,
                "badge_class": _identity_badge_class(category),
                "reason": reason,
                "member_id": row.get("member_id"),
            }
        )

    total_rows = len(attendance_rows)
    trust_score = round((confidence_total / total_rows), 2) if total_rows else 100.0
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
            haystack = _identity_norm_text(" ".join([record.get("participant_name", ""), record.get("participant_email", ""), record.get("linked_member", ""), record.get("member_email", "")]))
            if member_search not in haystack:
                continue
        filtered.append(record)
    return filtered


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
          .identity-hero{position:relative;overflow:hidden;border-radius:28px;padding:24px;border:1px solid rgba(125,211,252,.25);background:radial-gradient(circle at 85% 0%,rgba(34,197,94,.20),transparent 32%),radial-gradient(circle at 15% 20%,rgba(99,102,241,.24),transparent 30%),linear-gradient(135deg,rgba(15,23,42,.94),rgba(30,41,59,.78));box-shadow:0 28px 80px rgba(2,6,23,.38);margin-bottom:18px}.identity-kicker{color:#7dd3fc;font-weight:1000;text-transform:uppercase;letter-spacing:.14em;font-size:12px}.identity-title{margin:8px 0;color:#f8fafc;font-size:34px;line-height:1.05;font-weight:1000}.identity-copy{color:#cbd5e1;max-width:880px;line-height:1.55}.identity-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:14px;margin:18px 0}.identity-card{border-radius:20px;padding:16px;background:rgba(15,23,42,.72);border:1px solid rgba(148,163,184,.18);box-shadow:0 18px 42px rgba(0,0,0,.22)}.identity-card small{display:block;color:#94a3b8;text-transform:uppercase;letter-spacing:.09em;font-weight:950;font-size:11px}.identity-card strong{display:block;margin-top:8px;font-size:30px;color:#f8fafc}.identity-card.trust{background:linear-gradient(135deg,rgba(14,165,233,.18),rgba(34,197,94,.12));border-color:rgba(125,211,252,.30)}.identity-card.broken{background:linear-gradient(135deg,rgba(239,68,68,.18),rgba(124,58,237,.10));border-color:rgba(248,113,113,.28)}.identity-filters{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;align-items:end;margin:12px 0 18px}.identity-filters label{color:#94a3b8;font-size:11px;font-weight:950;text-transform:uppercase;letter-spacing:.08em}.identity-filters input,.identity-filters select{width:100%;border-radius:12px;border:1px solid rgba(148,163,184,.22);background:rgba(15,23,42,.74);color:#e5e7eb;padding:10px}.identity-table-wrap{overflow:auto;border-radius:20px;border:1px solid rgba(148,163,184,.16);background:rgba(2,6,23,.25)}.identity-table{width:100%;border-collapse:collapse;min-width:1050px}.identity-table th,.identity-table td{padding:12px;border-bottom:1px solid rgba(148,163,184,.12);text-align:left;vertical-align:top}.identity-table th{color:#cbd5e1;font-size:11px;text-transform:uppercase;letter-spacing:.08em}.identity-table td{color:#e5e7eb}.identity-pill{display:inline-flex;border-radius:999px;padding:6px 10px;font-size:11px;font-weight:1000;border:1px solid rgba(255,255,255,.12);white-space:nowrap}.identity-pill.ok{background:rgba(34,197,94,.15);color:#bbf7d0}.identity-pill.info{background:rgba(56,189,248,.15);color:#bae6fd}.identity-pill.warn{background:rgba(245,158,11,.15);color:#fde68a}.identity-pill.danger{background:rgba(239,68,68,.16);color:#fecaca}.identity-reason{max-width:380px;color:#cbd5e1;line-height:1.45}.identity-muted{color:#94a3b8}.identity-actions{display:flex;gap:10px;flex-wrap:wrap}.identity-actions .btn{display:inline-flex;align-items:center;justify-content:center;text-decoration:none;border-radius:12px;padding:10px 14px;font-weight:900}.identity-actions .secondary{background:rgba(148,163,184,.12);color:#e5e7eb;border:1px solid rgba(148,163,184,.18)}
        </style>
        <section class="identity-hero">
          <div class="identity-kicker">Trust & Intelligence Center</div>
          <h1 class="identity-title">Identity Integrity Scanner</h1>
          <p class="identity-copy">Read-only historical scan of attendance rows to identify Aditya-like member mapping inconsistencies. Attendance remains the master truth; this module detects confidence signals only and never remaps or edits attendance data.</p>
        </section>

        <div class="identity-grid">
          <div class="identity-card"><small>Total Attendance Rows</small><strong>{{ summary.total_rows }}</strong></div>
          <div class="identity-card trust"><small>Identity Trust Score</small><strong>{{ '%.2f'|format(summary.trust_score) }}%</strong></div>
          <div class="identity-card"><small>Verified</small><strong>{{ summary.verified }}</strong></div>
          <div class="identity-card"><small>Possible Match</small><strong>{{ summary.possible }}</strong></div>
          <div class="identity-card"><small>Suspicious</small><strong>{{ summary.suspicious }}</strong></div>
          <div class="identity-card broken"><small>Broken</small><strong>{{ summary.broken }}</strong></div>
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
            <div><label>Member Search</label><input type="search" name="member_search" placeholder="Name or email" value="{{ member_search }}"></div>
            <div class="identity-actions"><button type="submit">Apply Filters</button><a class="btn secondary" href="{{ url_for('identity_integrity') }}">Reset</a><a class="btn secondary" href="{{ url_for('identity_integrity', refresh=1) }}">Refresh Scan</a></div>
          </div>
          <div class="identity-muted">Showing {{ records|length }} of {{ filtered_total }} matching rows{% if records|length < filtered_total %} (render limit {{ render_limit }}){% endif %}. Scan cached briefly for performance; refresh forces a new read-only scan.</div>
        </form>

        <div class="identity-table-wrap">
          <table class="identity-table">
            <thead><tr><th>Meeting Date</th><th>Participant Name</th><th>Participant Email</th><th>Linked Member</th><th>Member Email</th><th>Confidence</th><th>Category</th><th>Reason</th></tr></thead>
            <tbody>
              {% for row in records %}
              <tr>
                <td>{{ row.meeting_date.strftime('%Y-%m-%d %H:%M') if row.meeting_date else '—' }}</td>
                <td>{{ row.participant_name }}</td>
                <td>{{ row.participant_email }}</td>
                <td>{{ row.linked_member }}</td>
                <td>{{ row.member_email }}</td>
                <td><strong>{{ row.confidence }}%</strong></td>
                <td><span class="identity-pill {{ row.badge_class }}">{{ row.category }}</span></td>
                <td class="identity-reason">{{ row.reason }}</td>
              </tr>
              {% else %}
              <tr><td colspan="8" class="identity-muted">No attendance rows match these filters.</td></tr>
              {% endfor %}
            </tbody>
          </table>
        </div>
        """,
        summary=scan["summary"],
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
    )
    return page("Identity Integrity Scanner", body, active="identity_integrity")
