import re
from datetime import date

from . import audit, i18n, serialize, webhooks
from .phone import normalise_phone
from .timeutil import add_months_safe, parse_date


class ValidationError(Exception):
    def __init__(self, errors):
        super().__init__("; ".join(errors.values()))
        self.errors = errors


class DuplicateWarning(Exception):
    def __init__(self, matches):
        super().__init__("Possible duplicate patient")
        self.matches = matches


FIELDS = ("first_name", "last_name", "dob", "gender", "phone", "email", "address",
          "guarantor_id", "preferred_contact", "notes")


def _clean_name(value):
    return re.sub(r"\s+", " ", (value or "").strip())


def clean(form, db=None, clinic_id=None, patient_id=None, country=None):
    """Validate form input. `country` is the clinic's country code, used to read local phone numbers.
    Returns clean data or raises ValidationError."""
    errors, data = {}, {}
    data["first_name"] = _clean_name(form.get("first_name"))
    data["last_name"] = _clean_name(form.get("last_name"))
    if not data["first_name"]:
        errors["first_name"] = "First name is needed."
    if not data["last_name"]:
        errors["last_name"] = "Last name is needed."

    dob = (form.get("dob") or "").strip()
    data["dob"] = None
    if dob:
        try:
            parsed = parse_date(dob)
            if parsed > date.today():
                errors["dob"] = "Date of birth cannot be in the future."
            elif parsed.year < 1900:
                errors["dob"] = "Check the year of birth."
            else:
                data["dob"] = parsed.isoformat()
        except ValueError:
            errors["dob"] = "Use the date picker or the format 2001-04-23."

    gender = (form.get("gender") or "").strip()
    data["gender"] = gender if gender in ("Female", "Male") else ""
    if "language" in form:                                    # only when the form has the field, so API updates do not wipe it
        data["language"] = i18n.pick(form.get("language"))   # None means: use the clinic's language

    try:
        data["phone"] = normalise_phone(form.get("phone"), country)
    except ValueError as exc:
        errors["phone"] = str(exc)

    email = (form.get("email") or "").strip().lower()
    if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        errors["email"] = "That email address does not look right."
    data["email"] = email or None
    data["address"] = (form.get("address") or "").strip() or None
    data["notes"] = (form.get("notes") or "").strip() or None

    pref = form.get("preferred_contact") or "whatsapp"
    data["prefers_sms"] = 1 if pref == "sms" else 0                  # SMS has its own column: older databases only allow the first three
    data["preferred_contact"] = pref if pref in ("whatsapp", "call", "email") else "whatsapp"
    data["reminders_ok"] = 1 if form.get("reminders_ok") else 0
    data["offers_ok"] = 1 if form.get("offers_ok") else 0

    data["guarantor_id"] = None
    guarantor = (form.get("guarantor_id") or "").strip()
    if guarantor:
        try:
            gid = int(guarantor)
        except ValueError:
            gid = None
        if gid is None or (patient_id and gid == patient_id):
            errors["guarantor_id"] = "Pick a different family head."
        elif db is not None and not get(db, clinic_id, gid):
            errors["guarantor_id"] = "That family head was not found."
        else:
            data["guarantor_id"] = gid

    if errors:
        raise ValidationError(errors)
    return data


def get(db, clinic_id, patient_id):
    return db.execute(
        "SELECT * FROM patients WHERE id = ? AND clinic_id = ?", (patient_id, clinic_id)
    ).fetchone()


def duplicates(db, clinic_id, data, exclude_id=None):
    return db.execute(
        """SELECT * FROM patients
           WHERE clinic_id = ? AND status = 'active' AND id != ?
             AND ((lower(first_name) = lower(?) AND lower(last_name) = lower(?)
                   AND (dob IS ? OR dob = ?))
                  OR phone = ?)
           ORDER BY last_name, first_name LIMIT 5""",
        (clinic_id, exclude_id or 0, data["first_name"], data["last_name"],
         data["dob"], data["dob"], data["phone"]),
    ).fetchall()


