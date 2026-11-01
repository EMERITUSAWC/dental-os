"""Treatment plans. A plan is one option (a patient can have several), split into phases, with a fee on each line.
Lines are only estimates until the patient accepts. Acceptance turns them into planned work on the treatment list,
which is where billing already looks. Declined plans create nothing."""
from . import audit, billing, serialize, webhooks
from . import feesched as FS
from . import treatment as TR
from .timeutil import add_months, now_local, parse_date
from datetime import timedelta

STATUSES = {"draft": "Draft", "presented": "Waiting for a decision", "accepted": "Accepted", "declined": "Declined"}
MAX_ITEMS = 60


class PlanError(Exception):
    pass


def _today(clinic):
    return now_local(clinic["timezone"]).date()


def get(db, cid, plan_id):
    return db.execute("SELECT * FROM treatment_plans WHERE id = ? AND clinic_id = ?", (plan_id, cid)).fetchone()


def _event(db, cid, plan_id, user_id, kind, note=None):
    db.execute("INSERT INTO plan_events(clinic_id, plan_id, kind, note, by_user) VALUES (?,?,?,?,?)",
               (cid, plan_id, kind, (note or "").strip()[:300] or None, user_id))


def create(db, clinic, user_id, pid, title, note=None):
    cid = clinic["id"]
    if not db.execute("SELECT 1 FROM patients WHERE id = ? AND clinic_id = ? AND status = 'active'", (pid, cid)).fetchone():
        raise PlanError("That patient was not found, or is archived.")
    title = (title or "").strip()
    if not title or len(title) > 80:
        raise PlanError("Give the plan a short name, for example Option A: crown.")
    cur = db.execute("INSERT INTO treatment_plans(clinic_id, patient_id, title, note, created_by) VALUES (?,?,?,?,?)",
                     (cid, pid, title, (note or "").strip()[:300] or None, user_id))
    _event(db, cid, cur.lastrowid, user_id, "created")
    audit.log(db, cid, user_id, "plan.create", "plan", cur.lastrowid)
    return cur.lastrowid


def _editable(db, cid, plan_id):
    plan = get(db, cid, plan_id)
    if not plan:
        raise LookupError("plan")
    if plan["status"] in ("accepted",):
        raise PlanError("This plan was accepted. Change the work on the treatment list instead.")
    return plan


def add_item(db, clinic, user_id, plan_id, code_id, tooth=None, surfaces=None, fee_p=None, phase=1, note=None):
    cid = clinic["id"]
    plan = _editable(db, cid, plan_id)
    code = db.execute("SELECT * FROM procedure_codes WHERE id = ? AND clinic_id = ? AND active = 1", (code_id, cid)).fetchone()
    if not code:
        raise PlanError("Pick a procedure from the list.")
    if not isinstance(phase, int) or not 1 <= phase <= 9:
        raise PlanError("The phase must be a number from 1 to 9.")
    fee = FS.fee_for(db, cid, plan["patient_id"], code) if fee_p is None else fee_p
    if fee < 0:
        raise PlanError("A fee cannot be negative.")
    n = db.execute("SELECT COUNT(*) FROM plan_items WHERE plan_id = ? AND clinic_id = ? AND removed = 0", (plan_id, cid)).fetchone()[0]
    if n >= MAX_ITEMS:
        raise PlanError("That is a lot of lines for one plan. Split it into two plans.")
    cur = db.execute("INSERT INTO plan_items(clinic_id, plan_id, code_id, tooth, surfaces, fee_p, phase, note) VALUES (?,?,?,?,?,?,?,?)",
                     (cid, plan_id, code_id, tooth, surfaces, fee, phase, (note or "").strip()[:200] or None))
    audit.log(db, cid, user_id, "plan.item.add", "plan", plan_id)
    if plan["status"] == "presented":
        _event(db, cid, plan_id, user_id, "changed", "A line was added after the plan was presented")
    return cur.lastrowid


def _item(db, cid, item_id):
    row = db.execute("SELECT i.*, p.status AS plan_status FROM plan_items i JOIN treatment_plans p ON p.id = i.plan_id "
                     "WHERE i.id = ? AND i.clinic_id = ? AND i.removed = 0", (item_id, cid)).fetchone()
    if not row:
        raise LookupError("item")
    if row["plan_status"] == "accepted":
        raise PlanError("This plan was accepted. Change the work on the treatment list instead.")
    return row


def update_item(db, clinic, user_id, item_id, fee_p=None, phase=None):
    cid = clinic["id"]
    item = _item(db, cid, item_id)
    if fee_p is not None:
        if fee_p < 0:
            raise PlanError("A fee cannot be negative.")
        db.execute("UPDATE plan_items SET fee_p = ? WHERE id = ?", (fee_p, item_id))
    if phase is not None:
        if not isinstance(phase, int) or not 1 <= phase <= 9:
            raise PlanError("The phase must be a number from 1 to 9.")
        db.execute("UPDATE plan_items SET phase = ? WHERE id = ?", (phase, item_id))
    audit.log(db, cid, user_id, "plan.item.update", "plan", item["plan_id"])
    return item["plan_id"]


