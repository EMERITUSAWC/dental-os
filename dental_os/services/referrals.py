"""Referrals both ways. Referrers are the people and places that send patients to us (a dentist, a doctor, a patient who
recommended us, a website) or that we send patients to (a specialist, a hospital). A referral out is tracked from draft to closed,
with a letter that can be printed."""
from . import audit

KINDS = {"dentist": "Dentist", "specialist": "Specialist", "doctor": "Doctor", "hospital": "Hospital or clinic",
         "patient": "Another patient", "online": "Online or social media", "other": "Other"}
SOURCES = {"": "Not asked", "walk_in": "Walked in", "word_of_mouth": "Friend or family", "online": "Online search or website",
           "social": "Social media", "insurer": "Insurer or employer", "signboard": "Saw the clinic", "other": "Other"}
URGENCY = {"routine": "Routine", "soon": "Soon (within two weeks)", "urgent": "Urgent"}
STATUSES = {"draft": "Draft", "sent": "Sent", "booked": "They booked the patient", "seen": "Patient seen", "report": "Report received",
            "closed": "Closed", "cancelled": "Cancelled"}
NEXT = {"draft": ("sent", "cancelled"), "sent": ("booked", "seen", "report", "cancelled"), "booked": ("seen", "report", "cancelled"),
        "seen": ("report", "closed"), "report": ("closed",), "closed": (), "cancelled": ()}
OPEN = ("draft", "sent", "booked", "seen", "report")


class ReferralError(Exception):
    pass


def _t(v, n):
    return (v or "").strip()[:n]


def list_referrers(db, cid, active_only=False):
    q = "SELECT * FROM referrers WHERE clinic_id = ?" + (" AND active = 1" if active_only else "") + " ORDER BY active DESC, name"
    return db.execute(q, (cid,)).fetchall()


def get_referrer(db, cid, rid):
    return db.execute("SELECT * FROM referrers WHERE id = ? AND clinic_id = ?", (rid, cid)).fetchone()


def save_referrer(db, clinic, user_id, rid, name, kind, specialty=None, practice=None, phone=None, email=None, address=None):
    cid = clinic["id"]
    name = _t(name, 100)
    if not name:
        raise ReferralError("Enter a name.")
    if kind not in KINDS:
        raise ReferralError("Pick what kind of referrer this is.")
    email = _t(email, 120)
    if email and ("@" not in email or " " in email):
        raise ReferralError("That email address does not look right.")
    vals = (name, kind, _t(specialty, 80) or None, _t(practice, 120) or None, _t(phone, 40) or None, email or None, _t(address, 300) or None)
    if rid:
        if not get_referrer(db, cid, rid):
            raise ReferralError("That referrer was not found.")
        db.execute("UPDATE referrers SET name=?, kind=?, specialty=?, practice=?, phone=?, email=?, address=? WHERE id=? AND clinic_id=?",
                   vals + (rid, cid))
    else:
        rid = db.execute("INSERT INTO referrers(clinic_id, name, kind, specialty, practice, phone, email, address) VALUES (?,?,?,?,?,?,?,?)",
                         (cid,) + vals).lastrowid
    audit.log(db, cid, user_id, "referrer.save", "referrer", rid)
    return rid


def toggle_referrer(db, clinic, user_id, rid):
    if not get_referrer(db, clinic["id"], rid):
        raise ReferralError("That referrer was not found.")
    db.execute("UPDATE referrers SET active = 1 - active WHERE id = ? AND clinic_id = ?", (rid, clinic["id"]))
    audit.log(db, clinic["id"], user_id, "referrer.toggle", "referrer", rid)


def set_source(db, clinic, user_id, pid, referrer_id, source):
    cid = clinic["id"]
    if referrer_id and not get_referrer(db, cid, referrer_id):
        raise ReferralError("Pick a referrer from the list.")
    if (source or "") not in SOURCES:
        raise ReferralError("Pick how they heard of us from the list.")
    db.execute("UPDATE patients SET referrer_id = ?, referral_source = ? WHERE id = ? AND clinic_id = ?",
               (referrer_id or None, source or None, pid, cid))
    audit.log(db, cid, user_id, "patient.referral_source", "patient", pid)


