"""In-house membership plans: a patient pays a fee each cycle and gets included services and a discount on the rest.
We record fees and track benefits. We do not take card payments: a person records each payment as it is received.

Money rules: a membership fee is income for the clinic but it does not pay for treatment, so it is kept out of the
patient's treatment balance (payments.purpose = 'membership'). Benefits are applied as adjustments when finished work
is marked done, so everything shows on the patient's statement and nothing is hidden."""
from datetime import date, timedelta

from . import audit, billing
from .timeutil import add_months, now_local, parse_date

CYCLES = {"monthly": ("Monthly", 1), "quarterly": ("Every 3 months", 3), "yearly": ("Yearly", 12)}
GRACE_DAYS = 14          # an unpaid fee this many days overdue makes the membership lapse. Paying it brings it back.
MAX_CATCH_UP = 24        # never create more than this many fees in one run


class MembershipError(Exception):
    pass


def _today(clinic):
    return now_local(clinic["timezone"]).date()


# ----------------------------------------------------------------------------- what the clinic sells
def list_plans(db, cid, active_only=False):
    sql = "SELECT * FROM membership_plans WHERE clinic_id = ?" + (" AND active = 1" if active_only else "") + " ORDER BY name"
    out = []
    for p in db.execute(sql, (cid,)):
        benefits = db.execute("""SELECT b.*, c.name AS code_name, c.code FROM membership_benefits b
                                 JOIN procedure_codes c ON c.id = b.code_id WHERE b.plan_id = ? AND b.clinic_id = ? ORDER BY c.name""",
                              (p["id"], cid)).fetchall()
        members = db.execute("SELECT COUNT(*) FROM memberships WHERE plan_id = ? AND clinic_id = ? AND status IN ('active','lapsed')",
                             (p["id"], cid)).fetchone()[0]
        out.append({"plan": p, "benefits": benefits, "members": members})
    return out


def save_plan(db, clinic, user_id, plan_id, name, description, fee_p, cycle, discount_pct, active=True):
    cid = clinic["id"]
    name = (name or "").strip()
    if not name or len(name) > 60:
        raise MembershipError("Give the plan a short name.")
    if cycle not in CYCLES:
        raise MembershipError("Pick how often the fee is paid.")
    if not isinstance(fee_p, int) or fee_p <= 0:
        raise MembershipError("The fee must be more than zero.")
    if not isinstance(discount_pct, int) or not 0 <= discount_pct <= 100:
        raise MembershipError("The discount must be a whole number from 0 to 100.")
    description = (description or "").strip()[:300] or None
    clash = db.execute("SELECT id FROM membership_plans WHERE clinic_id = ? AND name = ? COLLATE NOCASE AND id != ?",
                       (cid, name, plan_id or 0)).fetchone()
    if clash:
        raise MembershipError("There is already a plan with that name.")
    if plan_id:
        if not db.execute("SELECT 1 FROM membership_plans WHERE id = ? AND clinic_id = ?", (plan_id, cid)).fetchone():
            raise LookupError("plan")
        # Members keep the fee, cycle and discount they signed up for. Only new members get the changed terms.
        db.execute("UPDATE membership_plans SET name=?, description=?, fee_p=?, cycle=?, discount_pct=?, active=? WHERE id=? AND clinic_id=?",
                   (name, description, fee_p, cycle, discount_pct, 1 if active else 0, plan_id, cid))
        audit.log(db, cid, user_id, "membership.plan.edit", "membership_plan", plan_id)
        return plan_id
    cur = db.execute("INSERT INTO membership_plans(clinic_id, name, description, fee_p, cycle, discount_pct, active) VALUES (?,?,?,?,?,?,?)",
                     (cid, name, description, fee_p, cycle, discount_pct, 1 if active else 0))
    audit.log(db, cid, user_id, "membership.plan.add", "membership_plan", cur.lastrowid)
    return cur.lastrowid


def set_benefit(db, clinic, user_id, plan_id, code_id, qty):
    cid = clinic["id"]
    if not db.execute("SELECT 1 FROM membership_plans WHERE id = ? AND clinic_id = ?", (plan_id, cid)).fetchone():
        raise LookupError("plan")
    if not db.execute("SELECT 1 FROM procedure_codes WHERE id = ? AND clinic_id = ? AND active = 1", (code_id, cid)).fetchone():
        raise MembershipError("Pick a procedure from the list.")
    if not isinstance(qty, int) or not 0 <= qty <= 99:
        raise MembershipError("Use a number from 0 to 99. Zero removes it.")
    if qty == 0:
        db.execute("DELETE FROM membership_benefits WHERE plan_id = ? AND code_id = ? AND clinic_id = ?", (plan_id, code_id, cid))
    else:
        db.execute("""INSERT INTO membership_benefits(clinic_id, plan_id, code_id, qty_per_year) VALUES (?,?,?,?)
                      ON CONFLICT(plan_id, code_id) DO UPDATE SET qty_per_year = excluded.qty_per_year""", (cid, plan_id, code_id, qty))
    audit.log(db, cid, user_id, "membership.plan.benefit", "membership_plan", plan_id)


