"""Groups of clinics. A group view shows headline numbers side by side. It is read only and never opens another clinic's
patients or records. A member must be a manager of a clinic that belongs to the group."""
from . import audit, kpi
from .timeutil import now_local


class OrgError(Exception):
    pass


def create(db, name):
    name = (name or "").strip()
    if not name or len(name) > 80:
        raise OrgError("Give the group a name of up to 80 characters.")
    return db.execute("INSERT INTO organisations(name) VALUES (?)", (name,)).lastrowid


def add_clinic(db, org_id, clinic_id):
    if not db.execute("SELECT 1 FROM organisations WHERE id = ?", (org_id,)).fetchone():
        raise OrgError("No group has that id.")
    clinic = db.execute("SELECT * FROM clinics WHERE id = ?", (clinic_id,)).fetchone()
    if not clinic:
        raise OrgError("No clinic has that id.")
    if clinic["org_id"] not in (None, org_id):
        raise OrgError("That clinic already belongs to another group.")
    db.execute("UPDATE clinics SET org_id = ? WHERE id = ?", (org_id, clinic_id))


def add_member(db, org_id, user_id):
    user = db.execute("SELECT u.*, c.org_id FROM users u JOIN clinics c ON c.id = u.clinic_id WHERE u.id = ?", (user_id,)).fetchone()
    if not user:
        raise OrgError("No user has that id.")
    if user["role"] != "manager":
        raise OrgError("Only a clinic manager can be given the group view.")
    if user["org_id"] != org_id:
        raise OrgError("Add the user's clinic to the group first.")
    db.execute("INSERT OR IGNORE INTO org_members(org_id, user_id) VALUES (?,?)", (org_id, user_id))


def remove_member(db, org_id, user_id):
    db.execute("DELETE FROM org_members WHERE org_id = ? AND user_id = ?", (org_id, user_id))


def for_user(db, user):
    """The group this user may see, or None. Needs a manager whose own clinic is in the group."""
    if not user or user["role"] != "manager":
        return None
    return db.execute(
        """SELECT o.* FROM organisations o JOIN org_members m ON m.org_id = o.id
           JOIN clinics c ON c.org_id = o.id AND c.id = ? WHERE m.user_id = ? ORDER BY o.id LIMIT 1""",
        (user["clinic_id"], user["id"])).fetchone()


def clinics(db, org_id):
    return db.execute("SELECT * FROM clinics WHERE org_id = ? ORDER BY name, id", (org_id,)).fetchall()


def compare(db, org, start, end, viewer=None):
    """One row per clinic, plus totals for each currency. Money from different currencies is never added."""
    start, end = kpi.clamp_range(start, end)
    rows, totals = [], {}
    for c in clinics(db, org["id"]):
        today = now_local(c["timezone"]).date()
        cur = kpi.period(db, c, start, end)
        prev_s, prev_e = kpi.previous_range(start, end)
        prev = kpi.period(db, c, prev_s, prev_e)
        rec = kpi.recall_health(db, c["id"], today)
        owed = kpi.ageing_totals(db, c)["grand_p"]
        rows.append({"clinic": c, "cur": cur, "prev": prev, "recall": rec, "owed_p": owed,
                     "gross_change": kpi.change(cur["gross_p"], prev["gross_p"])})
        t = totals.setdefault(c["currency"], {"gross_p": 0, "net_p": 0, "collected_p": 0, "owed_p": 0, "clinics": 0})
        t["gross_p"] += cur["gross_p"]
        t["net_p"] += cur["net_p"]
        t["collected_p"] += cur["collected_p"]
        t["owed_p"] += owed
        t["clinics"] += 1
    if viewer is not None:
        audit.log(db, viewer["clinic_id"], viewer["id"], "org.view", "organisation", org["id"])
    return {"start": start, "end": end, "rows": rows, "totals": totals,
            "patients_new": sum(r["cur"]["new_patients"] for r in rows),
            "visits": sum(r["cur"]["visits"]["done"] for r in rows)}
