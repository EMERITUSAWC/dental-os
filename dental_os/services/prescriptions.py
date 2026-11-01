"""Prescriptions. A dentist picks medicines from the clinic's own list, the system checks them against the patient's allergies and
medicines, and a printable prescription is made. What was written is copied onto the prescription, so editing the list later never
changes one already written. Nothing is deleted: a prescription written in error is cancelled with a reason.

The checks are a safety net, not a replacement for the dentist's judgement or the national formulary. They look for plain words in
the patient's allergy alerts and medical history, so a misspelt or missing entry will not be caught."""
from datetime import date

from . import audit, permissions
from .timeutil import now_local, parse_date

CLASSES = {
    "penicillin": "Penicillin", "nsaid": "Anti-inflammatory painkiller (NSAID)", "paracetamol": "Paracetamol",
    "imidazole": "Metronidazole", "azole_antifungal": "Azole antifungal", "macrolide": "Macrolide antibiotic",
    "lincosamide": "Clindamycin", "antiseptic": "Antiseptic mouthwash", "fluoride": "Fluoride", "antiviral": "Antiviral",
    "other": "Other",
}
# Words that, in an allergy, mean the patient must not have a medicine of that class.
ALLERGY_WORDS = {
    "penicillin": ("penicillin", "amoxicillin", "amoxycillin", "ampicillin", "augmentin", "co-amoxiclav", "flucloxacillin", "phenoxymethylpenicillin"),
    "nsaid": ("nsaid", "ibuprofen", "aspirin", "diclofenac", "naproxen", "anti-inflammator"),
    "paracetamol": ("paracetamol", "acetaminophen"),
    "imidazole": ("metronidazole", "flagyl"),
    "macrolide": ("clarithromycin", "erythromycin", "azithromycin", "macrolide"),
    "lincosamide": ("clindamycin",),
    "azole_antifungal": ("fluconazole", "miconazole"),
    "antiseptic": ("chlorhexidine",),
}
STATINS = ("statin", "simvastatin", "atorvastatin", "rosuvastatin", "pravastatin", "fluvastatin")
WARFARIN = ("warfarin",)
# (class, words found in the medical history's medicines, message, source). The two below are from SDCEP, Drug Prescribing for
# Dentistry, 3rd edition, update June 2021.
INTERACTIONS = [
    ("imidazole", WARFARIN, "Metronidazole should not be given to someone taking warfarin.", "SDCEP Drug Prescribing for Dentistry (2021 update)"),
    ("azole_antifungal", WARFARIN + STATINS, "Fluconazole and miconazole should not be given to someone taking warfarin or a statin.",
     "SDCEP Drug Prescribing for Dentistry (2021 update)"),
]

# Starter list. The antibiotic regimens are the adult ones in SDCEP's guidance and the College of General Dentistry's chairside
# synopsis. Painkillers and the rest are listed without a dose, for the clinic's dentist to fill in from their national formulary.
STARTER_DRUGS = [
    ("Phenoxymethylpenicillin", "250 mg", "tablets", "2 tablets four times daily", 5, "40 tablets", "penicillin", 0,
     "Swallow whole with water, at least 30 minutes before food.", "SDCEP Drug Prescribing for Dentistry (2021 update)"),
    ("Amoxicillin", "500 mg", "capsules", "1 capsule three times daily", 5, "15 capsules", "penicillin", 0, "",
     "College of General Dentistry, Antimicrobial Prescribing in Dentistry (2022)"),
    ("Metronidazole", "400 mg", "tablets", "1 tablet three times daily", 5, "15 tablets", "imidazole", 0,
     "Avoid alcohol while taking this and for 48 hours after.", "SDCEP Drug Prescribing for Dentistry (2021 update)"),
    ("Ibuprofen", "400 mg", "tablets", "", None, "", "nsaid", 0, "Take with or after food.", None),
    ("Paracetamol", "500 mg", "tablets", "", None, "", "paracetamol", 0, "Do not take other medicines that contain paracetamol.", None),
    ("Chlorhexidine mouthwash", "0.2%", "mouthwash", "", None, "", "antiseptic", 0, "", None),
    ("Sodium fluoride toothpaste", "5000 ppm", "toothpaste", "", None, "", "fluoride", 0, "", None),
]
MAX_ITEMS = 8


class RxError(Exception):
    pass


def _text(v, limit):
    return (v or "").strip()[:limit]


# -------------------------------------------------------------------------------------------------------------- drug list
def list_drugs(db, cid, active_only=False):
    q = "SELECT * FROM drugs WHERE clinic_id = ?" + (" AND active = 1" if active_only else "") + " ORDER BY active DESC, name, strength"
    return db.execute(q, (cid,)).fetchall()


def get_drug(db, cid, drug_id):
    return db.execute("SELECT * FROM drugs WHERE id = ? AND clinic_id = ?", (drug_id, cid)).fetchone()


