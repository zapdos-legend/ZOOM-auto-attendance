# Extracted from original app.py during real modular split.
# Do not run this file directly. It registers routes on the shared Flask app.
import sys
_legacy = sys.modules.get("app") or sys.modules.get("__main__")
if _legacy is None:
    raise RuntimeError("This module must be imported by app.py")
globals().update({name: getattr(_legacy, name) for name in dir(_legacy) if not name.startswith("__")})

# ---- manual_finalize_meeting ----
@app.route("/meetings/<path:meeting_uuid>/finalize")
@login_required
@admin_required
def manual_finalize_meeting(meeting_uuid):
    finalize_meeting(meeting_uuid, now_local())
    log_activity("manual_finalize_meeting", meeting_uuid)
    flash("Meeting finalized successfully.", "success")
    return redirect(url_for("meetings"))



# ---- delete_meeting ----
@app.route("/meetings/<path:meeting_uuid>/delete", methods=["POST"])
@login_required
@admin_required
def delete_meeting(meeting_uuid):
    if not meeting_uuid:
        flash("Meeting UUID missing for this record.", "error")
        return redirect(url_for("meetings"))

    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM attendance WHERE meeting_uuid=%s", (meeting_uuid,))
            cur.execute("DELETE FROM meetings WHERE meeting_uuid=%s", (meeting_uuid,))
        conn.commit()

    log_activity("meeting_delete", meeting_uuid)
    flash("Meeting deleted successfully.", "success")
    return redirect(url_for("meetings"))



# ---- meetings ----
@app.route("/meetings")
@login_required
def meetings():
    maybe_finalize_stale_live_meetings()
    try:
        page_no = max(int(request.args.get("page", 1)), 1)
    except Exception:
        page_no = 1
    per_page = 50
    offset = (page_no - 1) * per_page

    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS total FROM meetings")
            total_meetings = int((cur.fetchone() or {}).get("total") or 0)
            cur.execute("SELECT * FROM meetings ORDER BY id DESC LIMIT %s OFFSET %s", (per_page, offset))
            rows = cur.fetchall()

    body = render_template_string(
        """
        <div class="hero">
            <h2>Meetings</h2>
            <div class="muted" style="color:#cbd5e1">View meeting summaries and download meeting-level reports.</div>
        </div>

        <div class='card'>
            <div class="table-wrap">
                <table>
                    <tr>
                        <th>Date</th>
                        <th>Topic</th>
                        <th>Status</th>
                        <th>Participants</th>
                        <th>Members</th>
                        <th>Unknown</th>
                        <th>Reports</th>
                    </tr>
                    {% for m in rows %}
                    <tr>
                        <td>{{ fmt_dt(m.start_time) }}</td>
                        <td>{{ m.topic or 'Untitled Meeting' }}</td>
                        <td>{{ m.status or '-' }}</td>
                        <td>{{ m.unique_participants or 0 }}</td>
                        <td>{{ m.member_participants or 0 }}</td>
                        <td>{{ m.unknown_participants or 0 }}</td>
                        <td>
                            {% if m.meeting_uuid %}
                                <div class="row">
                                    <a class='btn success small' href='{{ url_for("meeting_csv", meeting_uuid=m.meeting_uuid) }}'>CSV</a>
                                    <a class='btn purple small' href='{{ url_for("meeting_excel", meeting_uuid=m.meeting_uuid) }}'>Excel</a>
                                    <a class='btn secondary small' href='{{ url_for("meeting_pdf", meeting_uuid=m.meeting_uuid) }}'>PDF</a>
                                    {% if session.get('role') == 'admin' %}
                                        <a class='btn warning small' href='{{ url_for("send_meeting_smart_report", meeting_uuid=m.meeting_uuid) }}'>Send</a>
                                    {% endif %}
                                    {% if session.get('role') == 'admin' %}
                                        <form method='post' action='{{ url_for("delete_meeting", meeting_uuid=m.meeting_uuid) }}' onsubmit='return confirm("Delete this meeting and its attendance records?")'>
                                            <button type='submit' class='btn danger small'>Delete</button>
                                        </form>
                                    {% endif %}
                                    {% if session.get('role') == 'admin' and m.status == 'live' %}
                                        <a class='btn danger small' href='{{ url_for("manual_finalize_meeting", meeting_uuid=m.meeting_uuid) }}'>Finalize</a>
                                    {% endif %}
                                </div>
                            {% else %}
                                <span class='badge danger'>No UUID / old record</span>
                            {% endif %}
                        </td>
                    </tr>
                    {% endfor %}
                </table>
            </div>
        </div>
        """,
        rows=rows,
        fmt_dt=fmt_dt,
        fmt_time_ampm=fmt_time_ampm,
        member_display_name=member_display_name,
        session=session,
    )
    return page("Meetings", body, "meetings")



# ---- meeting_csv ----
@app.route("/meetings/<path:meeting_uuid>/report.csv")
@login_required
def meeting_csv(meeting_uuid):
    if not meeting_uuid:
        flash("Meeting UUID missing for this record.", "error")
        return redirect(url_for("meetings"))

    data = analytics_data({"meeting_uuid": meeting_uuid, "period_mode": "custom"})
    content = export_csv_bytes(data["rows"])
    return Response(
        content,
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={slugify(meeting_uuid)}.csv"},
    )





# ---- meeting_excel ----
@app.route("/meetings/<path:meeting_uuid>/report.xlsx")
@login_required
def meeting_excel(meeting_uuid):
    if not meeting_uuid:
        flash("Meeting UUID missing for this record.", "error")
        return redirect(url_for("meetings"))

    report_data = build_meeting_report_data(meeting_uuid)
    if not report_data:
        flash("Meeting report data not found.", "error")
        return redirect(url_for("meetings"))

    content = export_meeting_excel_bytes(report_data)
    filename = slugify(build_meeting_pdf_filename(report_data).replace(".pdf", "")) + ".xlsx"
    return Response(
        content,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


# ---- meeting_pdf ----
@app.route("/meetings/<path:meeting_uuid>/report.pdf")
@login_required
def meeting_pdf(meeting_uuid):
    if not meeting_uuid:
        flash("Meeting UUID missing for this record.", "error")
        return redirect(url_for("meetings"))

    report_data = build_meeting_report_data(meeting_uuid)
    if not report_data:
        flash("Meeting report data not found.", "error")
        return redirect(url_for("meetings"))

    pdf = export_meeting_pdf_bytes("Attendance Report", report_data)
    pdf_filename = build_meeting_pdf_filename(report_data)
    return send_file(
        io.BytesIO(pdf),
        download_name=pdf_filename,
        mimetype="application/pdf",
        as_attachment=True,
    )



# ---- send_meeting_smart_report ----
@app.route("/meetings/<path:meeting_uuid>/send-smart-report", methods=["POST", "GET"])
@login_required
@admin_required
def send_meeting_smart_report(meeting_uuid):
    if not meeting_uuid:
        flash("Meeting UUID missing for this record.", "error")
        return redirect(url_for("meetings"))
    ok, message = auto_send_smart_meeting_report(meeting_uuid, force=True)
    flash(("Smart report sent successfully." if ok else f"Smart report not sent: {message}"), "success" if ok else "error")
    return redirect(url_for("meetings"))


