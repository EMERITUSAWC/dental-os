"""Associate pay screens (version 0.36). Managers only."""
import datetime

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from .auth import manager_required
from .db import get_db
from .services import money as M, providerpay as PP

bp = Blueprint("pay", __name__)


@bp.get("/pay")
@manager_required
def home():
    db = get_db()
    return render_template("pay_home.html", rows=PP.providers(db, g.clinic["id"]), runs=PP.runs(db, g.clinic["id"])[:50],
                           bases=PP.BASES, statuses=PP.STATUSES)


@bp.route("/pay/providers/<int:provider_id>", methods=["GET", "POST"])
@manager_required
def provider(provider_id):
    db = get_db()
    cid = g.clinic["id"]
    p = db.execute("SELECT * FROM providers WHERE id = ? AND clinic_id = ?", (provider_id, cid)).fetchone()
    if not p:
        abort(404)
    if request.method == "POST":
        f = request.form
        try:
            if f.get("what") == "run":
                rid = PP.new_run(db, g.clinic, g.user["id"], provider_id, f.get("start"), f.get("end"))
                db.commit()
                return redirect(url_for("pay.run_view", rid=rid))
            daily = M.parse_money(f.get("daily") or "0", g.clinic["currency"], allow_zero=True)
            PP.set_terms(db, g.clinic, g.user["id"], provider_id, f.get("basis"), f.get("percent"), f.get("lab_percent"), daily, f.get("effective_from"))
            db.commit()
            flash("Pay terms saved.", "ok")
            return redirect(url_for("pay.provider", provider_id=provider_id))
        except (PP.PayError, ValueError) as exc:
            db.rollback()
            flash(str(exc), "error")
    today = datetime.date.today()
    first = today.replace(day=1)
    last_end = first - datetime.timedelta(days=1)
    return render_template("pay_provider.html", p=p, terms=PP.terms_history(db, cid, provider_id), runs=PP.runs(db, cid, provider_id),
                           bases=PP.BASES, statuses=PP.STATUSES, today=today.isoformat(), start=last_end.replace(day=1).isoformat(),
                           end=last_end.isoformat())


@bp.route("/pay/runs/<int:rid>", methods=["GET", "POST"])
@manager_required
def run_view(rid):
    db = get_db()
    r = PP.run(db, g.clinic["id"], rid)
    if not r:
        abort(404)
    if request.method == "POST":
        f = request.form
        what = f.get("what")
        try:
            if what == "extra":
                amount = M.parse_money(f.get("amount"), g.clinic["currency"])
                PP.add_extra(db, g.clinic, g.user["id"], rid, f.get("label"), -amount if f.get("sign") == "minus" else amount)
                flash("Line added.", "ok")
            elif what == "approve":
                PP.approve(db, g.clinic, g.user["id"], rid)
                flash("Pay run approved. It can no longer change.", "ok")
            elif what == "paid":
                PP.mark_paid(db, g.clinic, g.user["id"], rid, f.get("paid_on"), f.get("reference"))
                flash("Marked as paid.", "ok")
            elif what == "cancel":
                PP.cancel(db, g.clinic, g.user["id"], rid, f.get("reason"))
                flash("Pay run cancelled.", "ok")
            db.commit()
        except (PP.PayError, ValueError) as exc:
            db.rollback()
            flash(str(exc), "error")
        return redirect(url_for("pay.run_view", rid=rid))
    import json
    return render_template("pay_run.html", r=r, f=json.loads(r["figures"]), extras=PP.extras(r), bases=PP.BASES, statuses=PP.STATUSES,
                           today=datetime.date.today().isoformat())
