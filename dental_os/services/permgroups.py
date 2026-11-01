"""Permission groups (version 0.30).

The four built-in roles (manager, dentist, assistant, front desk) set the most a person can do. A permission group narrows that for
the people in it, area by area: for example "Front desk, no money" can book and register patients but cannot take payments, see
claims or run reports. A group never gives anyone more than their role allows, so it cannot open clinical records to front desk staff
by mistake. Managers are never put in a group, so a clinic cannot lock itself out.

Every screen belongs to one area below; the check runs before any screen opens. No web framework here."""
import json

from . import audit

# area: (label, endpoints or endpoint prefixes that belong to it). The longest matching prefix wins.
AREAS = {
    "diary": ("Diary and appointments", ["views.diary", "diary.", "views.appointment", "practice.kiosk", "practice.missed", "practice.unscheduled"]),
    "patients": ("Register and edit patients", ["views.patient_new", "views.patient_edit", "views.patient_archive", "record.import", "cforms."]),
    "clinical": ("Clinical record", ["record.", "care.", "steri.patient_instruments", "ortho.", "implants.", "aftercare.", "clinical.treatment", "clinical.procedure_action"]),
    "plans": ("Treatment plans and quotes", ["commercial.patient_plans", "commercial.plan_", "commercial.plans_board"]),
    "payments": ("Take payments and give receipts", ["clinical.account", "clinical.payment_add", "clinical.receipt", "payments.",
                                                     "commercial.instalment_pay", "commercial.membership_charge_pay", "practice.cashup"]),
    "money_changes": ("Void payments, discounts and write-offs", ["clinical.payment_void", "clinical.adjustment_add", "finance.invoice_void",
                                                                 "commercial.instalment_cancel", "commercial.membership_charge_waive",
                                                                 "commercial.membership_cancel", "commercial.membership_charge_fail"]),
    "insurance": ("Insurance, claims, invoices and statements", ["finance.", "edi.", "statements.", "ghclaims."]),
    "memberships": ("Memberships and instalment plans", ["commercial.memberships_home", "commercial.membership_plans", "commercial.patient_membership",
                                                        "commercial.patient_instalments"]),
    "messages": ("Patient messages, campaigns and booking requests", ["comms.", "voice.", "clinical.recall", "react."]),
    "reports": ("Reports and key numbers", ["clinical.report_view", "clinical.takings", "ops.kpi", "ops.group_view", "reports.", "practice.day_sheet",
                                            "practice.production", "finance.ageing_report"]),
    "lab": ("Lab cases", ["ops.lab", "ops.patient_lab"]),
    "stock": ("Stock", ["ops.stock", "kits."]),
    "sterilisation": ("Sterilisation", ["steri."]),
    "nhs": ("NHS courses and FP17s", ["nhs."]),
    "data": ("Exports and data rights", ["record.export_data", "practice.export", "privacy."]),
    "staff_admin": ("Time cards", ["practice.timecards"]),
    "governance": ("Complaints and safety incidents: see and handle them", ["gov."]),
}
ALWAYS = ("views.dashboard", "views.patients", "views.patient_view", "auth.", "static", "health", "team.", "practice.tasks", "practice.task_action",
          "practice.clock",
          "gov.complaint_new", "gov.incident_new", "cpd.mine",            # anyone can write down a complaint or an incident
          "pilot.problems")                                                  # 0.59: and report a problem


class GroupError(Exception):
    pass


def area_for(endpoint):
    if not endpoint:
        return None
    best, size = None, -1
    for key, (_label, prefixes) in AREAS.items():
        for p in prefixes:
            if endpoint.startswith(p) and len(p) > size:
                best, size = key, len(p)
    return best


def allowed(db, user, endpoint):
    """True unless the person's group leaves out the area this screen belongs to."""
    if not user or user["role"] == "manager" or not user["perm_group_id"]:
        return True
    if not endpoint or any(endpoint == a or (a.endswith(".") and endpoint.startswith(a)) for a in ALWAYS):
        return True
    area = area_for(endpoint)
    if area is None:
        return True
    return area in areas_of(db, user)


def areas_of(db, user):
    if not user or user["role"] == "manager" or not user["perm_group_id"]:
        return set(AREAS)
    row = db.execute("SELECT areas FROM perm_groups WHERE id = ? AND clinic_id = ?", (user["perm_group_id"], user["clinic_id"])).fetchone()
    return set(json.loads(row["areas"])) if row else set(AREAS)


def groups(db, cid):
    rows = db.execute("SELECT * FROM perm_groups WHERE clinic_id = ? ORDER BY name", (cid,)).fetchall()
    return [{"g": r, "areas": set(json.loads(r["areas"])), "members": db.execute(
        "SELECT display_name FROM users WHERE perm_group_id = ? AND active = 1 ORDER BY display_name", (r["id"],)).fetchall()} for r in rows]


def save(db, clinic, user_id, name, areas, group_id=None):
    cid = clinic["id"]
    name = (name or "").strip()[:60]
    if not name:
        raise GroupError("Give the group a name, for example Front desk, no money.")
    picked = sorted(a for a in areas if a in AREAS)
    if group_id:
        if not db.execute("SELECT 1 FROM perm_groups WHERE id = ? AND clinic_id = ?", (group_id, cid)).fetchone():
            raise GroupError("That group was not found.")
        if db.execute("SELECT 1 FROM perm_groups WHERE clinic_id = ? AND name = ? AND id != ?", (cid, name, group_id)).fetchone():
            raise GroupError("There is already a group with that name.")
        db.execute("UPDATE perm_groups SET name = ?, areas = ? WHERE id = ?", (name, json.dumps(picked), group_id))
    else:
        if db.execute("SELECT 1 FROM perm_groups WHERE clinic_id = ? AND name = ?", (cid, name)).fetchone():
            raise GroupError("There is already a group with that name.")
        group_id = db.execute("INSERT INTO perm_groups(clinic_id, name, areas) VALUES (?,?,?)", (cid, name, json.dumps(picked))).lastrowid
    audit.log(db, cid, user_id, "permgroup.save", None, None, name)
    return group_id


def assign(db, clinic, user_id, target_id, group_id):
    cid = clinic["id"]
    u = db.execute("SELECT * FROM users WHERE id = ? AND clinic_id = ?", (target_id, cid)).fetchone()
    if not u:
        raise GroupError("That person was not found.")
    if group_id:
        if u["role"] == "manager":
            raise GroupError("Managers are never put in a group, so the clinic cannot lock itself out.")
        if not db.execute("SELECT 1 FROM perm_groups WHERE id = ? AND clinic_id = ?", (group_id, cid)).fetchone():
            raise GroupError("That group was not found.")
    db.execute("UPDATE users SET perm_group_id = ? WHERE id = ?", (group_id or None, target_id))
    audit.log(db, cid, user_id, "permgroup.assign", None, None, f"{u['username']} -> {group_id or 'none'}")


AREA_LABELS = {k: v[0] for k, v in AREAS.items()}
