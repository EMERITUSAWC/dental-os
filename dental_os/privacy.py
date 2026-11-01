"""Privacy screens for managers: requests from people about their data, incidents, records kept too long, and for one patient a
full copy of their record, anonymising and the list of who looked."""
from pathlib import Path

from flask import (Blueprint, Response, abort, current_app, flash, g, redirect, render_template, request, url_for)

from .auth import manager_required
from .db import get_db
from .services import audit, billing, patients as P, privacy as PV
from .services.timeutil import now_local

bp = Blueprint("privacy", __name__)


@bp.before_request
def _managers_only():
    if g.user and g.user["role"] != "manager":
        abort(403)


def _patient_or_404(pid):
    row = P.get(get_db(), g.clinic["id"], pid)
    if not row:
        abort(404)
    return row


def _today():
    return now_local(g.clinic["timezone"]).date()


@bp.get("/privacy")
def home():
    db = get_db()
    return render_template("privacy_home.html", counts=PV.counts(db, g.clinic))


# ---------------------------------------------------------------------------------------------------------- requests
@bp.route("/privacy/requests", methods=["GET", "POST"])
def requests_list():
    db = get_db()
    if request.method == "POST":
        try:
            rid = PV.add_request(db, g.clinic, g.user["id"], request.form.get("patient_id", type=int), request.form.get("requester"),
                                 request.form.get("kind"), request.form.get("received_on"), request.form.get("due_on"),
                                 request.form.get("details"), request.form.get("identity_checked") == "1")
            flash("Request recorded.", "ok")
            return redirect(url_for("privacy.request_view", rid=rid))
        except PV.PrivacyError as exc:
            db.rollback()
            flash(str(exc), "error")
        return redirect(url_for("privacy.requests_list"))
    return render_template("privacy_requests.html", rows=PV.list_requests(db, g.clinic), kinds=PV.REQUEST_KINDS, today=_today(),
                           reply_days=PV.DEFAULT_REPLY_DAYS, patient_id=request.args.get("patient_id", type=int))


@bp.route("/privacy/requests/<int:rid>", methods=["GET", "POST"])
def request_view(rid):
    db = get_db()
    row = PV.get_request(db, g.clinic["id"], rid)
    if not row:
        abort(404)
    if request.method == "POST":
        action = request.form.get("action")
        try:
            if action == "identity":
                PV.set_identity_checked(db, g.clinic, g.user["id"], rid, request.form.get("value") == "1")
            elif action in ("done", "refused"):
                PV.close_request(db, g.clinic, g.user["id"], rid, action, request.form.get("outcome"))
                flash("Request closed.", "ok")
            elif action == "reopen":
                PV.reopen_request(db, g.clinic, g.user["id"], rid)
            else:
                abort(400)
        except PV.PrivacyError as exc:
            db.rollback()
            flash(str(exc), "error")
        return redirect(url_for("privacy.request_view", rid=rid))
    return render_template("privacy_request.html", r=row, kinds=PV.REQUEST_KINDS, today=_today())


# --------------------------------------------------------------------------------------------------------- incidents
@bp.route("/privacy/incidents", methods=["GET", "POST"])
def incidents_list():
    db = get_db()
    if request.method == "POST":
        try:
            iid = PV.add_incident(db, g.clinic, g.user["id"], request.form.get("title"), request.form.get("what_happened"),
                                  request.form.get("data_involved"), request.form.get("occurred_on"), request.form.get("found_at"),
                                  request.form.get("people_affected"), request.form.get("risk") or "low")
            flash("Incident recorded. Check the reporting clock on it.", "ok")
            return redirect(url_for("privacy.incident_view", iid=iid))
        except PV.PrivacyError as exc:
            db.rollback()
            flash(str(exc), "error")
        return redirect(url_for("privacy.incidents_list"))
    return render_template("privacy_incidents.html", rows=PV.list_incidents(db, g.clinic), hours=PV.BREACH_CLOCK_HOURS,
                           now=now_local(g.clinic["timezone"]).strftime("%Y-%m-%d %H:%M"))


