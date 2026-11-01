"""Statement runs and late fees (version 0.27)."""
from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from .auth import manager_required
from .db import get_db
from .services import ageing, billing, i18n, statements as S

bp = Blueprint("statements", __name__)


@bp.route("/statements", methods=["GET", "POST"])
def home():
    db = get_db()
    f = request.values
    min_balance, older, gap = f.get("min_balance", "1"), f.get("older_than_days", "0"), f.get("gap_days", "25")
    if request.method == "POST":
        add_fees = f.get("add_fees") == "1"
        if add_fees and g.user["role"] != "manager":
            abort(403)
        try:
            rid = S.make_run(db, g.clinic, g.user["id"], min_balance, older, gap, add_fees, request.form.getlist("patient_id"))
            flash("Statement run made.", "ok")
            return redirect(url_for("statements.run_view", rid=rid))
        except S.StatementError as exc:
            db.rollback()
            flash(str(exc), "error")
    try:
        rows = S.candidates(db, g.clinic, S._money(min_balance or "1", "x"), S._int(older or 0, "x", 0, 365), S._int(gap or 0, "x", 0, 365))
    except S.StatementError:
        flash("Check the numbers in the boxes.", "error")
        rows = []
    return render_template("statements_home.html", rows=rows, runs=S.runs(db, g.clinic["id"]), s=S.settings(db, g.clinic["id"]),
                           min_balance=min_balance, older=older, gap=gap)


@bp.get("/statements/<int:rid>")
def run_view(rid):
    data = S.run(get_db(), g.clinic["id"], rid)
    if not data:
        abort(404)
    from .services import mailer
    from .services import sms
    return render_template("statements_run.html", d=data, mail_on=mailer.is_on(get_db(), g.clinic["id"]), sms_on=sms.is_on(get_db(), g.clinic["id"]))


@bp.get("/statements/<int:rid>/print")
def run_print(rid):
    db = get_db()
    cid = g.clinic["id"]
    data = S.run(db, cid, rid)
    if not data:
        abort(404)
    g.doc_lang = i18n.clinic_lang(g.clinic)
    today = S._today(g.clinic)
    pages = []
    for it in data["lines"]:
        pid = it["patient_id"]
        pages.append({"p": it, "events": billing.statement(db, cid, pid, g.doc_lang), "t": billing.totals(db, cid, pid),
                      "ageing": ageing.patient_ageing(db, cid, pid, today), "insurers": ageing.insurers_owe(db, cid, pid)})
    return render_template("statements_print.html", pages=pages, d=data, today=today)


@bp.post("/statements/<int:rid>/printed")
def run_printed(rid):
    if not S.run(get_db(), g.clinic["id"], rid):
        abort(404)
    S.mark_printed(get_db(), g.clinic, g.user["id"], rid)
    flash("Marked as printed.", "ok")
    return redirect(url_for("statements.run_view", rid=rid))


@bp.post("/statements/items/<int:item_id>/whatsapp")
def item_whatsapp(item_id):
    db = get_db()
    try:
        link = S.send_whatsapp(db, g.clinic, g.user["id"], item_id, lambda t: url_for("public.portal", token=t, _external=True))
        db.commit()
        return redirect(link)
    except S.StatementError as exc:
        db.rollback()
        flash(str(exc), "error")
    it = db.execute("SELECT run_id FROM statement_items WHERE id = ? AND clinic_id = ?", (item_id, g.clinic["id"])).fetchone()
    return redirect(url_for("statements.run_view", rid=it["run_id"]) if it else url_for("statements.home"))


@bp.post("/statements/items/<int:item_id>/email")
def item_email(item_id):
    from flask import current_app
    from .services import mailer
    db = get_db()
    it = db.execute("SELECT run_id FROM statement_items WHERE id = ? AND clinic_id = ?", (item_id, g.clinic["id"])).fetchone()
    if not it:
        abort(404)
    try:
        mailer.send_statement(db, g.clinic, g.user["id"], current_app.config["SECRET_KEY"], item_id,
                              lambda t: url_for("public.portal", token=t, _external=True))
        flash("Statement emailed.", "ok")
    except mailer.MailError as exc:
        db.rollback()
        flash(str(exc), "error")
    return redirect(url_for("statements.run_view", rid=it["run_id"]))


@bp.post("/statements/<int:rid>/email-all")
def run_email_all(rid):
    from flask import current_app
    from .services import mailer
    db = get_db()
    if not S.run(db, g.clinic["id"], rid):
        abort(404)
    try:
        sent, skipped = mailer.email_run(db, g.clinic, g.user["id"], current_app.config["SECRET_KEY"], rid,
                                         lambda t: url_for("public.portal", token=t, _external=True))
        flash("Statements emailed. Anyone without an email address, or who has not agreed to messages, is left to print." if sent else
              "Nobody in this run has an email address and has agreed to messages. Print the statements instead.", "ok")
    except mailer.MailError as exc:
        db.rollback()
        flash(str(exc), "error")
    return redirect(url_for("statements.run_view", rid=rid))


@bp.route("/statements/late-fees", methods=["GET", "POST"])
@manager_required
def late_fees():
    db = get_db()
    if request.method == "POST":
        try:
            S.save_settings(db, g.clinic, g.user["id"], request.form)
            flash("Saved.", "ok")
        except S.StatementError as exc:
            db.rollback()
            flash(str(exc), "error")
        return redirect(url_for("statements.late_fees"))
    return render_template("statements_fees.html", s=S.settings(db, g.clinic["id"]))
