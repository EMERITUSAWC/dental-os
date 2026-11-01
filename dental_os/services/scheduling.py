"""Diary rules. No Flask in here, so this logic can move to any framework."""
from datetime import datetime, timedelta

from . import audit, diaryrules as R, recalls, serialize, webhooks
from .timeutil import (epoch, fmt_time, hhmm_to_minutes, now_local, parse_dt, to_str)

# Reasons for a visit. The first two start the six-monthly recall. A later version lets each clinic edit this list.
CATEGORIES = [
    "Consultation", "Cleaning & preventive care", "Fillings & restorations", "Root canal treatment",
    "Tooth extractions", "Crowns", "Bridges", "Dentures", "Implants", "Orthodontics & smile enhancement",
    "Dental imaging", "Emergency or pain", "Not sure yet",
]
DURATIONS = [10, 15, 20, 30, 40, 45, 60, 75, 90, 120, 150, 180, 240]
OCCUPYING = ("scheduled", "complete")

# Which status can move to which. Our own reading of how a diary behaves.
TRANSITIONS = {
    "planned": {"scheduled", "unscheduled", "asap"},
    "unscheduled": {"scheduled", "asap"},
    "asap": {"scheduled", "unscheduled"},
    "scheduled": {"complete", "broken"},
    "complete": set(),
    "broken": set(),
}


class SchedulingError(Exception):
    pass


def _hours(clinic):
    days = {int(d) for d in clinic["working_days"].split(",") if d.strip()}
    return hhmm_to_minutes(clinic["open_time"]), hhmm_to_minutes(clinic["close_time"]), days


def check_hours(clinic, start, duration):
    open_m, close_m, days = _hours(clinic)
    if start.isoweekday() not in days:
        raise SchedulingError(f"The clinic is closed on {start.strftime('%A')}s.")
    start_m = start.hour * 60 + start.minute
    if start_m < open_m or start_m + duration > close_m:
        raise SchedulingError(
            f"That time is outside opening hours ({clinic['open_time']} to {clinic['close_time']}).")


def check_rules(db, clinic, provider_id, chair_id, start, duration, type_row=None, allow_held=False):
    try:
        R.check(db, clinic, provider_id, chair_id, start, duration, type_row, allow_held)
    except R.RulesError as exc:
        raise SchedulingError(str(exc))


def _type(db, clinic, type_id):
    if not type_id:
        return None
    row = R.get_type(db, clinic["id"], type_id)
    if not row or not row["active"]:
        raise SchedulingError("Pick an appointment type from the list.")
    return row


def _plan_type_work(db, clinic, user_id, patient_id, appt_id, type_row, provider_id):
    """Booking a type with treatments attached: link the patient's matching planned work, or plan it."""
    from . import treatment as TR
    for code in R.type_procedures(db, clinic["id"], type_row):
        if not code["active"]:
            continue
        open_row = db.execute("""SELECT id FROM procedures WHERE clinic_id = ? AND patient_id = ? AND code_id = ? AND status = 'planned'
                                 AND appointment_id IS NULL ORDER BY id LIMIT 1""", (clinic["id"], patient_id, code["id"])).fetchone()
        if open_row:
            db.execute("UPDATE procedures SET appointment_id = ? WHERE id = ?", (appt_id, open_row["id"]))
            continue
        try:
            proc_id = TR.add_planned(db, clinic, user_id, patient_id, code["id"], provider_id=provider_id)
        except TR.TreatmentError:
            continue
        db.execute("UPDATE procedures SET appointment_id = ? WHERE id = ? AND clinic_id = ?", (appt_id, proc_id, clinic["id"]))


def _emit(db, clinic_id, appt_id, event):
    row = db.execute("SELECT * FROM appointments WHERE id = ? AND clinic_id = ?", (appt_id, clinic_id)).fetchone()
    if row:
        webhooks.emit(db, clinic_id, event, serialize.appointment(row))


def _owned(db, table, clinic_id, row_id):
    return db.execute(f"SELECT * FROM {table} WHERE id = ? AND clinic_id = ? AND active = 1",
                      (row_id, clinic_id)).fetchone()