def remove_item(db, clinic, user_id, item_id):
    cid = clinic["id"]
    item = _item(db, cid, item_id)
    db.execute("UPDATE plan_items SET removed = 1 WHERE id = ?", (item_id,))
    audit.log(db, cid, user_id, "plan.item.remove", "plan", item["plan_id"])
    return item["plan_id"]


def items(db, cid, plan_id):
    return db.execute(
        """SELECT i.*, c.code, c.name AS code_name, pr.status AS work_status FROM plan_items i
           JOIN procedure_codes c ON c.id = i.code_id LEFT JOIN procedures pr ON pr.id = i.procedure_id
           WHERE i.plan_id = ? AND i.clinic_id = ? AND i.removed = 0 ORDER BY i.phase, i.id""", (plan_id, cid)).fetchall()


def phases(rows):
    """[(phase number, [lines], subtotal)] in order."""
    out = {}
    for r in rows:
        out.setdefault(r["phase"], []).append(r)
    return [(n, lines, sum(x["fee_p"] for x in lines)) for n, lines in sorted(out.items())]


def total(rows):
    return sum(r["fee_p"] for r in rows)


def deposits(db, cid, plan_id):
    return db.execute("SELECT COALESCE(SUM(amount_p), 0) FROM payments WHERE clinic_id = ? AND plan_id = ? AND voided = 0",
                      (cid, plan_id)).fetchone()[0]


def events(db, cid, plan_id):
    return db.execute("""SELECT e.*, u.display_name AS user_name FROM plan_events e LEFT JOIN users u ON u.id = e.by_user
                         WHERE e.plan_id = ? AND e.clinic_id = ? ORDER BY e.id DESC""", (plan_id, cid)).fetchall()


def for_patient(db, cid, pid):
    out = []
    for p in db.execute("SELECT * FROM treatment_plans WHERE clinic_id = ? AND patient_id = ? ORDER BY id DESC", (cid, pid)):
        rows = items(db, cid, p["id"])
        out.append({"plan": p, "total": total(rows), "lines": len(rows)})
    return out


def present(db, clinic, user, plan_id, follow_up_days=7, owner_id=None):
    cid = clinic["id"]
    plan = _editable(db, cid, plan_id)
    if plan["status"] == "declined":
        raise PlanError("Use Reopen to present a declined plan again.")
    if not items(db, cid, plan_id):
        raise PlanError("Add at least one line before presenting the plan.")
    if not isinstance(follow_up_days, int) or not 1 <= follow_up_days <= 90:
        raise PlanError("Follow up within 1 to 90 days.")
    if owner_id is not None and not db.execute("SELECT 1 FROM users WHERE id = ? AND clinic_id = ? AND active = 1", (owner_id, cid)).fetchone():
        raise PlanError("Pick someone from this clinic to follow it up.")
    today = _today(clinic)
    db.execute("UPDATE treatment_plans SET status = 'presented', presented_on = COALESCE(presented_on, ?), follow_up_on = ?, owner_id = ? "
               "WHERE id = ? AND clinic_id = ?", (today.isoformat(), (today + timedelta(days=follow_up_days)).isoformat(),
                                                   owner_id or user["id"], plan_id, cid))
    _event(db, cid, plan_id, user["id"], "presented")
    audit.log(db, cid, user["id"], "plan.present", "plan", plan_id)


def log_followup(db, clinic, user, plan_id, note, next_in_days):
    cid = clinic["id"]
    plan = get(db, cid, plan_id)
    if not plan:
        raise LookupError("plan")
    if plan["status"] != "presented":
        raise PlanError("Only plans that are waiting for a decision need following up.")
    note = (note or "").strip()
    if not note:
        raise PlanError("Write what happened, for example: rang, no answer.")
    if not isinstance(next_in_days, int) or not 1 <= next_in_days <= 180:
        raise PlanError("Pick when to follow up next, from 1 to 180 days.")
    nxt = (_today(clinic) + timedelta(days=next_in_days)).isoformat()
    db.execute("UPDATE treatment_plans SET follow_up_on = ? WHERE id = ? AND clinic_id = ?", (nxt, plan_id, cid))
    _event(db, cid, plan_id, user["id"], "followed up", note)
    audit.log(db, cid, user["id"], "plan.followup", "plan", plan_id)


def _age_years(dob_iso, today):
    if not dob_iso:
        return None
    born = parse_date(dob_iso)
    return today.year - born.year - ((today.month, today.day) < (born.month, born.day))


