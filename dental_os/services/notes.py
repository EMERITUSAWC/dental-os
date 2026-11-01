"""Clinical notes. A note is a draft until a dentist signs it. A signed note is never edited: corrections are
added as dated addenda that sit under it. A draft that is not wanted is marked discarded, not deleted."""
from . import audit, permissions
from . import treatment as TR
from .timeutil import now_local

TEMPLATES = {
    "exam": ("Examination", ["Reason for visit", "Medical history checked", "Extra-oral and soft tissue findings",
                             "Teeth and restorations", "Gums", "X-rays taken and findings", "Diagnosis",
                             "Advice given", "Treatment plan agreed", "Next visit"]),
    "hygiene": ("Scale and polish", ["Reason for visit", "Oral hygiene", "Calculus and staining", "Treatment given",
                                     "Advice given", "Next visit"]),
    "restoration": ("Filling", ["Tooth and surfaces", "Diagnosis", "Anaesthetic (type and amount)", "Isolation",
                                "Preparation", "Material and shade", "Bite and contacts checked", "Advice given",
                                "Next visit"]),
    "extraction": ("Extraction", ["Tooth", "Reason", "Consent", "Anaesthetic (type and amount)", "Procedure",
                                  "Bleeding control", "Advice given", "Review"]),
    "rct": ("Root canal treatment", ["Tooth", "Diagnosis and pulp status", "Anaesthetic (type and amount)",
                                     "Isolation", "Working length", "Cleaning and shaping", "Filling of canals",
                                     "X-ray", "Restoration plan", "Advice given"]),
    "crown": ("Crown or bridge", ["Tooth or teeth", "Preparation", "Shade", "Impression or scan", "Temporary",
                                  "Laboratory", "Fit and cementation", "Advice given", "Next visit"]),
    "emergency": ("Emergency visit", ["Complaint", "History of the pain", "Findings", "Tests", "Diagnosis",
                                      "Treatment given", "Advice given", "Follow-up"]),
    "free": ("General note", ["Note"]),
}
MAX_BODY = 20000


class NoteError(Exception):
    pass


def _clean_tooth(raw):
    try:
        return TR.clean_tooth(raw)
    except TR.TreatmentError as exc:
        raise NoteError(str(exc))


def build_body(template, values):
    if template not in TEMPLATES:
        raise NoteError("Pick a note type from the list.")
    headings = TEMPLATES[template][1]
    parts = []
    for i, h in enumerate(headings):
        v = (values.get(f"f{i}") or "").strip()
        if v:
            parts.append(f"{h}: {v}")
    body = "\n".join(parts)
    if not body:
        raise NoteError("Write something in at least one box.")
    if len(body) > MAX_BODY:
        raise NoteError("That note is too long.")
    return body


def split_body(template, body):
    """Put a saved body back into the template's boxes so a draft can be edited."""
    headings = TEMPLATES.get(template, ("", ["Note"]))[1]
    values = {}
    lines = (body or "").split("\n")
    current = None
    for line in lines:
        hit = next((i for i, h in enumerate(headings) if line.startswith(h + ": ")), None)
        if hit is not None:
            current = hit
            values[f"f{hit}"] = line[len(headings[hit]) + 2:]
        elif current is not None:
            values[f"f{current}"] += "\n" + line
    return values


def _check_appointment(db, cid, pid, appt_id):
    if appt_id is None:
        return None
    if not db.execute("SELECT 1 FROM appointments WHERE id = ? AND clinic_id = ? AND patient_id = ?",
                      (appt_id, cid, pid)).fetchone():
        raise NoteError("That visit does not belong to this patient.")
    return appt_id


def create_draft(db, clinic, user, pid, template, values, tooth=None, appointment_id=None):
    cid = clinic["id"]
    if not db.execute("SELECT 1 FROM patients WHERE id = ? AND clinic_id = ?", (pid, cid)).fetchone():
        raise NoteError("That patient was not found.")
    body = build_body(template, values)
    cur = db.execute(
        """INSERT INTO clinical_notes(clinic_id, patient_id, appointment_id, tooth, template, title, body, author_id)
           VALUES (?,?,?,?,?,?,?,?)""",
        (cid, pid, _check_appointment(db, cid, pid, appointment_id), _clean_tooth(tooth), template,
         TEMPLATES[template][0], body, user["id"]))
    audit.log(db, cid, user["id"], "note.create", "patient", pid)
    return cur.lastrowid


