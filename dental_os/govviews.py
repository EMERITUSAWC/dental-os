"""Complaints and patient safety incidents screens (version 0.37).

Anyone can write down a complaint or an incident. Managers handle complaints. Managers and dentists see and review incidents."""
import datetime

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from .db import get_db
from .services import governance as GV, staffcpd as SC

bp = Blueprint("gov", __name__)


def _need(*roles):
    if g.user["role"] not in roles:
        abort(403)


def _staff(db):
    return db.execute("SELECT id, display_name FROM users WHERE clinic_id = ? AND active = 1 ORDER BY display_name", (g.clinic["id"],)).fetchall()


def _labels():
    return dict(channels=GV.CHANNELS, categories=GV.CATEGORIES, outcomes=GV.OUTCOMES, c_statuses=GV.C_STATUSES, kinds=GV.KINDS, harm=GV.HARM,
                i_statuses=GV.I_STATUSES)


@bp.get("/governance")
def home():
    _need("manager", "dentist")
    db = get_db()
    cid = g.clinic["id"]
    end = datetime.date.today()
    start = end.replace(year=end.year - 1) if not (end.month == 2 and end.day == 29) else end.replace(year=end.year - 1, day=28)
    return render_template("gov_home.html", regs=SC.warnings(db, cid) if g.user["role"] == "manager" else [], late=GV.late(db, cid) if g.user["role"] == "manager" else [],
                           open_c=GV.complaints(db, cid, "not_closed") if g.user["role"] == "manager" else [],
                           open_i=GV.incidents(db, cid, "not_closed"), cs=GV.complaint_summary(db, cid, start.isoformat(), end.isoformat()),
                           isum=GV.incident_summary(db, cid, start.isoformat(), end.isoformat()), start=start.isoformat(), end=end.isoformat(), **_labels())


@bp.route("/governance/complaints/new", methods=["GET", "POST"])
def complaint_new():
    db = get_db()
    if request.method == "POST":
        try:
            kid = GV.add_complaint(db, g.clinic, g.user["id"], request.form)
            db.commit()
            flash("Complaint written down. The manager will acknowledge it.", "ok")
            if g.user["role"] == "manager":
                return redirect(url_for("gov.complaint_view", kid=kid))
            return redirect(url_for("views.dashboard"))
        except GV.GovError as exc:
            db.rollback()
            flash(str(exc), "error")
    pid = request.args.get("pid", type=int)
    p = db.execute("SELECT id, first_name, last_name FROM patients WHERE id = ? AND clinic_id = ?", (pid, g.clinic["id"])).fetchone() if pid else None
    return render_template("gov_complaint_new.html", p=p, staff=_staff(db), today=datetime.date.today().isoformat(), **_labels())


@bp.get("/governance/complaints")
def complaints_list():
    _need("manager")
    return render_template("gov_complaints.html", rows=GV.complaints(get_db(), g.clinic["id"], request.args.get("status")), **_labels())


@bp.route("/governance/complaints/<int:kid>", methods=["GET", "POST"])
def complaint_view(kid):
    _need("manager")
    db = get_db()
    k = GV.complaint(db, g.clinic["id"], kid)
    if not k:
        abort(404)
    if request.method == "POST":
        f = request.form
        what = f.get("what")
        try:
            if what == "ack":
                GV.acknowledge(db, g.clinic, g.user["id"], kid, f.get("on"), f.get("response_due"), f.get("note"))
                flash("Acknowledged.", "ok")
            elif what == "note":
                GV.add_note(db, g.clinic, g.user["id"], kid, f.get("note"))
                flash("Note added.", "ok")
            elif what == "respond":
                GV.respond(db, g.clinic, g.user["id"], kid, f.get("on"), f.get("outcome"), f.get("note"))
                flash("Answer recorded.", "ok")
            elif what == "close":
                GV.close(db, g.clinic, g.user["id"], kid, f.get("learning"), f.get("escalated_to"))
                flash("Complaint closed.", "ok")
            db.commit()
        except GV.GovError as exc:
            db.rollback()
            flash(str(exc), "error")
        return redirect(url_for("gov.complaint_view", kid=kid))
    today = datetime.date.today()
    return render_template("gov_complaint.html", k=k, steps=GV.steps(db, kid), today=today.isoformat(),
                           suggest_due=(today + datetime.timedelta(days=28)).isoformat(), **_labels())


@bp.route("/governance/incidents/new", methods=["GET", "POST"])
def incident_new():
    db = get_db()
    if request.method == "POST":
        try:
            iid = GV.add_incident(db, g.clinic, g.user["id"], request.form)
            db.commit()
            flash("Incident written down. Thank you for reporting it.", "ok")
            if g.user["role"] in ("manager", "dentist"):
                return redirect(url_for("gov.incident_view", iid=iid))
            return redirect(url_for("views.dashboard"))
        except GV.GovError as exc:
            db.rollback()
            flash(str(exc), "error")
    pid = request.args.get("pid", type=int)
    p = db.execute("SELECT id, first_name, last_name FROM patients WHERE id = ? AND clinic_id = ?", (pid, g.clinic["id"])).fetchone() if pid else None
    return render_template("gov_incident_new.html", p=p, today=datetime.date.today().isoformat(), **_labels())


@bp.get("/governance/incidents")
def incidents_list():
    _need("manager", "dentist")
    return render_template("gov_incidents.html", rows=GV.incidents(get_db(), g.clinic["id"], request.args.get("status")), **_labels())


@bp.route("/governance/incidents/<int:iid>", methods=["GET", "POST"])
def incident_view(iid):
    _need("manager", "dentist")
    db = get_db()
    i = GV.incident(db, g.clinic["id"], iid)
    if not i:
        abort(404)
    if request.method == "POST":
        f = request.form
        what = f.get("what")
        try:
            if what == "review":
                GV.review(db, g.clinic, g.user["id"], iid, f.get("cause"), f.get("actions"), f.get("harm"), f.get("reported_to"), f.get("report_ref"))
                flash("Review saved.", "ok")
            elif what == "candour":
                GV.candour(db, g.clinic, g.user["id"], iid, f.get("told_on"), f.get("told_by"), f.get("apology") == "1", f.get("written_on"), f.get("note"))
                flash("Duty of candour steps saved.", "ok")
            elif what == "close":
                if g.user["role"] != "manager":
                    abort(403)
                GV.close_incident(db, g.clinic, g.user["id"], iid)
                flash("Incident closed.", "ok")
            db.commit()
        except GV.GovError as exc:
            db.rollback()
            flash(str(exc), "error")
        return redirect(url_for("gov.incident_view", iid=iid))
    return render_template("gov_incident.html", i=i, candour=GV.candour_needed(i), today=datetime.date.today().isoformat(), **_labels())