# ----------------------------------------------------------------------------- members
def get(db, cid, membership_id):
    return db.execute("SELECT * FROM memberships WHERE id = ? AND clinic_id = ?", (membership_id, cid)).fetchone()


def current_for(db, cid, pid):
    return db.execute("SELECT * FROM memberships WHERE clinic_id = ? AND patient_id = ? AND status IN ('active','lapsed')",
                      (cid, pid)).fetchone()


def history_for(db, cid, pid):
    return db.execute("SELECT * FROM memberships WHERE clinic_id = ? AND patient_id = ? ORDER BY id DESC", (cid, pid)).fetchall()


def enrol(db, clinic, user_id, pid, plan_id, start_on=None, pay_method=None, pay_reference=None):
    """Sign a patient up. The first fee falls due on the start date. If a payment method is given, it is taken now."""
    cid = clinic["id"]
    if not db.execute("SELECT 1 FROM patients WHERE id = ? AND clinic_id = ? AND status = 'active'", (pid, cid)).fetchone():
        raise MembershipError("That patient was not found, or is archived.")
    plan = db.execute("SELECT * FROM membership_plans WHERE id = ? AND clinic_id = ? AND active = 1", (plan_id, cid)).fetchone()
    if not plan:
        raise MembershipError("Pick one of the membership plans.")
    if current_for(db, cid, pid):
        raise MembershipError("This patient already has a membership. Cancel it first to change plan.")
    today = _today(clinic)
    start = start_on or today
    if start < today - timedelta(days=31) or start > today + timedelta(days=366):
        raise MembershipError("The start date should be within a month back and a year ahead.")
    months = CYCLES[plan["cycle"]][1]
    cur = db.execute(
        """INSERT INTO memberships(clinic_id, patient_id, plan_id, plan_name, fee_p, cycle, discount_pct, started_on, next_due_on, created_by)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (cid, pid, plan_id, plan["name"], plan["fee_p"], plan["cycle"], plan["discount_pct"], start.isoformat(),
         add_months(start, months).isoformat(), user_id))
    mid = cur.lastrowid
    charge_id = db.execute("INSERT INTO membership_charges(clinic_id, membership_id, due_on, amount_p) VALUES (?,?,?,?)",
                           (cid, mid, start.isoformat(), plan["fee_p"])).lastrowid
    audit.log(db, cid, user_id, "membership.enrol", "patient", pid, plan["name"][:60])
    if pay_method:
        pay_charge(db, clinic, user_id, charge_id, pay_method, pay_reference)
    return mid


def charges(db, cid, membership_id):
    return db.execute("SELECT * FROM membership_charges WHERE membership_id = ? AND clinic_id = ? ORDER BY due_on DESC, id DESC",
                      (membership_id, cid)).fetchall()


def _refresh_status(db, clinic, membership_id):
    """Active while fees are paid up. Lapsed when one has been unpaid past the grace period."""
    cid = clinic["id"]
    m = get(db, cid, membership_id)
    if not m or m["status"] == "cancelled":
        return
    limit = (_today(clinic) - timedelta(days=GRACE_DAYS)).isoformat()
    late = db.execute("SELECT 1 FROM membership_charges WHERE membership_id = ? AND clinic_id = ? AND status IN ('due','failed') AND due_on < ?",
                      (membership_id, cid, limit)).fetchone()
    status = "lapsed" if late else "active"
    if status != m["status"]:
        db.execute("UPDATE memberships SET status = ? WHERE id = ? AND clinic_id = ?", (status, membership_id, cid))


def run_renewals(db, clinic, user_id=None):
    """Create the fees that have fallen due and update who has lapsed. Safe to run as often as you like."""
    cid, today = clinic["id"], _today(clinic)
    created = 0
    for m in db.execute("SELECT * FROM memberships WHERE clinic_id = ? AND status IN ('active','lapsed')", (cid,)).fetchall():
        months = CYCLES[m["cycle"]][1]
        nxt, n = parse_date(m["next_due_on"]), 0
        while nxt <= today and n < MAX_CATCH_UP:
            db.execute("INSERT INTO membership_charges(clinic_id, membership_id, due_on, amount_p) VALUES (?,?,?,?)",
                       (cid, m["id"], nxt.isoformat(), m["fee_p"]))
            nxt, n = add_months(nxt, months), n + 1
            created += 1
        if n:
            db.execute("UPDATE memberships SET next_due_on = ? WHERE id = ?", (nxt.isoformat(), m["id"]))
        _refresh_status(db, clinic, m["id"])
    return created


def _open_charge(db, cid, charge_id):
    row = db.execute("""SELECT mc.*, m.patient_id, m.status AS membership_status FROM membership_charges mc
                        JOIN memberships m ON m.id = mc.membership_id WHERE mc.id = ? AND mc.clinic_id = ?""", (charge_id, cid)).fetchone()
    if not row:
        raise LookupError("charge")
    if row["status"] not in ("due", "failed"):
        raise MembershipError("That fee is already settled.")
    return row


def pay_charge(db, clinic, user_id, charge_id, method, reference=None):
    cid = clinic["id"]
    ch = _open_charge(db, cid, charge_id)
    try:
        pay_id = billing.record_payment(db, clinic, user_id, ch["patient_id"], ch["amount_p"], method, reference,
                                        "Membership fee", purpose="membership")
    except billing.BillingError as exc:
        raise MembershipError(str(exc))
    db.execute("UPDATE membership_charges SET status = 'paid', payment_id = ?, paid_on = ? WHERE id = ?",
               (pay_id, _today(clinic).isoformat(), charge_id))
    _refresh_status(db, clinic, ch["membership_id"])
    audit.log(db, cid, user_id, "membership.pay", "membership", ch["membership_id"])
    return pay_id


def mark_failed(db, clinic, user_id, charge_id, note):
    cid = clinic["id"]
    ch = _open_charge(db, cid, charge_id)
    note = (note or "").strip()
    if not note:
        raise MembershipError("Write what happened, for example: card declined, mobile money not received.")
    db.execute("UPDATE membership_charges SET status = 'failed', note = ? WHERE id = ?", (note[:200], charge_id))
    audit.log(db, cid, user_id, "membership.fail", "membership", ch["membership_id"])


def waive_charge(db, clinic, user_id, charge_id, note):
    cid = clinic["id"]
    ch = _open_charge(db, cid, charge_id)
    note = (note or "").strip()
    if len(note) < 3:
        raise MembershipError("Say why the fee is waived.")
    db.execute("UPDATE membership_charges SET status = 'waived', note = ? WHERE id = ?", (note[:200], charge_id))
    _refresh_status(db, clinic, ch["membership_id"])
    audit.log(db, cid, user_id, "membership.waive", "membership", ch["membership_id"])


def cancel(db, clinic, user_id, membership_id, reason):
    cid = clinic["id"]
    m = get(db, cid, membership_id)
    if not m:
        raise LookupError("membership")
    if m["status"] == "cancelled":
        raise MembershipError("That membership is already cancelled.")
    reason = (reason or "").strip()
    if len(reason) < 3:
        raise MembershipError("Write why it is being cancelled.")
    db.execute("UPDATE memberships SET status = 'cancelled', cancelled_on = ?, cancel_reason = ? WHERE id = ? AND clinic_id = ?",
               (_today(clinic).isoformat(), reason[:200], membership_id, cid))
    # Fees that were never paid are not chased after cancelling. Fees already paid stay as they are.
    db.execute("UPDATE membership_charges SET status = 'waived', note = 'Membership cancelled' "
               "WHERE membership_id = ? AND clinic_id = ? AND status IN ('due','failed')", (membership_id, cid))
    audit.log(db, cid, user_id, "membership.cancel", "patient", m["patient_id"], reason[:60])


# ----------------------------------------------------------------------------- benefits
def benefit_year(started_on, today):
    """The membership year that today falls in: (first day, last day)."""
    start = parse_date(started_on)
    years = today.year - start.year - ((today.month, today.day) < (start.month, start.day))
    first = add_months(start, 12 * max(years, 0))
    return first, add_months(first, 12) - timedelta(days=1)


def benefit_status(db, clinic, membership):
    """Each included service with how many are left this membership year."""
    cid, today = clinic["id"], _today(clinic)
    first, last = benefit_year(membership["started_on"], today)
    out = []
    for b in db.execute("""SELECT b.*, c.name AS code_name, c.code FROM membership_benefits b JOIN procedure_codes c ON c.id = b.code_id
                           WHERE b.plan_id = ? AND b.clinic_id = ? ORDER BY c.name""", (membership["plan_id"], cid)):
        used = db.execute("""SELECT COUNT(*) FROM membership_uses u JOIN procedures pr ON pr.id = u.procedure_id
                             WHERE u.membership_id = ? AND u.clinic_id = ? AND u.kind = 'included' AND u.reversed = 0
                             AND pr.code_id = ? AND u.used_on BETWEEN ? AND ?""",
                          (membership["id"], cid, b["code_id"], first.isoformat(), last.isoformat())).fetchone()[0]
        out.append({"benefit": b, "used": used, "left": max(b["qty_per_year"] - used, 0)})
    return {"year_start": first, "year_end": last, "items": out}


def uses(db, cid, membership_id):
    return db.execute("""SELECT u.*, c.name AS code_name FROM membership_uses u JOIN procedures pr ON pr.id = u.procedure_id
                         JOIN procedure_codes c ON c.id = pr.code_id WHERE u.membership_id = ? AND u.clinic_id = ? ORDER BY u.id DESC""",
                      (membership_id, cid)).fetchall()


def apply_benefits(db, clinic, user_id, procedure_id):
    """Call after work is marked done. Gives an included service for free if one is left, otherwise the member discount.
    Returns a short description of what was applied, or None."""
    cid = clinic["id"]
    proc = db.execute("""SELECT pr.*, c.name AS code_name FROM procedures pr JOIN procedure_codes c ON c.id = pr.code_id
                         WHERE pr.id = ? AND pr.clinic_id = ? AND pr.status = 'done'""", (procedure_id, cid)).fetchone()
    if not proc or proc["fee_p"] <= 0:
        return None
    m = current_for(db, cid, proc["patient_id"])
    if not m or m["status"] != "active":
        return None
    if db.execute("SELECT 1 FROM membership_uses WHERE procedure_id = ?", (procedure_id,)).fetchone():
        return None
    status = benefit_status(db, clinic, m)
    included = next((i for i in status["items"] if i["benefit"]["code_id"] == proc["code_id"] and i["left"] > 0), None)
    if included:
        amount, kind, note = proc["fee_p"], "included", f"Membership: included {proc['code_name']}"
    elif m["discount_pct"] > 0:
        amount, kind, note = proc["fee_p"] * m["discount_pct"] // 100, "discount", f"Membership discount {m['discount_pct']}%: {proc['code_name']}"
    else:
        return None
    if amount <= 0:
        return None
    adj = billing.add_adjustment(db, clinic, user_id, proc["patient_id"], -amount, "discount", note[:200])
    db.execute("INSERT INTO membership_uses(clinic_id, membership_id, procedure_id, kind, amount_p, adjustment_id, used_on) VALUES (?,?,?,?,?,?,?)",
               (cid, m["id"], procedure_id, kind, amount, adj, proc["done_on"] or _today(clinic).isoformat()))
    audit.log(db, cid, user_id, "membership.benefit", "procedure", procedure_id, kind)
    return "included in the membership" if kind == "included" else f"{m['discount_pct']}% member discount"


def reverse_for_procedure(db, clinic, user_id, procedure_id):
    """Call when finished work is voided, so a benefit is given back and the discount is taken off the account."""
    cid = clinic["id"]
    u = db.execute("SELECT * FROM membership_uses WHERE procedure_id = ? AND clinic_id = ? AND reversed = 0", (procedure_id, cid)).fetchone()
    if not u:
        return False
    pat = db.execute("SELECT patient_id FROM procedures WHERE id = ? AND clinic_id = ?", (procedure_id, cid)).fetchone()["patient_id"]
    billing.add_adjustment(db, clinic, user_id, pat, u["amount_p"], "correction", "Membership benefit reversed because the treatment was voided")
    db.execute("UPDATE membership_uses SET reversed = 1 WHERE id = ?", (u["id"],))
    audit.log(db, cid, user_id, "membership.benefit.reverse", "procedure", procedure_id)
    return True


# ----------------------------------------------------------------------------- lists
def attention(db, clinic):
    """Fees due or failed, oldest first, with how many days late."""
    cid, today = clinic["id"], _today(clinic)
    out = []
    for r in db.execute("""SELECT mc.*, m.patient_id, m.plan_name, m.status AS membership_status, pa.first_name, pa.last_name, pa.phone
                           FROM membership_charges mc JOIN memberships m ON m.id = mc.membership_id
                           JOIN patients pa ON pa.id = m.patient_id
                           WHERE mc.clinic_id = ? AND mc.status IN ('due','failed') AND mc.due_on <= ? ORDER BY mc.due_on, mc.id""",
                        (cid, today.isoformat())):
        out.append({"row": r, "days_late": (today - parse_date(r["due_on"])).days})
    return out


def members(db, cid):
    return db.execute("""SELECT m.*, pa.first_name, pa.last_name FROM memberships m JOIN patients pa ON pa.id = m.patient_id
                         WHERE m.clinic_id = ? AND m.status IN ('active','lapsed') ORDER BY pa.last_name, pa.first_name""", (cid,)).fetchall()


def monthly_value(rows):
    """Recurring income per month from active members, in minor units, for a quick headline number."""
    total = 0
    for m in rows:
        if m["status"] == "active":
            total += m["fee_p"] // CYCLES[m["cycle"]][1]
    return total
