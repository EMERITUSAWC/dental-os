"""Letters to and about patients from templates the clinic edits. Fields in curly brackets are filled in from the record, the
person writing can change any word before it is saved, and the words as printed are kept on the patient."""
import re

from . import audit, billing, i18n
from .timeutil import now_local, parse_date, parse_dt

FIELDS = {
    "patient_name": "Patient's full name", "first_name": "Patient's first name", "date_of_birth": "Date of birth",
    "patient_address": "Patient's address", "today": "Today's date", "clinic_name": "Clinic name", "clinic_phone": "Clinic phone",
    "clinic_address": "Clinic address", "dentist": "Dentist's name", "last_visit": "Date of the last visit",
    "next_visit": "Date and time of the next visit", "balance": "Amount owed",
}
STARTERS = [
    ("Attendance letter", "To whom it may concern\n\nThis is to confirm that {patient_name} (date of birth {date_of_birth}) attended "
     "{clinic_name} for dental treatment on {last_visit}.\n\nYours faithfully\n\n{dentist}"),
    ("Excuse from duty", "To whom it may concern\n\n{patient_name} was seen at {clinic_name} on {today} and had dental treatment. "
     "I advise rest from work for ___ day(s), returning on ________.\n\nYours faithfully\n\n{dentist}"),
    ("Recall reminder", "Dear {first_name}\n\nOur records show you are due for your regular dental check-up. Please call us on "
     "{clinic_phone} to book a time that suits you.\n\nKind regards\n\n{clinic_name}"),
    ("Account reminder", "Dear {first_name}\n\nOur records show {balance} is owed on your account. If you have already paid, thank "
     "you, and please ignore this letter. Otherwise, please call us on {clinic_phone} to settle it or agree a plan.\n\nKind regards\n\n{clinic_name}"),
]
_FIELD = re.compile(r"\{([a-z_]+)\}")
MAX_BODY = 8000


class LetterError(Exception):
    pass


def ensure_starters(db, cid):
    added = 0
    for name, body in STARTERS:
        cur = db.execute("INSERT OR IGNORE INTO letter_templates(clinic_id, name, body) VALUES (?,?,?)", (cid, name, body))
        added += cur.rowcount
    return added


def list_templates(db, cid, active_only=False):
    q = "SELECT * FROM letter_templates WHERE clinic_id = ?" + (" AND active = 1" if active_only else "") + " ORDER BY active DESC, name"
    return db.execute(q, (cid,)).fetchall()


def get_template(db, cid, tid):
    return db.execute("SELECT * FROM letter_templates WHERE id = ? AND clinic_id = ?", (tid, cid)).fetchone()


def save_template(db, clinic, user_id, tid, name, body):
    cid = clinic["id"]
    name, body = (name or "").strip()[:80], (body or "").strip()
    if not name or not body:
        raise LetterError("Give the letter a name and some wording.")
    if len(body) > MAX_BODY:
        raise LetterError("That letter is too long.")
    unknown = sorted(set(_FIELD.findall(body)) - set(FIELDS))
    if unknown:
        raise LetterError("Some fields in curly brackets are not known: " + ", ".join("{%s}" % u for u in unknown) + ".")
    clash = db.execute("SELECT id FROM letter_templates WHERE clinic_id = ? AND name = ? AND id != ?", (cid, name, tid or 0)).fetchone()
    if clash:
        raise LetterError("There is already a letter with that name.")
    if tid:
        if not get_template(db, cid, tid):
            raise LetterError("That letter was not found.")
        db.execute("UPDATE letter_templates SET name = ?, body = ? WHERE id = ? AND clinic_id = ?", (name, body, tid, cid))
    else:
        tid = db.execute("INSERT INTO letter_templates(clinic_id, name, body) VALUES (?,?,?)", (cid, name, body)).lastrowid
    audit.log(db, cid, user_id, "letter.template.save", "letter_template", tid)
    return tid


