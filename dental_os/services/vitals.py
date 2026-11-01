"""Vital signs taken at a visit: blood pressure, pulse, temperature, oxygen, blood sugar.
Blood pressure is graded against two levels the clinic sets. The defaults follow a dental school's clinic manual (University of
Oklahoma College of Dentistry): from 160/100, put off planned treatment and refer to a doctor; from 180/110, no treatment of any kind
without a doctor's advice. Below 90/60, watch for fainting. The clinic's dentists decide the levels they use."""
from . import audit
from .timeutil import now_local

RANGES = {"bp_systolic": (60, 260), "bp_diastolic": (30, 160), "pulse": (30, 220), "temperature_c": (30.0, 43.0),
          "spo2": (50, 100), "glucose_mmol": (1.0, 40.0)}
GRADES = {"ok": "In range", "low": "Low: watch for fainting", "amber": "High: put off planned treatment and refer to a doctor",
          "red": "Very high: no treatment without a doctor's advice"}


class VitalsError(Exception):
    pass


def _num(raw, key):
    raw = (str(raw).strip() if raw is not None else "")
    if not raw:
        return None
    try:
        v = float(raw.replace(",", "."))
    except ValueError:
        raise VitalsError("Use numbers only for the readings.")
    lo, hi = RANGES[key]
    if not lo <= v <= hi:
        raise VitalsError("A reading is outside what a person can have. Check it was typed right.")
    return v if isinstance(lo, float) else int(round(v))


def parse_level(text):
    try:
        s, d = (int(x) for x in (text or "").split("/"))
    except ValueError:
        raise VitalsError("Write a blood pressure level like 160/100.")
    if not (80 <= s <= 260 and 40 <= d <= 160):
        raise VitalsError("Write a blood pressure level like 160/100.")
    return s, d


def grade(clinic, sys_, dia):
    if sys_ is None or dia is None:
        return None
    rs, rd = parse_level(clinic["bp_red"])
    as_, ad = parse_level(clinic["bp_amber"])
    if sys_ >= rs or dia >= rd:
        return "red"
    if sys_ >= as_ or dia >= ad:
        return "amber"
    if sys_ < 90 or dia < 60:
        return "low"
    return "ok"


def record(db, clinic, user_id, pid, form, appointment_id=None):
    cid = clinic["id"]
    if not db.execute("SELECT 1 FROM patients WHERE id = ? AND clinic_id = ?", (pid, cid)).fetchone():
        raise VitalsError("That patient was not found.")
    vals = {k: _num(form.get(k), k) for k in RANGES}
    if all(v is None for v in vals.values()):
        raise VitalsError("Enter at least one reading.")
    if (vals["bp_systolic"] is None) != (vals["bp_diastolic"] is None):
        raise VitalsError("Enter both blood pressure numbers.")
    if vals["bp_systolic"] is not None and vals["bp_systolic"] <= vals["bp_diastolic"]:
        raise VitalsError("The top blood pressure number must be higher than the bottom one.")
    if appointment_id and not db.execute("SELECT 1 FROM appointments WHERE id = ? AND clinic_id = ? AND patient_id = ?",
                                         (appointment_id, cid, pid)).fetchone():
        raise VitalsError("That visit does not belong to this patient.")
    when = now_local(clinic["timezone"]).strftime("%Y-%m-%d %H:%M")
    vid = db.execute(
        """INSERT INTO vitals(clinic_id, patient_id, appointment_id, taken_at, bp_systolic, bp_diastolic, pulse, temperature_c, spo2,
                              glucose_mmol, note, recorded_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (cid, pid, appointment_id or None, when, vals["bp_systolic"], vals["bp_diastolic"], vals["pulse"], vals["temperature_c"],
         vals["spo2"], vals["glucose_mmol"], (form.get("note") or "").strip()[:300] or None, user_id)).lastrowid
    audit.log(db, cid, user_id, "vitals.record", "patient", pid)
    return vid, grade(clinic, vals["bp_systolic"], vals["bp_diastolic"])


def history(db, cid, pid, limit=20):
    return db.execute("""SELECT v.*, u.display_name AS by_name FROM vitals v LEFT JOIN users u ON u.id = v.recorded_by
                         WHERE v.clinic_id = ? AND v.patient_id = ? ORDER BY v.taken_at DESC, v.id DESC LIMIT ?""", (cid, pid, limit)).fetchall()


def latest_today(db, clinic, pid):
    """Today's latest blood pressure and its grade, for the alert bar."""
    day = now_local(clinic["timezone"]).date().isoformat()
    row = db.execute("""SELECT * FROM vitals WHERE clinic_id = ? AND patient_id = ? AND taken_at >= ? AND bp_systolic IS NOT NULL
                        ORDER BY taken_at DESC, id DESC LIMIT 1""", (clinic["id"], pid, day)).fetchone()
    if not row:
        return None
    return {"row": row, "grade": grade(clinic, row["bp_systolic"], row["bp_diastolic"])}


def save_levels(db, clinic, user_id, amber, red):
    a, r = parse_level(amber), parse_level(red)
    if not (r[0] > a[0] and r[1] > a[1]):
        raise VitalsError("The very high level must be above the high level.")
    db.execute("UPDATE clinics SET bp_amber = ?, bp_red = ? WHERE id = ?", (f"{a[0]}/{a[1]}", f"{r[0]}/{r[1]}", clinic["id"]))
    audit.log(db, clinic["id"], user_id, "settings.bp_levels", None, None, f"{a[0]}/{a[1]} {r[0]}/{r[1]}")