@bp.route("/privacy/incidents/<int:iid>", methods=["GET", "POST"])
def incident_view(iid):
    db = get_db()
    row = PV.get_incident(db, g.clinic["id"], iid)
    if not row:
        abort(404)
    if request.method == "POST":
        action = request.form.get("action")
        try:
            if action == "update":
                f = request.form
                PV.update_incident(db, g.clinic, g.user["id"], iid, f.get("risk"), f.get("people_affected"), f.get("regulator_told") == "1",
                                   f.get("regulator_on"), f.get("patients_told") == "1", f.get("patients_on"), f.get("actions"))
                flash("Saved.", "ok")
            elif action == "close":
                PV.close_incident(db, g.clinic, g.user["id"], iid, True)
                flash("Incident closed.", "ok")
            elif action == "reopen":
                PV.close_incident(db, g.clinic, g.user["id"], iid, False)
            else:
                abort(400)
        except PV.PrivacyError as exc:
            db.rollback()
            flash(str(exc), "error")
        return redirect(url_for("privacy.incident_view", iid=iid))
    return render_template("privacy_incident.html", i=PV.incident_view(g.clinic, row), hours=PV.BREACH_CLOCK_HOURS)


# -------------------------------------------------------------------------------------------------------- retention
@bp.route("/privacy/retention", methods=["GET", "POST"])
def retention():
    db = get_db()
    if request.method == "POST":
        try:
            PV.set_retention(db, g.clinic, g.user["id"], request.form.get("years"))
            flash("Saved.", "ok")
        except PV.PrivacyError as exc:
            db.rollback()
            flash(str(exc), "error")
        return redirect(url_for("privacy.retention"))
    clinic = db.execute("SELECT * FROM clinics WHERE id = ?", (g.clinic["id"],)).fetchone()
    rows, cutoff = PV.retention_candidates(db, clinic)
    return render_template("privacy_retention.html", rows=rows, cutoff=cutoff, years=clinic["retention_years"])


# ---------------------------------------------------------------------------------------------------------- one patient
@bp.get("/patients/<int:pid>/privacy")
def patient_privacy(pid):
    db = get_db()
    patient = _patient_or_404(pid)
    open_reqs = [r for r in PV.list_requests(db, g.clinic, "open") if r["patient_id"] == pid]
    return render_template("privacy_patient.html", p=patient, why=PV.anonymise_check(db, g.clinic, pid), word=PV.ANONYMISE_WORD,
                           log=PV.access_log(db, g.clinic["id"], pid), requests=open_reqs, kinds=PV.REQUEST_KINDS)


@bp.get("/patients/<int:pid>/record-copy")
def record_copy(pid):
    db = get_db()
    patient = _patient_or_404(pid)
    data = PV.record_data(db, g.clinic["id"], pid)
    audit.log(db, g.clinic["id"], g.user["id"], "record.copy.view", "patient", pid)
    return render_template("record_copy.html", p=patient, data=data, sections=PV.SECTIONS, bal=billing.totals(db, g.clinic["id"], pid),
                           day=_today())


@bp.post("/patients/<int:pid>/record-copy.zip")
def record_copy_zip(pid):
    db = get_db()
    patient = _patient_or_404(pid)
    root = Path(current_app.config.get("FILES_PATH") or Path(current_app.instance_path) / "files")
    blob, notes = PV.record_zip(db, g.clinic, pid, root)
    audit.log(db, g.clinic["id"], g.user["id"], "record.copy.download", "patient", pid)
    name = f"record-copy-{pid}-{_today().isoformat()}.zip"
    return Response(blob, mimetype="application/zip", headers={"Content-Disposition": f'attachment; filename="{name}"'})


@bp.post("/patients/<int:pid>/anonymise")
def patient_anonymise(pid):
    db = get_db()
    _patient_or_404(pid)
    try:
        PV.anonymise(db, g.clinic, g.user["id"], pid, request.form.get("reason"), request.form.get("confirm"),
                     request.form.get("request_id", type=int))
    except PV.PrivacyError as exc:
        db.rollback()
        flash(str(exc), "error")
        return redirect(url_for("privacy.patient_privacy", pid=pid))
    flash("Done. The person's name, contact details and notes are removed. Treatment and money records are kept.", "ok")
    return redirect(url_for("views.patient_view", pid=pid))
