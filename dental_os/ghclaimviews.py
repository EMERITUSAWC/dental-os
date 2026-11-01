"""Diagnoses, what a payer covers, monthly claim batches and the printed claim form (version 0.52)."""
import datetime

from flask import Blueprint, Response, abort, flash, g, redirect, render_template, request, url_for

from .auth import manager_required
from .db import get_db
from .services import ghclaims as GC, insurance as INS

bp = Blueprint("ghclaims", __name__)


@bp.post("/procedures/<int:proc_id>/dx")
def set_dx(proc_id):
    if g.user["role"] == "front_desk":
        abort(403)
    db = get_db()
    proc = db.execute("SELECT patient_id FROM procedures WHERE id = ? AND clinic_id = ?", (proc_id, g.clinic["id"])).fetchone()
    if not proc:
        abort(404)
    try:
        GC.set_dx(db, g.clinic, g.user["id"], proc_id, request.form.get("dx_code"))
        db.commit()
        flash("Diagnosis saved.", "ok")
    except GC.ClaimFormError as exc:
        db.rollback()
        flash(str(exc), "error")
    return redirect(url_for("clinical.treatment", pid=proc["patient_id"]))


@bp.route("/claims/diagnoses", methods=["GET", "POST"])
@manager_required
def diagnoses():
    db = get_db()
    if request.method == "POST":
        try:
            GC.add_diagnosis(db, g.clinic, g.user["id"], request.form.get("code"), request.form.get("title"))
            db.commit()
            flash("Added.", "ok")
        except GC.ClaimFormError as exc:
            db.rollback()
            flash(str(exc), "error")
        return redirect(url_for("ghclaims.diagnoses"))
    return render_template("gh_diagnoses.html", rows=GC.diagnoses(db, g.clinic["id"]), starter=GC.ICD10_DENTAL)


@bp.route("/payers/<int:payer_id>/covered", methods=["GET", "POST"])
@manager_required
def covered(payer_id):
    db = get_db()
    payer = INS.get_payer(db, g.clinic["id"], payer_id)
    if not payer:
        abort(404)
    if request.method == "POST":
        try:
            GC.set_covered(db, g.clinic, g.user["id"], payer_id, [int(x) for x in request.form.getlist("code") if x.isdigit()],
                           request.form.get("needs_dx") == "1")
            db.commit()
            flash("Saved.", "ok")
        except GC.ClaimFormError as exc:
            db.rollback()
            flash(str(exc), "error")
        return redirect(url_for("ghclaims.covered", payer_id=payer_id))
    codes = db.execute("SELECT id, code, name, category FROM procedure_codes WHERE clinic_id = ? AND active = 1 ORDER BY category, name",
                       (g.clinic["id"],)).fetchall()
    return render_template("gh_covered.html", payer=payer, codes=codes, picked=GC.covered(db, g.clinic["id"], payer_id))


@bp.post("/payers/nhis")
@manager_required
def nhis():
    db = get_db()
    try:
        pid = GC.add_nhis(db, g.clinic, g.user["id"])
        db.commit()
        flash("The NHIS is on the payer list. Now tick the treatments it covers.", "ok")
        return redirect(url_for("ghclaims.covered", payer_id=pid))
    except GC.ClaimFormError as exc:
        db.rollback()
        flash(str(exc), "error")
        return redirect(url_for("finance.payers"))


@bp.route("/claims/batches", methods=["GET", "POST"])
def batches():
    if g.user["role"] == "assistant":
        abort(403)
    db = get_db()
    cid = g.clinic["id"]
    last = (datetime.date.today().replace(day=1) - datetime.timedelta(days=1)).strftime("%Y-%m")
    payer_id = request.values.get("payer_id", type=int)
    month = request.values.get("month") or last
    if request.method == "POST":
        try:
            bid = GC.make_batch(db, g.clinic, g.user["id"], payer_id, month)
            db.commit()
            return redirect(url_for("ghclaims.batch", bid=bid))
        except GC.ClaimFormError as exc:
            db.rollback()
            flash(str(exc), "error")
    ready = []
    if payer_id:
        try:
            ready = GC.ready_for_batch(db, cid, payer_id, month)
        except GC.ClaimFormError as exc:
            flash(str(exc), "error")
    return render_template("gh_batches.html", rows=GC.batches(db, cid), payers=INS.list_payers(db, cid, True), payer_id=payer_id, month=month,
                           ready=ready, statuses=GC.BATCH_STATUS)


@bp.route("/claims/batches/<int:bid>", methods=["GET", "POST"])
def batch(bid):
    if g.user["role"] == "assistant":
        abort(403)
    db = get_db()
    cid = g.clinic["id"]
    b = GC.batch(db, cid, bid)
    if not b:
        abort(404)
    if request.method == "POST":
        try:
            if request.form.get("what") == "remove":
                GC.remove_from_batch(db, g.clinic, g.user["id"], bid, request.form.get("claim_id", type=int))
                flash("Taken out of the batch.", "ok")
            else:
                GC.mark_sent(db, g.clinic, g.user["id"], bid, request.form.get("sent_on"), request.form.get("payer_ref"))
                flash("Marked as sent.", "ok")
            db.commit()
        except (GC.ClaimFormError, INS.InsuranceError) as exc:
            db.rollback()
            flash(str(exc), "error")
        return redirect(url_for("ghclaims.batch", bid=bid))
    if request.args.get("csv"):
        return Response(GC.batch_csv(db, g.clinic, bid), mimetype="text/csv",
                        headers={"Content-Disposition": f"attachment; filename=claims-{b['month']}-{bid}.csv"})
    rows = GC.batch_claims(db, cid, bid)
    return render_template("gh_batch.html", b=b, rows=rows, total=sum(r["total_fee_p"] for r in rows), expected=sum(r["estimate_p"] for r in rows),
                           statuses=GC.BATCH_STATUS, today=datetime.date.today().isoformat())


@bp.get("/claims/<int:claim_id>/form")
def form(claim_id):
    if g.user["role"] == "assistant":
        abort(403)
    db = get_db()
    cid = g.clinic["id"]
    c = INS.get_claim(db, cid, claim_id)
    if not c:
        abort(404)
    p = db.execute("SELECT * FROM patients WHERE id = ?", (c["patient_id"],)).fetchone()
    cover = db.execute("SELECT * FROM patient_cover WHERE id = ?", (c["cover_id"],)).fetchone() if c["cover_id"] else None
    items = db.execute("""SELECT ci.*, pr.done_on, pr.tooth, pr.surfaces, pc.code, pc.name, pv.name AS provider_name FROM claim_items ci
                          JOIN procedures pr ON pr.id = ci.procedure_id JOIN procedure_codes pc ON pc.id = pr.code_id
                          LEFT JOIN providers pv ON pv.id = pr.provider_id WHERE ci.claim_id = ? ORDER BY pr.done_on, ci.id""", (claim_id,)).fetchall()
    return render_template("gh_claim_form.html", c=c, p=p, cover=cover, items=items, dx=GC.diagnoses(db, cid))