def conflicts(db, clinic_id, patient_id, provider_id, chair_id, start, duration, exclude_id=None):
    start_e, end_e = epoch(start), epoch(start) + duration * 60
    return db.execute(
        """SELECT a.id, a.patient_id, a.provider_id, a.chair_id FROM appointments a
           WHERE a.clinic_id = ? AND a.status IN ('scheduled','complete') AND a.start_at IS NOT NULL
             AND (a.provider_id = ? OR a.chair_id = ? OR a.patient_id = ?)
             AND CAST(strftime('%s', a.start_at) AS INTEGER) < ?
             AND CAST(strftime('%s', a.start_at) AS INTEGER) + a.duration_min * 60 > ?
             AND a.id != ?""",
        (clinic_id, provider_id, chair_id, patient_id, end_e, start_e, exclude_id or 0),
    ).fetchall()


def _explain(rows, patient_id, provider_id, chair_id):
    reasons = []
    if any(r["provider_id"] == provider_id for r in rows):
        reasons.append("the dentist is already booked")
    if any(r["chair_id"] == chair_id for r in rows):
        reasons.append("the chair is already in use")
    if any(r["patient_id"] == patient_id for r in rows):
        reasons.append("the patient already has an appointment then")
    return "That time clashes: " + " and ".join(reasons) + "."


def _begin(db):
    if not db.in_transaction:
        db.execute("BEGIN IMMEDIATE")


def _check_refs(db, clinic_id, patient_id, provider_id, chair_id):
    if not db.execute("SELECT 1 FROM patients WHERE id = ? AND clinic_id = ? AND status = 'active'",
                      (patient_id, clinic_id)).fetchone():
        raise SchedulingError("That patient was not found, or is archived.")
    if provider_id is not None and not _owned(db, "providers", clinic_id, provider_id):
        raise SchedulingError("Pick a dentist from this clinic.")
    if chair_id is not None and not _owned(db, "chairs", clinic_id, chair_id):
        raise SchedulingError("Pick a chair from this clinic.")


def book(db, clinic, user_id, patient_id, provider_id, chair_id, start, duration,
         category="Consultation", notes=None, type_id=None, allow_held=False):
    """Put an appointment in the diary. Raises SchedulingError if it cannot be held."""
    if duration not in DURATIONS:
        raise SchedulingError("Pick a length from the list.")
    _begin(db)
    _check_refs(db, clinic["id"], patient_id, provider_id, chair_id)
    type_row = _type(db, clinic, type_id)
    check_hours(clinic, start, duration)
    if start < now_local(clinic["timezone"]) - timedelta(minutes=1):
        raise SchedulingError("That time has already passed.")
    check_rules(db, clinic, provider_id, chair_id, start, duration, type_row, allow_held)
    clash = conflicts(db, clinic["id"], patient_id, provider_id, chair_id, start, duration)
    if clash:
        raise SchedulingError(_explain(clash, patient_id, provider_id, chair_id))
    cur = db.execute(
        """INSERT INTO appointments(clinic_id, patient_id, provider_id, chair_id, start_at, duration_min,
                                    status, category, notes, created_by, type_id)
           VALUES (?,?,?,?,?,?, 'scheduled', ?,?,?,?)""",
        (clinic["id"], patient_id, provider_id, chair_id, to_str(start), duration,
         type_row["category"] if type_row else category, notes, user_id, type_row["id"] if type_row else None),
    )
    if type_row:
        _plan_type_work(db, clinic, user_id, patient_id, cur.lastrowid, type_row, provider_id)
    audit.log(db, clinic["id"], user_id, "appointment.book", "appointment", cur.lastrowid)
    _emit(db, clinic["id"], cur.lastrowid, "appointment.booked")
    return cur.lastrowid