def save_drug(db, clinic, user_id, drug_id, name, strength, form, dose, days, quantity, drug_class, controlled, warnings):
    cid = clinic["id"]
    name = _text(name, 80)
    if not name:
        raise RxError("Enter the medicine's generic name.")
    if drug_class not in CLASSES:
        raise RxError("Pick what kind of medicine it is.")
    days = _days(days)
    values = (name, _text(strength, 30), _text(form, 30), _text(dose, 200), days, _text(quantity, 40), drug_class,
              1 if controlled else 0, _text(warnings, 300))
    if drug_id:
        if not get_drug(db, cid, drug_id):
            raise RxError("That medicine was not found.")
        db.execute("""UPDATE drugs SET name=?, strength=?, form=?, dose=?, days=?, quantity=?, drug_class=?, controlled=?, warnings=?
                      WHERE id=? AND clinic_id=?""", values + (drug_id, cid))
    else:
        drug_id = db.execute("""INSERT INTO drugs(clinic_id, name, strength, form, dose, days, quantity, drug_class, controlled, warnings)
                                VALUES (?,?,?,?,?,?,?,?,?,?)""", (cid,) + values).lastrowid
    audit.log(db, cid, user_id, "rx.drug.save", "drug", drug_id)
    return drug_id


def toggle_drug(db, clinic, user_id, drug_id):
    if not get_drug(db, clinic["id"], drug_id):
        raise RxError("That medicine was not found.")
    db.execute("UPDATE drugs SET active = 1 - active WHERE id = ? AND clinic_id = ?", (drug_id, clinic["id"]))
    audit.log(db, clinic["id"], user_id, "rx.drug.toggle", "drug", drug_id)


def add_starter_drugs(db, clinic, user_id):
    added = 0
    for name, strength, form, dose, days, qty, cls, ctl, warn, source in STARTER_DRUGS:
        if db.execute("SELECT 1 FROM drugs WHERE clinic_id = ? AND name = ? AND strength = ?", (clinic["id"], name, strength)).fetchone():
            continue
        db.execute("""INSERT INTO drugs(clinic_id, name, strength, form, dose, days, quantity, drug_class, controlled, warnings, source)
                      VALUES (?,?,?,?,?,?,?,?,?,?,?)""", (clinic["id"], name, strength, form, dose, days, qty, cls, ctl, warn, source))
        added += 1
    audit.log(db, clinic["id"], user_id, "rx.drug.starters", None, None, str(added))
    return added


def _days(raw):
    if raw in (None, ""):
        return None
    try:
        n = int(raw)
    except (TypeError, ValueError):
        raise RxError("Give the number of days as a whole number.")
    if not 1 <= n <= 90:
        raise RxError("A course is from 1 to 90 days.")
    return n


# ---------------------------------------------------------------------------------------------------------------- checks
def _words(text):
    return (text or "").lower()


def checks(db, cid, pid, drug_rows):
    """Warnings for these medicines and this patient. 'stop' warnings need a reason to go ahead."""
    allergies = " ".join(r["text"] for r in db.execute(
        "SELECT text FROM patient_alerts WHERE clinic_id = ? AND patient_id = ? AND active = 1 AND kind = 'allergy'", (cid, pid)))
    hist = db.execute("SELECT * FROM medical_history WHERE clinic_id = ? AND patient_id = ?", (cid, pid)).fetchone()
    allergy_text = _words(allergies + " " + (hist["allergies"] if hist and hist["allergies"] else ""))
    meds = _words(" ".join(r["text"] for r in db.execute(
        "SELECT text FROM patient_alerts WHERE clinic_id = ? AND patient_id = ? AND active = 1 AND kind = 'medication'", (cid, pid)))
                  + " " + (hist["medications"] if hist and hist["medications"] else ""))
    out, seen = [], set()

    def add(level, text, source=None):
        if text not in seen:
            seen.add(text)
            out.append({"level": level, "text": text, "source": source})

    for d in drug_rows:
        cls = d["drug_class"]
        for word in ALLERGY_WORDS.get(cls, ()):
            if word in allergy_text:
                add("stop", f"Allergy on record ({word}) and {d['name']} is in that group.")
                break
        if d["name"].lower() in allergy_text and cls not in ALLERGY_WORDS:
            add("stop", f"Allergy on record mentions {d['name']}.")
        for i_cls, words, message, source in INTERACTIONS:
            if cls == i_cls and any(w in meds for w in words):
                add("stop", message, source)
        if cls == "nsaid" and hist and hist["blood_thinners"]:
            add("caution", "The patient takes blood thinners. Anti-inflammatory painkillers raise the risk of bleeding. Check the formulary.")
        if d["controlled"]:
            add("caution", f"{d['name']} is a controlled drug. Follow your country's extra rules for writing it.")
    if drug_rows and hist and hist["pregnant"] == "yes":
        add("caution", "The patient may be pregnant. Check each medicine is safe in pregnancy.")
    return out


# --------------------------------------------------------------------------------------------------------- prescriptions
def _provider(db, cid, provider_id):
    row = db.execute("SELECT * FROM providers WHERE id = ? AND clinic_id = ? AND active = 1", (provider_id, cid)).fetchone()
    if not row:
        raise RxError("Pick the dentist who is prescribing.")
    if not (row["reg_number"] or "").strip():
        raise RxError("Add the dentist's registration number under Settings first. It is printed on the prescription.")
    return row


