"""Closed days screen (version 0.47). Managers only."""
import datetime

from flask import Blueprint, flash, g, redirect, render_template, request, url_for

from .auth import manager_required
from .db import get_db
from .services import closeddays as CD

bp = Blueprint("closed", __name__)


@bp.route("/closed-days", methods=["GET", "POST"])
@manager_required
def home():
    db = get_db()
    year = request.values.get("year", type=int) or datetime.date.today().year
    if request.method == "POST":
        f = request.form
        try:
            if f.get("what") == "remove":
                CD.remove(db, g.clinic, g.user["id"], f.get("cd_id", type=int))
                flash("Removed.", "ok")
            elif f.get("what") == "copy":
                n = CD.copy_year(db, g.clinic, g.user["id"], year, {int(x) for x in f.getlist("pick") if x.isdigit()})
                flash("Copied to next year." if n else "Nothing new to copy.", "ok")
                year += 1
            else:
                CD.add(db, g.clinic, g.user["id"], f.get("day"), f.get("name"))
                flash("Added.", "ok")
            db.commit()
        except CD.ClosedError as exc:
            db.rollback()
            flash(str(exc), "error")
        return redirect(url_for("closed.home", year=year))
    return render_template("closed_days.html", rows=CD.listing(db, g.clinic["id"], year), year=year)
