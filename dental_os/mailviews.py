"""Email set-up (version 0.31)."""
from flask import Blueprint, current_app, flash, g, redirect, render_template, request, url_for

from .auth import manager_required
from .db import get_db
from .services import mailer as M

bp = Blueprint("mail", __name__)


@bp.route("/settings/email", methods=["GET", "POST"])
@manager_required
def setup():
    db = get_db()
    if request.method == "POST":
        try:
            if request.form.get("what") == "test":
                M.send_test(db, g.clinic, g.user["id"], current_app.config["SECRET_KEY"], request.form.get("to"))
                flash("Test email sent. Check the inbox it went to.", "ok")
            else:
                M.save_account(db, g.clinic, g.user["id"], current_app.config["SECRET_KEY"], request.form)
                flash("Saved.", "ok")
        except M.MailError as exc:
            db.rollback()
            flash(str(exc), "error")
        return redirect(url_for("mail.setup"))
    return render_template("mail_setup.html", a=M.account(db, g.clinic["id"]), security=M.SECURITY)