def get(db, cid, note_id):
    return db.execute("SELECT n.*, a.display_name AS author_name, s.display_name AS signer_name "
                      "FROM clinical_notes n LEFT JOIN users a ON a.id = n.author_id LEFT JOIN users s ON s.id = n.signed_by "
                      "WHERE n.id = ? AND n.clinic_id = ?", (note_id, cid)).fetchone()


def _editable_draft(db, clinic, user, note_id):
    note = get(db, clinic["id"], note_id)
    if not note:
        raise NoteError("That note was not found.")
    if note["status"] != "draft":
        raise NoteError("This note is " + ("signed and locked. Add an addendum instead." if note["status"] == "signed"
                                           else "no longer in use."))
    if note["author_id"] != user["id"] and user["role"] != "manager":
        raise NoteError("Only the author or a manager can change someone else's draft.")
    return note


def update_draft(db, clinic, user, note_id, values, tooth=None):
    note = _editable_draft(db, clinic, user, note_id)
    body = build_body(note["template"], values)
    db.execute("UPDATE clinical_notes SET body = ?, tooth = ?, updated_at = datetime('now') WHERE id = ? AND clinic_id = ?",
               (body, _clean_tooth(tooth), note_id, clinic["id"]))
    audit.log(db, clinic["id"], user["id"], "note.update", "patient", note["patient_id"])
    return note["patient_id"]


def sign(db, clinic, user, note_id):
    if not permissions.can_sign(user):
        raise NoteError("Only a dentist or a manager can sign a note.")
    note = get(db, clinic["id"], note_id)
    if not note or note["status"] != "draft":
        raise NoteError("Only a draft can be signed.")
    now = now_local(clinic["timezone"]).strftime("%Y-%m-%d %H:%M")
    db.execute("UPDATE clinical_notes SET status='signed', signed_by=?, signed_at=?, updated_at=datetime('now') "
               "WHERE id=? AND clinic_id=?", (user["id"], now, note_id, clinic["id"]))
    audit.log(db, clinic["id"], user["id"], "note.sign", "patient", note["patient_id"])
    return note["patient_id"]


def discard(db, clinic, user, note_id):
    note = _editable_draft(db, clinic, user, note_id)
    db.execute("UPDATE clinical_notes SET status='discarded', updated_at=datetime('now') WHERE id=? AND clinic_id=?",
               (note_id, clinic["id"]))
    audit.log(db, clinic["id"], user["id"], "note.discard", "patient", note["patient_id"])
    return note["patient_id"]


def add_addendum(db, clinic, user, note_id, text):
    if not permissions.can_sign(user):
        raise NoteError("Only a dentist or a manager can add to a signed note.")
    parent = get(db, clinic["id"], note_id)
    if not parent or parent["status"] != "signed" or parent["addendum_of"]:
        raise NoteError("An addendum goes under a signed note.")
    text = (text or "").strip()
    if not text:
        raise NoteError("Write the addendum.")
    if len(text) > MAX_BODY:
        raise NoteError("That addendum is too long.")
    now = now_local(clinic["timezone"]).strftime("%Y-%m-%d %H:%M")
    cur = db.execute(
        """INSERT INTO clinical_notes(clinic_id, patient_id, appointment_id, tooth, template, title, body, status,
                                      addendum_of, author_id, signed_by, signed_at)
           VALUES (?,?,?,?,?,?,?, 'signed', ?,?,?,?)""",
        (clinic["id"], parent["patient_id"], parent["appointment_id"], parent["tooth"], parent["template"],
         "Addendum", text, note_id, user["id"], user["id"], now))
    audit.log(db, clinic["id"], user["id"], "note.addendum", "patient", parent["patient_id"])
    return cur.lastrowid


def list_for_patient(db, cid, pid):
    """Top-level notes, newest first, each with its addenda in time order."""
    rows = db.execute(
        """SELECT n.*, a.display_name AS author_name, s.display_name AS signer_name FROM clinical_notes n
           LEFT JOIN users a ON a.id = n.author_id LEFT JOIN users s ON s.id = n.signed_by
           WHERE n.clinic_id = ? AND n.patient_id = ? AND n.status != 'discarded' ORDER BY n.id""", (cid, pid)).fetchall()
    addenda = {}
    for r in rows:
        if r["addendum_of"]:
            addenda.setdefault(r["addendum_of"], []).append(r)
    return [{"note": r, "addenda": addenda.get(r["id"], [])} for r in reversed(rows) if not r["addendum_of"]]
