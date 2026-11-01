"""Treatment plans and the procedure log.
One table holds the plan and the work done: finishing a procedure is a status change, never a copy.
A done procedure cannot be edited or deleted. If it was entered in error, a manager voids it with a reason."""
from . import audit, teeth
from . import feesched as FS
from .timeutil import now_local, parse_dt

PERMANENT = [n for q in (1, 2, 3, 4) for n in range(q * 10 + 1, q * 10 + 9)]
PRIMARY = [n for q in (5, 6, 7, 8) for n in range(q * 10 + 1, q * 10 + 6)]
VALID_TEETH = set(PERMANENT + PRIMARY)
SURFACE_ORDER = "MODBLI"          # mesial, occlusal, distal, buccal, lingual, incisal

STARTER_CODES = [                  # our own codes. Fees stay at zero until the manager sets them.
    ("CONS", "Consultation", "Examination"), ("REVIEW", "Review visit", "Examination"),
    ("SCALE", "Scale and polish", "Preventive"), ("FLUOR", "Fluoride treatment", "Preventive"),
    ("FILL", "Filling, tooth-coloured", "Restorative"), ("FILL-TEMP", "Temporary filling", "Restorative"),
    ("EXT", "Simple extraction", "Surgery"), ("EXT-SURG", "Surgical extraction", "Surgery"),
    ("RCT", "Root canal treatment", "Endodontics"), ("CROWN", "Crown", "Prosthetics"),
    ("BRIDGE", "Bridge, per unit", "Prosthetics"), ("DENT-PART", "Partial denture", "Prosthetics"),
    ("DENT-FULL", "Full denture", "Prosthetics"), ("IMPLANT", "Implant", "Surgery"),
    ("WHITEN", "Teeth whitening", "Cosmetic"), ("ORTHO-REV", "Orthodontic review", "Orthodontics"),
    ("XRAY-PA", "Periapical X-ray", "Imaging"), ("OPG", "OPG X-ray", "Imaging"), ("CBCT", "CBCT scan", "Imaging"),
]


class TreatmentError(Exception):
    pass


def ensure_catalogue(db, clinic_id):
    """Add the starter list once. Returns how many codes were added."""
    added = 0
    for code, name, category in STARTER_CODES:
        cur = db.execute("INSERT OR IGNORE INTO procedure_codes(clinic_id, code, name, category) VALUES (?,?,?,?)",
                         (clinic_id, code, name, category))
        added += cur.rowcount
    return added


def clean_tooth(raw):
    """A typed tooth in the clinic's own numbering (see services/teeth.py), returned as an FDI number."""
    try:
        return teeth.parse(raw)
    except teeth.ToothError as exc:
        raise TreatmentError(str(exc))


def clean_surfaces(values):
    letters = {str(v).strip().upper() for v in (values or []) if str(v).strip()}
    if not letters <= set(SURFACE_ORDER):
        raise TreatmentError("Surfaces can only be M, O, D, B, L or I.")
    return "".join(s for s in SURFACE_ORDER if s in letters) or None


def _owned(db, table, clinic_id, row_id, active_only=True):
    sql = f"SELECT * FROM {table} WHERE id = ? AND clinic_id = ?" + (" AND active = 1" if active_only else "")
    return db.execute(sql, (row_id, clinic_id)).fetchone()


def get(db, clinic_id, proc_id):
    return db.execute(
        """SELECT pr.*, c.code, c.name AS code_name, c.category FROM procedures pr
           JOIN procedure_codes c ON c.id = pr.code_id WHERE pr.id = ? AND pr.clinic_id = ?""",
        (proc_id, clinic_id)).fetchone()


