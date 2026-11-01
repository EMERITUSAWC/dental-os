"""Diary depth (version 0.16): dentists' hours and time off, blocked time, appointment types, the week view, the check-in board,
booking a family together, and filling a gap left by a cancellation."""
from datetime import timedelta
from functools import wraps

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from .safeurl import local_path
from .auth import manager_required
from .db import get_db
from .services import diaryrules as R, patients as P, scheduling as S
from .services.timeutil import now_local, parse_date, parse_dt

bp = Blueprint("diary", __name__)


def _today():
    return now_local(g.clinic["timezone"]).date()


def _day_arg(name="date"):
    raw = request.values.get(name)
    if raw:
        try:
            return parse_date(raw)
        except ValueError:
            pass
    return _today()


def desk_required(view):
    """Managers and front desk run the diary: time off and blocked time."""
    @wraps(view)
    def wrapped(*a, **kw):
        if g.user["role"] not in ("manager", "front_desk"):
            abort(403)
        return view(*a, **kw)
    return wrapped


def _providers(active_only=True):
    q = "SELECT * FROM providers WHERE clinic_id = ?" + (" AND active = 1" if active_only else "") + " ORDER BY id"
    return get_db().execute(q, (g.clinic["id"],)).fetchall()


def _chairs():
    return get_db().execute("SELECT * FROM chairs WHERE clinic_id = ? AND active = 1 ORDER BY id", (g.clinic["id"],)).fetchall()


def _common():
    return {"block_kinds": R.BLOCK_KINDS, "repeats": R.REPEATS, "colours": R.COLOURS, "weekdays": R.WEEKDAYS}


# ------------------------------------------------------------------------------------------------ hours
@bp.route("/diary/setup/hours", methods=["GET", "POST"])
@manager_required
def hours():
    db, cid = get_db(), g.clinic["id"]
    providers = _providers()
    if not providers:
        flash("Add a dentist under Settings first.", "error")
        return redirect(url_for("views.settings"))
    pid = request.values.get("provider_id", type=int) or providers[0]["id"]
    if not any(p["id"] == pid for p in providers):
        abort(404)
    if request.method == "POST":
        rows = []
        for d in R.WEEKDAYS:
            for k in (1, 2):
                rows.append((d, request.form.get(f"s{d}_{k}"), request.form.get(f"e{d}_{k}")))
        try:
            R.save_hours(db, g.clinic, g.user["id"], pid, [] if request.form.get("clear") == "1" else rows)
            flash("Hours saved.", "ok")
        except R.RulesError as exc:
            db.rollback()
            flash(str(exc), "error")
        return redirect(url_for("diary.hours", provider_id=pid))
    own = R.hours_for(db, cid, pid)
    open_days = {int(d) for d in g.clinic["working_days"].split(",") if d}
    return render_template("diary_hours.html", providers=providers, pid=pid, own=own, open_days=open_days, active="hours", **_common())


# ------------------------------------------------------------------------------------------------ time off
@bp.route("/diary/setup/time-off", methods=["GET", "POST"])
@desk_required
def time_off():
    db = get_db()
    if request.method == "POST":
        try:
            if request.form.get("remove"):
                R.remove_time_off(db, g.clinic, g.user["id"], request.form.get("remove", type=int))
                flash("Time off removed.", "ok")
            else:
                R.add_time_off(db, g.clinic, g.user["id"], request.form.get("provider_id", type=int), request.form.get("start_date"),
                               request.form.get("end_date"), request.form.get("start_time"), request.form.get("end_time"),
                               request.form.get("reason"))
                flash("Time off added. The diary will not offer those times.", "ok")
        except R.RulesError as exc:
            db.rollback()
            flash(str(exc), "error")
        return redirect(url_for("diary.time_off"))
    today = _today()
    rows = R.time_off_list(db, g.clinic["id"], today)
    clashes = _booked_during_off(db, rows)
    return render_template("diary_off.html", providers=_providers(), rows=rows, clashes=clashes, today=today, active="off", **_common())


