"""The clinical record screens: medical history and alerts, tooth chart, gum charting, notes, consents, files,
and the manager's data tools."""
from pathlib import Path

from flask import (Blueprint, Response, abort, current_app, flash, g, redirect, render_template, request,
                   send_file, url_for)

from .auth import clinical_required, manager_required, signer_required
from .db import get_db
from .services import ainotes, audit, chart, consent, dataio, files, medical, notes, perio, permissions
from .services.timeutil import now_local
from .views import _document_for, _patient_or_404, _today

bp = Blueprint("record", __name__)


def _files_root():
    return Path(current_app.config.get("FILES_PATH") or Path(current_app.instance_path) / "files")


def _back(pid, endpoint, **kw):
    return redirect(url_for(endpoint, pid=pid, **kw))


# ----------------------------------------------------------------------------- medical history and alerts
@bp.route("/patients/<int:pid>/medical", methods=["GET", "POST"])
@clinical_required
def medical_view(pid):
    db, cid = get_db(), g.clinic["id"]
    patient = _patient_or_404(pid)
    if request.method == "POST":
        try:
            if request.form.get("action") == "alert":
                medical.add_alert(db, g.clinic, g.user["id"], pid, request.form.get("kind"), request.form.get("severity"),
                                  request.form.get("text"))
                flash("Alert added.", "ok")
            else:
                medical.save_history(db, g.clinic, g.user["id"], pid, request.form, _today().isoformat())
                flash("Medical history saved.", "ok")
        except medical.RecordError as exc:
            db.rollback()
            flash(str(exc), "error")
        return _back(pid, "record.medical_view")
    audit.log(db, cid, g.user["id"], "medical.view", "patient", pid)
    resolved = db.execute("SELECT * FROM patient_alerts WHERE clinic_id=? AND patient_id=? AND active=0 "
                          "ORDER BY id DESC LIMIT 10", (cid, pid)).fetchall()
    return render_template("medical.html", p=patient, h=medical.get_history(db, cid, pid),
                           alerts=medical.active_alerts(db, cid, pid), resolved=resolved,
                           kinds=medical.KINDS, severities=medical.SEVERITIES, smoker=medical.SMOKER)


@bp.post("/alerts/<int:alert_id>/resolve")
@clinical_required
def alert_resolve(alert_id):
    db = get_db()
    try:
        pid = medical.resolve_alert(db, g.clinic, g.user["id"], alert_id)
    except medical.RecordError as exc:
        abort(404, str(exc))
    flash("Alert resolved.", "ok")
    return _back(pid, "record.medical_view")


# ----------------------------------------------------------------------------- tooth chart
def _arch(teeth, data):
    out = []
    for n in teeth:
        entry = data.get(n)
        surf, whole = chart.surface_states(entry)
        out.append({"n": n, "side": chart.side_of(n), "surf": surf, "whole": whole, "entry": entry})
    return out


@bp.get("/patients/<int:pid>/chart")
@clinical_required
def chart_view(pid):
    db, cid = get_db(), g.clinic["id"]
    patient = _patient_or_404(pid)
    child = request.args.get("dentition") == "child"
    data = chart.chart_data(db, cid, pid)
    upper, lower = (chart.CHILD_UPPER, chart.CHILD_LOWER) if child else (chart.ADULT_UPPER, chart.ADULT_LOWER)
    selected = request.args.get("tooth", type=int)
    if selected not in upper + lower:
        selected = None
    audit.log(db, cid, g.user["id"], "chart.view", "patient", pid)
    return render_template("chart.html", p=patient, upper=_arch(upper, data), lower=_arch(lower, data), child=child,
                           selected=selected, entry=data.get(selected) if selected else None,
                           conditions=chart.CONDITIONS, whole=chart.WHOLE_TOOTH, surfaces="MODBLI")


@bp.post("/patients/<int:pid>/chart/conditions")
@clinical_required
def condition_add(pid):
    db = get_db()
    _patient_or_404(pid)
    tooth = request.form.get("tooth")
    try:
        chart.add_condition(db, g.clinic, g.user["id"], pid, tooth, request.form.getlist("surfaces"),
                            request.form.get("condition"), request.form.get("note"))
        flash("Added to the chart.", "ok")
    except chart.ChartError as exc:
        db.rollback()
        flash(str(exc), "error")
    return _back(pid, "record.chart_view", tooth=_fdi(tooth), dentition="child" if request.form.get("child") else None)


