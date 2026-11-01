"""Version 0.65: the assistant page, and the manager's switch."""
from flask import Blueprint, current_app, flash, g, redirect, render_template, request, url_for

from .auth import manager_required
from .db import get_db
from .services import assistant as A

bp = Blueprint("assistant", __name__)


@bp.route("/assistant", methods=["GET", "POST"])
def ask():
    db = get_db()
    on = A.is_on(db, g.clinic["id"], current_app.config["SECRET_KEY"])
    result, question = None, ""
    if request.method == "POST" and on:
        question = request.form.get("q") or ""
        try:
            result = A.ask(db, g.clinic, g.user, current_app.config["SECRET_KEY"], question, transport=current_app.config.get("ASSISTANT_TRANSPORT"),
                           lang=g.get("lang") or "en")
        except A.AssistantError as exc:
            flash(str(exc), "error")
    tool_names = [t["name"] for t in A.tools_for(db, g.user)]
    return render_template("assistant.html", on=on, result=result, question=question, tool_names=tool_names)


@bp.post("/settings/assistant")
@manager_required
def switch():
    try:
        A.set_on(get_db(), g.clinic, g.user["id"], request.form.get("on") == "1")
        flash("Saved.", "ok")
    except A.AssistantError as exc:
        flash(str(exc), "error")
    return redirect(url_for("views.settings"))
