"""The tooth chart. FDI numbers. One place that joins what is already in the mouth (conditions)
with planned and finished work from the procedure log."""
from . import audit
from .timeutil import now_local
from . import treatment as TR

CONDITIONS = {
    "caries": "Decay", "filling": "Filling", "crown": "Crown", "missing": "Missing", "implant": "Implant",
    "root_canal": "Root canal", "bridge_pontic": "Bridge, false tooth", "bridge_abutment": "Bridge, support tooth",
    "veneer": "Veneer", "fracture": "Fracture", "impacted": "Impacted", "unerupted": "Not yet through",
    "sealant": "Sealant", "wear": "Wear", "defective": "Failing restoration", "watch": "Keep an eye on",
}
# Whole-tooth conditions: shown on the whole tooth, not on one surface.
WHOLE_TOOTH = {"crown", "missing", "implant", "root_canal", "bridge_pontic", "bridge_abutment", "veneer",
               "impacted", "unerupted", "fracture", "watch"}

ADULT_UPPER = list(range(18, 10, -1)) + list(range(21, 29))
ADULT_LOWER = list(range(48, 40, -1)) + list(range(31, 39))
CHILD_UPPER = list(range(55, 50, -1)) + list(range(61, 66))
CHILD_LOWER = list(range(85, 80, -1)) + list(range(71, 76))


class ChartError(Exception):
    pass


def side_of(tooth):
    """'right' for quadrants 1, 4, 5, 8 (the patient's right, drawn on the left). Mesial faces the middle."""
    return "right" if tooth // 10 in (1, 4, 5, 8) else "left"


def _own_patient(db, cid, pid):
    if not db.execute("SELECT 1 FROM patients WHERE id = ? AND clinic_id = ?", (pid, cid)).fetchone():
        raise ChartError("That patient was not found.")


def add_condition(db, clinic, user_id, pid, tooth, surfaces, condition, note=None):
    cid = clinic["id"]
    _own_patient(db, cid, pid)
    try:
        tooth = TR.clean_tooth(tooth)
        if isinstance(surfaces, str):
            surfaces = list(surfaces)
        surfaces = TR.clean_surfaces(surfaces)
    except TR.TreatmentError as exc:
        raise ChartError(str(exc))
    if tooth is None:
        raise ChartError("Pick a tooth.")
    if condition not in CONDITIONS:
        raise ChartError("Pick a condition from the list.")
    if condition in WHOLE_TOOTH:
        surfaces = None
    elif not surfaces:
        raise ChartError("Pick the surface or surfaces this affects.")
    note = (note or "").strip()[:200] or None
    dup = db.execute("""SELECT 1 FROM tooth_conditions WHERE clinic_id=? AND patient_id=? AND tooth=? AND condition=?
                        AND status='active' AND surfaces IS ?""", (cid, pid, tooth, condition, surfaces)).fetchone()
    if dup:
        raise ChartError("That is already on the chart.")
    today = now_local(clinic["timezone"]).date().isoformat()
    cur = db.execute(
        """INSERT INTO tooth_conditions(clinic_id, patient_id, tooth, surfaces, condition, note, recorded_by, recorded_on)
           VALUES (?,?,?,?,?,?,?,?)""", (cid, pid, tooth, surfaces, condition, note, user_id, today))
    audit.log(db, cid, user_id, "chart.add", "patient", pid, f"{tooth} {condition}")
    return cur.lastrowid


def remove_condition(db, clinic, user_id, cond_id):
    row = db.execute("SELECT * FROM tooth_conditions WHERE id = ? AND clinic_id = ? AND status = 'active'",
                     (cond_id, clinic["id"])).fetchone()
    if not row:
        raise ChartError("That entry was not found.")
    today = now_local(clinic["timezone"]).date().isoformat()
    db.execute("UPDATE tooth_conditions SET status='removed', removed_by=?, removed_on=? WHERE id=?",
               (user_id, today, cond_id))
    audit.log(db, clinic["id"], user_id, "chart.remove", "patient", row["patient_id"], f"{row['tooth']} {row['condition']}")
    return row["patient_id"]


def chart_data(db, cid, pid):
    """tooth -> {'conditions': [...], 'planned': [...], 'done': [...]}"""
    out = {}

    def slot(t):
        return out.setdefault(t, {"conditions": [], "planned": [], "done": []})
    for r in db.execute("SELECT * FROM tooth_conditions WHERE clinic_id=? AND patient_id=? AND status='active' ORDER BY id",
                        (cid, pid)):
        slot(r["tooth"])["conditions"].append(r)
    for r in db.execute(
            """SELECT pr.*, c.name AS code_name FROM procedures pr JOIN procedure_codes c ON c.id = pr.code_id
               WHERE pr.clinic_id=? AND pr.patient_id=? AND pr.tooth IS NOT NULL AND pr.status IN ('planned','done')
               ORDER BY pr.id""", (cid, pid)):
        slot(r["tooth"])[r["status"]].append(r)
    return out


def surface_states(entry):
    """For one tooth: which of M O D B L I are drawn as planned, done or existing, plus whole-tooth state."""
    surf, whole = {}, None
    if not entry:
        return surf, whole
    for c in entry["conditions"]:
        if c["condition"] == "missing":
            whole = "missing"
        elif c["surfaces"]:
            for s in c["surfaces"]:
                surf.setdefault(s, "cond")
        elif whole != "missing":
            whole = whole or "cond"
    for r in entry["done"]:
        for s in (r["surfaces"] or ""):
            surf[s] = "done"
        if not r["surfaces"] and whole != "missing":
            whole = "done" if whole in (None, "cond") else whole
    for r in entry["planned"]:
        for s in (r["surfaces"] or ""):
            surf[s] = "planned"
        if not r["surfaces"] and whole != "missing":
            whole = "planned"
    return surf, whole
