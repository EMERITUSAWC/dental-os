"""The time clock: staff clock in and out, and a manager sees time cards by week. A manager can correct an entry, with a reason that
is kept. Times are clinic time. This records hours worked; it does not work out pay."""
from datetime import datetime, timedelta

from . import audit
from .timeutil import now_local, parse_date, parse_dt

MAX_SHIFT_HOURS = 16


class ClockError(Exception):
    pass


def _now(clinic):
    return now_local(clinic["timezone"]).replace(second=0, microsecond=0)


def open_entry(db, cid, user_id):
    return db.execute("SELECT * FROM time_entries WHERE clinic_id = ? AND user_id = ? AND clock_out IS NULL ORDER BY id DESC LIMIT 1",
                      (cid, user_id)).fetchone()


def clock_in(db, clinic, user):
    cid = clinic["id"]
    if open_entry(db, cid, user["id"]):
        raise ClockError("You are already clocked in.")
    eid = db.execute("INSERT INTO time_entries(clinic_id, user_id, clock_in) VALUES (?,?,?)",
                     (cid, user["id"], _now(clinic).strftime("%Y-%m-%d %H:%M"))).lastrowid
    audit.log(db, cid, user["id"], "clock.in", "time_entry", eid)
    return eid


def clock_out(db, clinic, user, break_minutes=0):
    cid = clinic["id"]
    e = open_entry(db, cid, user["id"])
    if not e:
        raise ClockError("You are not clocked in.")
    try:
        break_minutes = int(break_minutes or 0)
    except (TypeError, ValueError):
        raise ClockError("Give the break in whole minutes.")
    now = _now(clinic)
    worked = (now - parse_dt(e["clock_in"])).total_seconds() // 60
    if not 0 <= break_minutes <= max(worked, 0):
        raise ClockError("The break cannot be longer than the time worked.")
    db.execute("UPDATE time_entries SET clock_out = ?, break_minutes = ? WHERE id = ?", (now.strftime("%Y-%m-%d %H:%M"), break_minutes, e["id"]))
    audit.log(db, cid, user["id"], "clock.out", "time_entry", e["id"])
    return e["id"]


def minutes(entry, now=None):
    end = parse_dt(entry["clock_out"]) if entry["clock_out"] else now
    if end is None:
        return 0
    return max(int((end - parse_dt(entry["clock_in"])).total_seconds() // 60) - (entry["break_minutes"] or 0), 0)


def _check(clock_in, clock_out, break_minutes, need_out=False):
    try:
        cin = datetime.strptime((clock_in or "").strip(), "%Y-%m-%d %H:%M")
        cout = datetime.strptime(clock_out.strip(), "%Y-%m-%d %H:%M") if (clock_out or "").strip() else None
        brk = int(break_minutes or 0)
    except ValueError:
        raise ClockError("Use times like 2026-11-30 08:15.")
    if need_out and not cout:
        raise ClockError("A shift added by a manager needs a clock-out time.")
    if cout and cout <= cin:
        raise ClockError("Clocking out must be after clocking in.")
    if cout and (cout - cin) > timedelta(hours=MAX_SHIFT_HOURS):
        raise ClockError("That is longer than one shift can be. Split it into two entries.")
    if brk < 0 or (cout and brk > (cout - cin).total_seconds() // 60):
        raise ClockError("The break cannot be longer than the time worked.")
    return cin.strftime("%Y-%m-%d %H:%M"), (cout.strftime("%Y-%m-%d %H:%M") if cout else None), brk


def correct(db, clinic, manager_id, entry_id, clock_in, clock_out, break_minutes, reason):
    cid = clinic["id"]
    e = db.execute("SELECT * FROM time_entries WHERE id = ? AND clinic_id = ?", (entry_id, cid)).fetchone()
    if not e:
        raise ClockError("That entry was not found.")
    reason = (reason or "").strip()[:200]
    if not reason:
        raise ClockError("Say why the entry is being corrected.")
    cin, cout, brk = _check(clock_in, clock_out, break_minutes)
    db.execute("""UPDATE time_entries SET clock_in = ?, clock_out = ?, break_minutes = ?, edited_by = ?, edited_at = datetime('now'),
                  edit_reason = ? WHERE id = ?""", (cin, cout, brk, manager_id, reason, entry_id))
    audit.log(db, cid, manager_id, "clock.correct", "time_entry", entry_id, reason[:80])


def add_missed(db, clinic, manager_id, user_id, clock_in, clock_out, break_minutes, reason):
    """A manager adds a shift someone forgot to clock."""
    cid = clinic["id"]
    if not db.execute("SELECT 1 FROM users WHERE id = ? AND clinic_id = ?", (user_id, cid)).fetchone():
        raise ClockError("Pick someone from this clinic.")
    reason = (reason or "").strip()[:200]
    if not reason:
        raise ClockError("Say why the entry is being corrected.")
    cin, cout, brk = _check(clock_in, clock_out, break_minutes, need_out=True)
    eid = db.execute("""INSERT INTO time_entries(clinic_id, user_id, clock_in, clock_out, break_minutes, edited_by, edited_at, edit_reason)
                        VALUES (?,?,?,?,?,?,datetime('now'),?)""", (cid, user_id, cin, cout, brk, manager_id, reason)).lastrowid
    audit.log(db, cid, manager_id, "clock.add", "time_entry", eid, reason[:80])
    return eid


def week(db, clinic, monday):
    """Time cards for the week starting on monday: per person, minutes each day and the total."""
    cid = clinic["id"]
    end = monday + timedelta(days=7)
    now = _now(clinic)
    rows = db.execute("""SELECT e.*, u.display_name FROM time_entries e JOIN users u ON u.id = e.user_id
                         WHERE e.clinic_id = ? AND e.clock_in >= ? AND e.clock_in < ? ORDER BY u.display_name, e.clock_in""",
                      (cid, f"{monday} 00:00", f"{end} 00:00")).fetchall()
    people = {}
    for r in rows:
        p = people.setdefault(r["user_id"], {"name": r["display_name"], "days": [0] * 7, "total": 0, "entries": [], "open": False})
        m = minutes(r, now)
        i = (parse_date(r["clock_in"][:10]) - monday).days
        p["days"][i] += m
        p["total"] += m
        p["entries"].append({"e": r, "minutes": m})
        p["open"] = p["open"] or r["clock_out"] is None
    return sorted(people.values(), key=lambda p: p["name"])


def csv_rows(db, clinic, monday):
    out = [["Name", "Clock in", "Clock out", "Break minutes", "Minutes worked", "Corrected", "Reason"]]
    for p in week(db, clinic, monday):
        for x in p["entries"]:
            e = x["e"]
            out.append([p["name"], e["clock_in"], e["clock_out"] or "", e["break_minutes"] or 0, x["minutes"], "yes" if e["edited_by"] else "",
                        e["edit_reason"] or ""])
    return out


def hm(m):
    return "%d:%02d" % divmod(int(m), 60)
