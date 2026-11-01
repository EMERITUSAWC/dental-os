"""The rules that shape the diary beyond opening hours: each dentist's own working hours, their time off, time blocked out
(lunch, meetings, time held back for emergencies or new patients) and appointment types. No Flask in here.

How they combine for one dentist on one day:
1. Working time is the dentist's own hours for that weekday, or the clinic's opening hours if the dentist has none set.
   A day the clinic is closed is closed for everyone.
2. Time off is taken out of that.
3. A hard block (lunch, meeting, training, closed) cannot be booked.
4. A held-back block (emergency, new patients, reserved) is left free in the finder unless the appointment type is allowed to use
   that kind of block, or a person ticks to book into it anyway."""
from datetime import date, datetime, timedelta

from . import audit
from .timeutil import hhmm_to_minutes, parse_date

BLOCK_KINDS = {
    "lunch": "Lunch", "meeting": "Meeting", "training": "Training", "closed": "Closed",
    "emergency": "Kept for emergencies", "new_patients": "Kept for new patients", "reserved": "Held back",
}
HARD_KINDS = ("lunch", "meeting", "training", "closed")
HELD_KINDS = ("emergency", "new_patients", "reserved")
REPEATS = {"once": "Once", "daily": "Every working day", "weekly": "Every week on that day"}
COLOURS = {"blue": "Blue", "green": "Green", "teal": "Teal", "purple": "Purple", "orange": "Orange", "red": "Red",
           "pink": "Pink", "grey": "Grey"}
WEEKDAYS = {1: "Monday", 2: "Tuesday", 3: "Wednesday", 4: "Thursday", 5: "Friday", 6: "Saturday", 7: "Sunday"}
MAX_SPAN_DAYS = 400


class RulesError(Exception):
    pass


def _clinic_hours(clinic):
    days = {int(d) for d in clinic["working_days"].split(",") if d.strip()}
    return hhmm_to_minutes(clinic["open_time"]), hhmm_to_minutes(clinic["close_time"]), days


def _clean_time(raw):
    raw = (raw or "").strip()
    try:
        datetime.strptime(raw, "%H:%M")
    except ValueError:
        raise RulesError("Use times like 09:00 or 13:30.")
    return raw


def _clean_day(raw):
    try:
        return parse_date((raw or "").strip())
    except ValueError:
        raise RulesError("Use a date like 2026-11-30.")


def _provider(db, cid, provider_id):
    row = db.execute("SELECT * FROM providers WHERE id = ? AND clinic_id = ?", (provider_id, cid)).fetchone()
    if not row:
        raise RulesError("Pick a dentist from this clinic.")
    return row


# ---------------------------------------------------------------------------------------------------------------- hours
def hours_for(db, cid, provider_id):
    """{weekday: [(start, end), ...]} as HH:MM text, for the settings screen. Empty means the clinic's hours."""
    out = {}
    for r in db.execute("SELECT * FROM provider_hours WHERE clinic_id = ? AND provider_id = ? ORDER BY weekday, start_time",
                        (cid, provider_id)):
        out.setdefault(r["weekday"], []).append((r["start_time"], r["end_time"]))
    return out


def save_hours(db, clinic, user_id, provider_id, rows):
    """rows: [(weekday, start, end)]. Up to two spans a day (a split shift). An empty list goes back to the clinic's hours."""
    cid = clinic["id"]
    _provider(db, cid, provider_id)
    open_m, close_m, days = _clinic_hours(clinic)
    clean, per_day = [], {}
    for weekday, start, end in rows:
        if not (start or "").strip() and not (end or "").strip():
            continue
        start, end = _clean_time(start), _clean_time(end)
        try:
            weekday = int(weekday)
        except (TypeError, ValueError):
            raise RulesError("Pick a day of the week.")
        if weekday not in WEEKDAYS:
            raise RulesError("Pick a day of the week.")
        if weekday not in days:
            raise RulesError("The clinic is closed on one of those days. Open it under Settings first.")
        s, e = hhmm_to_minutes(start), hhmm_to_minutes(end)
        if s >= e:
            raise RulesError("Each start time must be before its end time.")
        if s < open_m or e > close_m:
            raise RulesError("A dentist's hours must sit inside the clinic's opening hours.")
        for os_, oe in per_day.get(weekday, []):
            if s < oe and e > os_:
                raise RulesError("Two spans on the same day overlap.")
        per_day.setdefault(weekday, []).append((s, e))
        if len(per_day[weekday]) > 2:
            raise RulesError("Use at most two spans a day.")
        clean.append((weekday, start, end))
    db.execute("DELETE FROM provider_hours WHERE clinic_id = ? AND provider_id = ?", (cid, provider_id))
    for weekday, start, end in clean:
        db.execute("INSERT INTO provider_hours(clinic_id, provider_id, weekday, start_time, end_time) VALUES (?,?,?,?,?)",
                   (cid, provider_id, weekday, start, end))
    audit.log(db, cid, user_id, "diary.hours", "provider", provider_id, f"{len(clean)} spans")
    return len(clean)


