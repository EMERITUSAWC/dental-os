"""Fee schedule screens (version 0.39). Managers set up price lists and put patients on them."""
from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from .auth import manager_required
from .db import get_db
from .services import feesched as FS, money as M

bp = Blueprint("fees", __name__)


@bp.app_template_global()
def fee_schedule_name(sid):
    if not sid or not g.get("clinic"):
        return None
    s = FS.get(get_db(), g.clinic["id"], sid)
    return s["name"] if s else None


@bp.route("/fees/schedules", methods=["GET", "POST"])
@manager_required
def schedules():
    db = get_db()
    if request.method == "POST":
        try:
            sid = FS.save(db, g.clinic, g.user["id"], request.form.get("name"), request.form.get("percent_off"), request.form.get("notes"))
            db.commit()
            flash("Fee schedule saved.", "ok")
            return redirect(url_for("fees.schedule", sid=sid))
        except FS.FeeError as exc:
            db.rollback()
            flash(str(exc), "error")
    return render_template("fees_list.html", rows=FS.schedules(db, g.clinic["id"]))


@bp.route("/fees/schedules/<int:sid>", methods=["GET", "POST"])
@manager_required
def schedule(sid):
    db = get_db()
    s = FS.get(db, g.clinic["id"], sid)
    if not s:
        abort(404)
    if request.method == "POST":
        try:
            if request.form.get("what") == "prices":
                prices = {}
                for key, raw in request.form.items():
                    if key.startswith("fee_"):
                        prices[int(key[4:])] = M.parse_money(raw, g.clinic["currency"], allow_zero=True) if raw.strip() else None
                FS.set_prices(db, g.clinic, g.user["id"], sid, prices)
                flash("Prices saved.", "ok")
            else:
                FS.save(db, g.clinic, g.user["id"], request.form.get("name"), request.form.get("percent_off"), request.form.get("notes"), sid,
                        request.form.get("active", "1") == "1")
                flash("Fee schedule saved.", "ok")
            db.commit()
        except (FS.FeeError, ValueError) as exc:
            db.rollback()
            flash(str(exc), "error")
        return redirect(url_for("fees.schedule", sid=sid))
    return render_template("fees_schedule.html", s=s, rows=FS.table(db, g.clinic["id"], sid))


@bp.route("/patients/<int:pid>/prices", methods=["GET", "POST"])
@manager_required
def patient(pid):
    db = get_db()
    p = db.execute("SELECT * FROM patients WHERE id = ? AND clinic_id = ?", (pid, g.clinic["id"])).fetchone()
    if not p:
        abort(404)
    if request.method == "POST":
        try:
            FS.assign(db, g.clinic, g.user["id"], pid, request.form.get("schedule_id", type=int))
            db.commit()
            flash("Price list changed. Treatment planned from now on uses it; prices already planned stay as they are.", "ok")
            return redirect(url_for("views.patient_view", pid=pid))
        except FS.FeeError as exc:
            db.rollback()
            flash(str(exc), "error")
    return render_template("fees_patient.html", p=p, rows=FS.schedules(db, g.clinic["id"], True))
