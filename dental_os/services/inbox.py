"""Replies from patients. With no official WhatsApp connection the system cannot read replies by itself, so staff log what a
patient wrote (type or paste it) and it shows up here until someone has read it. A clear YES can confirm the next visit."""
import re
from datetime import timedelta

from . import audit, scheduling
from .messaging import wa_link
from .timeutil import now_local

YES = {"yes", "y", "yes please", "yep", "yeah", "confirm", "confirmed", "ok", "okay", "i confirm", "will be there", "yes i will be there"}
REPLY_WINDOW_DAYS = 7


class InboxError(Exception):
    pass


def looks_like_yes(body):
    return re.sub(r"[^a-z ]", "", (body or "").lower()).strip() in YES


def _patient(db, cid, pid):
    return db.execute("SELECT * FROM patients WHERE id = ? AND clinic_id = ?", (pid, cid)).fetchone()


def log_inbound(db, clinic, user_id, pid, body, confirms=False):
    cid = clinic["id"]
    p = _patient(db, cid, pid)
    if not p:
        raise LookupError("patient")
    body = (body or "").strip()
    if not body:
        raise InboxError("Type or paste what the patient wrote.")
    if len(body) > 1000:
        raise InboxError("That is too long. Keep the important part.")
    if confirms:
        now = now_local(clinic["timezone"]).strftime("%Y-%m-%d %H:%M")
        nxt = db.execute("""SELECT id FROM appointments WHERE clinic_id = ? AND patient_id = ? AND status = 'scheduled' AND start_at >= ?
                            ORDER BY start_at LIMIT 1""", (cid, pid, now)).fetchone()
        if not nxt:
            raise InboxError("There is no booked visit to confirm.")
        scheduling.set_confirmation(db, cid, user_id, nxt["id"], "confirmed")
    cur = db.execute("""INSERT INTO messages(clinic_id, patient_id, direction, kind, body, status, created_by)
                        VALUES (?,?, 'in', 'reply', ?, 'received', ?)""", (cid, pid, body, user_id))
    audit.log(db, cid, user_id, "message.in", "message", cur.lastrowid)
    return cur.lastrowid


def unread(db, cid):
    """One row per patient with replies not yet read, newest first."""
    return db.execute(
        """SELECT p.id AS patient_id, p.first_name, p.last_name, p.phone, COUNT(*) AS n, MAX(m.created_at) AS last_at,
                  (SELECT body FROM messages x WHERE x.clinic_id = m.clinic_id AND x.patient_id = p.id AND x.direction = 'in'
                   ORDER BY x.id DESC LIMIT 1) AS last_body
           FROM messages m JOIN patients p ON p.id = m.patient_id
           WHERE m.clinic_id = ? AND m.direction = 'in' AND m.read_at IS NULL
           GROUP BY p.id, m.clinic_id ORDER BY last_at DESC""", (cid,)).fetchall()


def thread(db, cid, pid, limit=40):
    return db.execute("""SELECT m.*, u.display_name AS user_name FROM messages m LEFT JOIN users u ON u.id = m.created_by
                         WHERE m.clinic_id = ? AND m.patient_id = ? ORDER BY m.id DESC LIMIT ?""", (cid, pid, limit)).fetchall()


def mark_read(db, cid, pid):
    db.execute("UPDATE messages SET read_at = datetime('now') WHERE clinic_id = ? AND patient_id = ? AND direction = 'in' AND read_at IS NULL", (cid, pid))


def reply(db, clinic, user_id, pid, text):
    """Log a message to a patient and return the WhatsApp link. Allowed if they wrote to us in the last 7 days, or agreed to messages."""
    cid = clinic["id"]
    p = _patient(db, cid, pid)
    if not p:
        raise LookupError("patient")
    text = (text or "").strip()
    if not text:
        raise InboxError("Write the message first.")
    if len(text) > 1000:
        raise InboxError("That message is too long.")
    since = (now_local(clinic["timezone"]).date() - timedelta(days=REPLY_WINDOW_DAYS)).isoformat()
    wrote_lately = db.execute("SELECT 1 FROM messages WHERE clinic_id = ? AND patient_id = ? AND direction = 'in' AND date(created_at) >= ?",
                              (cid, pid, since)).fetchone()
    if not (wrote_lately or p["reminders_ok"]):
        raise InboxError("This patient has not agreed to messages, and has not written to us in the last week.")
    cur = db.execute("INSERT INTO messages(clinic_id, patient_id, kind, body, status, created_by) VALUES (?,?, 'reply', ?, 'opened', ?)",
                     (cid, pid, text, user_id))
    mark_read(db, cid, pid)
    audit.log(db, cid, user_id, "message.reply", "message", cur.lastrowid)
    return wa_link(p["phone"], text)