# ------------------------------------------------------------------------------------------------------------- time off
def add_time_off(db, clinic, user_id, provider_id, start_date, end_date, start_time=None, end_time=None, reason=None):
    cid = clinic["id"]
    _provider(db, cid, provider_id)
    s, e = _clean_day(start_date), _clean_day(end_date or start_date)
    if e < s:
        raise RulesError("The last day is before the first day.")
    if (e - s).days > MAX_SPAN_DAYS:
        raise RulesError("That is more than a year. Add it in parts.")
    st = _clean_time(start_time) if (start_time or "").strip() else None
    et = _clean_time(end_time) if (end_time or "").strip() else None
    if (st or et) and s != e:
        raise RulesError("Part of a day off can only be for one day. Leave the times empty for whole days.")
    if st and et and st >= et:
        raise RulesError("Each start time must be before its end time.")
    reason = (reason or "").strip()[:120] or None
    cur = db.execute("""INSERT INTO provider_time_off(clinic_id, provider_id, start_date, end_date, start_time, end_time, reason, created_by)
                        VALUES (?,?,?,?,?,?,?,?)""", (cid, provider_id, s.isoformat(), e.isoformat(), st, et, reason, user_id))
    audit.log(db, cid, user_id, "diary.time_off.add", "provider", provider_id, f"{s} to {e}")
    return cur.lastrowid


def remove_time_off(db, clinic, user_id, off_id):
    row = db.execute("SELECT * FROM provider_time_off WHERE id = ? AND clinic_id = ? AND removed_at IS NULL",
                     (off_id, clinic["id"])).fetchone()
    if not row:
        raise RulesError("That time off was not found.")
    db.execute("UPDATE provider_time_off SET removed_at = datetime('now'), removed_by = ? WHERE id = ?", (user_id, off_id))
    audit.log(db, clinic["id"], user_id, "diary.time_off.remove", "provider", row["provider_id"])


def time_off_list(db, cid, from_day):
    return db.execute(
        """SELECT t.*, p.name AS provider_name FROM provider_time_off t JOIN providers p ON p.id = t.provider_id
           WHERE t.clinic_id = ? AND t.removed_at IS NULL AND t.end_date >= ? ORDER BY t.start_date, p.name""",
        (cid, from_day.isoformat())).fetchall()


