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


# ---- v0.70 drawn teeth -------------------------------------------------------------------
# Each tooth is drawn from the side (crown and roots) and from above (the biting surface, split into
# its five surfaces). Paths are for an upper tooth, roots up, crown down, in a 40 x 52 box; lower teeth
# are the same drawing turned upside down. Only the outline changes with the kind of tooth.
_ROOT_1 = "M13 30 C14 18 17 6 20 2 C23 6 26 18 27 30 Z"
SHAPES = {
    "incisor": {"crown": "M12 30 C11 38 11 45 12 51 C17 53 23 53 28 51 C29 45 29 38 28 30 Z",
                "roots": [_ROOT_1], "canals": ["M20 30 L20 7"], "top": "M7 18 C7 11 33 11 33 18 C33 25 7 25 7 18 Z"},
    "canine": {"crown": "M11 30 C9 38 11 45 20 53 C29 45 31 38 29 30 Z",
               "roots": ["M12 30 C13 15 17 2 20 0 C23 2 27 15 28 30 Z"], "canals": ["M20 30 L20 4"],
               "top": "M9 18 C9 9 31 9 31 18 C31 27 9 27 9 18 Z"},
    "premolar": {"crown": "M10 30 C8 38 9 46 12 50 C16 53 24 53 28 50 C31 46 32 38 30 30 Z",
                 "roots": [_ROOT_1], "canals": ["M20 30 L20 6"], "top": "M8 18 C8 7 32 7 32 18 C32 29 8 29 8 18 Z"},
    "molar_upper": {"crown": "M6 30 C4 38 4 46 8 50 C14 53 26 53 32 50 C36 46 36 38 34 30 Z",
                    "roots": ["M7 30 C6 20 7 10 10 3 C13 9 15 19 16 30 Z", "M16 30 C17 21 18 13 20 7 C22 13 23 21 24 30 Z",
                              "M24 30 C25 19 27 9 30 3 C33 10 34 20 33 30 Z"],
                    "canals": ["M11 30 L10 9", "M20 30 L20 11", "M29 30 L30 9"],
                    "top": "M5 12 C5 6 9 4 14 4 L26 4 C31 4 35 6 35 12 L35 24 C35 30 31 32 26 32 L14 32 C9 32 5 30 5 24 Z"},
    "molar_lower": {"crown": "M6 30 C4 38 4 46 8 50 C14 53 26 53 32 50 C36 46 36 38 34 30 Z",
                    "roots": ["M7 30 C6 20 7 10 11 3 C14 10 16 20 17 30 Z", "M23 30 C24 20 26 10 29 3 C33 10 34 20 33 30 Z"],
                    "canals": ["M12 30 L11 9", "M28 30 L29 9"],
                    "top": "M5 12 C5 6 9 4 14 4 L26 4 C31 4 35 6 35 12 L35 24 C35 30 31 32 26 32 L14 32 C9 32 5 30 5 24 Z"},
    "child_molar": {"crown": "M6 32 C4 40 5 47 9 50 C15 52 25 52 31 50 C35 47 36 40 34 32 Z",
                    "roots": ["M7 32 C5 25 4 19 3 13 C8 17 11 24 14 32 Z", "M26 32 C29 24 32 17 37 13 C36 19 35 25 33 32 Z"],
                    "canals": ["M10 32 L6 17", "M30 32 L34 17"],
                    "top": "M6 13 C6 8 10 6 15 6 L25 6 C30 6 34 8 34 13 L34 23 C34 28 30 30 25 30 L15 30 C10 30 6 28 6 23 Z"},
}
# Biting-surface split: four sides and a centre, cut to the outline above. The incisor centre is a thin edge.
CENTRE = {"incisor": "14,15 26,15 26,21 14,21", "canine": "15,13 25,13 25,23 15,23"}
CENTRE_DEFAULT = "13,11 27,11 27,25 13,25"
IMPLANT = "M15 30 L16 7 L24 7 L25 30 Z M14.5 25 H25.5 M15 19 H25 M15.5 13 H24.5"


def is_upper(n):
    return int(n) // 10 in (1, 2, 5, 6)


def tooth_kind(n):
    """incisor, canine, premolar, molar_upper, molar_lower or child_molar, from the FDI number."""
    q, pos = divmod(int(n), 10)
    if pos in (1, 2):
        return "incisor"
    if pos == 3:
        return "canine"
    if q >= 5:
        return "child_molar"
    if pos in (4, 5):
        return "premolar"
    return "molar_upper" if is_upper(n) else "molar_lower"


def whole_marks(entry):
    """Whole-tooth things recorded on a tooth (crown, implant, root canal and so on), for the drawing."""
    if not entry:
        return set()
    return {c["condition"] for c in entry["conditions"] if c["condition"] in WHOLE_TOOTH and not c["surfaces"]}


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