def create_out(db, clinic, user_id, pid, referrer_id, reason, urgency="routine", teeth=None, history=None, provider_id=None):
    cid = clinic["id"]
    if not db.execute("SELECT 1 FROM patients WHERE id = ? AND clinic_id = ?", (pid, cid)).fetchone():
        raise ReferralError("That patient was not found.")
    ref = get_referrer(db, cid, referrer_id)
    if not ref or not ref["active"]:
        raise ReferralError("Pick who the patient is being referred to.")
    reason = _t(reason, 2000)
    if not reason:
        raise ReferralError("Write the reason for the referral.")
    if urgency not in URGENCY:
        raise ReferralError("Pick how urgent it is.")
    if provider_id and not db.execute("SELECT 1 FROM providers WHERE id = ? AND clinic_id = ?", (provider_id, cid)).fetchone():
        raise ReferralError("Pick a dentist from this clinic.")
    rid = db.execute("""INSERT INTO referrals_out(clinic_id, patient_id, referrer_id, provider_id, urgency, reason, teeth, history, created_by)
                        VALUES (?,?,?,?,?,?,?,?,?)""",
                     (cid, pid, referrer_id, provider_id or None, urgency, reason, _t(teeth, 80) or None, _t(history, 4000) or None, user_id)).lastrowid
    audit.log(db, cid, user_id, "referral.create", "patient", pid, f"referral {rid}")
    return rid


def get_out(db, cid, rid):
    return db.execute(
        """SELECT r.*, f.name AS to_name, f.kind AS to_kind, f.specialty AS to_specialty, f.practice AS to_practice, f.address AS to_address,
                  f.email AS to_email, f.phone AS to_phone, p.first_name, p.last_name, p.dob, p.phone AS patient_phone, p.address AS patient_address,
                  pr.name AS provider_name, pr.reg_body, pr.reg_number, pr.qualifications
           FROM referrals_out r JOIN referrers f ON f.id = r.referrer_id JOIN patients p ON p.id = r.patient_id
           LEFT JOIN providers pr ON pr.id = r.provider_id WHERE r.id = ? AND r.clinic_id = ?""", (rid, cid)).fetchone()


def move(db, clinic, user_id, rid, status, note=None, today=None):
    r = get_out(db, clinic["id"], rid)
    if not r:
        raise ReferralError("That referral was not found.")
    if status not in NEXT.get(r["status"], ()):
        raise ReferralError("That step does not follow from where the referral is now.")
    note = _t(note, 2000) or None
    if status == "report" and not note:
        raise ReferralError("Write what the report says, or upload it to Files and say so here.")
    db.execute("""UPDATE referrals_out SET status = ?, sent_on = COALESCE(sent_on, ?), report_note = COALESCE(?, report_note),
                  updated_at = datetime('now') WHERE id = ?""", (status, today if status == "sent" else None, note, rid))
    audit.log(db, clinic["id"], user_id, f"referral.{status}", "patient", r["patient_id"], f"referral {rid}")
    return r["patient_id"]


def for_patient(db, cid, pid):
    return db.execute("""SELECT r.*, f.name AS to_name FROM referrals_out r JOIN referrers f ON f.id = r.referrer_id
                         WHERE r.clinic_id = ? AND r.patient_id = ? ORDER BY r.id DESC""", (cid, pid)).fetchall()


def open_list(db, cid):
    return db.execute(
        f"""SELECT r.*, f.name AS to_name, p.first_name, p.last_name FROM referrals_out r JOIN referrers f ON f.id = r.referrer_id
            JOIN patients p ON p.id = r.patient_id WHERE r.clinic_id = ? AND r.status IN ({','.join('?' * len(OPEN))})
            ORDER BY CASE r.urgency WHEN 'urgent' THEN 0 WHEN 'soon' THEN 1 ELSE 2 END, r.created_at""", (cid, *OPEN)).fetchall()


def sources_report(db, cid, start, end):
    """New patients in a period by who sent them, and by how they heard of us."""
    by_ref = db.execute(
        """SELECT f.id, f.name, f.kind, COUNT(*) AS n FROM patients p JOIN referrers f ON f.id = p.referrer_id
           WHERE p.clinic_id = ? AND p.created_at >= ? AND p.created_at < ? GROUP BY f.id, f.name, f.kind ORDER BY n DESC, f.name""",
        (cid, start, end)).fetchall()
    by_source = db.execute(
        """SELECT COALESCE(referral_source, '') AS source, COUNT(*) AS n FROM patients WHERE clinic_id = ? AND created_at >= ? AND created_at < ?
           AND referrer_id IS NULL GROUP BY COALESCE(referral_source, '') ORDER BY n DESC""", (cid, start, end)).fetchall()
    return by_ref, by_source