def _fdi(typed):
    """The chart's own links use FDI numbers. A tooth that came back from a form is in the clinic's numbering."""
    from .services import teeth
    return teeth.parse_or_none(typed)


@bp.post("/chart/conditions/<int:cond_id>/remove")
@clinical_required
def condition_remove(cond_id):
    db = get_db()
    try:
        pid = chart.remove_condition(db, g.clinic, g.user["id"], cond_id)
    except chart.ChartError as exc:
        abort(404, str(exc))
    flash("Removed from the chart. The history is kept.", "ok")
    return _back(pid, "record.chart_view", tooth=_fdi(request.form.get("tooth")),
                 dentition="child" if request.form.get("child") else None)


# ----------------------------------------------------------------------------- gum charting
@bp.route("/patients/<int:pid>/perio/new", methods=["GET", "POST"])
@clinical_required
def perio_new(pid):
    db, cid = get_db(), g.clinic["id"]
    patient = _patient_or_404(pid)
    missing = perio.missing_teeth(db, cid, pid)
    if request.method == "POST":
        try:
            rows = perio.parse_form(request.form, skip_teeth=missing)
            chart_id = perio.create_chart(db, g.clinic, g.user["id"], pid, rows, request.form.get("notes"))
            flash("Gum chart saved.", "ok")
            return redirect(url_for("record.perio_show", chart_id=chart_id))
        except perio.PerioError as exc:
            db.rollback()
            flash(str(exc), "error")
    return render_template("perio_form.html", p=patient, missing=missing, upper=perio.UPPER, lower=perio.LOWER,
                           order=perio.site_order, form=request.form, voice_on=bool(g.clinic["voice_perio"]),
                           voice_cloud=bool(g.clinic["voice_cloud"]))


@bp.get("/patients/<int:pid>/perio")
@clinical_required
def perio_list(pid):
    db, cid = get_db(), g.clinic["id"]
    patient = _patient_or_404(pid)
    charts = perio.list_charts(db, cid, pid)
    out = []
    for c in charts:
        out.append({"chart": c, "sum": perio.summary(perio.readings(db, cid, c["id"]))})
    return render_template("perio_list.html", p=patient, items=out)


@bp.get("/perio/<int:chart_id>")
@clinical_required
def perio_show(chart_id):
    db, cid = get_db(), g.clinic["id"]
    ch = perio.get_chart(db, cid, chart_id)
    if not ch:
        abort(404)
    patient = _patient_or_404(ch["patient_id"])
    rows = perio.readings(db, cid, chart_id)
    new = perio.summary(rows)
    older = [c for c in perio.list_charts(db, cid, ch["patient_id"]) if (c["charted_on"], c["id"]) < (ch["charted_on"], ch["id"])]
    change = perio.compare(new, perio.summary(perio.readings(db, cid, older[0]["id"]))) if older else None
    audit.log(db, cid, g.user["id"], "perio.view", "patient", ch["patient_id"])
    return render_template("perio_show.html", p=patient, chart=ch, rows=rows, sum=new, change=change,
                           upper=perio.UPPER, lower=perio.LOWER, order=perio.site_order,
                           missing=perio.missing_teeth(db, cid, ch["patient_id"]))


# ----------------------------------------------------------------------------- notes
@bp.get("/patients/<int:pid>/notes")
@clinical_required
def notes_list(pid):
    db, cid = get_db(), g.clinic["id"]
    patient = _patient_or_404(pid)
    audit.log(db, cid, g.user["id"], "notes.view", "patient", pid)
    return render_template("notes.html", p=patient, items=notes.list_for_patient(db, cid, pid), templates=notes.TEMPLATES,
                           can_sign=permissions.can_sign(g.user))


@bp.route("/patients/<int:pid>/notes/new", methods=["GET", "POST"])
@clinical_required
def note_new(pid):
    db, cid = get_db(), g.clinic["id"]
    patient = _patient_or_404(pid)
    key = request.values.get("template", "exam")
    if key not in notes.TEMPLATES:
        key = "exam"
    if request.method == "POST":
        try:
            nid = notes.create_draft(db, g.clinic, g.user, pid, key, request.form, request.form.get("tooth"),
                                     request.form.get("appointment_id", type=int))
            flash("Draft saved. A dentist signs it to lock it.", "ok")
            return _back(pid, "record.notes_list")
        except notes.NoteError as exc:
            db.rollback()
            flash(str(exc), "error")
    from .services import teeth
    tooth = request.form.get("tooth", "") if request.method == "POST" else teeth.show(request.args.get("tooth", type=int))
    return _note_form(db, cid, patient, key, request.form, tooth)


