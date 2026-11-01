"""Diary rules. No Flask in here, so this logic can move to any framework."""
from datetime import datetime, timedelta

from . import audit, recalls, serialize, webhooks
from .timeutil import (epoch, fmt_time, hhmm_to_minutes, now_local, parse_dt, to_str)

# Reasons for a visit. The first two start the six-monthly recall. A later version lets each clinic edit this list.
CATEGORIES = [
    "Consultation", "Cleaning & preventive care", "Fillings & restorations", "Root canal treatment",
    "Tooth extractions", "Crowns", "Bridges", "Dentures", "Implants", "Orthodontics & smile enhancement",
    "Dental imaging", "Emergency or pain", "Not sure yet",
]
DURATIONS = [15, 30, 45, 60, 90, 120]
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
         category="Consultation", notes=None):
    """Put an appointment in the diary. Raises SchedulingError if it cannot be held."""
    if duration not in DURATIONS:
        raise SchedulingError("Pick a length from the list.")
    _begin(db)
    _check_refs(db, clinic["id"], patient_id, provider_id, chair_id)
    check_hours(clinic, start, duration)
    if start < now_local(clinic["timezone"]) - timedelta(minutes=1):
        raise SchedulingError("That time has already passed.")
    clash = conflicts(db, clinic["id"], patient_id, provider_id, chair_id, start, duration)
    if clash:
        raise SchedulingError(_explain(clash, patient_id, provider_id, chair_id))
    cur = db.execute(
        """INSERT INTO appointments(clinic_id, patient_id, provider_id, chair_id, start_at, duration_min,
                                    status, category, notes, created_by)
           VALUES (?,?,?,?,?,?, 'scheduled', ?,?,?)""",
        (clinic["id"], patient_id, provider_id, chair_id, to_str(start), duration,
         category, notes, user_id),
    )
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
                  pr.name AS provider_name, c.name AS chair_name
           FROM appointments a
           JOIN patients p ON p.id = a.patient_id
           LEFT JOIN providers pr ON pr.id = a.provider_id
           LEFT JOIN chairs c ON c.id = a.chair_id
           WHERE a.id = ? AND a.clinic_id = ?""",
        (appt_id, clinic_id),
    ).fetchone()


def place(db, clinic, user_id, appt_id, provider_id, chair_id, start, duration=None):
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


def free_slots(db, clinic, provider_id, day, duration, now=None):
    """Start times on `day` when this dentist and at least one chair are free."""
    open_m, close_m, days = _hours(clinic)
    if day.isoweekday() not in days:
        return []
    now = now or now_local(clinic["timezone"])
    chairs = [r["id"] for r in db.execute(
        "SELECT id FROM chairs WHERE clinic_id = ? AND active = 1 ORDER BY id", (clinic["id"],))]
    if not chairs or not _owned(db, "providers", clinic["id"], provider_id):
        return []
    day_start = datetime(day.year, day.month, day.day)
    busy = db.execute(
        """SELECT provider_id, chair_id, start_at, duration_min FROM appointments
           WHERE clinic_id = ? AND status IN ('scheduled','complete') AND start_at >= ? AND start_at < ?""",
        (clinic["id"], to_str(day_start), to_str(day_start + timedelta(days=1))),
    ).fetchall()
    spans = []
    for b in busy:
        s = parse_dt(b["start_at"])
        spans.append((b["provider_id"], b["chair_id"], epoch(s), epoch(s) + b["duration_min"] * 60))
    out = []
    step = clinic["slot_minutes"]
    t = open_m
    while t + duration <= close_m:
        start = day_start + timedelta(minutes=t)
        t += step
        if start <= now:
            continue
        s_e, e_e = epoch(start), epoch(start) + duration * 60
        overlapping = [sp for sp in spans if sp[2] < e_e and sp[3] > s_e]
        if any(sp[0] == provider_id for sp in overlapping):
            continue
        taken = {sp[1] for sp in overlapping}
        free_chair = next((c for c in chairs if c not in taken), None)
        if free_chair is not None:
            out.append({"time": start.strftime("%H:%M"), "label": fmt_time(start), "chair_id": free_chair})
    return out


def day_view(db, clinic, day):
    """Everything the diary screen needs for one day."""
    open_m, close_m, _days = _hours(clinic)
    step = clinic["slot_minutes"]
    providers = db.execute(
        "SELECT * FROM providers WHERE clinic_id = ? AND active = 1 ORDER BY id", (clinic["id"],)).fetchall()
    col = {p["id"]: i + 2 for i, p in enumerate(providers)}
    day_start = datetime(day.year, day.month, day.day)
    rows = db.execute(
        """SELECT a.*, p.first_name, p.last_name, c.name AS chair_name FROM appointments a
           JOIN patients p ON p.id = a.patient_id
           LEFT JOIN chairs c ON c.id = a.chair_id
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
                      "time": fmt_time(s)})
    labels = []
    t = open_m
    while t < close_m:
        labels.append({"grid_row": (t - open_m) // step + 2,
                       "label": fmt_time(day_start + timedelta(minutes=t)) if t % 60 == 0 else ""})
        t += step
    return {"providers": providers, "cells": cells, "broken": broken, "labels": labels,
            "row_count": len(labels)}