def add_to_waiting_list(db, clinic, user_id, patient_id, status="asap", provider_id=None,
                        duration=30, category="Consultation", notes=None):
    if status not in ("asap", "unscheduled"):
        raise SchedulingError("Waiting list status must be ASAP or unscheduled.")
    _begin(db)
    _check_refs(db, clinic["id"], patient_id, provider_id, None)
    cur = db.execute(
        """INSERT INTO appointments(clinic_id, patient_id, provider_id, duration_min, status, category,
                                    notes, created_by)
           VALUES (?,?,?,?,?,?,?,?)""",
        (clinic["id"], patient_id, provider_id, duration, status, category, notes, user_id),
    )
    audit.log(db, clinic["id"], user_id, "appointment.waiting", "appointment", cur.lastrowid)
    return cur.lastrowid


def get_appointment(db, clinic_id, appt_id):
    return db.execute(
        """SELECT a.*, p.first_name, p.last_name, p.phone, p.reminders_ok,
                  pr.name AS provider_name, c.name AS chair_name, t.name AS type_name, t.colour AS type_colour
           FROM appointments a
           JOIN patients p ON p.id = a.patient_id
           LEFT JOIN providers pr ON pr.id = a.provider_id
           LEFT JOIN chairs c ON c.id = a.chair_id
           LEFT JOIN appointment_types t ON t.id = a.type_id
           WHERE a.id = ? AND a.clinic_id = ?""",
        (appt_id, clinic_id),
    ).fetchone()


def place(db, clinic, user_id, appt_id, provider_id, chair_id, start, duration=None, allow_held=False):
    """Give a time to a waiting-list entry, or move a scheduled appointment."""
    _begin(db)
    appt = get_appointment(db, clinic["id"], appt_id)
    if not appt:
        raise LookupError("appointment")
    if appt["status"] not in ("scheduled", "asap", "unscheduled", "planned"):
        raise SchedulingError("That appointment is closed and cannot be moved.")
    duration = duration or appt["duration_min"]
    _check_refs(db, clinic["id"], appt["patient_id"], provider_id, chair_id)
    check_hours(clinic, start, duration)
    if start < now_local(clinic["timezone"]) - timedelta(minutes=1):
        raise SchedulingError("That time has already passed.")
    check_rules(db, clinic, provider_id, chair_id, start, duration, R.get_type(db, clinic["id"], appt["type_id"]), allow_held)
    clash = conflicts(db, clinic["id"], appt["patient_id"], provider_id, chair_id, start, duration,
                      exclude_id=appt_id)
    if clash:
        raise SchedulingError(_explain(clash, appt["patient_id"], provider_id, chair_id))
    moved = appt["status"] == "scheduled"
    db.execute(
        """UPDATE appointments SET provider_id=?, chair_id=?, start_at=?, duration_min=?, status='scheduled',
                  confirmation='none', updated_at=datetime('now') WHERE id=? AND clinic_id=?""",
        (provider_id, chair_id, to_str(start), duration, appt_id, clinic["id"]),
    )
    audit.log(db, clinic["id"], user_id, "appointment.reschedule" if moved else "appointment.book",
              "appointment", appt_id)
    _emit(db, clinic["id"], appt_id, "appointment.booked")


def set_status(db, clinic_id, user_id, appt_id, new_status, break_reason=None):
    appt = get_appointment(db, clinic_id, appt_id)
    if not appt:
        raise LookupError("appointment")
    if new_status not in TRANSITIONS.get(appt["status"], set()):
        raise SchedulingError(f"A {appt['status']} appointment cannot become {new_status}.")
    if new_status == "broken" and break_reason not in ("missed", "cancelled"):
        raise SchedulingError("Say whether it was missed or cancelled.")
    db.execute(
        "UPDATE appointments SET status=?, break_reason=?, updated_at=datetime('now') WHERE id=? AND clinic_id=?",
        (new_status, break_reason if new_status == "broken" else None, appt_id, clinic_id),
    )
    audit.log(db, clinic_id, user_id, f"appointment.{new_status}", "appointment", appt_id, break_reason)
    _emit(db, clinic_id, appt_id, "appointment.updated")
    if new_status == "complete" and appt["category"] in recalls.RECALL_CATEGORIES and appt["start_at"]:
        recalls.set_due_after_visit(db, clinic_id, appt["patient_id"], parse_dt(appt["start_at"]).date())


