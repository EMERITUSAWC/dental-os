"""Medical history and alerts. An alert is a short warning that follows the patient everywhere they appear
(patient page, diary, appointment). Alerts are resolved, never deleted."""
import re

from . import audit

KINDS = {"allergy": "Allergy", "medical": "Medical", "medication": "Medication", "behaviour": "Behaviour", "other": "Other"}
SEVERITIES = {"info": "For information", "warning": "Take care", "critical": "Critical"}
_RANK = {"info": 1, "warning": 2, "critical": 3}
SMOKER = {"": "Not asked", "never": "Never", "former": "Former", "current": "Current"}


class RecordError(Exception):
    pass


def _patient_exists(db, cid, pid):
    if not db.execute("SELECT 1 FROM patients WHERE id = ? AND clinic_id = ?", (pid, cid)).fetchone():
        raise RecordError("That patient was not found.")


def _text(value, limit, label):
    value = re.sub(r"[ \t]+", " ", (value or "").strip())
    if len(value) > limit:
        raise RecordError(f"{label} is too long (the limit is {limit} characters).")
    return value or None


def get_history(db, cid, pid):
    return db.execute("SELECT * FROM medical_history WHERE clinic_id = ? AND patient_id = ?", (cid, pid)).fetchone()


def save_history(db, clinic, user_id, pid, form, today_iso):
    cid = clinic["id"]
    _patient_exists(db, cid, pid)
    conditions = _text(form.get("conditions"), 2000, "Conditions")
    medications = _text(form.get("medications"), 2000, "Medicines")
    allergies = _text(form.get("allergies"), 1000, "Allergies")
    notes = _text(form.get("notes"), 2000, "Notes")
    pregnant = form.get("pregnant") if form.get("pregnant") in ("yes", "no") else ""
    smoker = form.get("smoker") if form.get("smoker") in ("never", "former", "current") else ""
    thinners = 1 if form.get("blood_thinners") else 0
    old = get_history(db, cid, pid)
    reviewed = today_iso if form.get("reviewed") else (old["reviewed_on"] if old else None)
    db.execute(
        """INSERT INTO medical_history(clinic_id, patient_id, conditions, medications, allergies, pregnant, smoker,
                                       blood_thinners, notes, reviewed_on, updated_by, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?, datetime('now'))
           ON CONFLICT(clinic_id, patient_id) DO UPDATE SET conditions=excluded.conditions,
             medications=excluded.medications, allergies=excluded.allergies, pregnant=excluded.pregnant,
             smoker=excluded.smoker, blood_thinners=excluded.blood_thinners, notes=excluded.notes,
             reviewed_on=excluded.reviewed_on, updated_by=excluded.updated_by, updated_at=datetime('now')""",
        (cid, pid, conditions, medications, allergies, pregnant, smoker, thinners, notes, reviewed, user_id))
    _sync_alerts(db, cid, user_id, pid, allergies, thinners, pregnant)
    audit.log(db, cid, user_id, "medical.save", "patient", pid)


def _sync_alerts(db, cid, user_id, pid, allergies, thinners, pregnant):
    """Keep the alerts that come from the history form in step with it. Manual alerts are left alone."""
    wanted = []
    if allergies:
        sev = "critical" if re.search(r"anaphyla|epipen|severe", allergies, re.I) else "warning"
        wanted.append(("allergy", sev, allergies[:300]))
    if thinners:
        wanted.append(("medical", "warning", "Takes blood thinners, so bleeding may last longer"))
    if pregnant == "yes":
        wanted.append(("medical", "info", "Pregnant"))
    current = db.execute("SELECT id, kind, severity, text FROM patient_alerts WHERE clinic_id = ? AND patient_id = ? "
                         "AND source = 'history' AND active = 1", (cid, pid)).fetchall()
    keep = {(r["kind"], r["severity"], r["text"]) for r in current}
    for r in current:
        if (r["kind"], r["severity"], r["text"]) not in wanted:
            db.execute("UPDATE patient_alerts SET active = 0, resolved_by = ?, resolved_at = datetime('now') WHERE id = ?",
                       (user_id, r["id"]))
    for item in wanted:
        if item not in keep:
            db.execute("INSERT INTO patient_alerts(clinic_id, patient_id, kind, severity, text, source, created_by) "
                       "VALUES (?,?,?,?,?, 'history', ?)", (cid, pid, *item, user_id))


def active_alerts(db, cid, pid):
    return db.execute(
        """SELECT * FROM patient_alerts WHERE clinic_id = ? AND patient_id = ? AND active = 1
           ORDER BY CASE severity WHEN 'critical' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END, id""", (cid, pid)).fetchall()


def alert_level(db, cid, pid):
    """The worst active severity for a patient, or None. For small markers in lists."""
    rows = db.execute("SELECT DISTINCT severity FROM patient_alerts WHERE clinic_id = ? AND patient_id = ? AND active = 1",
                      (cid, pid)).fetchall()
    return max((r["severity"] for r in rows), key=lambda s: _RANK[s], default=None)


def add_alert(db, clinic, user_id, pid, kind, severity, text):
    cid = clinic["id"]
    _patient_exists(db, cid, pid)
    if kind not in KINDS or severity not in SEVERITIES:
        raise RecordError("Pick a type and a level for the alert.")
    text = _text(text, 300, "The alert")
    if not text:
        raise RecordError("Write what the alert says.")
    cur = db.execute("INSERT INTO patient_alerts(clinic_id, patient_id, kind, severity, text, created_by) VALUES (?,?,?,?,?,?)",
                     (cid, pid, kind, severity, text, user_id))
    audit.log(db, cid, user_id, "alert.add", "patient", pid, severity)
    return cur.lastrowid


def resolve_alert(db, clinic, user_id, alert_id):
    row = db.execute("SELECT * FROM patient_alerts WHERE id = ? AND clinic_id = ? AND active = 1",
                     (alert_id, clinic["id"])).fetchone()
    if not row:
        raise RecordError("That alert was not found.")
    db.execute("UPDATE patient_alerts SET active = 0, resolved_by = ?, resolved_at = datetime('now') WHERE id = ?",
               (user_id, alert_id))
    audit.log(db, clinic["id"], user_id, "alert.resolve", "patient", row["patient_id"])
    return row["patient_id"]