def _note_form(db, cid, patient, key, values, tooth, ai_draft=False, shorthand=""):
    visits = db.execute("SELECT id, start_at, category FROM appointments WHERE clinic_id=? AND patient_id=? "
                        "AND status IN ('scheduled','complete') ORDER BY start_at DESC LIMIT 10", (cid, patient["id"])).fetchall()
    return render_template("note_form.html", p=patient, key=key, templates=notes.TEMPLATES, values=values,
                           note=None, visits=visits, tooth=tooth, ai_on=ainotes.is_on(db, cid, current_app.config["SECRET_KEY"]),
                           ai_draft=ai_draft, shorthand=shorthand, ai_max=ainotes.MAX_INPUT)


@bp.route("/patients/<int:pid>/notes/ai-draft", methods=["POST"])
@clinical_required
def note_ai(pid):
    """Ask the AI for a draft. Nothing is saved: the boxes come back filled in for the dentist to read, change and save."""
    db, cid = get_db(), g.clinic["id"]
    patient = _patient_or_404(pid)
    key = request.form.get("template", "exam")
    if key not in notes.TEMPLATES:
        key = "exam"
    shorthand = request.form.get("shorthand", "")
    tooth = request.form.get("tooth", "")
    try:
        values = ainotes.draft(db, g.clinic, g.user, current_app.config["SECRET_KEY"], patient, key, shorthand)
        db.commit()                                                     # keep the record that a draft was asked for
        flash("This is an AI draft. Read every line and fix it before you save.", "ok")
        return _note_form(db, cid, patient, key, values, tooth, ai_draft=True, shorthand=shorthand)
    except ainotes.AIError as exc:
        db.commit()
        flash(str(exc), "error")
    return _note_form(db, cid, patient, key, {}, tooth, shorthand=shorthand)


@bp.route("/notes/<int:note_id>/edit", methods=["GET", "POST"])
@clinical_required
def note_edit(note_id):
    db, cid = get_db(), g.clinic["id"]
    note = notes.get(db, cid, note_id)
    if not note:
        abort(404)
    patient = _patient_or_404(note["patient_id"])
    if request.method == "POST":
        try:
            notes.update_draft(db, g.clinic, g.user, note_id, request.form, request.form.get("tooth"))
            flash("Draft updated.", "ok")
            return _back(patient["id"], "record.notes_list")
        except notes.NoteError as exc:
            db.rollback()
            flash(str(exc), "error")
    values = request.form if request.method == "POST" else notes.split_body(note["template"], note["body"])
    return render_template("note_form.html", p=patient, key=note["template"], templates=notes.TEMPLATES, values=values,
                           note=note, visits=[], tooth=(request.form.get("tooth", "") if request.method == "POST" else _show_tooth(note["tooth"])), ai_on=False)


def _show_tooth(fdi):
    from .services import teeth
    return teeth.show(fdi)


def _note_action(fn, ok_message):
    def view(note_id):
        db = get_db()
        try:
            pid = fn(db, g.clinic, g.user, note_id)
            flash(ok_message, "ok")
        except notes.NoteError as exc:
            db.rollback()
            flash(str(exc), "error")
            row = notes.get(db, g.clinic["id"], note_id)
            pid = row["patient_id"] if row else None
        if pid is None:
            abort(404)
        return _back(pid, "record.notes_list")
    return view


bp.add_url_rule("/notes/<int:note_id>/sign", "note_sign", signer_required(clinical_required(_note_action(notes.sign, "Signed and locked."))), methods=["POST"])
bp.add_url_rule("/notes/<int:note_id>/discard", "note_discard", clinical_required(_note_action(notes.discard, "Draft discarded.")), methods=["POST"])


