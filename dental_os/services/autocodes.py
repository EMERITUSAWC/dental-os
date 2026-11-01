"""Auto codes (version 0.46): pick the treatment by type, and the system picks the right code.

A filling is billed by how many surfaces it covers and, in many fee lists, whether the tooth is at the front or the back. Rather than
staff choosing between "composite filling, one surface, back tooth" and six others, the clinic makes an auto code ("Composite
filling") with a rule for each case, and staff pick that, the tooth and the surfaces. The most exact rule that fits wins: one that
names both the number of surfaces and front or back beats one that names only one of them, which beats one that names neither.

Front teeth are the incisors and canines (FDI positions 1 to 3, milk teeth included); the rest are back teeth. No web framework."""
from . import audit

REGIONS = {"any": "Any tooth", "front": "Front tooth (incisor or canine)", "back": "Back tooth (premolar or molar)"}


class AutoError(ValueError):
    pass


def region(tooth):
    if not tooth:
        return None
    return "front" if int(tooth) % 10 <= 3 else "back"


def groups(db, cid, active_only=False):
    return db.execute("SELECT * FROM autocode_groups WHERE clinic_id = ?" + (" AND active = 1" if active_only else "") + " ORDER BY name", (cid,)).fetchall()


def get(db, cid, gid):
    return db.execute("SELECT * FROM autocode_groups WHERE id = ? AND clinic_id = ?", (gid, cid)).fetchone()


def rules(db, cid, gid):
    return db.execute("""SELECT r.*, c.code, c.name AS code_name FROM autocode_rules r JOIN procedure_codes c ON c.id = r.code_id
                         WHERE r.clinic_id = ? AND r.group_id = ? ORDER BY r.surfaces, r.region""", (cid, gid)).fetchall()


def save_group(db, clinic, user_id, name, gid=None, active=True):
    cid = clinic["id"]
    name = (name or "").strip()[:80]
    if not name:
        raise AutoError("Give the auto code a name, for example Composite filling.")
    if gid:
        if not get(db, cid, gid):
            raise AutoError("That auto code was not found.")
        db.execute("UPDATE autocode_groups SET name = ?, active = ? WHERE id = ?", (name, 1 if active else 0, gid))
    else:
        gid = db.execute("INSERT INTO autocode_groups(clinic_id, name, active) VALUES (?,?,?)", (cid, name, 1 if active else 0)).lastrowid
    audit.log(db, cid, user_id, "autocode.save", None, None, name)
    return gid


def add_rule(db, clinic, user_id, gid, surfaces, region_, code_id):
    cid = clinic["id"]
    if not get(db, cid, gid):
        raise AutoError("That auto code was not found.")
    if not isinstance(surfaces, int) or not 0 <= surfaces <= 5:
        raise AutoError("Surfaces is a number from 1 to 5, or any.")
    if region_ not in REGIONS:
        raise AutoError("Pick which teeth the rule is for.")
    if not db.execute("SELECT 1 FROM procedure_codes WHERE id = ? AND clinic_id = ? AND active = 1", (code_id, cid)).fetchone():
        raise AutoError("Pick a treatment from the list.")
    if db.execute("SELECT 1 FROM autocode_rules WHERE group_id = ? AND surfaces = ? AND region = ?", (gid, surfaces, region_)).fetchone():
        raise AutoError("There is already a rule for that case. Remove it first.")
    db.execute("INSERT INTO autocode_rules(clinic_id, group_id, surfaces, region, code_id) VALUES (?,?,?,?,?)", (cid, gid, surfaces, region_, code_id))
    audit.log(db, cid, user_id, "autocode.rule", None, None, str(gid))


def remove_rule(db, clinic, user_id, rule_id):
    n = db.execute("DELETE FROM autocode_rules WHERE id = ? AND clinic_id = ?", (rule_id, clinic["id"])).rowcount
    if not n:
        raise AutoError("That rule was not found.")
    audit.log(db, clinic["id"], user_id, "autocode.rule_remove", None, None, str(rule_id))


def resolve(db, cid, gid, tooth, surfaces):
    """The treatment code for this auto code, tooth and surfaces. Raises AutoError with what is missing."""
    g = get(db, cid, gid)
    if not g or not g["active"]:
        raise AutoError("That auto code was not found.")
    n = len(surfaces or "")
    reg = region(tooth)
    best, score = None, -1
    for r in rules(db, cid, gid):
        if r["surfaces"] not in (0, n) or r["region"] not in ("any", reg):
            continue
        s = (2 if r["surfaces"] else 0) + (1 if r["region"] != "any" else 0)
        if s > score:
            best, score = r, s
    if not best:
        if not tooth and not n:
            raise AutoError("No rule of this auto code fits. Pick the tooth and the surfaces.")
        if not tooth:
            raise AutoError("No rule of this auto code fits. Pick the tooth.")
        if not n:
            raise AutoError("No rule of this auto code fits. Pick the surfaces.")
        raise AutoError("No rule of this auto code fits. Ask a manager to add a rule for this case.")
    return best["code_id"]