def create(db, clinic, user, pid, provider_id, items, notes=None, override_reason=None, appointment_id=None):
    """items: [{"drug_id", "dose", "days", "quantity"}]. Returns the new prescription id."""
    cid = clinic["id"]
    if not permissions.can_sign(user):
        raise RxError("Only a dentist or a manager can write a prescription.")
    if not (clinic["address"] or "").strip():
        raise RxError("Add the clinic's address under Settings first. It is printed on the prescription.")
    patient = db.execute("SELECT * FROM patients WHERE id = ? AND clinic_id = ? AND status = 'active'", (pid, cid)).fetchone()
    if not patient:
        raise RxError("That patient was not found, or is archived.")
    _provider(db, cid, provider_id)
    clean, drugs = [], []
    for it in items:
        if not it.get("drug_id"):
            continue
        d = get_drug(db, cid, it["drug_id"])
        if not d or not d["active"]:
            raise RxError("Pick medicines from the clinic's list.")
        dose = _text(it.get("dose") or d["dose"], 200)
        if not dose:
            raise RxError(f"Write how {d['name']} is to be taken.")
        clean.append((d, dose, _days(it.get("days") if it.get("days") not in (None, "") else d["days"]),
                      _text(it.get("quantity") or d["quantity"], 40)))
        drugs.append(d)
    if not clean:
        raise RxError("Add at least one medicine.")
    if len(clean) > MAX_ITEMS:
        raise RxError("Put at most eight medicines on one prescription.")
    warnings = checks(db, cid, pid, drugs)
    override_reason = _text(override_reason, 300) or None
    if any(w["level"] == "stop" for w in warnings) and not override_reason:
        raise RxError("This prescription has a safety warning. Write why it is right to go ahead, or change the medicines.")
    if appointment_id and not db.execute("SELECT 1 FROM appointments WHERE id = ? AND clinic_id = ? AND patient_id = ?",
                                         (appointment_id, cid, pid)).fetchone():
        raise RxError("That visit does not belong to this patient.")
    today = now_local(clinic["timezone"]).date().isoformat()
    rx_id = db.execute("""INSERT INTO prescriptions(clinic_id, patient_id, provider_id, appointment_id, issued_on, notes, override_reason, created_by)
                          VALUES (?,?,?,?,?,?,?,?)""",
                       (cid, pid, provider_id, appointment_id or None, today, _text(notes, 300) or None, override_reason, user["id"])).lastrowid
    for d, dose, days, qty in clean:
        db.execute("""INSERT INTO prescription_items(clinic_id, prescription_id, drug_id, name, strength, form, dose, days, quantity, controlled)
                      VALUES (?,?,?,?,?,?,?,?,?,?)""", (cid, rx_id, d["id"], d["name"], d["strength"], d["form"], dose, days, qty, d["controlled"]))
    audit.log(db, cid, user["id"], "rx.issue", "patient", pid, f"prescription {rx_id}" + (", warning overridden" if override_reason else ""))
    return rx_id


def cancel(db, clinic, user, rx_id, reason):
    rx = get(db, clinic["id"], rx_id)
    if not rx:
        raise RxError("That prescription was not found.")
    if rx["status"] != "issued":
        raise RxError("That prescription is already cancelled.")
    if not permissions.can_sign(user):
        raise RxError("Only a dentist or a manager can cancel a prescription.")
    reason = _text(reason, 200)
    if not reason:
        raise RxError("Say why it is being cancelled.")
    db.execute("UPDATE prescriptions SET status='cancelled', cancelled_by=?, cancelled_at=datetime('now'), cancel_reason=? WHERE id=?",
               (user["id"], reason, rx_id))
    audit.log(db, clinic["id"], user["id"], "rx.cancel", "patient", rx["patient_id"], f"prescription {rx_id}")
    return rx["patient_id"]


def get(db, cid, rx_id):
    return db.execute(
        """SELECT r.*, p.name AS provider_name, p.reg_body, p.reg_number, p.qualifications, u.display_name AS written_by
           FROM prescriptions r JOIN providers p ON p.id = r.provider_id LEFT JOIN users u ON u.id = r.created_by
           WHERE r.id = ? AND r.clinic_id = ?""", (rx_id, cid)).fetchone()


def items(db, rx_id):
    return db.execute("SELECT * FROM prescription_items WHERE prescription_id = ? ORDER BY id", (rx_id,)).fetchall()


def list_for_patient(db, cid, pid):
    rows = db.execute("""SELECT r.*, p.name AS provider_name FROM prescriptions r JOIN providers p ON p.id = r.provider_id
                         WHERE r.clinic_id = ? AND r.patient_id = ? ORDER BY r.id DESC""", (cid, pid)).fetchall()
    return [{"rx": r, "items": items(db, r["id"])} for r in rows]


def age_on(dob, on):
    """Whole years, or None. A prescription for a child under 12 must show the age in the UK."""
    if not dob:
        return None
    try:
        b = parse_date(dob)
    except ValueError:
        return None
    on = on if isinstance(on, date) else parse_date(on)
    return on.year - b.year - ((on.month, on.day) < (b.month, b.day))
