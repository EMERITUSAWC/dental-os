"""Refunds, credit balances and closing the books (versions 0.42 and 0.43). Managers only."""
from flask import Blueprint, flash, g, redirect, render_template, request, url_for

from .auth import manager_required
from .db import get_db
from .services import money as M, periodlock as PL, refunds as RF

bp = Blueprint("money", __name__)


@bp.post("/patients/<int:pid>/refunds")
@manager_required
def refund(pid):
    db = get_db()
    try:
        RF.refund(db, g.clinic, g.user["id"], pid, M.parse_money(request.form.get("amount"), g.clinic["currency"]), request.form.get("method"),
                  request.form.get("reason"), request.form.get("reference"), request.form.get("note"), request.form.get("refunded_on"))
        db.commit()
        flash("Refund recorded.", "ok")
    except (RF.RefundError, PL.LockedError, ValueError) as exc:
        db.rollback()
        flash(str(exc), "error")
    return redirect(url_for("clinical.account", pid=pid))


@bp.post("/refunds/<int:rid>/void")
@manager_required
def void(rid):
    db = get_db()
    row = db.execute("SELECT patient_id FROM refunds WHERE id = ? AND clinic_id = ?", (rid, g.clinic["id"])).fetchone()
    try:
        RF.void(db, g.clinic, g.user["id"], rid, request.form.get("reason"))
        db.commit()
        flash("Refund voided.", "ok")
    except (RF.RefundError, PL.LockedError) as exc:
        db.rollback()
        flash(str(exc), "error")
    return redirect(url_for("clinical.account", pid=row["patient_id"]) if row else url_for("money.credits"))


@bp.get("/credits")
@manager_required
def credits():
    return render_template("money_credits.html", rows=RF.credits(get_db(), g.clinic["id"]))


@bp.route("/books", methods=["GET", "POST"])
@manager_required
def books():
    db = get_db()
    if request.method == "POST":
        try:
            PL.set_lock(db, g.clinic, g.user["id"], request.form.get("locked_to"), request.form.get("reason"))
            db.commit()
            flash("Saved.", "ok")
        except PL.LockedError as exc:
            db.rollback()
            flash(str(exc), "error")
        return redirect(url_for("money.books"))
    return render_template("money_books.html", locked=PL.lock_date(db, g.clinic["id"]), rows=PL.history(db, g.clinic["id"]))