# --------------------------------------------------------------------------------------------------------------- blocks
def add_block(db, clinic, user_id, kind, start_date, start_time, end_time, provider_id=None, chair_id=None,
              repeat="once", end_date=None, label=None):
    cid = clinic["id"]
    if kind not in BLOCK_KINDS:
        raise RulesError("Pick what the time is kept for.")
    if repeat not in REPEATS:
        raise RulesError("Pick how often it repeats.")
    if provider_id:
        _provider(db, cid, provider_id)
    if chair_id and not db.execute("SELECT 1 FROM chairs WHERE id = ? AND clinic_id = ?", (chair_id, cid)).fetchone():
        raise RulesError("Pick a chair from this clinic.")
    s = _clean_day(start_date)
    e = _clean_day(end_date) if (repeat != "once" and (end_date or "").strip()) else (s + timedelta(days=MAX_SPAN_DAYS) if repeat != "once" else s)
    if e < s:
        raise RulesError("The last day is before the first day.")
    if (e - s).days > MAX_SPAN_DAYS:
        raise RulesError("That is more than a year. Add it in parts.")
    st, et = _clean_time(start_time), _clean_time(end_time)
    if st >= et:
        raise RulesError("Each start time must be before its end time.")
    label = (label or "").strip()[:60] or None
    cur = db.execute(
        """INSERT INTO blockouts(clinic_id, provider_id, chair_id, kind, label, start_date, end_date, start_time, end_time, repeat, created_by)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (cid, provider_id or None, chair_id or None, kind, label, s.isoformat(), e.isoformat(), st, et, repeat, user_id))
    audit.log(db, cid, user_id, "diary.block.add", "blockout", cur.lastrowid, kind)
    return cur.lastrowid


def remove_block(db, clinic, user_id, block_id):
    row = db.execute("SELECT * FROM blockouts WHERE id = ? AND clinic_id = ? AND removed_at IS NULL",
                     (block_id, clinic["id"])).fetchone()
    if not row:
        raise RulesError("That blocked time was not found.")
    db.execute("UPDATE blockouts SET removed_at = datetime('now'), removed_by = ? WHERE id = ?", (user_id, block_id))
    audit.log(db, clinic["id"], user_id, "diary.block.remove", "blockout", block_id)


def block_list(db, cid, from_day):
    return db.execute(
        """SELECT b.*, p.name AS provider_name, c.name AS chair_name FROM blockouts b
           LEFT JOIN providers p ON p.id = b.provider_id LEFT JOIN chairs c ON c.id = b.chair_id
           WHERE b.clinic_id = ? AND b.removed_at IS NULL AND b.end_date >= ? ORDER BY b.start_date, b.start_time""",
        (cid, from_day.isoformat())).fetchall()


def _block_on(b, day, clinic_days):
    if not (b["start_date"] <= day.isoformat() <= b["end_date"]):
        return False
    if b["repeat"] == "weekly":
        return day.isoweekday() == parse_date(b["start_date"]).isoweekday()
    if b["repeat"] == "daily":
        return day.isoweekday() in clinic_days
    return True


def blocks_on(db, clinic, day):
    """The blocks that apply on `day`, as dicts with minutes from midnight."""
    _o, _c, days = _clinic_hours(clinic)
    rows = db.execute("""SELECT * FROM blockouts WHERE clinic_id = ? AND removed_at IS NULL AND start_date <= ? AND end_date >= ?""",
                      (clinic["id"], day.isoformat(), day.isoformat())).fetchall()
    out = []
    for b in rows:
        if _block_on(b, day, days):
            out.append({"id": b["id"], "provider_id": b["provider_id"], "chair_id": b["chair_id"], "kind": b["kind"],
                        "label": b["label"], "start": hhmm_to_minutes(b["start_time"]), "end": hhmm_to_minutes(b["end_time"]),
                        "hard": b["kind"] in HARD_KINDS})
    return out


# --------------------------------------------------------------------------------------------------- working windows
def _subtract(spans, cut_s, cut_e):
    out = []
    for s, e in spans:
        if cut_e <= s or cut_s >= e:
            out.append((s, e))
            continue
        if s < cut_s:
            out.append((s, cut_s))
        if cut_e < e:
            out.append((cut_e, e))
    return out


def off_on(db, cid, provider_id, day):
    return db.execute(
        """SELECT * FROM provider_time_off WHERE clinic_id = ? AND provider_id = ? AND removed_at IS NULL
           AND start_date <= ? AND end_date >= ?""", (cid, provider_id, day.isoformat(), day.isoformat())).fetchall()


def windows(db, clinic, provider_id, day, hours_cache=None):
    """When this dentist can be booked on `day`: a list of (start, end) minutes, before blocks. Empty means not working."""
    open_m, close_m, days = _clinic_hours(clinic)
    if day.isoweekday() not in days:
        return []
    from . import closeddays
    if closeddays.name_on(db, clinic["id"], day):
        return []                                            # a public holiday or a day the clinic is closed
    rows = hours_cache.get(provider_id) if hours_cache is not None else None
    if rows is None:
        rows = db.execute("SELECT * FROM provider_hours WHERE clinic_id = ? AND provider_id = ?",
                          (clinic["id"], provider_id)).fetchall()
    if rows:
        spans = sorted((hhmm_to_minutes(r["start_time"]), hhmm_to_minutes(r["end_time"]))
                       for r in rows if r["weekday"] == day.isoweekday())
    else:
        spans = [(open_m, close_m)]
    for off in off_on(db, clinic["id"], provider_id, day):
        s = hhmm_to_minutes(off["start_time"]) if off["start_time"] else 0
        e = hhmm_to_minutes(off["end_time"]) if off["end_time"] else 24 * 60
        spans = _subtract(spans, s, e)
    return spans


def has_own_hours(db, cid, provider_id):
    return bool(db.execute("SELECT 1 FROM provider_hours WHERE clinic_id = ? AND provider_id = ? LIMIT 1",
                           (cid, provider_id)).fetchone())


def check(db, clinic, provider_id, chair_id, start, duration, type_row=None, allow_held=False):
    """Raises RulesError if the dentist is not working then, or the time is blocked. Opening hours are checked elsewhere."""
    day = start.date() if isinstance(start, datetime) else start
    s = start.hour * 60 + start.minute
    e = s + duration
    from . import closeddays
    if closeddays.name_on(db, clinic["id"], day):
        raise RulesError("The clinic is closed that day.")
    if provider_id is not None:
        spans = windows(db, clinic, provider_id, day)
        if not any(ws <= s and e <= we for ws, we in spans):
            if off_on(db, clinic["id"], provider_id, day):
                raise RulesError("The dentist is off at that time.")
            raise RulesError("The dentist is not working at that time.")
    for b in blocks_on(db, clinic, day):
        if not (b["start"] < e and b["end"] > s):
            continue
        if b["provider_id"] and b["provider_id"] != provider_id:
            continue
        if b["chair_id"] and chair_id is not None and b["chair_id"] != chair_id:
            continue
        if b["hard"]:
            raise RulesError("That time is blocked in the diary.")
        if allow_held or (type_row is not None and type_row["block_kind"] == b["kind"]):
            continue
        raise RulesError("That time is held back. Tick Book into held-back time to use it.")


def free_for(db, clinic, provider_id, day, s, e, chair_id, type_row=None, allow_held=False, ctx=None):
    """True when the span (minutes) is inside working time and clear of blocks that stop it. ctx caches the day's rules."""
    if ctx is None:
        ctx = day_context(db, clinic, day, [provider_id])
    spans = ctx["windows"].get(provider_id, [])
    if not any(ws <= s and e <= we for ws, we in spans):
        return False
    for b in ctx["blocks"]:
        if not (b["start"] < e and b["end"] > s):
            continue
        if b["provider_id"] and b["provider_id"] != provider_id:
            continue
        if b["chair_id"] and b["chair_id"] != chair_id:
            continue
        if b["hard"]:
            return False
        if allow_held or (type_row is not None and type_row["block_kind"] == b["kind"]):
            continue
        return False
    return True


def day_context(db, clinic, day, provider_ids):
    hours = {}
    for r in db.execute("SELECT * FROM provider_hours WHERE clinic_id = ?", (clinic["id"],)):
        hours.setdefault(r["provider_id"], []).append(r)
    cache = {pid: hours.get(pid, []) for pid in provider_ids}
    return {"windows": {pid: windows(db, clinic, pid, day, cache) for pid in provider_ids},
            "blocks": blocks_on(db, clinic, day)}


# ---------------------------------------------------------------------------------------------------- appointment types
def list_types(db, cid, active_only=False):
    q = "SELECT * FROM appointment_types WHERE clinic_id = ?" + (" AND active = 1" if active_only else "") + " ORDER BY active DESC, name"
    return db.execute(q, (cid,)).fetchall()


def get_type(db, cid, type_id):
    if not type_id:
        return None
    return db.execute("SELECT * FROM appointment_types WHERE id = ? AND clinic_id = ?", (type_id, cid)).fetchone()


def save_type(db, clinic, user_id, type_id, name, duration, colour, category, block_kind, procedure_ids, durations, categories):
    cid = clinic["id"]
    name = (name or "").strip()
    if not name or len(name) > 60:
        raise RulesError("Give the type a name of up to 60 characters.")
    try:
        duration = int(duration)
    except (TypeError, ValueError):
        duration = 0
    if duration not in durations:
        raise RulesError("Pick a length from the list.")
    if colour not in COLOURS:
        raise RulesError("Pick a colour from the list.")
    if category not in categories:
        raise RulesError("Pick a reason from the list.")
    block_kind = block_kind or None
    if block_kind and block_kind not in HELD_KINDS:
        raise RulesError("Pick a kind of held-back time from the list.")
    ids = []
    for raw in procedure_ids or []:
        try:
            pid = int(raw)
        except (TypeError, ValueError):
            continue
        if db.execute("SELECT 1 FROM procedure_codes WHERE id = ? AND clinic_id = ?", (pid, cid)).fetchone() and pid not in ids:
            ids.append(pid)
    clash = db.execute("SELECT id FROM appointment_types WHERE clinic_id = ? AND name = ? AND id != ?",
                       (cid, name, type_id or 0)).fetchone()
    if clash:
        raise RulesError("There is already a type with that name.")
    if type_id:
        if not get_type(db, cid, type_id):
            raise RulesError("That type was not found.")
        db.execute("""UPDATE appointment_types SET name=?, duration_min=?, colour=?, category=?, block_kind=?, procedure_ids=?
                      WHERE id=? AND clinic_id=?""", (name, duration, colour, category, block_kind, ",".join(map(str, ids)), type_id, cid))
    else:
        type_id = db.execute("""INSERT INTO appointment_types(clinic_id, name, duration_min, colour, category, block_kind, procedure_ids)
                                VALUES (?,?,?,?,?,?,?)""", (cid, name, duration, colour, category, block_kind, ",".join(map(str, ids)))).lastrowid
    audit.log(db, cid, user_id, "diary.type.save", "appointment_type", type_id)
    return type_id


def toggle_type(db, clinic, user_id, type_id):
    if not get_type(db, clinic["id"], type_id):
        raise RulesError("That type was not found.")
    db.execute("UPDATE appointment_types SET active = 1 - active WHERE id = ? AND clinic_id = ?", (type_id, clinic["id"]))
    audit.log(db, clinic["id"], user_id, "diary.type.toggle", "appointment_type", type_id)


def type_procedures(db, cid, type_row):
    ids = [int(x) for x in (type_row["procedure_ids"] or "").split(",") if x.strip().isdigit()] if type_row else []
    if not ids:
        return []
    rows = db.execute(f"SELECT * FROM procedure_codes WHERE clinic_id = ? AND id IN ({','.join('?' * len(ids))})",
                      (cid, *ids)).fetchall()
    order = {i: n for n, i in enumerate(ids)}
    return sorted(rows, key=lambda r: order[r["id"]])


STARTER_TYPES = [   # (name, minutes, colour, category, held-back kind). Added when a clinic asks for them, never by themselves.
    ("New patient examination", 45, "blue", "Consultation", "new_patients"),
    ("Check-up", 30, "green", "Consultation", None),
    ("Scale and polish", 30, "teal", "Cleaning & preventive care", None),
    ("Filling", 45, "purple", "Fillings & restorations", None),
    ("Root canal treatment", 90, "orange", "Root canal treatment", None),
    ("Extraction", 45, "red", "Tooth extractions", None),
    ("Emergency", 30, "red", "Emergency or pain", "emergency"),
    ("Crown preparation", 90, "pink", "Crowns", None),
]


def add_starter_types(db, clinic, user_id):
    added = 0
    for name, minutes, colour, category, kind in STARTER_TYPES:
        if db.execute("SELECT 1 FROM appointment_types WHERE clinic_id = ? AND name = ?", (clinic["id"], name)).fetchone():
            continue
        db.execute("""INSERT INTO appointment_types(clinic_id, name, duration_min, colour, category, block_kind)
                      VALUES (?,?,?,?,?,?)""", (clinic["id"], name, minutes, colour, category, kind))
        added += 1
    audit.log(db, clinic["id"], user_id, "diary.type.starters", None, None, str(added))
    return added


def monday_of(day):
    return day - timedelta(days=day.isoweekday() - 1)


def as_date(v):
    return v if isinstance(v, date) else parse_date(v)