@bp.post("/notes/<int:note_id>/addendum")
@clinical_required
def note_addendum(note_id):
    db = get_db()
    row = notes.get(db, g.clinic["id"], note_id)
    if not row:
        abort(404)
    try:
        notes.add_addendum(db, g.clinic, g.user, note_id, request.form.get("text"))
        flash("Addendum added.", "ok")
    except notes.NoteError as exc:
        db.rollback()
        flash(str(exc), "error")
    return _back(row["patient_id"], "record.notes_list")


# ----------------------------------------------------------------------------- consents
@bp.route("/patients/<int:pid>/consents", methods=["GET", "POST"])
@clinical_required
def consents_view(pid):
    db, cid = get_db(), g.clinic["id"]
    patient = _patient_or_404(pid)
    if request.method == "POST":
        try:
            consent.record(db, g.clinic, g.user, pid, request.form.get("template_id", type=int),
                           request.form.get("signer_name"), request.form.get("relation"), request.form.get("method"),
                           request.form.get("agreed"), request.form.get("note"))
            flash("Consent recorded.", "ok")
            return _back(pid, "record.consents_view")
        except consent.ConsentError as exc:
            db.rollback()
            flash(str(exc), "error")
    templates = db.execute("SELECT * FROM consent_templates WHERE clinic_id=? AND active=1 ORDER BY name", (cid,)).fetchall()
    return render_template("consents.html", p=patient, rows=consent.list_for_patient(db, cid, pid), templates=templates,
                           selected=request.args.get("template", type=int))


@bp.get("/consents/<int:consent_id>")
@clinical_required
def consent_show(consent_id):
    db = get_db()
    row = db.execute("SELECT c.*, u.display_name AS recorded_name FROM consents c LEFT JOIN users u ON u.id = c.recorded_by "
                     "WHERE c.id = ? AND c.clinic_id = ?", (consent_id, g.clinic["id"])).fetchone()
    if not row:
        abort(404)
    return render_template("consent_show.html", c=row, p=_document_for(row["patient_id"]))


@bp.post("/consents/<int:consent_id>/withdraw")
@clinical_required
def consent_withdraw(consent_id):
    db = get_db()
    row = db.execute("SELECT patient_id FROM consents WHERE id = ? AND clinic_id = ?", (consent_id, g.clinic["id"])).fetchone()
    if not row:
        abort(404)
    try:
        consent.withdraw(db, g.clinic, g.user, consent_id, request.form.get("reason"))
        flash("Consent marked as withdrawn.", "ok")
    except consent.ConsentError as exc:
        db.rollback()
        flash(str(exc), "error")
    return _back(row["patient_id"], "record.consents_view")


@bp.route("/consent-forms", methods=["GET", "POST"])
@manager_required
def consent_forms():
    db, cid = get_db(), g.clinic["id"]
    if request.method == "POST":
        try:
            if request.form.get("action") == "starter":
                n = consent.ensure_templates(db, cid)
                flash(f"Added {n} starter forms. Have your dentist and a lawyer review the wording before use.", "ok")
            else:
                consent.save_template(db, g.clinic, g.user["id"], request.form.get("id", type=int),
                                      request.form.get("name"), request.form.get("body"), bool(request.form.get("active")))
                flash("Saved.", "ok")
        except consent.ConsentError as exc:
            db.rollback()
            flash(str(exc), "error")
        return redirect(url_for("record.consent_forms"))
    rows = db.execute("SELECT * FROM consent_templates WHERE clinic_id=? ORDER BY name", (cid,)).fetchall()
    return render_template("consent_forms.html", rows=rows)


# ----------------------------------------------------------------------------- files
@bp.route("/patients/<int:pid>/files", methods=["GET", "POST"])
@clinical_required
def files_view(pid):
    db, cid = get_db(), g.clinic["id"]
    patient = _patient_or_404(pid)
    if request.method == "POST":
        try:
            files.save_upload(db, g.clinic, g.user, pid, request.files.get("file"), request.form.get("kind"),
                              request.form.get("note"), request.form.get("taken_on"), _files_root())
            flash("File added.", "ok")
        except files.FileError as exc:
            db.rollback()
            flash(str(exc), "error")
        return _back(pid, "record.files_view")
    from .services import imaging
    return render_template("files.html", p=patient, rows=files.list_for_patient(db, cid, pid), kinds=files.KINDS,
                           inline=files.INLINE, today=_today(), stations=imaging.stations(db, cid), sessions=imaging.for_patient(db, cid, pid))


