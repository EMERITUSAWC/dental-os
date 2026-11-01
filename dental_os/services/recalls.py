"""Recalls: the standing reminder to bring a patient back. One per patient per type.
A recall is created or pushed forward when a check-up style visit is marked done."""
import calendar
from datetime import date, timedelta

from . import audit
from .timeutil import now_local, parse_date

RECALL_CATEGORIES = ("Consultation", "Cleaning & preventive care")


def ensure_default_type(db, clinic_id):
    row = db.execute("SELECT id FROM recall_types WHERE clinic_id=? AND active=1 ORDER BY id LIMIT 1", (clinic_id,)).fetchone()
    if row:
        return row["id"]
    cur = db.execute("INSERT INTO recall_types(clinic_id, name, interval_months) VALUES (?, 'Check-up and clean', 6)",
                     (clinic_id,))
    return cur.lastrowid


def add_months(d, months):
    index = d.month - 1 + months
    year, month = d.year + index // 12, index % 12 + 1
    return date(year, month, min(d.day, calendar.monthrange(year, month)[1]))


def set_due_after_visit(db, clinic_id, patient_id, visit_day, type_id=None):
    """Create or move the recall to visit date + interval. Resets contact status."""
    type_id = type_id or ensure_default_type(db, clinic_id)
    rtype = db.execute("SELECT * FROM recall_types WHERE id=? AND clinic_id=?", (type_id, clinic_id)).fetchone()
    if not rtype:
        return None
    due = add_months(visit_day, rtype["interval_months"]).isoformat()
    db.execute(
        """INSERT INTO recalls(clinic_id, patient_id, type_id, due_on, status) VALUES (?,?,?,?, 'due')
           ON CONFLICT(clinic_id, patient_id, type_id)
           DO UPDATE SET due_on = excluded.due_on, status = 'due', contacted_on = NULL, snoozed_until = NULL""",
        (clinic_id, patient_id, type_id, due))
    return due


def upcoming(db, clinic_id, today, horizon_days=30):
    """Recalls that are due now or soon, for patients with no future booking, minus snoozed ones."""
    limit = (today + timedelta(days=horizon_days)).isoformat()
    return db.execute(
        """SELECT r.*, p.first_name, p.last_name, p.phone, p.reminders_ok, t.name AS type_name
           FROM recalls r JOIN patients p ON p.id = r.patient_id JOIN recall_types t ON t.id = r.type_id
           WHERE r.clinic_id = ? AND r.status IN ('due','contacted') AND r.due_on <= ?
             AND p.status = 'active'
             AND (r.snoozed_until IS NULL OR r.snoozed_until <= ?)
             AND NOT EXISTS (SELECT 1 FROM appointments a WHERE a.clinic_id = r.clinic_id AND a.patient_id = r.patient_id
                             AND a.status = 'scheduled' AND a.start_at >= ?)
           ORDER BY r.due_on, p.last_name""",
        (clinic_id, limit, today.isoformat(), f"{today.isoformat()} 00:00")).fetchall()


def get(db, clinic_id, recall_id):
    return db.execute(
        """SELECT r.*, p.first_name, p.last_name, p.phone, p.reminders_ok FROM recalls r
           JOIN patients p ON p.id = r.patient_id WHERE r.id=? AND r.clinic_id=?""",
        (recall_id, clinic_id)).fetchone()


def snooze(db, clinic, user_id, recall_id, days):
    r = get(db, clinic["id"], recall_id)
    if not r:
        raise LookupError("recall")
    until = (now_local(clinic["timezone"]).date() + timedelta(days=days)).isoformat()
    db.execute("UPDATE recalls SET snoozed_until=? WHERE id=? AND clinic_id=?", (until, recall_id, clinic["id"]))
    audit.log(db, clinic["id"], user_id, "recall.snooze", "recall", recall_id, f"{days} days")


def disable(db, clinic_id, user_id, recall_id):
    if not get(db, clinic_id, recall_id):
        raise LookupError("recall")
    db.execute("UPDATE recalls SET status='disabled' WHERE id=? AND clinic_id=?", (recall_id, clinic_id))
    audit.log(db, clinic_id, user_id, "recall.disable", "recall", recall_id)


def mark_contacted(db, clinic, recall_id):
    db.execute("UPDATE recalls SET status='contacted', contacted_on=? WHERE id=? AND clinic_id=?",
               (now_local(clinic["timezone"]).date().isoformat(), recall_id, clinic["id"]))
