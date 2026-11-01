"""Version 0.59: what a clinic needs to start using Dental OS for real, and to run it next to its old system for a while.

  * The go-live checklist: what the software can check by itself, plus the few things only a person can confirm.
  * Problem reports: any member of staff writes down what went wrong, from the page it went wrong on.
  * The error log: when a page breaks, the person sees a reference number and the log keeps which screen it was.
    No form contents, no search words and no patient details are kept: only the screen's name, the kind of error and when.
  * Side by side: each evening the manager types in the old system's figures for the day, and the page shows where the
    two disagree. Two weeks with no unexplained differences is the sign the old system can be switched off.
"""
import datetime
import secrets
from zoneinfo import ZoneInfo

from . import audit
from .timeutil import now_local


class PilotError(ValueError):
    pass


# ------------------------------------------------------------------------------------------------------------ go-live checklist
# Things only a person can confirm. Each is ticked by a manager, with their name and the time kept.
BY_HAND = {
    "hours": "Opening hours and each dentist's working hours are right",
    "prices": "A dentist has checked the price list",
    "wording": "The appointment and recall message wording has been read and agreed",
    "training": "Every member of staff has logged in and booked, charted and taken a payment on a test patient",
    "parallel": "The old system will keep running alongside for at least two weeks",
    "contact": "Staff know who to call when something goes wrong",
}

SIDE_BY_SIDE_DAYS = 10          # matching working days before the old system can go


def _count(db, sql, args):
    return db.execute(sql, args).fetchone()[0]


def checklist(db, clinic, config):
    """[(key, label, done, must, fix endpoint or None)]. 'must' items stop the go-live; the rest are strongly advised."""
    cid = clinic["id"]
    from . import offsite
    managers = db.execute("SELECT totp_enabled FROM users WHERE clinic_id = ? AND role = 'manager' AND active = 1", (cid,)).fetchall()
    ticks = {r["item"] for r in db.execute("SELECT item FROM golive_ticks WHERE clinic_id = ?", (cid,))}
    st = offsite.status(db)
    drill = offsite.drill_status(db)
    today = now_local(clinic["timezone"]).date()
    public = (config.get("PUBLIC_URL") or "").lower()
    items = [
        ("dentists", "At least one dentist is set up", _count(db, "SELECT COUNT(*) FROM providers WHERE clinic_id = ? AND active = 1", (cid,)) > 0, True, "views.settings"),
        ("chairs", "At least one chair is set up", _count(db, "SELECT COUNT(*) FROM chairs WHERE clinic_id = ? AND active = 1", (cid,)) > 0, True, "views.settings"),
        ("price_list", "Prices are entered for the treatments the clinic does", _count(db, "SELECT COUNT(*) FROM procedure_codes WHERE clinic_id = ? AND active = 1 AND fee_p > 0", (cid,)) >= 5, True, "clinical.catalogue"),
        ("patients", "Patients are in: moved in from the old system or entered", _count(db, "SELECT COUNT(*) FROM patients WHERE clinic_id = ?", (cid,)) > 0, True, "migrate.home"),
        ("two_managers", "Two managers, so one can reset the other's login", len(managers) >= 2, False, "views.settings"),
        ("two_step", "Every manager uses two-step login", bool(managers) and all(m["totp_enabled"] for m in managers), True, "auth.security"),
        ("copies", "An off-site copy was made in the last day", bool(st["last_ok"]) and not offsite.stale(db), True, "backups.home"),
        ("drill", "A restore drill has passed this month", not drill["due"], True, "backups.home"),
        ("holidays", "Public holidays for the coming months are entered", _count(db, "SELECT COUNT(*) FROM closed_days WHERE clinic_id = ? AND day >= ?", (cid, today.isoformat())) > 0, False, "closed.home"),
        ("https", "The site is reached over https with secure cookies", public.startswith("https://") and bool(config.get("SESSION_COOKIE_SECURE")), False, None),
    ]
    items += [(k, label, k in ticks, True, "pilot.golive") for k, label in BY_HAND.items()]
    return items


def ready(items):
    return all(done for _k, _l, done, must, _f in items if must)


def tick(db, clinic, user_id, item, on=True):
    if item not in BY_HAND:
        raise PilotError("That item is not on the list.")
    cid = clinic["id"]
    db.execute("DELETE FROM golive_ticks WHERE clinic_id = ? AND item = ?", (cid, item))
    if on:
        db.execute("INSERT INTO golive_ticks(clinic_id, item, done_by) VALUES (?,?,?)", (cid, item, user_id))
    audit.log(db, cid, user_id, "golive.tick" if on else "golive.untick", detail=item)