def _booked_during_off(db, rows):
    """Visits already booked inside time off, so the desk can move them."""
    out = []
    for r in rows:
        q = ("SELECT a.*, p.first_name, p.last_name FROM appointments a JOIN patients p ON p.id = a.patient_id "
             "WHERE a.clinic_id = ? AND a.provider_id = ? AND a.status = 'scheduled' AND a.start_at >= ? AND a.start_at < ?")
        start = f"{r['start_date']} {r['start_time'] or '00:00'}"
        end = f"{r['end_date']} {r['end_time'] or '23:59'}"
        for a in db.execute(q, (g.clinic["id"], r["provider_id"], start, end)).fetchall():
            out.append({"off": r, "appt": a})
    return out


# ------------------------------------------------------------------------------------------------ blocked time
@bp.route("/diary/setup/blocks", methods=["GET", "POST"])
@desk_required
def blocks():
    db = get_db()
    if request.method == "POST":
        try:
            if request.form.get("remove"):
                R.remove_block(db, g.clinic, g.user["id"], request.form.get("remove", type=int))
                flash("Blocked time removed.", "ok")
            else:
                R.add_block(db, g.clinic, g.user["id"], request.form.get("kind"), request.form.get("start_date"),
                            request.form.get("start_time"), request.form.get("end_time"),
                            request.form.get("provider_id", type=int), request.form.get("chair_id", type=int),
                            request.form.get("repeat") or "once", request.form.get("end_date"), request.form.get("label"))
                flash("Blocked time added.", "ok")
        except R.RulesError as exc:
            db.rollback()
            flash(str(exc), "error")
        return redirect(url_for("diary.blocks"))
    today = _today()
    return render_template("diary_blocks.html", providers=_providers(), chairs=_chairs(), rows=R.block_list(db, g.clinic["id"], today),
                           today=today, hard=R.HARD_KINDS, active="blocks", **_common())


# ------------------------------------------------------------------------------------------------ appointment types
@bp.route("/diary/setup/types", methods=["GET", "POST"])
@manager_required
def types():
    db, cid = get_db(), g.clinic["id"]
    if request.method == "POST":
        try:
            if request.form.get("action") == "starters":
                n = R.add_starter_types(db, g.clinic, g.user["id"])
                flash("Starter types added." if n else "You already have all the starter types.", "ok")
            elif request.form.get("action") == "toggle":
                R.toggle_type(db, g.clinic, g.user["id"], request.form.get("id", type=int))
                flash("Updated.", "ok")
            else:
                R.save_type(db, g.clinic, g.user["id"], request.form.get("id", type=int), request.form.get("name"),
                            request.form.get("duration"), request.form.get("colour"), request.form.get("category"),
                            request.form.get("block_kind"), request.form.getlist("procedure_id"), S.DURATIONS, S.CATEGORIES)
                flash("Appointment type saved.", "ok")
        except R.RulesError as exc:
            db.rollback()
            flash(str(exc), "error")
        return redirect(url_for("diary.types"))
    rows = R.list_types(db, cid)
    codes = db.execute("SELECT * FROM procedure_codes WHERE clinic_id = ? AND active = 1 ORDER BY category, name", (cid,)).fetchall()
    names = {c["id"]: c["name"] for c in codes}
    edit = R.get_type(db, cid, request.args.get("edit", type=int))
    return render_template("diary_types.html", rows=rows, codes=codes, names=names, edit=edit, durations=S.DURATIONS,
                           categories=S.CATEGORIES, held=R.HELD_KINDS, active="types",
                           chosen=set(int(x) for x in (edit["procedure_ids"] or "").split(",") if x.strip().isdigit()) if edit else set(),
                           **_common())


# ------------------------------------------------------------------------------------------------ week view
@bp.get("/diary/week")
def week():
    providers = _providers()
    if not providers:
        return render_template("diary_week.html", providers=[], days=[], pid=None, monday=_today(), prev=None, next=None,
                               block_kinds=R.BLOCK_KINDS)
    pid = request.args.get("provider_id", type=int) or providers[0]["id"]
    if not any(p["id"] == pid for p in providers):
        abort(404)
    monday = R.monday_of(_day_arg())
    return render_template("diary_week.html", providers=providers, pid=pid, monday=monday,
                           days=S.week_view(get_db(), g.clinic, pid, monday),
                           prev=monday - timedelta(days=7), next=monday + timedelta(days=7), today=_today(),
                           block_kinds=R.BLOCK_KINDS)


