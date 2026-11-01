"""Running the practice (version 0.19): staff tasks, the time clock, end-of-day cash-up, exports for the accountant, self check-in,
and more reports."""
import csv
import io
from datetime import timedelta

from flask import Blueprint, Response, abort, flash, g, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash

from .safeurl import local_path
from .auth import manager_required
from .db import get_db
from .services import (acctexport as AX, audit, billing, cashup as CU, dataio, diaryrules as R, kiosk as K, money, reports2 as R2,
                       tasks as T, throttle, timeclock as TC)
from .services.timeutil import now_local, parse_date

bp = Blueprint("practice", __name__)
KIOSK_ENDPOINTS = ("practice.kiosk", "practice.kiosk_exit", "static", "health")


def _today():
    return now_local(g.clinic["timezone"]).date()


def _day(name="date", default=None):
    raw = request.values.get(name)
    if raw:
        try:
            return parse_date(raw)
        except ValueError:
            pass
    return default or _today()


def _users():
    return get_db().execute("SELECT id, display_name FROM users WHERE clinic_id = ? AND active = 1 ORDER BY display_name",
                            (g.clinic["id"],)).fetchall()


def _back(default):
    back = request.form.get("back") or ""
    if local_path(back):
        return redirect(back)
    return redirect(default)


def _csv(rows, name):
    buf = io.StringIO()
    csv.writer(buf).writerows([[dataio._safe_cell(c) for c in row] for row in rows])
    return Response(buf.getvalue(), mimetype="text/csv", headers={"Content-Disposition": f'attachment; filename="{name}"'})


# ------------------------------------------------------------------------------------------------------------------ tasks
@bp.route("/tasks", methods=["GET", "POST"])
def tasks():
    db = get_db()
    if request.method == "POST":
        try:
            T.add(db, g.clinic, g.user["id"], request.form.get("title"), request.form.get("assigned_to", type=int),
                  request.form.get("due_on"), request.form.get("priority") or "normal", request.form.get("patient_id", type=int),
                  request.form.get("detail"), request.form.get("repeat") or "none")
            flash("Task added.", "ok")
        except T.TaskError as exc:
            db.rollback()
            flash(str(exc), "error")
        return _back(url_for("practice.tasks"))
    view = request.args.get("view", "mine")
    status = "open" if view in ("mine", "all") else ("done" if view == "done" else "open")
    rows = T.board(db, g.clinic, g.user["id"], mine=(view == "mine"), status=status)
    return render_template("tasks.html", rows=rows, view=view, users=_users(), priorities=T.PRIORITIES, repeats=T.REPEATS, today=_today())


@bp.post("/tasks/<int:tid>/<action>")
def task_action(tid, action):
    db = get_db()
    try:
        if action in ("done", "cancelled"):
            T.finish(db, g.clinic, g.user["id"], tid, action, request.form.get("note"))
            flash("Task closed." if action == "cancelled" else "Task done.", "ok")
        elif action == "assign":
            T.reassign(db, g.clinic, g.user["id"], tid, request.form.get("assigned_to", type=int))
            flash("Updated.", "ok")
        else:
            abort(404)
    except T.TaskError as exc:
        db.rollback()
        flash(str(exc), "error")
    return _back(url_for("practice.tasks"))


# ------------------------------------------------------------------------------------------------------------- time clock
@bp.post("/clock")
def clock():
    db = get_db()
    try:
        if request.form.get("action") == "out":
            TC.clock_out(db, g.clinic, g.user, request.form.get("break_minutes"))
            flash("Clocked out.", "ok")
        else:
            TC.clock_in(db, g.clinic, g.user)
            flash("Clocked in.", "ok")
    except TC.ClockError as exc:
        db.rollback()
        flash(str(exc), "error")
    return _back(url_for("views.dashboard"))


@bp.route("/timecards", methods=["GET", "POST"])
@manager_required
def timecards():
    db = get_db()
    monday = R.monday_of(_day())
    if request.method == "POST":
        try:
            if request.form.get("entry_id"):
                TC.correct(db, g.clinic, g.user["id"], request.form.get("entry_id", type=int), request.form.get("clock_in"),
                           request.form.get("clock_out"), request.form.get("break_minutes"), request.form.get("reason"))
                flash("Entry corrected. The reason is kept.", "ok")
            else:
                TC.add_missed(db, g.clinic, g.user["id"], request.form.get("user_id", type=int), request.form.get("clock_in"),
                              request.form.get("clock_out"), request.form.get("break_minutes"), request.form.get("reason"))
                flash("Shift added.", "ok")
        except TC.ClockError as exc:
            db.rollback()
            flash(str(exc), "error")
        return redirect(url_for("practice.timecards", date=monday))
    return render_template("timecards.html", people=TC.week(db, g.clinic, monday), monday=monday, days=[monday + timedelta(days=i) for i in range(7)],
                           prev=monday - timedelta(days=7), next=monday + timedelta(days=7), users=_users(), hm=TC.hm,
                           edit=request.args.get("edit", type=int))


