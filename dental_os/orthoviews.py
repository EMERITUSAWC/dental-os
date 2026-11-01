"""Orthodontics screens (version 0.26): the ortho board, and a patient's case, ortho chart and contract."""
from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from .auth import clinical_required
from .db import get_db
from .services import ortho as O

bp = Blueprint("ortho", __name__)


@bp.get("/ortho")
@clinical_required
def board():
    db = get_db()
    O.post_due(db, g.clinic, g.user["id"])
    return render_template("ortho_board.html", rows=O.board(db, g.clinic), statuses=O.STATUSES, appliances=O.APPLIANCES,
                           today=O._today(g.clinic))


@bp.route("/patients/<int:pid>/ortho", methods=["GET", "POST"])
@clinical_required
def patient(pid):
    db = get_db()
    cid = g.clinic["id"]
    p = db.execute("SELECT * FROM patients WHERE id = ? AND clinic_id = ?", (pid, cid)).fetchone()
    if not p:
        abort(404)
    if request.method == "POST":
        f = request.form
        what = f.get("what")
        case_id = f.get("case_id", type=int)
        if case_id and not db.execute("SELECT 1 FROM ortho_cases WHERE id = ? AND clinic_id = ? AND patient_id = ?", (case_id, cid, pid)).fetchone():
            abort(404)
        try:
            if what == "open":
                O.open_case(db, g.clinic, g.user["id"], pid, f.get("provider_id", type=int), f)
                flash("Ortho case opened.", "ok")
            elif what == "update":
                O.update_case(db, g.clinic, g.user["id"], case_id, f)
                flash("Saved.", "ok")
            elif what == "start":
                O.start(db, g.clinic, g.user["id"], case_id, f.get("started_on"))
                flash("Treatment started.", "ok")
            elif what == "visit":
                O.add_visit(db, g.clinic, g.user["id"], case_id, f)
                flash("Visit added to the ortho chart.", "ok")
            elif what == "contract":
                O.make_contract(db, g.clinic, g.user["id"], case_id, f.get("total"), f.get("deposit"), f.get("months"), f.get("first_due"))
                flash("Contract saved. Each amount goes on the account on its due date.", "ok")
            elif what == "stop_contract":
                O.stop_contract(db, g.clinic, g.user["id"], case_id, f.get("reason"))
                flash("Contract stopped. Amounts not yet due will not be charged.", "ok")
            elif what == "debond":
                O.debond(db, g.clinic, g.user["id"], case_id, f.get("retention"), f.get("debonded_on"))
                flash("Braces off. The case is now in retention.", "ok")
            elif what == "close":
                O.close(db, g.clinic, g.user["id"], case_id, f.get("outcome"), f.get("reason"))
                flash("Case closed.", "ok")
        except O.OrthoError as exc:
            db.rollback()
            flash(str(exc), "error")
        return redirect(url_for("ortho.patient", pid=pid))
    O.post_due(db, g.clinic, g.user["id"])
    cases = []
    today = O._today(g.clinic)
    for c in O.for_patient(db, cid, pid):
        k = O.contract_for(db, cid, c["id"])
        cases.append({"c": c, "visits": O.visits(db, cid, c["id"]), "contract": k, "charges": O.charges(db, cid, k["id"]) if k else [],
                      "months": O.months_in(c, today), "next_due": O.next_due(db, c)})
    providers = db.execute("SELECT id, name FROM providers WHERE clinic_id = ? AND active = 1 ORDER BY name", (cid,)).fetchall()
    visits_today = db.execute("SELECT id, start_at FROM appointments WHERE clinic_id = ? AND patient_id = ? AND substr(start_at, 1, 10) = ?",
                              (cid, pid, today.isoformat())).fetchall()
    return render_template("ortho_patient.html", p=p, cases=cases, providers=providers, appliances=O.APPLIANCES, arches=O.ARCHES,
                           molar=O.MOLAR, crowding=O.CROWDING, statuses=O.STATUSES, today=today.isoformat(), visits_today=visits_today,
                           next_month=O.add_months(today, 1).isoformat())