@bp.get("/files/<int:file_id>")
@clinical_required
def file_get(file_id):
    db = get_db()
    row = files.get(db, g.clinic["id"], file_id)
    if not row:
        abort(404)
    path = files.path_for(_files_root(), row)
    if not path.exists():
        abort(404)
    audit.log(db, g.clinic["id"], g.user["id"], "file.view", "patient", row["patient_id"])
    inline = row["mime"] in files.INLINE
    resp = send_file(path, mimetype=row["mime"], as_attachment=not inline, download_name=row["original_name"])
    resp.headers["Content-Security-Policy"] = "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; sandbox"
    return resp


@bp.get("/files/<int:file_id>/preview.png")
@clinical_required
def file_preview(file_id):
    """A DICOM X-ray as a PNG, so the browser can show it. The original file is never changed."""
    from .services import dicom
    db = get_db()
    row = files.get(db, g.clinic["id"], file_id)
    if not row or row["mime"] != "application/dicom":
        abort(404)
    try:
        path = files.preview_png(_files_root(), row)
    except (dicom.DicomError, OSError) as exc:
        return Response(str(exc), status=415, mimetype="text/plain")
    resp = send_file(path, mimetype="image/png", max_age=0)
    resp.headers["Content-Security-Policy"] = "default-src 'none'; sandbox"
    return resp


@bp.post("/files/<int:file_id>/archive")
@clinical_required
def file_archive(file_id):
    db = get_db()
    try:
        pid = files.archive(db, g.clinic, g.user, file_id)
    except files.FileError as exc:
        abort(404, str(exc))
    flash("File hidden. It is kept and can be found by a manager.", "ok")
    return _back(pid, "record.files_view")


# ----------------------------------------------------------------------------- manager data tools
@bp.get("/tools")
@manager_required
def tools():
    return render_template("tools.html")


@bp.post("/tools/export")
@manager_required
def export_data():
    db = get_db()
    blob = dataio.export_zip(db, g.clinic["id"])
    audit.log(db, g.clinic["id"], g.user["id"], "data.export")
    stamp = now_local(g.clinic["timezone"]).strftime("%Y%m%d-%H%M")
    return Response(blob, mimetype="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="clinic-export-{stamp}.zip"'})


@bp.route("/tools/import", methods=["GET", "POST"])
@manager_required
def import_start():
    db = get_db()
    if request.method == "POST":
        upload = request.files.get("file")
        if not upload or not upload.filename:
            flash("Choose a CSV file.", "error")
        else:
            raw = upload.read(dataio.MAX_IMPORT_BYTES + 1)
            if len(raw) > dataio.MAX_IMPORT_BYTES:
                flash("That file is bigger than 1 MB. Split it into smaller files.", "error")
            else:
                try:
                    text = raw.decode("utf-8-sig")
                except UnicodeDecodeError:
                    text = raw.decode("latin-1")
                try:
                    batch = dataio.stage(db, g.clinic, g.user["id"], text)
                    return redirect(url_for("record.import_preview", batch_id=batch))
                except ValueError as exc:
                    db.rollback()
                    flash(str(exc), "error")
    return render_template("import.html", batch=None, payload=None)


@bp.get("/tools/import/<int:batch_id>")
@manager_required
def import_preview(batch_id):
    row, payload = dataio.get_batch(get_db(), g.clinic["id"], batch_id)
    if not row:
        abort(404)
    import json as _json
    counts = _json.loads(row["summary"])
    return render_template("import.html", batch=row, payload=payload, counts=counts)


@bp.post("/tools/import/<int:batch_id>/confirm")
@manager_required
def import_confirm(batch_id):
    db = get_db()
    try:
        added, skipped = dataio.commit(db, g.clinic, g.user["id"], batch_id)
        flash(f"Added {added} patients." + (f" Skipped {skipped} that already existed." if skipped else ""), "ok")
        return redirect(url_for("views.patients"))
    except ValueError as exc:
        db.rollback()
        flash(str(exc), "error")
        return redirect(url_for("record.import_start"))


@bp.post("/tools/import/<int:batch_id>/cancel")
@manager_required
def import_cancel(batch_id):
    db = get_db()
    try:
        dataio.cancel(db, g.clinic, g.user["id"], batch_id)
        flash("Import cancelled. Nothing was added.", "ok")
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(url_for("record.import_start"))
