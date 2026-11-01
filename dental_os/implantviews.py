"""The implant and materials register screens (version 0.35)."""
from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from .auth import clinical_required
from .db import get_db
from .services import booking, i18n, implants as IM, teeth

bp = Blueprint("implants", __name__)


def _patient(db, pid):
    p = db.execute("SELECT * FROM patients WHERE id = ? AND clinic_id = ?", (pid, g.clinic["id"])).fetchone()
    if not p:
        abort(404)
    return p


@bp.route("/patients/<int:pid>/implants", methods=["GET", "POST"])
@clinical_required
def patient(pid):
    db = get_db()
    p = _patient(db, pid)
    if request.method == "POST":
        try:
            if request.form.get("what") == "remove":
                IM.remove(db, g.clinic, g.user["id"], request.form.get("item_id", type=int), request.form.get("reason"),
                          request.form.get("removed_on"), request.form.get("note"))
                flash("Marked as removed. The line stays on the record.", "ok")
            else:
                IM.add(db, g.clinic, g.user["id"], pid, request.form)
                flash("Item added to the register.", "ok")
            db.commit()
            return redirect(url_for("implants.patient", pid=pid))
        except IM.ImplantError as exc:
            db.rollback()
            flash(str(exc), "error")
    return render_template("implants_patient.html", p=p, rows=IM.for_patient(db, g.clinic["id"], pid), kinds=IM.KINDS, reasons=IM.REASONS,
                           providers=booking.providers(db, g.clinic["id"]), tooth_help=teeth.HELP[teeth.current()])


@bp.get("/patients/<int:pid>/implants/card")
@clinical_required
def card(pid):
    db = get_db()
    p = _patient(db, pid)
    g.doc_lang = i18n.patient_lang(p, g.clinic)
    return render_template("implants_card.html", p=p, rows=IM.for_patient(db, g.clinic["id"], pid, True), kinds=IM.KINDS)


@bp.get("/implants")
@clinical_required
def search():
    db = get_db()
    q = {k: (request.args.get(k) or "").strip() for k in ("maker", "product", "lot", "placed_from", "placed_to")}
    rows = None
    if any(q.values()):
        try:
            rows = IM.find(db, g.clinic["id"], **q)
        except IM.ImplantError as exc:
            flash(str(exc), "error")
    return render_template("implants_search.html", q=q, rows=rows, kinds=IM.KINDS, recalls=IM.recalls(db, g.clinic["id"]))


@bp.post("/implants/recalls")
@clinical_required
def recall_new():
    db = get_db()
    try:
        rid, n = IM.open_recall(db, g.clinic, g.user["id"], request.form.get("maker"), request.form.get("lot"), request.form.get("reason"),
                                request.form.get("product"))
        db.commit()
        flash("Recall opened. Work through the patients below.", "ok")
        return redirect(url_for("implants.recall_view", rid=rid))
    except IM.ImplantError as exc:
        db.rollback()
        flash(str(exc), "error")
        return redirect(url_for("implants.search", maker=request.form.get("maker") or "", lot=request.form.get("lot") or ""))


@bp.route("/implants/recalls/<int:rid>", methods=["GET", "POST"])
@clinical_required
def recall_view(rid):
    db = get_db()
    r = IM.recall(db, g.clinic["id"], rid)
    if not r:
        abort(404)
    if request.method == "POST":
        try:
            if request.form.get("what") == "close":
                IM.close_recall(db, g.clinic, g.user["id"], rid)
                flash("Recall closed.", "ok")
            else:
                IM.mark_contacted(db, g.clinic, g.user["id"], rid, request.form.get("row_id", type=int), request.form.get("note"))
                flash("Marked as contacted.", "ok")
            db.commit()
        except IM.ImplantError as exc:
            db.rollback()
            flash(str(exc), "error")
        return redirect(url_for("implants.recall_view", rid=rid))
    return render_template("implants_recall.html", r=r, rows=IM.recall_patients(db, g.clinic["id"], rid))
