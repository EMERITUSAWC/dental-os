"""WhatsApp messages. Version 1 prepares the text and opens WhatsApp for a person to press send.
The official WhatsApp Business API can replace `open_whatsapp` later without changing the screens."""
from urllib.parse import quote

from . import audit, i18n
from .phone import wa_digits
from .timeutil import parse_dt


def wa_link(e164, text):
    return f"https://wa.me/{wa_digits(e164)}?text={quote(text)}"


def appointment_text(clinic, patient, appt, kind, db=None):
    """Only name, dentist, day and time. Never put treatment details in a WhatsApp message. Uses the clinic's own wording when db is given."""
    from . import msgtemplates as MT
    start = parse_dt(appt["start_at"])
    lang = i18n.patient_lang(patient, clinic)
    body = MT.body_for(db, clinic["id"], "reminder" if kind == "reminder" else "confirmation", lang) if db is not None \
        else MT.KINDS["reminder" if kind == "reminder" else "confirmation"][1]
    return MT.render(body, {"first_name": patient["first_name"], "clinic": clinic["name"],
                            "dentist": appt["provider_name"] or i18n.t(lang, "our team"),
                            "day": i18n.fmt_day_date(start, lang), "time": i18n.fmt_time(start, lang)})


def prepare(db, clinic, user_id, patient, appt, kind):
    """Log the message and return the WhatsApp link. Refuses if the patient has not agreed to messages."""
    if kind not in ("confirmation", "reminder"):
        raise ValueError("Unknown message kind.")
    if not patient["reminders_ok"]:
        raise PermissionError("This patient has not agreed to appointment messages.")
    if appt["status"] != "scheduled" or not appt["start_at"]:
        raise PermissionError("Only booked appointments can be messaged.")
    text = appointment_text(clinic, patient, appt, kind, db)
    cur = db.execute(
        """INSERT INTO messages(clinic_id, patient_id, appointment_id, kind, body, status, created_by)
           VALUES (?,?,?,?,?, 'opened', ?)""",
        (clinic["id"], patient["id"], appt["id"], kind, text, user_id),
    )
    if appt["confirmation"] == "none":
        db.execute("UPDATE appointments SET confirmation='sent', updated_at=datetime('now') WHERE id=? AND clinic_id=?",
                   (appt["id"], clinic["id"]))
    audit.log(db, clinic["id"], user_id, f"message.{kind}", "message", cur.lastrowid)
    return wa_link(patient["phone"], text)


def history(db, clinic_id, patient_id, limit=20):
    return db.execute(
        """SELECT m.*, u.display_name AS user_name FROM messages m
           LEFT JOIN users u ON u.id = m.created_by
           WHERE m.clinic_id = ? AND m.patient_id = ? ORDER BY m.id DESC LIMIT ?""",
        (clinic_id, patient_id, limit),
    ).fetchall()


def recall_text(clinic, patient, db=None):
    from . import msgtemplates as MT
    body = MT.body_for(db, clinic["id"], "recall", i18n.patient_lang(patient, clinic)) if db is not None else MT.KINDS["recall"][1]
    return MT.render(body, {"first_name": patient["first_name"], "clinic": clinic["name"]})


def prepare_recall(db, clinic, user_id, patient, recall):
    """Log a come-back message and return the WhatsApp link. Needs the patient's agreement."""
    from . import recalls
    if not patient["reminders_ok"]:
        raise PermissionError("This patient has not agreed to messages.")
    text = recall_text(clinic, patient, db)
    cur = db.execute(
        "INSERT INTO messages(clinic_id, patient_id, kind, body, status, created_by) VALUES (?,?, 'recall', ?, 'opened', ?)",
        (clinic["id"], patient["id"], text, user_id))
    recalls.mark_contacted(db, clinic, recall["id"])
    audit.log(db, clinic["id"], user_id, "message.recall", "message", cur.lastrowid)
    return wa_link(patient["phone"], text)