@bp.get("/timecards.csv")
@manager_required
def timecards_csv():
    monday = R.monday_of(_day())
    return _csv(TC.csv_rows(get_db(), g.clinic, monday), f"time-cards-{monday}.csv")


# -------------------------------------------------------------------------------------------------------------- cash-up
@bp.route("/cashup", methods=["GET", "POST"])
def cashup():
    if g.user["role"] not in ("manager", "front_desk"):
        abort(403)
    db, cid = get_db(), g.clinic["id"]
    day = _day()
    methods = [m for m in billing.enabled_methods(g.clinic) if m != "insurance"]
    if request.method == "POST":
        try:
            if request.form.get("action") == "reopen":
                if g.user["role"] != "manager":
                    abort(403)
                CU.reopen(db, g.clinic, g.user["id"], day.isoformat(), request.form.get("reason"))
                flash("Day reopened. The earlier close is kept.", "ok")
            else:
                counted = {}
                for m in methods:
                    raw = (request.form.get(f"count_{m}") or "").strip()
                    counted[m] = money.parse_money(raw, g.clinic["currency"], allow_zero=True) if raw else None
                CU.close(db, g.clinic, g.user["id"], day.isoformat(), counted, request.form.get("note"), methods)
                flash("Day closed.", "ok")
        except (CU.CashUpError, ValueError) as exc:
            db.rollback()
            flash(str(exc), "error")
        return redirect(url_for("practice.cashup", date=day))
    row = CU.current(db, cid, day.isoformat())
    return render_template("cashup.html", day=day, methods=methods, method_names=billing.METHODS, exp=CU.expected(db, cid, day.isoformat()),
                           row=row, lines=CU.lines_of(row), late=CU.after_close(db, cid, day.isoformat(), row),
                           history=CU.history(db, cid, day.isoformat()), recent=CU.recent(db, cid), today=_today(),
                           prev=day - timedelta(days=1), next=day + timedelta(days=1))


# ------------------------------------------------------------------------------------------------------- accountant export
@bp.route("/reports/export", methods=["GET", "POST"])
@manager_required
def export():
    db = get_db()
    today = _today()
    start = _day("start", today.replace(day=1))
    end = _day("end", today)
    if request.method == "POST":
        db.execute("UPDATE clinics SET export_account = ?, export_tax_type = ? WHERE id = ?",
                   ((request.form.get("account") or "").strip()[:20] or None, (request.form.get("tax_type") or "").strip()[:40] or None, g.clinic["id"]))
        audit.log(db, g.clinic["id"], g.user["id"], "settings.export")
        flash("Saved.", "ok")
        return redirect(url_for("practice.export", start=start, end=end))
    return render_template("export.html", start=start, end=end)


@bp.get("/reports/export/<kind>.csv")
@manager_required
def export_csv(kind):
    db = get_db()
    today = _today()
    start, end = _day("start", today.replace(day=1)), _day("end", today)
    if end < start:
        start, end = end, start
    audit.log(db, g.clinic["id"], g.user["id"], f"export.{kind}", detail=f"{start} to {end}")
    if kind == "journal":
        return _csv(AX.journal(db, g.clinic, start.isoformat(), end.isoformat()), f"journal-{start}-to-{end}.csv")
    if kind == "invoices":
        return _csv(AX.xero_invoices(db, g.clinic, start.isoformat(), end.isoformat(), g.clinic["export_account"], g.clinic["export_tax_type"]),
                    f"invoices-{start}-to-{end}.csv")
    abort(404)


# ------------------------------------------------------------------------------------------------------------------ kiosk
@bp.post("/kiosk/start")
def kiosk_start():
    if g.user["role"] not in ("manager", "front_desk"):
        abort(403)
    session["kiosk"] = g.clinic["id"]
    audit.log(get_db(), g.clinic["id"], g.user["id"], "kiosk.start")
    return redirect(url_for("practice.kiosk"))


