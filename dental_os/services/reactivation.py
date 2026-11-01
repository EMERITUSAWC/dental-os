"""Patients the clinic has not seen for a while (version 0.41).

The recall list covers patients with a check-up due. This list catches the ones who slipped through: seen before, but not for a set
number of months (18 by default), with nothing booked and nothing on the waiting list. For each it shows the last visit, the last time
anyone contacted them, and any treatment planned but never done. Staff can queue a "we have not seen you for a while" message (sent
like any other message from the send list, only to patients who agreed to messages, never with treatment details), write down a call,
or put someone aside for a while (moved away, seeing another dentist) with a reason. No web framework here."""
import calendar
import datetime

from . import audit, outbox
from .timeutil import now_local

MONTHS = (6, 12, 18, 24, 36)
REASONS = {"moved": "Moved away", "elsewhere": "Sees another dentist", "asked": "Asked not to be contacted", "later": "Try again later",
           "other": "Other"}


class ReactError(ValueError):
    pass


def _months_ago(day, n):
    """The same day n months earlier (or later, for a negative n), moved back to the month's last day where needed."""
    y, m = divmod(day.year * 12 + day.month - 1 - n, 12)
    return datetime.date(y, m + 1, min(day.day, calendar.monthrange(y, m + 1)[1]))


def lapsed(db, clinic, months=18, today=None, limit=500):
    cid = clinic["id"]
    today = today or now_local(clinic["timezone"]).date()
    cutoff = _months_ago(today, months).isoformat()
    t = today.isoformat()
    rows = db.execute("""
        SELECT p.id, p.first_name, p.last_name, p.phone, p.reminders_ok, p.dob, v.last_visit,
               (SELECT MAX(substr(o.done_at, 1, 10)) FROM outbox o WHERE o.clinic_id = p.clinic_id AND o.patient_id = p.id AND o.kind = 'reactivation' AND o.status = 'sent') AS last_message,
               (SELECT MAX(substr(c.created_at, 1, 10)) FROM contact_log c WHERE c.clinic_id = p.clinic_id AND c.patient_id = p.id) AS last_contact,
               (SELECT COALESCE(SUM(pr.fee_p), 0) FROM procedures pr WHERE pr.clinic_id = p.clinic_id AND pr.patient_id = p.id AND pr.status = 'planned') AS planned_p,
               (SELECT COUNT(*) FROM outbox o WHERE o.clinic_id = p.clinic_id AND o.patient_id = p.id AND o.kind = 'reactivation' AND o.status = 'queued') AS queued
        FROM patients p
        JOIN (SELECT patient_id, MAX(substr(start_at, 1, 10)) AS last_visit FROM appointments
              WHERE clinic_id = ? AND status = 'complete' GROUP BY patient_id) v ON v.patient_id = p.id
        WHERE p.clinic_id = ? AND p.status = 'active' AND v.last_visit < ?
          AND NOT EXISTS (SELECT 1 FROM appointments a WHERE a.patient_id = p.id AND a.clinic_id = ?
                          AND ((a.status = 'scheduled' AND substr(a.start_at, 1, 10) >= ?) OR a.status IN ('asap','planned')))
          AND NOT EXISTS (SELECT 1 FROM reactivation_holds h WHERE h.patient_id = p.id AND h.clinic_id = ? AND h.until_on >= ?)
        ORDER BY v.last_visit DESC LIMIT ?""", (cid, cid, cutoff, cid, t, cid, t, limit)).fetchall()
    return rows


def queue_message(db, clinic, user_id, pid, today=None):
    cid = clinic["id"]
    p = db.execute("SELECT * FROM patients WHERE id = ? AND clinic_id = ? AND status = 'active'", (pid, cid)).fetchone()
    if not p:
        raise ReactError("That patient was not found.")
    if not p["reminders_ok"]:
        raise ReactError("This patient has not agreed to messages. Call them instead, and write the call down.")
    today = today or now_local(clinic["timezone"]).date()
    key = f"react:{pid}:{today.strftime('%Y-%m')}"
    if not outbox._enqueue(db, cid, pid, "reactivation", "care", today.isoformat(), key):
        raise ReactError("A message for this patient is already on the send list this month.")
    audit.log(db, cid, user_id, "reactivation.queue", "patient", pid)


def queue_all(db, clinic, user_id, months=18, today=None):
    n = 0
    for r in lapsed(db, clinic, months, today):
        if r["reminders_ok"] and not r["queued"]:
            try:
                queue_message(db, clinic, user_id, r["id"], today)
                n += 1
            except ReactError:
                pass
    return n


def hold(db, clinic, user_id, pid, reason, months, note=None, today=None):
    cid = clinic["id"]
    if not db.execute("SELECT 1 FROM patients WHERE id = ? AND clinic_id = ?", (pid, cid)).fetchone():
        raise ReactError("That patient was not found.")
    if reason not in REASONS:
        raise ReactError("Pick why this patient is put aside.")
    if not isinstance(months, int) or not 1 <= months <= 120:
        raise ReactError("Put a patient aside for 1 to 120 months.")
    today = today or now_local(clinic["timezone"]).date()
    until = _months_ago(today, -months).isoformat()
    db.execute("INSERT INTO reactivation_holds(clinic_id, patient_id, reason, note, until_on, created_by) VALUES (?,?,?,?,?,?)",
               (cid, pid, reason, (note or "").strip()[:300] or None, until, user_id))
    audit.log(db, cid, user_id, "reactivation.hold", "patient", pid, reason)
    return until
