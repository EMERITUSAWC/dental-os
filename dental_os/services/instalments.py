"""Instalment plans: a balance agreed to be paid in equal parts on a schedule. A payment is recorded as an ordinary
payment on the patient's account (so receipts and the balance work as before) and ticks off the instalment."""
from datetime import timedelta

from . import audit, billing
from .timeutil import add_months, now_local, parse_date

FREQUENCIES = {"weekly": "Every week", "fortnightly": "Every 2 weeks", "monthly": "Every month"}


class InstalmentError(Exception):
    pass


def _today(clinic):
    return now_local(clinic["timezone"]).date()


def schedule(total_p, count, frequency, start_on):
    """[(due date, amount)]. Any leftover minor unit goes on the first instalments so the parts add up exactly."""
    base, extra = divmod(total_p, count)
    out = []
    for i in range(count):
        if frequency == "weekly":
            due = start_on + timedelta(days=7 * i)
        elif frequency == "fortnightly":
            due = start_on + timedelta(days=14 * i)
        else:
            due = add_months(start_on, i)
        out.append((due, base + (1 if i < extra else 0)))
    return out


def create(db, clinic, user_id, pid, total_p, count, frequency, start_on, treatment_plan_id=None, note=None):
    cid = clinic["id"]
    if not db.execute("SELECT 1 FROM patients WHERE id = ? AND clinic_id = ? AND status = 'active'", (pid, cid)).fetchone():
        raise InstalmentError("That patient was not found, or is archived.")
    if not isinstance(total_p, int) or total_p <= 0:
        raise InstalmentError("The amount must be more than zero.")
    if not isinstance(count, int) or not 2 <= count <= 36:
        raise InstalmentError("Use from 2 to 36 instalments.")
    if frequency not in FREQUENCIES:
        raise InstalmentError("Pick how often they pay.")
    if total_p < count:
        raise InstalmentError("That is too small to split that many ways.")
    today = _today(clinic)
    if start_on < today - timedelta(days=7) or start_on > today + timedelta(days=366):
        raise InstalmentError("The first payment date should be within a week back and a year ahead.")
    if treatment_plan_id and not db.execute("SELECT 1 FROM treatment_plans WHERE id = ? AND clinic_id = ? AND patient_id = ?",
                                            (treatment_plan_id, cid, pid)).fetchone():
        raise InstalmentError("That treatment plan does not belong to this patient.")
    cur = db.execute("""INSERT INTO payment_plans(clinic_id, patient_id, treatment_plan_id, total_p, instalment_count, frequency,
                        start_on, note, created_by) VALUES (?,?,?,?,?,?,?,?,?)""",
                     (cid, pid, treatment_plan_id or None, total_p, count, frequency, start_on.isoformat(),
                      (note or "").strip()[:200] or None, user_id))
    for due, amount in schedule(total_p, count, frequency, start_on):
        db.execute("INSERT INTO instalments(clinic_id, payment_plan_id, due_on, amount_p) VALUES (?,?,?,?)",
                   (cid, cur.lastrowid, due.isoformat(), amount))
    audit.log(db, cid, user_id, "instalments.create", "patient", pid)
    return cur.lastrowid


def get(db, cid, plan_id):
    return db.execute("SELECT * FROM payment_plans WHERE id = ? AND clinic_id = ?", (plan_id, cid)).fetchone()


def lines(db, cid, plan_id):
    return db.execute("SELECT * FROM instalments WHERE payment_plan_id = ? AND clinic_id = ? ORDER BY due_on, id", (plan_id, cid)).fetchall()


def for_patient(db, cid, pid):
    out = []
    for p in db.execute("SELECT * FROM payment_plans WHERE clinic_id = ? AND patient_id = ? ORDER BY id DESC", (cid, pid)):
        rows = lines(db, cid, p["id"])
        out.append({"plan": p, "lines": rows, "paid": sum(r["amount_p"] for r in rows if r["status"] == "paid"),
                    "left": sum(r["amount_p"] for r in rows if r["status"] == "due")})
    return out


def _settle_if_complete(db, cid, plan_id):
    if not db.execute("SELECT 1 FROM instalments WHERE payment_plan_id = ? AND clinic_id = ? AND status = 'due'", (plan_id, cid)).fetchone():
        db.execute("UPDATE payment_plans SET status = 'completed' WHERE id = ? AND clinic_id = ? AND status = 'active'", (plan_id, cid))


def pay(db, clinic, user_id, instalment_id, method, reference=None):
    cid = clinic["id"]
    row = db.execute("""SELECT i.*, p.patient_id, p.status AS plan_status FROM instalments i JOIN payment_plans p ON p.id = i.payment_plan_id
                        WHERE i.id = ? AND i.clinic_id = ?""", (instalment_id, cid)).fetchone()
    if not row:
        raise LookupError("instalment")
    if row["status"] != "due" or row["plan_status"] != "active":
        raise InstalmentError("That instalment is already settled.")
    try:
        pay_id = billing.record_payment(db, clinic, user_id, row["patient_id"], row["amount_p"], method, reference, "Instalment")
    except billing.BillingError as exc:
        raise InstalmentError(str(exc))
    db.execute("UPDATE instalments SET status = 'paid', payment_id = ?, paid_on = ? WHERE id = ?",
               (pay_id, _today(clinic).isoformat(), instalment_id))
    _settle_if_complete(db, cid, row["payment_plan_id"])
    audit.log(db, cid, user_id, "instalments.pay", "patient", row["patient_id"])
    return pay_id


def cancel(db, clinic, user_id, plan_id, reason):
    cid = clinic["id"]
    plan = get(db, cid, plan_id)
    if not plan:
        raise LookupError("plan")
    if plan["status"] != "active":
        raise InstalmentError("That plan is already finished.")
    reason = (reason or "").strip()
    if len(reason) < 3:
        raise InstalmentError("Write why it is being cancelled.")
    db.execute("UPDATE payment_plans SET status = 'cancelled', note = COALESCE(note || ' | ', '') || ? WHERE id = ? AND clinic_id = ?",
               ("Cancelled: " + reason[:150], plan_id, cid))
    db.execute("UPDATE instalments SET status = 'waived' WHERE payment_plan_id = ? AND clinic_id = ? AND status = 'due'", (plan_id, cid))
    audit.log(db, cid, user_id, "instalments.cancel", "patient", plan["patient_id"])


def overdue(db, clinic):
    cid, today = clinic["id"], _today(clinic)
    out = []
    for r in db.execute("""SELECT i.*, p.patient_id, pa.first_name, pa.last_name, pa.phone FROM instalments i
                           JOIN payment_plans p ON p.id = i.payment_plan_id JOIN patients pa ON pa.id = p.patient_id
                           WHERE i.clinic_id = ? AND i.status = 'due' AND p.status = 'active' AND i.due_on < ?
                           ORDER BY i.due_on, i.id""", (cid, today.isoformat())):
        out.append({"row": r, "days_late": (today - parse_date(r["due_on"])).days})
    return out