def set_confirmation(db, clinic_id, user_id, appt_id, value):
    if value not in ("none", "sent", "confirmed", "declined", "failed"):
        raise SchedulingError("Unknown confirmation status.")
    appt = get_appointment(db, clinic_id, appt_id)
    if not appt:
        raise LookupError("appointment")
    if appt["status"] != "scheduled":
        raise SchedulingError("Only booked appointments can be confirmed.")
    db.execute(
        "UPDATE appointments SET confirmation=?, updated_at=datetime('now') WHERE id=? AND clinic_id=?",
        (value, appt_id, clinic_id),
    )
    audit.log(db, clinic_id, user_id, f"appointment.confirmation.{value}", "appointment", appt_id)
    _emit(db, clinic_id, appt_id, "appointment.updated")


def free_slots(db, clinic, provider_id, day, duration, now=None, type_row=None, allow_held=False, exclude_patient=None):
    """Start times on `day` when this dentist is working, the time is not blocked, and a chair is free."""
    open_m, close_m, days = _hours(clinic)
    if day.isoweekday() not in days:
        return []
    now = now or now_local(clinic["timezone"])
    chairs = [r["id"] for r in db.execute(
        "SELECT id FROM chairs WHERE clinic_id = ? AND active = 1 ORDER BY id", (clinic["id"],))]
    if not chairs or not _owned(db, "providers", clinic["id"], provider_id):
        return []
    ctx = R.day_context(db, clinic, day, [provider_id])
    if not ctx["windows"].get(provider_id):
        return []
    day_start = datetime(day.year, day.month, day.day)
    busy = db.execute(
        """SELECT provider_id, chair_id, patient_id, start_at, duration_min FROM appointments
           WHERE clinic_id = ? AND status IN ('scheduled','complete') AND start_at >= ? AND start_at < ?""",
        (clinic["id"], to_str(day_start), to_str(day_start + timedelta(days=1))),
    ).fetchall()
    spans = []
    for b in busy:
        st = parse_dt(b["start_at"])
        spans.append((b["provider_id"], b["chair_id"], epoch(st), epoch(st) + b["duration_min"] * 60, b["patient_id"]))
    out = []
    step = clinic["slot_minutes"]
    t = open_m
    while t + duration <= close_m:
        start = day_start + timedelta(minutes=t)
        m = t
        t += step
        if start <= now:
            continue
        s_e, e_e = epoch(start), epoch(start) + duration * 60
        overlapping = [sp for sp in spans if sp[2] < e_e and sp[3] > s_e]
        if any(sp[0] == provider_id for sp in overlapping):
            continue
        if exclude_patient and any(sp[4] == exclude_patient for sp in overlapping):
            continue
        taken = {sp[1] for sp in overlapping}
        free_chair = next((c for c in chairs if c not in taken
                           and R.free_for(db, clinic, provider_id, day, m, m + duration, c, type_row, allow_held, ctx)), None)
        if free_chair is not None:
            out.append({"time": start.strftime("%H:%M"), "label": fmt_time(start), "chair_id": free_chair})
    return out


def family_slots(db, clinic, provider_id, day, members, now=None, allow_held=False):
    """Times when several people can be seen one after another by the same dentist.
    members: [(patient_id, minutes)] in order. Returns [{time, label, plan: [(patient_id, start, minutes, chair_id)]}]."""
    if not members:
        return []
    out = []
    for first in free_slots(db, clinic, provider_id, day, members[0][1], now, allow_held=allow_held, exclude_patient=members[0][0]):
        start = parse_dt(f"{day.isoformat()} {first['time']}")
        plan, cursor, ok = [], start, True
        for patient_id, minutes in members:
            chair = _free_chair_at(db, clinic, provider_id, patient_id, cursor, minutes, allow_held)
            if chair is None:
                ok = False
                break
            plan.append((patient_id, cursor, minutes, chair))
            cursor = cursor + timedelta(minutes=minutes)
        if ok:
            out.append({"time": first["time"], "label": first["label"], "plan": plan})
    return out