def create(db, clinic_id, user_id, data, allow_duplicates=False):
    if not allow_duplicates:
        matches = duplicates(db, clinic_id, data)
        if matches:
            raise DuplicateWarning(matches)
    cur = db.execute(
        """INSERT INTO patients(clinic_id, first_name, last_name, dob, gender, phone, email, address,
                                guarantor_id, preferred_contact, reminders_ok, offers_ok, notes)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (clinic_id, data["first_name"], data["last_name"], data["dob"], data["gender"], data["phone"],
         data["email"], data["address"], data["guarantor_id"], data["preferred_contact"],
         data["reminders_ok"], data["offers_ok"], data["notes"]),
    )
    if data.get("language"):
        db.execute("UPDATE patients SET language = ? WHERE id = ?", (data["language"], cur.lastrowid))
    if data.get("prefers_sms"):
        db.execute("UPDATE patients SET prefers_sms = 1 WHERE id = ?", (cur.lastrowid,))
    audit.log(db, clinic_id, user_id, "patient.create", "patient", cur.lastrowid)
    webhooks.emit(db, clinic_id, "patient.created", serialize.patient(get(db, clinic_id, cur.lastrowid)))
    return cur.lastrowid


def update(db, clinic_id, user_id, patient_id, data):
    if not get(db, clinic_id, patient_id):
        raise LookupError("patient")
    db.execute(
        """UPDATE patients SET first_name=?, last_name=?, dob=?, gender=?, phone=?, email=?, address=?,
                  guarantor_id=?, preferred_contact=?, reminders_ok=?, offers_ok=?, notes=?,
                  updated_at=datetime('now')
           WHERE id = ? AND clinic_id = ?""",
        (data["first_name"], data["last_name"], data["dob"], data["gender"], data["phone"], data["email"],
         data["address"], data["guarantor_id"], data["preferred_contact"], data["reminders_ok"],
         data["offers_ok"], data["notes"], patient_id, clinic_id),
    )
    if "language" in data:
        db.execute("UPDATE patients SET language = ? WHERE id = ?", (data["language"], patient_id))
    if "prefers_sms" in data:
        db.execute("UPDATE patients SET prefers_sms = ? WHERE id = ?", (data["prefers_sms"], patient_id))
    audit.log(db, clinic_id, user_id, "patient.update", "patient", patient_id)
    webhooks.emit(db, clinic_id, "patient.updated", serialize.patient(get(db, clinic_id, patient_id)))


def set_archived(db, clinic_id, user_id, patient_id, archived):
    """Patients are archived, never deleted. Clinical and money records must stay intact."""
    if not get(db, clinic_id, patient_id):
        raise LookupError("patient")
    db.execute(
        "UPDATE patients SET status = ?, updated_at = datetime('now') WHERE id = ? AND clinic_id = ?",
        ("archived" if archived else "active", patient_id, clinic_id),
    )
    audit.log(db, clinic_id, user_id, "patient.archive" if archived else "patient.restore", "patient", patient_id)


def _like(token):
    return "%" + token.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def search(db, clinic_id, query="", include_archived=False, page=1, per_page=25):
    where, params = ["clinic_id = ?"], [clinic_id]
    if not include_archived:
        where.append("status = 'active'")
    for token in (query or "").split():
        digits = re.sub(r"\D", "", token)
        if len(digits) >= 4:
            d = digits[1:] if digits.startswith("0") else digits
            where.append("phone LIKE ? ESCAPE '\\'")
            params.append(_like(d))
        else:
            where.append("(first_name LIKE ? ESCAPE '\\' OR last_name LIKE ? ESCAPE '\\')")
            params += [_like(token), _like(token)]
    clause = " AND ".join(where)
    total = db.execute(f"SELECT COUNT(*) FROM patients WHERE {clause}", params).fetchone()[0]
    rows = db.execute(
        f"SELECT * FROM patients WHERE {clause} ORDER BY last_name, first_name LIMIT ? OFFSET ?",
        params + [per_page, (max(page, 1) - 1) * per_page],
    ).fetchall()
    return rows, total


def family(db, clinic_id, patient_id):
    """The family head (if any) and everyone who lists this person as their family head."""
    p = get(db, clinic_id, patient_id)
    head = get(db, clinic_id, p["guarantor_id"]) if p and p["guarantor_id"] else None
    dependants = db.execute(
        "SELECT * FROM patients WHERE clinic_id = ? AND guarantor_id = ? AND status = 'active' ORDER BY first_name",
        (clinic_id, patient_id),
    ).fetchall()
    return head, dependants


def birthdays(db, clinic_id, today, days=7):
    """Patients who agreed to offers and have a birthday in the next `days` days."""
    rows = db.execute(
        "SELECT * FROM patients WHERE clinic_id = ? AND status = 'active' AND offers_ok = 1 AND dob IS NOT NULL",
        (clinic_id,),
    ).fetchall()
    out = []
    for r in rows:
        born = parse_date(r["dob"])
        this_year = add_months_safe(born, today.year)
        if this_year < today:
            this_year = add_months_safe(born, today.year + 1)
        gap = (this_year - today).days
        if 0 <= gap <= days:
            out.append((gap, this_year, r))
    out.sort(key=lambda t: (t[0], t[2]["last_name"]))
    return out
