"""SMS set-up and sending (version 0.33)."""
from flask import Blueprint, abort, current_app, flash, g, redirect, render_template, request, url_for

from .auth import manager_required
from .db import get_db
from .services import outbox, sms as SMS

bp = Blueprint("sms", __name__)


def _secret():
    return current_app.config["SECRET_KEY"]


@bp.route("/settings/sms", methods=["GET", "POST"])
@manager_required
def setup():
    db = get_db()
    if request.method == "POST":
        try:
            if request.form.get("what") == "test":
                SMS.send_test(db, g.clinic, g.user["id"], _secret(), request.form.get("to"))
                flash("Test SMS sent. Check the phone it went to.", "ok")
            else:
                SMS.save_account(db, g.clinic, g.user["id"], _secret(), request.form)
                flash("Saved.", "ok")
        except SMS.SmsError as exc:
            db.rollback()
            flash(str(exc), "error")
        return redirect(url_for("sms.setup"))
    return render_template("sms_setup.html", a=SMS.account(db, g.clinic["id"]), providers=SMS.PROVIDERS)


@bp.post("/messages/queue/<int:oid>/sms")
def queue_sms(oid):
    db = get_db()
    try:
        SMS.deliver_outbox(db, g.clinic, g.user["id"], _secret(), oid)
        flash("Sent by SMS.", "ok")
    except LookupError:
        abort(404)
    except outbox.MessageError as exc:
        db.rollback()
        flash(str(exc), "error")
    return redirect(url_for("comms.queue"))


@bp.post("/messages/queue/sms-all")
def queue_sms_all():
    db = get_db()
    if not SMS.is_on(db, g.clinic["id"]):
        flash("SMS is not switched on.", "error")
        return redirect(url_for("comms.queue"))
    res = SMS.send_ready(db, g.clinic, g.user["id"], _secret())
    if res["stopped"]:
        flash(res["stopped"], "error")
    else:
        flash("Texts sent to everyone who prefers SMS. Anything left needs a person." if res["left"] else "Texts sent to everyone who prefers SMS.", "ok")
    return redirect(url_for("comms.queue"))


@bp.post("/statements/items/<int:item_id>/sms")
def statement_sms(item_id):
    db = get_db()
    it = db.execute("SELECT run_id FROM statement_items WHERE id = ? AND clinic_id = ?", (item_id, g.clinic["id"])).fetchone()
    if not it:
        abort(404)
    try:
        SMS.send_statement(db, g.clinic, g.user["id"], _secret(), item_id, lambda t: url_for("public.portal", token=t, _external=True))
        flash("Statement sent by SMS.", "ok")
    except SMS.SmsError as exc:
        db.rollback()
        flash(str(exc), "error")
    return redirect(url_for("statements.run_view", rid=it["run_id"]))