# ------------------------------------------------------------------------------------------------ check-in board
@bp.get("/board")
def board():
    day = _today()
    return render_template("board.html", day=day, lanes=S.board(get_db(), g.clinic, day), stages=S.STAGES)


@bp.post("/appointments/<int:aid>/stage")
def stage(aid):
    db = get_db()
    try:
        S.move_stage(db, g.clinic, g.user["id"], aid, request.form.get("action", ""))
    except LookupError:
        abort(404)
    except S.SchedulingError as exc:
        db.rollback()
        flash(str(exc), "error")
    back = request.form.get("back") or ""
    if local_path(back):
        return redirect(back)
    return redirect(url_for("diary.board"))


# ------------------------------------------------------------------------------------------------ filling a gap
@bp.route("/diary/gap/<int:aid>", methods=["GET", "POST"])
def gap(aid):
    db = get_db()
    appt, rows = S.gap_candidates(db, g.clinic, aid)
    if appt is None:
        abort(404)
    if request.method == "POST":
        entry = request.form.get("entry_id", type=int)
        if not any(r["id"] == entry for r in rows):
            flash("That person can no longer take this time.", "error")
            return redirect(url_for("diary.gap", aid=aid))
        try:
            S.place(db, g.clinic, g.user["id"], entry, appt["provider_id"], appt["chair_id"], parse_dt(appt["start_at"]),
                    None, allow_held=True)
            flash("Booked into the gap. Send them a confirmation.", "ok")
            return redirect(url_for("views.appointment_view", aid=entry))
        except S.SchedulingError as exc:
            db.rollback()
            flash(str(exc), "error")
            return redirect(url_for("diary.gap", aid=aid))
    return render_template("diary_gap.html", a=appt, rows=rows, start=parse_dt(appt["start_at"]))


# ------------------------------------------------------------------------------------------------ booking a family
@bp.route("/patients/<int:pid>/family-book", methods=["GET", "POST"])
def family_book(pid):
    db, cid = get_db(), g.clinic["id"]
    patient = P.get(db, cid, pid)
    if not patient:
        abort(404)
    head, dependants = P.family(db, cid, pid)
    root = head or patient
    people = [root] + [d for d in (P.family(db, cid, root["id"])[1] if head else dependants)]
    people = [p for p in people if p["status"] == "active"]
    providers = _providers()
    chosen = []
    source = request.form if request.method == "POST" else request.args
    for p in people:
        if source.get(f"m{p['id']}") == "1":
            try:
                minutes = int(source.get(f"d{p['id']}") or 30)
            except ValueError:
                minutes = 30
            chosen.append((p["id"], minutes))
    provider_id = source.get("provider_id", type=int) or (providers[0]["id"] if providers else None)
    day = _day_arg()
    category = source.get("category") or "Consultation"
    if category not in S.CATEGORIES:
        category = "Consultation"
    if request.method == "POST":
        try:
            ids = S.book_family(db, g.clinic, g.user["id"], provider_id, day, request.form.get("time", ""), chosen, category)
            flash("Family booked back to back.", "ok")
            return redirect(url_for("views.diary", date=day))
        except S.SchedulingError as exc:
            db.rollback()
            flash(str(exc), "error")
            return redirect(url_for("diary.family_book", pid=pid))
        except ValueError:
            db.rollback()
            flash("Pick a time first.", "error")
            return redirect(url_for("diary.family_book", pid=pid))
    slots = []
    searched = request.args.get("find") == "1"
    if searched and provider_id and len(chosen) >= 2:
        slots = S.family_slots(db, g.clinic, provider_id, day, chosen)
    names = {p["id"]: f"{p['first_name']} {p['last_name']}" for p in people}
    return render_template("family_book.html", patient=patient, people=people, providers=providers, provider_id=provider_id,
                           day=day, chosen=dict(chosen), slots=slots, searched=searched, names=names, durations=S.DURATIONS,
                           categories=S.CATEGORIES, category=category)