def _free_chair_at(db, clinic, provider_id, patient_id, start, minutes, allow_held=False):
    try:
        check_hours(clinic, start, minutes)
        R.check(db, clinic, provider_id, None, start, minutes, None, allow_held)
    except (SchedulingError, R.RulesError):
        return None
    chairs = [r["id"] for r in db.execute("SELECT id FROM chairs WHERE clinic_id = ? AND active = 1 ORDER BY id", (clinic["id"],))]
    for c in chairs:
        try:
            R.check(db, clinic, provider_id, c, start, minutes, None, allow_held)
        except R.RulesError:
            continue
        if not conflicts(db, clinic["id"], patient_id, provider_id, c, start, minutes):
            return c
    return None


def book_family(db, clinic, user_id, provider_id, day, start_time, members, category="Consultation", notes=None, allow_held=False):
    """Book several people back to back in one go: all of them or none."""
    if len(members) < 2:
        raise SchedulingError("Pick at least two people to book together.")
    if len(members) > 8:
        raise SchedulingError("Book at most eight people together.")
    for _pid, minutes in members:
        if minutes not in DURATIONS:
            raise SchedulingError("Pick a length from the list.")
    _begin(db)
    cursor = parse_dt(f"{day.isoformat()} {start_time}")
    made = []
    for patient_id, minutes in members:
        chair = _free_chair_at(db, clinic, provider_id, patient_id, cursor, minutes, allow_held)
        if chair is None:
            raise SchedulingError("That time no longer fits everyone. Find the times again.")
        made.append(book(db, clinic, user_id, patient_id, provider_id, chair, cursor, minutes, category, notes, allow_held=allow_held))
        cursor = cursor + timedelta(minutes=minutes)
    return made


def gap_candidates(db, clinic, appt_id, limit=20):
    """For a missed or cancelled visit: people on the waiting list who could take the time it freed."""
    appt = get_appointment(db, clinic["id"], appt_id)
    if not appt or appt["status"] != "broken" or not appt["start_at"]:
        return None, []
    start = parse_dt(appt["start_at"])
    if start <= now_local(clinic["timezone"]):
        return appt, []
    rows = db.execute(
        """SELECT a.*, p.first_name, p.last_name, p.phone FROM appointments a JOIN patients p ON p.id = a.patient_id
           WHERE a.clinic_id = ? AND a.status IN ('asap','unscheduled') AND p.status = 'active'
             AND a.duration_min <= ? AND (a.provider_id IS NULL OR a.provider_id = ?)
           ORDER BY a.status = 'asap' DESC, a.created_at LIMIT ?""",
        (clinic["id"], appt["duration_min"], appt["provider_id"], limit * 3)).fetchall()
    out = []
    for r in rows:
        if conflicts(db, clinic["id"], r["patient_id"], appt["provider_id"], appt["chair_id"], start, r["duration_min"]):
            continue
        out.append(r)
        if len(out) >= limit:
            break
    return appt, out


# ------------------------------------------------------------------------- the visit itself: arrived, in the chair, left
STAGES = ("expected", "waiting", "in_chair", "left")


def stage(appt):
    if appt["dismissed_at"]:
        return "left"
    if appt["seated_at"]:
        return "in_chair"
    if appt["arrived_at"]:
        return "waiting"
    return "expected"


