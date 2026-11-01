"""A log of contact that happens outside the system: phone calls in and out, people at the desk, letters and emails sent by
hand. Each entry can carry a follow-up date, and follow-ups that are due show on the Today screen."""
from . import audit
from .timeutil import now_local, parse_date

KINDS = {"call_in": "Phone call from the patient", "call_out": "Phone call to the patient", "in_person": "At the desk",
         "email": "Email", "letter": "Letter", "sms": "Text message", "other": "Other"}


class ContactError(Exception):
    pass


def add(db, clinic, user_id, pid, kind, summary, outcome=None, follow_up_on=None):
    cid = clinic["id"]
    if kind not in KINDS:
        raise ContactError("Pick what kind of contact it was.")
    summary = (summary or "").strip()[:1000]
    if not summary:
        raise ContactError("Write what it was about.")
    if not db.execute("SELECT 1 FROM patients WHERE id = ? AND clinic_id = ?", (pid, cid)).fetchone():
        raise ContactError("That patient was not found.")
    fu = None
    if (follow_up_on or "").strip():
        try:
            fu = parse_date(follow_up_on.strip())
        except ValueError:
            raise ContactError("Use a date like 2026-11-30.")
        if fu < now_local(clinic["timezone"]).date():
            raise ContactError("A follow-up date cannot be in the past.")
    lid = db.execute("""INSERT INTO contact_log(clinic_id, patient_id, kind, summary, outcome, follow_up_on, created_by)
                        VALUES (?,?,?,?,?,?,?)""",
                     (cid, pid, kind, summary, (outcome or "").strip()[:300] or None, fu.isoformat() if fu else None, user_id)).lastrowid
    audit.log(db, cid, user_id, "contact.add", "patient", pid, kind)
    return lid


def done(db, clinic, user_id, log_id):
    row = db.execute("SELECT * FROM contact_log WHERE id = ? AND clinic_id = ?", (log_id, clinic["id"])).fetchone()
    if not row or not row["follow_up_on"]:
        raise ContactError("That follow-up was not found.")
    db.execute("UPDATE contact_log SET follow_up_done = 1 WHERE id = ?", (log_id,))
    audit.log(db, clinic["id"], user_id, "contact.followed_up", "patient", row["patient_id"])
    return row["patient_id"]


def for_patient(db, cid, pid):
    return db.execute("""SELECT c.*, u.display_name AS by_name FROM contact_log c LEFT JOIN users u ON u.id = c.created_by
                         WHERE c.clinic_id = ? AND c.patient_id = ? ORDER BY c.id DESC""", (cid, pid)).fetchall()


def due(db, clinic):
    today = now_local(clinic["timezone"]).date().isoformat()
    return db.execute("""SELECT c.*, p.first_name, p.last_name FROM contact_log c JOIN patients p ON p.id = c.patient_id
                         WHERE c.clinic_id = ? AND c.follow_up_on IS NOT NULL AND c.follow_up_done = 0 AND c.follow_up_on <= ?
                         ORDER BY c.follow_up_on""", (clinic["id"], today)).fetchall()
