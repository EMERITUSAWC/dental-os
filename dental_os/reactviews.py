"""Patients not seen for a while (version 0.41)."""
from flask import Blueprint, flash, g, redirect, render_template, request, url_for

from .db import get_db
from .services import reactivation as RE

bp = Blueprint("react", __name__)


def _months():
    m = request.values.get("months", 18, type=int)
    return m if m in RE.MONTHS else 18


@bp.get("/reactivation")
def home():
    return render_template("react_list.html", rows=RE.lapsed(get_db(), g.clinic, _months()), months=_months(), choices=RE.MONTHS, reasons=RE.REASONS)


@bp.post("/reactivation")
def act():
    db = get_db()
    f = request.form
    try:
        if f.get("what") == "all":
            n = RE.queue_all(db, g.clinic, g.user["id"], _months())
            flash("Messages added to the send list." if n else "Nobody new to message: the rest have not agreed to messages, or already have one waiting.", "ok")
        elif f.get("what") == "hold":
            RE.hold(db, g.clinic, g.user["id"], f.get("pid", type=int), f.get("reason"), f.get("hold_months", type=int), f.get("note"))
            flash("Put aside.", "ok")
        else:
            RE.queue_message(db, g.clinic, g.user["id"], f.get("pid", type=int))
            flash("Message added to the send list.", "ok")
        db.commit()
    except RE.ReactError as exc:
        db.rollback()
        flash(str(exc), "error")
    return redirect(url_for("react.home", months=_months()))
