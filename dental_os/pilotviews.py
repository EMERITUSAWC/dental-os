"""Version 0.59: the go-live checklist, problem reports, the error list and the side-by-side check with the old system."""
from datetime import timedelta

from flask import Blueprint, abort, current_app, flash, g, redirect, render_template, request, url_for

from .auth import manager_required
from .db import get_db
from .safeurl import local_path
from .services import pilot as P
from .services.timeutil import now_local, parse_date

bp = Blueprint("pilot", __name__)


@bp.route("/settings/go-live", methods=["GET", "POST"])
@manager_required
def golive():
    db = get_db()
    if request.method == "POST":
        try:
            P.tick(db, g.clinic, g.user["id"], request.form.get("item"), request.form.get("on") == "1")
            flash("Saved.", "ok")
        except P.PilotError as exc:
            flash(str(exc), "error")
        return redirect(url_for("pilot.golive"))
    items = P.checklist(db, g.clinic, current_app.config)
    return render_template("pilot_golive.html", items=items, ready=P.ready(items), ticks=P.ticked(db, g.clinic["id"]),
                           side=P.side_by_side(db, g.clinic), need=P.SIDE_BY_SIDE_DAYS)


@bp.route("/problems", methods=["GET", "POST"])
def problems():
    db = get_db()
    if request.method == "POST":
        try:
            P.report_problem(db, g.clinic, g.user["id"], request.form.get("page"), request.form.get("what"),
                             request.form.get("urgency"), request.form.get("share") == "1")
            flash("Thank you. The problem is written down for the manager.", "ok")
            back = local_path(request.form.get("page"))
            return redirect(back or url_for("pilot.problems"))
        except P.PilotError as exc:
            flash(str(exc), "error")
    page = local_path(request.args.get("page") or request.form.get("page") or "") or ""
    status = request.args.get("status")
    status = status if status in P.PROBLEM_STATES else None
    return render_template("pilot_problems.html", rows=P.problems(db, g.clinic["id"], g.user, status), page=page.split("?", 1)[0],
                           states=P.PROBLEM_STATES, urgency=P.URGENCY, status=status,
                           errors=P.errors(db, g.clinic["id"]) if g.user["role"] == "manager" else [])


@bp.post("/problems/<int:problem_id>")
@manager_required
def problem_status(problem_id):
    try:
        P.set_problem(get_db(), g.clinic, g.user["id"], problem_id, request.form.get("status"), request.form.get("note"))
        flash("Saved.", "ok")
    except P.PilotError as exc:
        flash(str(exc), "error")
    except LookupError:
        abort(404)
    return redirect(url_for("pilot.problems"))


@bp.route("/settings/side-by-side", methods=["GET", "POST"])
@manager_required
def side():
    db = get_db()
    today = now_local(g.clinic["timezone"]).date()
    raw = request.values.get("day")
    try:
        day = parse_date(raw) if raw else today
    except ValueError:
        day = today
    if request.method == "POST":
        try:
            P.save_old_figures(db, g.clinic, g.user["id"], day, request.form, request.form.get("note"))
            flash("Saved.", "ok")
            return redirect(url_for("pilot.side"))
        except P.PilotError as exc:
            flash(str(exc), "error")
    return render_template("pilot_side.html", day=day, prev=day - timedelta(days=1), today=today, ours=P.ours(db, g.clinic, day),
                           kinds=P.KINDS, side=P.side_by_side(db, g.clinic), need=P.SIDE_BY_SIDE_DAYS)
