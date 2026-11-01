"""Version 0.61: chairside mode. A tablet at the chair shows the day's patients for that chair, and for the patient in the chair
the medical alerts first, then big buttons for the screens a dentist or nurse uses there. It uses the same screens and the same
rules as everywhere else: nothing here skips a check. The chair is remembered for this login only."""
from flask import Blueprint, abort, flash, g, redirect, render_template, request, session, url_for

from .auth import clinical_required
from .db import get_db
from .services import medical, scheduling as S
from .services.timeutil import now_local

bp = Blueprint("chair", __name__)
ORDER = ("in_chair", "waiting", "expected", "left")


def _chair(db):
    cid = session.get("chair_id")
    if not cid:
        return None
    return db.execute("SELECT * FROM chairs WHERE id = ? AND clinic_id = ? AND active = 1", (cid, g.clinic["id"])).fetchone()


@bp.get("/chair")
@clinical_required
def home():
    db = get_db()
    chair = _chair(db)
    chairs = db.execute("SELECT * FROM chairs WHERE clinic_id = ? AND active = 1 ORDER BY name", (g.clinic["id"],)).fetchall()
    if not chair or request.args.get("pick"):
        return render_template("chair_pick.html", chairs=chairs, current=chair)
    day = now_local(g.clinic["timezone"]).date()
    lanes = S.board(db, g.clinic, day)
    mine = {k: [x for x in lanes[k] if x["row"]["chair_id"] == chair["id"]] for k in ORDER}
    return render_template("chair_home.html", chair=chair, lanes=mine, order=ORDER, day=day)


@bp.post("/chair/pick")
@clinical_required
def pick():
    db = get_db()
    cid = request.form.get("chair_id", type=int)
    if not db.execute("SELECT 1 FROM chairs WHERE id = ? AND clinic_id = ? AND active = 1", (cid, g.clinic["id"])).fetchone():
        abort(404)
    session["chair_id"] = cid
    return redirect(url_for("chair.home"))


@bp.get("/chair/visit/<int:aid>")
@clinical_required
def visit(aid):
    db = get_db()
    a = S.get_appointment(db, g.clinic["id"], aid)
    if not a or a["status"] not in ("scheduled", "complete"):
        abort(404)
    return render_template("chair_visit.html", a=a, stage=S.stage(a), alerts=medical.active_alerts(db, g.clinic["id"], a["patient_id"]))


@bp.post("/chair/visit/<int:aid>/stage")
@clinical_required
def stage(aid):
    db = get_db()
    try:
        S.move_stage(db, g.clinic, g.user["id"], aid, request.form.get("action", ""))
    except LookupError:
        abort(404)
    except S.SchedulingError as exc:
        db.rollback()
        flash(str(exc), "error")
    if request.form.get("action") == "leave":
        return redirect(url_for("chair.home"))
    return redirect(url_for("chair.visit", aid=aid))