def toggle_template(db, clinic, user_id, tid):
    if not get_template(db, clinic["id"], tid):
        raise LetterError("That letter was not found.")
    db.execute("UPDATE letter_templates SET active = 1 - active WHERE id = ? AND clinic_id = ?", (tid, clinic["id"]))
    audit.log(db, clinic["id"], user_id, "letter.template.toggle", "letter_template", tid)


def context(db, clinic, patient, provider=None, lang="en"):
    cid, pid = clinic["id"], patient["id"]
    today = now_local(clinic["timezone"]).date()
    last = db.execute("""SELECT start_at FROM appointments WHERE clinic_id = ? AND patient_id = ? AND status = 'complete'
                         ORDER BY start_at DESC LIMIT 1""", (cid, pid)).fetchone()
    nxt = db.execute("""SELECT start_at FROM appointments WHERE clinic_id = ? AND patient_id = ? AND status = 'scheduled' AND start_at >= ?
                        ORDER BY start_at LIMIT 1""", (cid, pid, today.isoformat())).fetchone()
    from . import money
    owed = billing.totals(db, cid, pid)["balance"]
    dob = None
    if patient["dob"]:
        try:
            dob = i18n.fmt_date(parse_date(patient["dob"]), lang)
        except ValueError:
            dob = patient["dob"]
    return {
        "patient_name": f"{patient['first_name']} {patient['last_name']}", "first_name": patient["first_name"],
        "date_of_birth": dob or "", "patient_address": patient["address"] or "", "today": i18n.fmt_date(today, lang),
        "clinic_name": clinic["name"], "clinic_phone": clinic["phone"] or clinic["whatsapp"] or "", "clinic_address": clinic["address"] or "",
        "dentist": provider["name"] if provider else "",
        "last_visit": i18n.fmt_date(parse_dt(last["start_at"]).date(), lang) if last else "",
        "next_visit": (i18n.fmt_date(parse_dt(nxt["start_at"]).date(), lang) + ", " + i18n.fmt_time(parse_dt(nxt["start_at"]), lang)) if nxt else "",
        "balance": money.fmt(max(owed, 0), clinic["currency"]),
    }


def fill(body, values):
    return _FIELD.sub(lambda m: str(values.get(m.group(1), m.group(0))), body)


def save_letter(db, clinic, user_id, pid, title, body, addressee=None, template_id=None, provider_id=None):
    cid = clinic["id"]
    title, body = (title or "").strip()[:120], (body or "").strip()
    if not title or not body:
        raise LetterError("The letter needs a title and some wording.")
    if len(body) > MAX_BODY:
        raise LetterError("That letter is too long.")
    if not db.execute("SELECT 1 FROM patients WHERE id = ? AND clinic_id = ?", (pid, cid)).fetchone():
        raise LetterError("That patient was not found.")
    if provider_id and not db.execute("SELECT 1 FROM providers WHERE id = ? AND clinic_id = ?", (provider_id, cid)).fetchone():
        raise LetterError("Pick a dentist from this clinic.")
    lid = db.execute("""INSERT INTO letters(clinic_id, patient_id, template_id, title, addressee, body, provider_id, created_by)
                        VALUES (?,?,?,?,?,?,?,?)""",
                     (cid, pid, template_id or None, title, (addressee or "").strip()[:300] or None, body, provider_id or None, user_id)).lastrowid
    audit.log(db, cid, user_id, "letter.create", "patient", pid, f"letter {lid}")
    return lid


def get_letter(db, cid, lid):
    return db.execute("""SELECT l.*, p.name AS provider_name, p.reg_body, p.reg_number, p.qualifications FROM letters l
                         LEFT JOIN providers p ON p.id = l.provider_id WHERE l.id = ? AND l.clinic_id = ?""", (lid, cid)).fetchone()


def for_patient(db, cid, pid):
    return db.execute("""SELECT l.*, u.display_name AS by_name FROM letters l LEFT JOIN users u ON u.id = l.created_by
                         WHERE l.clinic_id = ? AND l.patient_id = ? ORDER BY l.id DESC""", (cid, pid)).fetchall()