def move_stage(db, clinic, user_id, appt_id, action):
    """arrive, seat, leave or undo. Leaving also marks a booked visit as done, so recalls start as they always have."""
    appt = get_appointment(db, clinic["id"], appt_id)
    if not appt:
        raise LookupError("appointment")
    if appt["status"] not in ("scheduled", "complete"):
        raise SchedulingError("Only a booked visit can be checked in.")
    now = now_local(clinic["timezone"]).strftime("%Y-%m-%d %H:%M")
    current = stage(appt)
    if action == "arrive" and current == "expected":
        db.execute("UPDATE appointments SET arrived_at = ? WHERE id = ? AND clinic_id = ?", (now, appt_id, clinic["id"]))
    elif action == "seat" and current in ("expected", "waiting"):
        db.execute("UPDATE appointments SET arrived_at = COALESCE(arrived_at, ?), seated_at = ? WHERE id = ? AND clinic_id = ?",
                   (now, now, appt_id, clinic["id"]))
    elif action == "leave" and current in ("waiting", "in_chair"):
        db.execute("UPDATE appointments SET seated_at = COALESCE(seated_at, ?), dismissed_at = ? WHERE id = ? AND clinic_id = ?",
                   (now, now, appt_id, clinic["id"]))
        if appt["status"] == "scheduled":
            set_status(db, clinic["id"], user_id, appt_id, "complete")
    elif action == "undo" and current != "expected":
        col = {"waiting": "arrived_at", "in_chair": "seated_at", "left": "dismissed_at"}[current]
        db.execute(f"UPDATE appointments SET {col} = NULL WHERE id = ? AND clinic_id = ?", (appt_id, clinic["id"]))
    else:
        raise SchedulingError("That step does not fit where the patient is now.")
    audit.log(db, clinic["id"], user_id, f"appointment.stage.{action}", "appointment", appt_id)
    _emit(db, clinic["id"], appt_id, "appointment.updated")


