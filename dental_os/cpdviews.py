"""Staff registration, indemnity and CPD screens (version 0.38). Everyone keeps their own CPD; managers see the whole team."""
import datetime

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from .auth import manager_required
from .db import get_db
from .services import staffcpd as SC

bp = Blueprint("cpd", __name__)


def _labels():
    return dict(titles=SC.TITLES, schemes=SC.SCHEMES, outcomes=SC.OUTCOMES, bodies=SC.BODIES, gdc_hours=SC.GDC_HOURS)


def _page(uid, template, back):
    db = get_db()
    cid = g.clinic["id"]
    person = db.execute("SELECT id, display_name, role FROM users WHERE id = ? AND clinic_id = ?", (uid, cid)).fetchone()
    if not person:
        abort(404)
    if request.method == "POST":
        f = request.form
        try:
            if f.get("what") == "registration":
                if g.user["role"] != "manager":
                    abort(403)
                SC.save_registration(db, g.clinic, g.user["id"], uid, f)
                flash("Registration saved.", "ok")
            elif f.get("what") == "strike":
                SC.strike(db, g.clinic, g.user["id"], f.get("record_id", type=int), f.get("reason"), None if g.user["role"] == "manager" else uid)
                flash("Entry struck out.", "ok")
            else:
                SC.add_cpd(db, g.clinic, g.user["id"], uid, f)
                flash("CPD added.", "ok")
            db.commit()
            return redirect(back)
        except SC.CpdError as exc:
            db.rollback()
            flash(str(exc), "error")
    return render_template(template, person=person, reg=SC.registration(db, cid, uid), rows=SC.records(db, cid, uid),
                           progress=SC.progress(db, cid, uid), today=datetime.date.today().isoformat(), **_labels())


@bp.route("/cpd/mine", methods=["GET", "POST"])
def mine():
    return _page(g.user["id"], "cpd_person.html", url_for("cpd.mine"))


@bp.get("/cpd")
@manager_required
def team():
    return render_template("cpd_team.html", rows=SC.team(get_db(), g.clinic["id"]), **_labels())


@bp.route("/cpd/people/<int:uid>", methods=["GET", "POST"])
@manager_required
def person(uid):
    return _page(uid, "cpd_person.html", url_for("cpd.person", uid=uid))