@bp.route("/kiosk", methods=["GET", "POST"])
def kiosk():
    if not session.get("kiosk"):
        return redirect(url_for("views.dashboard"))
    db = get_db()
    from flask import current_app
    bucket = throttle.key(current_app.config["SECRET_KEY"], "kiosk", g.clinic["id"], g.user["id"])
    state, name, choices = "ask", None, []
    if request.method == "POST":
        if not throttle.allow(db, bucket, K.TRIES_PER_10_MIN, 10):
            flash("Too many tries. Please ask at the desk.", "error")
            return render_template("kiosk.html", state="ask")
        try:
            if request.form.get("appt_id"):
                rows = K.find(db, g.clinic, request.form.get("last4"), request.form.get("dob"))
                pick = next((r for r in rows if r["id"] == request.form.get("appt_id", type=int)), None)
                if not pick:
                    raise K.KioskError("We could not find that visit. Please ask at the desk.")
                K.check_in(db, g.clinic, g.user, pick["id"])
                state, name = "done", pick["first_name"]
            else:
                rows = K.find(db, g.clinic, request.form.get("last4"), request.form.get("dob"))
                if not rows:
                    raise K.KioskError("We could not find a visit for you today. Please ask at the desk.")
                if len(rows) == 1:
                    K.check_in(db, g.clinic, g.user, rows[0]["id"])
                    state, name = "done", rows[0]["first_name"]
                else:
                    state, choices = "choose", rows
        except K.KioskError as exc:
            flash(str(exc), "error")
    return render_template("kiosk.html", state=state, name=name, choices=choices, form=request.form)


@bp.route("/kiosk/exit", methods=["GET", "POST"])
def kiosk_exit():
    if not session.get("kiosk"):
        return redirect(url_for("views.dashboard"))
    db = get_db()
    if request.method == "POST":
        from flask import current_app
        bucket = throttle.key(current_app.config["SECRET_KEY"], "kiosk-exit", g.clinic["id"])
        username = (request.form.get("username") or "").strip()
        staff = db.execute("SELECT * FROM users WHERE username = ? AND clinic_id = ? AND active = 1 AND role IN ('manager','front_desk')",
                           (username, g.clinic["id"])).fetchone()
        if throttle.allow(db, bucket, 5, 10) and staff and check_password_hash(staff["password_hash"], request.form.get("password") or ""):
            session.pop("kiosk", None)
            audit.log(db, g.clinic["id"], staff["id"], "kiosk.stop")
            flash("Kiosk mode is off.", "ok")
            return redirect(url_for("diary.board"))
        flash("That did not work. A manager or front desk login is needed.", "error")
    return render_template("kiosk_exit.html")


# ---------------------------------------------------------------------------------------------------------------- reports
@bp.get("/reports/day")
def day_sheet():
    if g.user["role"] not in ("manager", "front_desk"):
        abort(403)
    day = _day()
    return render_template("report_day.html", day=day, d=R2.day_sheet(get_db(), g.clinic["id"], day), methods=billing.METHODS,
                           prev=day - timedelta(days=1), next=day + timedelta(days=1))


@bp.get("/reports/production")
@manager_required
def production():
    today = _today()
    start, end = _day("start", today.replace(day=1)), _day("end", today)
    rows = R2.production_by_code(get_db(), g.clinic["id"], start.isoformat(), end.isoformat())
    if request.args.get("csv"):
        return _csv([["Code", "Treatment", "Category", "Times", "Total"]] + [[r["code"], r["name"], r["category"], r["n"], "%.2f" % (r["total"] / 100)] for r in rows],
                    f"production-{start}-to-{end}.csv")
    return render_template("report_production.html", rows=rows, start=start, end=end, total=sum(r["total"] for r in rows))


@bp.get("/reports/unscheduled")
def unscheduled():
    if g.user["role"] == "assistant":
        abort(403)
    return render_template("report_unscheduled.html", rows=R2.unscheduled(get_db(), g.clinic["id"]))


@bp.get("/reports/missed")
def missed():
    if g.user["role"] not in ("manager", "front_desk"):
        abort(403)
    today = _today()
    start, end = _day("start", today - timedelta(days=30)), _day("end", today)
    return render_template("report_missed.html", rows=R2.broken(get_db(), g.clinic["id"], start.isoformat(), end.isoformat()), start=start, end=end)