def _minutes_between(a, b):
    return max(0, int((parse_dt(b) - parse_dt(a)).total_seconds() // 60))


def board(db, clinic, day):
    """Today's visits grouped by where the patient is, with waiting times."""
    now_s = now_local(clinic["timezone"]).strftime("%Y-%m-%d %H:%M")
    rows = db.execute(
        """SELECT a.*, p.first_name, p.last_name, pr.name AS provider_name, c.name AS chair_name, t.name AS type_name, t.colour AS type_colour
           FROM appointments a JOIN patients p ON p.id = a.patient_id
           LEFT JOIN providers pr ON pr.id = a.provider_id LEFT JOIN chairs c ON c.id = a.chair_id
           LEFT JOIN appointment_types t ON t.id = a.type_id
           WHERE a.clinic_id = ? AND a.start_at >= ? AND a.start_at < ? AND a.status IN ('scheduled','complete')
           ORDER BY a.start_at""", (clinic["id"], f"{day} 00:00", f"{day + timedelta(days=1)} 00:00")).fetchall()
    out = {k: [] for k in STAGES}
    for r in rows:
        st = stage(r)
        item = {"row": r, "late": 0, "waited": None, "in_chair": None}
        if st == "expected" and r["start_at"] < now_s:
            item["late"] = _minutes_between(r["start_at"], now_s)
        if r["arrived_at"]:
            item["waited"] = _minutes_between(r["arrived_at"], r["seated_at"] or now_s)
        if r["seated_at"]:
            item["in_chair"] = _minutes_between(r["seated_at"], r["dismissed_at"] or now_s)
        out[st].append(item)
    return out


def _grid_rows(minutes, open_m, step):
    return (minutes - open_m) // step + 2


def day_view(db, clinic, day):
    """Everything the diary screen needs for one day: appointments, blocked time, and time each dentist is not working."""
    open_m, close_m, _days = _hours(clinic)
    step = clinic["slot_minutes"]
    providers = db.execute(
        "SELECT * FROM providers WHERE clinic_id = ? AND active = 1 ORDER BY id", (clinic["id"],)).fetchall()
    col = {p["id"]: i + 2 for i, p in enumerate(providers)}
    day_start = datetime(day.year, day.month, day.day)
    rows = db.execute(
        """SELECT a.*, p.first_name, p.last_name, c.name AS chair_name, t.name AS type_name, t.colour AS type_colour
           FROM appointments a
           JOIN patients p ON p.id = a.patient_id
           LEFT JOIN chairs c ON c.id = a.chair_id
           LEFT JOIN appointment_types t ON t.id = a.type_id
           WHERE a.clinic_id = ? AND a.start_at >= ? AND a.start_at < ?
           ORDER BY a.start_at""",
        (clinic["id"], to_str(day_start), to_str(day_start + timedelta(days=1))),
    ).fetchall()
    cells, broken = [], []
    for r in rows:
        if r["status"] == "broken":
            broken.append(r)
            continue
        if r["provider_id"] not in col:
            continue
        s = parse_dt(r["start_at"])
        offset = max(0, (s.hour * 60 + s.minute - open_m) // step)
        span = max(1, -(-r["duration_min"] // step))
        cells.append({"row": r, "col": col[r["provider_id"]], "grid_row": offset + 2, "span": span,
                      "time": fmt_time(s), "stage": stage(r)})
    ctx = R.day_context(db, clinic, day, list(col))
    shade, blocks, off = [], [], {}
    for pid, c in col.items():
        free = [(max(s, open_m), min(e, close_m)) for s, e in ctx["windows"].get(pid, []) if e > open_m and s < close_m]
        cursor = open_m
        for s, e in sorted(free) + [(close_m, close_m)]:
            if s > cursor:
                a = (cursor - open_m) // step
                b = -(-(s - open_m) // step)
                shade.append({"col": c, "grid_row": a + 2, "span": max(1, b - a)})
            cursor = max(cursor, e)
        rows_off = R.off_on(db, clinic["id"], pid, day)
        if rows_off:
            off[pid] = [r["reason"] or "" for r in rows_off]
    for b in ctx["blocks"]:
        s, e = max(b["start"], open_m), min(b["end"], close_m)
        if e <= s:
            continue
        a = (s - open_m) // step
        z = -(-(e - open_m) // step)
        targets = [col[b["provider_id"]]] if b["provider_id"] in col else ([] if b["provider_id"] else list(col.values()))
        for c in targets:
            blocks.append({"col": c, "grid_row": a + 2, "span": max(1, z - a), "kind": b["kind"], "label": b["label"],
                           "hard": b["hard"], "time": fmt_time(day_start + timedelta(minutes=b["start"]))})
    labels = []
    t = open_m
    while t < close_m:
        labels.append({"grid_row": (t - open_m) // step + 2,
                       "label": fmt_time(day_start + timedelta(minutes=t)) if t % 60 == 0 else ""})
        t += step
    return {"providers": providers, "cells": cells, "broken": broken, "labels": labels,
            "row_count": len(labels), "shade": shade, "blocks": blocks, "off": off}


def week_view(db, clinic, provider_id, monday):
    """One dentist's week: for each day, the visits, blocked time and whether they are working."""
    open_m, close_m, days = _hours(clinic)
    out = []
    for i in range(7):
        day = monday + timedelta(days=i)
        if day.isoweekday() not in days:
            continue
        ctx = R.day_context(db, clinic, day, [provider_id])
        appts = db.execute(
            """SELECT a.*, p.first_name, p.last_name, t.colour AS type_colour, t.name AS type_name FROM appointments a
               JOIN patients p ON p.id = a.patient_id LEFT JOIN appointment_types t ON t.id = a.type_id
               WHERE a.clinic_id = ? AND a.provider_id = ? AND a.start_at >= ? AND a.start_at < ? AND a.status != 'broken'
               ORDER BY a.start_at""", (clinic["id"], provider_id, f"{day} 00:00", f"{day + timedelta(days=1)} 00:00")).fetchall()
        blocks = [b for b in ctx["blocks"] if not b["provider_id"] or b["provider_id"] == provider_id]
        windows = ctx["windows"].get(provider_id, [])
        booked = sum(a["duration_min"] for a in appts)
        working = sum(e - s for s, e in windows)
        out.append({"day": day, "appts": appts, "blocks": blocks, "working": working, "booked": booked,
                    "windows": [(fmt_time(datetime(day.year, day.month, day.day) + timedelta(minutes=s)),
                                 fmt_time(datetime(day.year, day.month, day.day) + timedelta(minutes=e))) for s, e in windows],
                    "off": [r["reason"] or "" for r in R.off_on(db, clinic["id"], provider_id, day)]})
    return out