def ticked(db, cid):
    return {r["item"]: r for r in db.execute("""SELECT t.*, u.display_name FROM golive_ticks t LEFT JOIN users u ON u.id = t.done_by
                                                WHERE t.clinic_id = ?""", (cid,))}


# ------------------------------------------------------------------------------------------------------------ problem reports
PROBLEM_STATES = {"open": "Open", "fixed": "Fixed", "not_a_fault": "Not a fault", "later": "Later"}
URGENCY = {"blocking": "Stops me working", "annoying": "I can work around it", "idea": "An idea"}


def report_problem(db, clinic, user_id, page, what, urgency="annoying", share=False):
    what = (what or "").strip()
    if len(what) < 5:
        raise PilotError("Say what went wrong, in a sentence or two.")
    if urgency not in URGENCY:
        urgency = "annoying"
    page = (page or "").strip()[:200]
    if not page.startswith("/") or page.startswith("//"):
        page = ""
    page = page.split("?", 1)[0]                      # never the search words or filters
    pid = db.execute("""INSERT INTO problem_reports(clinic_id, user_id, page, what, urgency, shared) VALUES (?,?,?,?,?,?)""",
                     (clinic["id"], user_id, page or None, what[:2000], urgency, 1 if share else 0)).lastrowid
    audit.log(db, clinic["id"], user_id, "problem.report", detail=str(pid))
    return pid


def problems(db, cid, user=None, status=None):
    sql = ["SELECT r.*, u.display_name FROM problem_reports r LEFT JOIN users u ON u.id = r.user_id WHERE r.clinic_id = ?"]
    args = [cid]
    if user is not None and user["role"] != "manager":
        sql.append("AND r.user_id = ?")
        args.append(user["id"])
    if status:
        sql.append("AND r.status = ?")
        args.append(status)
    sql.append("ORDER BY CASE r.status WHEN 'open' THEN 0 ELSE 1 END, CASE r.urgency WHEN 'blocking' THEN 0 WHEN 'annoying' THEN 1 ELSE 2 END, r.id DESC")
    return db.execute(" ".join(sql), args).fetchall()


def set_problem(db, clinic, user_id, problem_id, status, note=None):
    if status not in PROBLEM_STATES:
        raise PilotError("Pick what happened to it.")
    row = db.execute("SELECT * FROM problem_reports WHERE id = ? AND clinic_id = ?", (problem_id, clinic["id"])).fetchone()
    if not row:
        raise LookupError("problem")
    closed = None if status == "open" else datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    db.execute("UPDATE problem_reports SET status = ?, note = ?, closed_at = ?, closed_by = ? WHERE id = ? AND clinic_id = ?",
               (status, (note or "").strip()[:1000] or row["note"], closed, None if status == "open" else user_id, problem_id, clinic["id"]))
    audit.log(db, clinic["id"], user_id, "problem.status", detail=f"{problem_id}:{status}")


def open_counts(db, cid):
    row = db.execute("""SELECT COUNT(*) AS n, SUM(CASE WHEN urgency = 'blocking' THEN 1 ELSE 0 END) AS blocking
                        FROM problem_reports WHERE clinic_id = ? AND status = 'open'""", (cid,)).fetchone()
    return {"open": row["n"] or 0, "blocking": row["blocking"] or 0}


# ------------------------------------------------------------------------------------------------------------ the error log
def new_ref():
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"          # no 0/O or 1/I, so it can be read out on the phone
    return "E-" + "".join(secrets.choice(alphabet) for _ in range(6))


def log_error(conn, ref, clinic_id, user_id, method, endpoint, exc):
    """Only the screen's name (never the address, which can hold a search), the kind of error and the line it came from."""
    where = ""
    tb = getattr(exc, "__traceback__", None)
    while tb is not None and tb.tb_next is not None:
        tb = tb.tb_next
    if tb is not None:
        f = tb.tb_frame.f_code
        where = f"{f.co_filename.rsplit('dental_os', 1)[-1].lstrip('/')}:{tb.tb_lineno} in {f.co_name}"
    conn.execute("""INSERT INTO error_log(ref, clinic_id, user_id, method, endpoint, kind, at_line) VALUES (?,?,?,?,?,?,?)""",
                 (ref, clinic_id, user_id, (method or "")[:8], (endpoint or "")[:120], type(exc).__name__[:80], where[:200]))
    conn.commit()


