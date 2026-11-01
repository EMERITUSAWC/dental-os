"""Consent forms. The wording is kept as a snapshot at the moment of signing, so changing a template later
never changes what a patient actually agreed to. The starter wording is plain and general. A dentist and a
lawyer in your country must review it before it is used with patients."""
from . import audit
from .timeutil import now_local, parse_date

STARTER = [
    ("General treatment consent",
     "I agree to have dental examination and treatment from this clinic. The dentist has explained what is needed, "
     "the options, the likely benefits and the main risks, and I have had the chance to ask questions. I know I can "
     "change my mind and stop treatment at any time. I understand that results cannot be guaranteed, and that I will "
     "be told the cost before treatment starts."),
    ("Tooth extraction",
     "I agree to have the tooth or teeth named by my dentist taken out. I have been told about the risks, which can "
     "include pain, swelling, bleeding, infection, dry socket, damage to nearby teeth or fillings, and numbness of the "
     "lip, chin or tongue that is usually temporary. I have been given aftercare advice and know who to call if I am worried."),
    ("Root canal treatment",
     "I agree to root canal treatment on the tooth named by my dentist. I understand the aim is to keep the tooth, that "
     "it may need more than one visit, and that a crown or other strong filling is usually needed afterwards. I have been "
     "told about the risks, which include pain, infection, a broken instrument in the canal, the treatment not working "
     "and the tooth needing to be taken out later."),
    ("Local anaesthetic",
     "I agree to have local anaesthetic (an injection to numb the area). I have told the dentist about my health, "
     "medicines and allergies. I understand that the numbness may last for some hours, that I should not eat hot food or "
     "bite my lip, cheek or tongue while it wears off, and that in rare cases there can be a reaction or longer numbness."),
    ("Teeth whitening",
     "I agree to teeth whitening. I understand that teeth and some fillings, crowns and veneers may not change colour, that "
     "teeth can become sensitive for a short time, that gums may be irritated, and that results vary and fade over time."),
    ("Dental implant",
     "I agree to have a dental implant placed. I have been told about the stages, the time it takes, the cost and the "
     "alternatives. I understand the risks, which include pain, swelling, bleeding, infection, nerve injury, sinus problems "
     "and the implant failing to join the bone. I know smoking, poor cleaning and some illnesses can raise the risk of failure."),
    ("Photographs, X-rays and records",
     "I agree that my clinical photographs, X-rays and records may be kept in my file and used for my care. I understand "
     "they will not be shown to anyone outside my care team, or used for teaching, training or promotion, unless I give "
     "separate written permission."),
]


class ConsentError(Exception):
    pass


def ensure_templates(db, clinic_id):
    added = 0
    for name, body in STARTER:
        added += db.execute("INSERT OR IGNORE INTO consent_templates(clinic_id, name, body) VALUES (?,?,?)",
                            (clinic_id, name, body)).rowcount
    return added


def save_template(db, clinic, user_id, template_id, name, body, active=True):
    name, body = (name or "").strip(), (body or "").strip()
    if not name or not body:
        raise ConsentError("Give the form a name and some wording.")
    if len(name) > 80 or len(body) > 6000:
        raise ConsentError("That is too long.")
    if template_id:
        cur = db.execute("UPDATE consent_templates SET name=?, body=?, active=? WHERE id=? AND clinic_id=?",
                         (name, body, 1 if active else 0, template_id, clinic["id"]))
        if not cur.rowcount:
            raise ConsentError("That form was not found.")
    else:
        try:
            cur = db.execute("INSERT INTO consent_templates(clinic_id, name, body, active) VALUES (?,?,?,?)",
                             (clinic["id"], name, body, 1 if active else 0))
        except Exception as exc:
            if "UNIQUE" in str(exc):
                raise ConsentError("A form with that name already exists.")
            raise
        template_id = cur.lastrowid
    audit.log(db, clinic["id"], user_id, "consent.template", "consent_template", template_id)
    return template_id


def _age_years(dob_iso, today):
    if not dob_iso:
        return None
    born = parse_date(dob_iso)
    return today.year - born.year - ((today.month, today.day) < (born.month, born.day))


def record(db, clinic, user, pid, template_id, signer_name, relation, method, agreed, note=None, procedure_id=None):
    cid = clinic["id"]
    patient = db.execute("SELECT * FROM patients WHERE id = ? AND clinic_id = ?", (pid, cid)).fetchone()
    if not patient:
        raise ConsentError("That patient was not found.")
    tpl = db.execute("SELECT * FROM consent_templates WHERE id = ? AND clinic_id = ? AND active = 1",
                     (template_id, cid)).fetchone()
    if not tpl:
        raise ConsentError("Pick a consent form from the list.")
    signer_name = (signer_name or "").strip()
    if not signer_name or len(signer_name) > 120:
        raise ConsentError("Enter the full name of the person who signed.")
    if relation not in ("self", "parent", "guardian", "other") or method not in ("typed", "paper"):
        raise ConsentError("Pick who signed and how.")
    if not agreed:
        raise ConsentError("Tick the box to confirm they read the form and agreed.")
    today = now_local(clinic["timezone"])
    age = _age_years(patient["dob"], today.date())
    if relation == "self" and age is not None and age < 18:
        raise ConsentError("This patient is under 18. A parent or guardian needs to sign.")
    if procedure_id is not None and not db.execute(
            "SELECT 1 FROM procedures WHERE id = ? AND clinic_id = ? AND patient_id = ?", (procedure_id, cid, pid)).fetchone():
        raise ConsentError("That treatment does not belong to this patient.")
    cur = db.execute(
        """INSERT INTO consents(clinic_id, patient_id, template_id, title, body, procedure_id, signer_name, signer_relation,
                                method, signed_at, recorded_by, note) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (cid, pid, tpl["id"], tpl["name"], tpl["body"], procedure_id, signer_name, relation, method,
         today.strftime("%Y-%m-%d %H:%M"), user["id"], (note or "").strip()[:200] or None))
    audit.log(db, cid, user["id"], "consent.record", "patient", pid, tpl["name"][:60])
    return cur.lastrowid


def withdraw(db, clinic, user, consent_id, reason):
    row = db.execute("SELECT * FROM consents WHERE id = ? AND clinic_id = ? AND status = 'active'",
                     (consent_id, clinic["id"])).fetchone()
    if not row:
        raise ConsentError("That consent was not found.")
    reason = (reason or "").strip()
    if not reason:
        raise ConsentError("Write why it was withdrawn.")
    today = now_local(clinic["timezone"]).date().isoformat()
    db.execute("UPDATE consents SET status='withdrawn', withdrawn_on=?, withdrawn_by=?, withdrawn_reason=? WHERE id=?",
               (today, user["id"], reason[:200], consent_id))
    audit.log(db, clinic["id"], user["id"], "consent.withdraw", "patient", row["patient_id"])
    return row["patient_id"]


def list_for_patient(db, cid, pid):
    return db.execute(
        """SELECT c.*, u.display_name AS recorded_name FROM consents c LEFT JOIN users u ON u.id = c.recorded_by
           WHERE c.clinic_id = ? AND c.patient_id = ? ORDER BY c.id DESC""", (cid, pid)).fetchall()