def add_planned(db, clinic, user_id, patient_id, code_id, tooth=None, surfaces=None, fee_p=None,
                provider_id=None, notes=None):
    cid = clinic["id"]
    if not db.execute("SELECT 1 FROM patients WHERE id = ? AND clinic_id = ? AND status = 'active'",
                      (patient_id, cid)).fetchone():
        raise TreatmentError("That patient was not found, or is archived.")
    code = _owned(db, "procedure_codes", cid, code_id)
    if not code:
        raise TreatmentError("Pick a procedure from the list.")
    if provider_id is not None and not _owned(db, "providers", cid, provider_id):
        raise TreatmentError("Pick a dentist from this clinic.")
    fee = FS.fee_for(db, cid, patient_id, code) if fee_p is None else fee_p
    if fee < 0:
        raise TreatmentError("A fee cannot be negative.")
    today = now_local(clinic["timezone"]).date().isoformat()
    cur = db.execute(
        """INSERT INTO procedures(clinic_id, patient_id, code_id, tooth, surfaces, fee_p, provider_id,
                                  planned_on, notes, created_by) VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (cid, patient_id, code_id, tooth, surfaces, fee, provider_id, today, notes, user_id))
    audit.log(db, cid, user_id, "procedure.plan", "procedure", cur.lastrowid)
    return cur.lastrowid


def mark_done(db, clinic, user_id, proc_id, appointment_id=None, provider_id=None):
    cid = clinic["id"]
    proc = get(db, cid, proc_id)
    if not proc:
        raise LookupError("procedure")
    if proc["status"] != "planned":
        raise TreatmentError("Only planned work can be marked as done.")
    done_on = now_local(clinic["timezone"]).date().isoformat()
    if appointment_id:
        appt = db.execute("SELECT * FROM appointments WHERE id = ? AND clinic_id = ?", (appointment_id, cid)).fetchone()
        if not appt or appt["patient_id"] != proc["patient_id"] or appt["status"] not in ("scheduled", "complete"):
            raise TreatmentError("Pick one of this patient's booked or finished visits.")
        done_on = parse_dt(appt["start_at"]).date().isoformat()
    if provider_id is not None and not _owned(db, "providers", cid, provider_id):
        raise TreatmentError("Pick a dentist from this clinic.")
    from . import periodlock
    periodlock.check(db, cid, done_on, TreatmentError)
    db.execute(
        """UPDATE procedures SET status='done', done_on=?, appointment_id=?, provider_id=COALESCE(?, provider_id)
           WHERE id = ? AND clinic_id = ?""",
        (done_on, appointment_id or None, provider_id, proc_id, cid))
    audit.log(db, cid, user_id, "procedure.done", "procedure", proc_id)
    from . import kits
    return kits.use_for(db, clinic, user_id, proc_id)                  # the stock this treatment uses comes off the shelf


def set_fee(db, clinic_id, user_id, proc_id, fee_p):
    proc = get(db, clinic_id, proc_id)
    if not proc:
        raise LookupError("procedure")
    if proc["status"] != "planned":
        raise TreatmentError("The fee is locked once work is done. Use a billing adjustment instead.")
    if fee_p < 0:
        raise TreatmentError("A fee cannot be negative.")
    db.execute("UPDATE procedures SET fee_p = ? WHERE id = ? AND clinic_id = ?", (fee_p, proc_id, clinic_id))
    audit.log(db, clinic_id, user_id, "procedure.fee", "procedure", proc_id)


def cancel(db, clinic_id, user_id, proc_id):
    proc = get(db, clinic_id, proc_id)
    if not proc:
        raise LookupError("procedure")
    if proc["status"] != "planned":
        raise TreatmentError("Only planned work can be dropped from the plan.")
    db.execute("UPDATE procedures SET status='cancelled' WHERE id = ? AND clinic_id = ?", (proc_id, clinic_id))
    audit.log(db, clinic_id, user_id, "procedure.cancel", "procedure", proc_id)


def void(db, clinic_id, user_id, proc_id, reason):
    proc = get(db, clinic_id, proc_id)
    if not proc:
        raise LookupError("procedure")
    if proc["status"] != "done":
        raise TreatmentError("Only finished work can be voided.")
    from . import periodlock
    periodlock.check(db, clinic_id, proc["done_on"], TreatmentError)
    reason = (reason or "").strip()
    if len(reason) < 3:
        raise TreatmentError("Say why it is being voided.")
    db.execute("UPDATE procedures SET status='voided', void_reason=? WHERE id = ? AND clinic_id = ?",
               (reason, proc_id, clinic_id))
    from . import kits
    kits.give_back(db, db.execute("SELECT * FROM clinics WHERE id = ?", (clinic_id,)).fetchone(), user_id, proc_id)
    audit.log(db, clinic_id, user_id, "procedure.void", "procedure", proc_id, reason[:80])


def list_for_patient(db, clinic_id, patient_id):
    return db.execute(
        """SELECT pr.*, c.code, c.name AS code_name, p.name AS provider_name FROM procedures pr
           JOIN procedure_codes c ON c.id = pr.code_id
           LEFT JOIN providers p ON p.id = pr.provider_id
           WHERE pr.clinic_id = ? AND pr.patient_id = ?
           ORDER BY CASE pr.status WHEN 'planned' THEN 0 WHEN 'done' THEN 1 ELSE 2 END,
                    COALESCE(pr.done_on, pr.planned_on) DESC, pr.id DESC""",
        (clinic_id, patient_id)).fetchall()


def summary(rows):
    return {"planned_p": sum(r["fee_p"] for r in rows if r["status"] == "planned"),
            "done_p": sum(r["fee_p"] for r in rows if r["status"] == "done"),
            "planned_n": sum(1 for r in rows if r["status"] == "planned")}