def errors(db, cid=None, days=14):
    since = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    if cid is None:
        return db.execute("""SELECT e.*, c.name AS clinic_name FROM error_log e LEFT JOIN clinics c ON c.id = e.clinic_id
                             WHERE e.created_at >= ? ORDER BY e.id DESC LIMIT 300""", (since,)).fetchall()
    return db.execute("SELECT * FROM error_log WHERE clinic_id = ? AND created_at >= ? ORDER BY id DESC LIMIT 300", (cid, since)).fetchall()


# ------------------------------------------------------------------------------------------------------------ side by side
KINDS = {
    "takings": ("Money taken (after refunds)", True),
    "appointments": ("Appointments on the day, not cancelled or missed", False),
    "new_patients": ("New patients registered", False),
}


def _utc_range(clinic, day):
    """The clinic's local day as a UTC from-to pair, for times the database keeps in UTC."""
    tz = ZoneInfo(clinic["timezone"] or "UTC")
    start = datetime.datetime.combine(day, datetime.time(0, 0), tz).astimezone(datetime.timezone.utc)
    end = start + datetime.timedelta(days=1)
    return start.strftime("%Y-%m-%d %H:%M:%S"), end.strftime("%Y-%m-%d %H:%M:%S")


def ours(db, clinic, day):
    """Dental OS's own figures for one day, worked out the way the side-by-side page explains."""
    from . import billing
    cid, iso = clinic["id"], day.isoformat()
    lo, hi = _utc_range(clinic, day)
    return {
        "takings": billing.daily_takings(db, cid, iso)["total"],
        "appointments": _count(db, """SELECT COUNT(*) FROM appointments WHERE clinic_id = ? AND status IN ('scheduled','complete')
                                      AND start_at >= ? AND start_at < ?""", (cid, f"{iso} 00:00", f"{(day + datetime.timedelta(days=1)).isoformat()} 00:00")),
        "new_patients": _count(db, "SELECT COUNT(*) FROM patients WHERE clinic_id = ? AND created_at >= ? AND created_at < ?", (cid, lo, hi)),
    }


def save_old_figures(db, clinic, user_id, day, values, note=None):
    """values: {kind: text as typed}. Money in the clinic's currency, counts as whole numbers. Blank leaves a figure out."""
    from .money import parse_money
    if day > now_local(clinic["timezone"]).date():
        raise PilotError("That day has not happened yet.")
    cid = clinic["id"]
    saved = 0
    for kind, (_label, is_money) in KINDS.items():
        raw = (values.get(kind) or "").strip()
        if not raw:
            continue
        if is_money:
            try:
                v = parse_money(raw, clinic["currency"], allow_zero=True)
            except ValueError:
                raise PilotError("Enter the old system's takings as an amount, like 1,250.00.")
        else:
            if not raw.isdigit():
                raise PilotError("Enter the counts as whole numbers.")
            v = int(raw)
        db.execute("DELETE FROM parallel_checks WHERE clinic_id = ? AND day = ? AND kind = ?", (cid, day.isoformat(), kind))
        db.execute("INSERT INTO parallel_checks(clinic_id, day, kind, old_value, note, created_by) VALUES (?,?,?,?,?,?)",
                   (cid, day.isoformat(), kind, v, (note or "").strip()[:300] or None, user_id))
        saved += 1
    if not saved:
        raise PilotError("Type in at least one of the old system's figures.")
    audit.log(db, cid, user_id, "parallel.save", detail=day.isoformat())
    return saved


def side_by_side(db, clinic, days=21):
    """The last few weeks, newest first: for each day with the old figures typed in, ours next to theirs."""
    cid = clinic["id"]
    rows = {}
    for r in db.execute("SELECT * FROM parallel_checks WHERE clinic_id = ? ORDER BY day DESC", (cid,)).fetchall():
        rows.setdefault(r["day"], {})[r["kind"]] = r
    out = []
    for iso in sorted(rows, reverse=True)[:days]:
        day = datetime.date.fromisoformat(iso)
        mine = ours(db, clinic, day)
        cells, note, match = [], None, True
        for kind in KINDS:
            r = rows[iso].get(kind)
            if r is None:
                cells.append({"kind": kind, "old": None, "ours": mine[kind], "same": None})
                continue
            note = note or r["note"]
            same = r["old_value"] == mine[kind]
            match = match and same
            cells.append({"kind": kind, "old": r["old_value"], "ours": mine[kind], "same": same})
        out.append({"day": iso, "cells": cells, "match": match, "note": note})
    streak = 0
    for d in out:
        if not d["match"]:
            break
        streak += 1
    return {"days": out, "streak": streak, "enough": streak >= SIDE_BY_SIDE_DAYS}