def accept(db, clinic, user, plan_id, signer_name, relation, method, agreed, deposit_p=None, deposit_method=None,
           deposit_reference=None):
    """Patient says yes. Lines become planned work. A deposit, if any, is a payment held as credit on the account."""
    cid = clinic["id"]
    plan = _editable(db, cid, plan_id)
    if plan["status"] == "declined":
        raise PlanError("This plan was declined. Reopen it first.")
    rows = items(db, cid, plan_id)
    if not rows:
        raise PlanError("There is nothing on this plan yet.")
    signer_name = (signer_name or "").strip()
    if not signer_name or len(signer_name) > 120:
        raise PlanError("Enter the full name of the person who agreed.")
    if relation not in ("self", "parent", "guardian", "other") or method not in ("typed", "paper"):
        raise PlanError("Pick who agreed and how.")
    if not agreed:
        raise PlanError("Tick the box to confirm the patient has seen the cost and agreed.")
    patient = db.execute("SELECT * FROM patients WHERE id = ? AND clinic_id = ?", (plan["patient_id"], cid)).fetchone()
    age = _age_years(patient["dob"], _today(clinic))
    if relation == "self" and age is not None and age < 18:
        raise PlanError("This patient is under 18. A parent or guardian needs to agree.")
    if deposit_p is not None and deposit_p <= 0:
        raise PlanError("A deposit must be more than zero.")
    for r in rows:
        procedure_id = TR.add_planned(db, clinic, user["id"], plan["patient_id"], r["code_id"], tooth=r["tooth"],
                                      surfaces=r["surfaces"], fee_p=r["fee_p"],
                                      notes=f"From plan: {plan['title']}, phase {r['phase']}")
        db.execute("UPDATE plan_items SET procedure_id = ? WHERE id = ?", (procedure_id, r["id"]))
    deposit_id = None
    if deposit_p:
        try:
            deposit_id = billing.record_payment(db, clinic, user["id"], plan["patient_id"], deposit_p, deposit_method,
                                                deposit_reference, f"Deposit, {plan['title']}"[:100], plan_id=plan_id)
        except billing.BillingError as exc:
            raise PlanError(str(exc))
    db.execute("""UPDATE treatment_plans SET status = 'accepted', decided_on = ?, signer_name = ?, signer_relation = ?,
                  accept_method = ?, follow_up_on = NULL WHERE id = ? AND clinic_id = ?""",
               (_today(clinic).isoformat(), signer_name, relation, method, plan_id, cid))
    _event(db, cid, plan_id, user["id"], "accepted", f"Agreed by {signer_name}")
    audit.log(db, cid, user["id"], "plan.accept", "plan", plan_id)
    webhooks.emit(db, cid, "plan.accepted", serialize.plan(get(db, cid, plan_id), total(rows)))
    return deposit_id


def decline(db, clinic, user, plan_id, reason):
    cid = clinic["id"]
    plan = _editable(db, cid, plan_id)
    reason = (reason or "").strip()
    if len(reason) < 3:
        raise PlanError("Write why, for example: cost, wants to wait, going elsewhere.")
    db.execute("UPDATE treatment_plans SET status = 'declined', decided_on = ?, decline_reason = ?, follow_up_on = NULL "
               "WHERE id = ? AND clinic_id = ?", (_today(clinic).isoformat(), reason[:200], plan_id, cid))
    _event(db, cid, plan_id, user["id"], "declined", reason)
    audit.log(db, cid, user["id"], "plan.decline", "plan", plan_id)


def reopen(db, clinic, user, plan_id, follow_up_days=7):
    cid = clinic["id"]
    plan = get(db, cid, plan_id)
    if not plan:
        raise LookupError("plan")
    if plan["status"] != "declined":
        raise PlanError("Only a declined plan can be reopened.")
    db.execute("UPDATE treatment_plans SET status = 'presented', decided_on = NULL, decline_reason = NULL, follow_up_on = ?, owner_id = ? "
               "WHERE id = ? AND clinic_id = ?", ((_today(clinic) + timedelta(days=follow_up_days)).isoformat(), user["id"], plan_id, cid))
    _event(db, cid, plan_id, user["id"], "reopened")
    audit.log(db, cid, user["id"], "plan.reopen", "plan", plan_id)


def progress(rows):
    """For an accepted plan: how many lines are done, still planned, or dropped."""
    done = sum(1 for r in rows if r["work_status"] == "done")
    dropped = sum(1 for r in rows if r["work_status"] in ("cancelled", "voided"))
    return {"done": done, "dropped": dropped, "open": len(rows) - done - dropped, "lines": len(rows)}


def waiting(db, clinic, today=None):
    """Plans waiting for a decision, most overdue follow-up first, with value and days since presented."""
    cid = clinic["id"]
    today = today or _today(clinic)
    out = []
    for p in db.execute(
            """SELECT tp.*, pa.first_name, pa.last_name, pa.phone, pa.reminders_ok, u.display_name AS owner_name
               FROM treatment_plans tp JOIN patients pa ON pa.id = tp.patient_id
               LEFT JOIN users u ON u.id = tp.owner_id
               WHERE tp.clinic_id = ? AND tp.status = 'presented' AND pa.status = 'active'
               ORDER BY COALESCE(tp.follow_up_on, '9999'), tp.id""", (cid,)):
        value = total(items(db, cid, p["id"]))
        since = (today - parse_date(p["presented_on"])).days if p["presented_on"] else 0
        due = bool(p["follow_up_on"]) and p["follow_up_on"] <= today.isoformat()
        out.append({"plan": p, "value": value, "days": since, "due": due})
    return out
