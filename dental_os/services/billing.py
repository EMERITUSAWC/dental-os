"""Charges, payments, adjustments and receipts. Balance = work done + adjustments - payments.
A positive balance means the patient owes. Nothing is deleted: payments are voided with a reason."""
from . import audit, serialize, teeth, webhooks
from .timeutil import now_local

METHODS = {"cash": "Cash", "mobile_money": "Mobile money", "card": "Card", "bank_transfer": "Bank transfer",
           "insurance": "Insurance"}
NEEDS_REFERENCE = {"mobile_money", "bank_transfer", "card", "insurance"}
ADJUSTMENT_REASONS = {"birthday_offer": "Birthday offer", "seasonal_offer": "Seasonal offer",
                      "discount": "Other discount", "write_off": "Write-off", "correction": "Correction"}
NEEDS_NOTE = {"discount", "write_off", "correction"}


class BillingError(Exception):
    pass


def enabled_methods(clinic):
    """The ways of paying this clinic has switched on, in a fixed order."""
    on = {m.strip() for m in (clinic["payment_methods"] or "").split(",")}
    return {k: v for k, v in METHODS.items() if k in on}


def methods_in_use(db, clinic):
    """Enabled methods plus any that already have payments, so old payments never vanish from reports."""
    shown = dict(enabled_methods(clinic))
    for r in db.execute("SELECT DISTINCT method FROM payments WHERE clinic_id = ?", (clinic["id"],)):
        shown[r["method"]] = METHODS[r["method"]]
    return {k: v for k, v in METHODS.items() if k in shown}


def totals(db, clinic_id, patient_id):
    charges = db.execute("SELECT COALESCE(SUM(fee_p),0) FROM procedures WHERE clinic_id=? AND patient_id=? AND status='done'",
                         (clinic_id, patient_id)).fetchone()[0]
    adjust = db.execute("SELECT COALESCE(SUM(amount_p),0) FROM adjustments WHERE clinic_id=? AND patient_id=?",
                        (clinic_id, patient_id)).fetchone()[0]
    paid = db.execute("SELECT COALESCE(SUM(amount_p),0) FROM payments WHERE clinic_id=? AND patient_id=? AND voided=0 "
                      "AND purpose='treatment'",
                      (clinic_id, patient_id)).fetchone()[0]
    from . import refunds
    back = refunds.total(db, clinic_id, patient_id)
    return {"charges": charges, "adjustments": adjust, "paid": paid, "refunded": back, "balance": charges + adjust - paid + back}


def family_balance(db, clinic_id, patient_id):
    """Total for the family head and everyone who lists them as family head. None if there is no family."""
    row = db.execute("SELECT guarantor_id FROM patients WHERE id=? AND clinic_id=?", (patient_id, clinic_id)).fetchone()
    if not row:
        return None
    root = row["guarantor_id"] or patient_id
    members = [r["id"] for r in db.execute(
        "SELECT id FROM patients WHERE clinic_id=? AND (id=? OR guarantor_id=?) AND status='active'",
        (clinic_id, root, root))]
    if len(members) < 2:
        return None
    total = sum(totals(db, clinic_id, m)["balance"] for m in members)
    return {"members": len(members), "balance": total}


def _begin(db):
    if not db.in_transaction:
        db.execute("BEGIN IMMEDIATE")


def record_payment(db, clinic, user_id, patient_id, amount_p, method, reference=None, note=None, purpose="treatment", plan_id=None, claim_id=None,
                   online=False):
    """online: a payment confirmed by the clinic's payment company, which counts whatever ways of paying the desk has switched on."""
    cid = clinic["id"]
    if method not in enabled_methods(clinic) and not (online and method in ("card", "mobile_money")):
        raise BillingError("Pick how the patient paid.")
    if amount_p <= 0:
        raise BillingError("The amount must be more than zero.")
    reference = (reference or "").strip() or None
    if method in NEEDS_REFERENCE and not reference:
        raise BillingError("Enter the transaction or reference number for this kind of payment.")
    if purpose not in ("treatment", "membership"):
        raise BillingError("Unknown kind of payment.")
    _begin(db)
    if not db.execute("SELECT 1 FROM patients WHERE id=? AND clinic_id=?", (patient_id, cid)).fetchone():
        raise BillingError("That patient was not found.")
    receipt_no = db.execute("SELECT COALESCE(MAX(receipt_no),0)+1 FROM payments WHERE clinic_id=?", (cid,)).fetchone()[0]
    cur = db.execute(
        """INSERT INTO payments(clinic_id, patient_id, amount_p, method, reference, note, receipt_no, received_on, received_by,
                                purpose, plan_id, claim_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (cid, patient_id, amount_p, method, reference, (note or "").strip() or None, receipt_no,
         now_local(clinic["timezone"]).date().isoformat(), user_id, purpose, plan_id, claim_id))
    audit.log(db, cid, user_id, "payment.record", "payment", cur.lastrowid, method)
    webhooks.emit(db, cid, "payment.received", serialize.payment(db.execute("SELECT * FROM payments WHERE id = ?", (cur.lastrowid,)).fetchone()))
    return cur.lastrowid


def void_payment(db, clinic_id, user_id, payment_id, reason):
    pay = db.execute("SELECT * FROM payments WHERE id=? AND clinic_id=?", (payment_id, clinic_id)).fetchone()
    if not pay:
        raise LookupError("payment")
    if pay["voided"]:
        raise BillingError("That payment is already voided.")
    from . import periodlock
    periodlock.check(db, clinic_id, pay["received_on"], BillingError)
    reason = (reason or "").strip()
    if len(reason) < 3:
        raise BillingError("Say why the payment is being voided.")
    db.execute("UPDATE payments SET voided=1, void_reason=?, voided_by=? WHERE id=? AND clinic_id=?",
               (reason, user_id, payment_id, clinic_id))
    audit.log(db, clinic_id, user_id, "payment.void", "payment", payment_id, reason[:80])


def add_adjustment(db, clinic, user_id, patient_id, amount_p, reason, note=None):
    cid = clinic["id"]
    if reason not in ADJUSTMENT_REASONS:
        raise BillingError("Pick a reason.")
    if amount_p == 0:
        raise BillingError("The amount cannot be zero.")
    note = (note or "").strip() or None
    if reason in NEEDS_NOTE and not note:
        raise BillingError("Add a short note saying why.")
    if not db.execute("SELECT 1 FROM patients WHERE id=? AND clinic_id=?", (patient_id, cid)).fetchone():
        raise BillingError("That patient was not found.")
    cur = db.execute(
        "INSERT INTO adjustments(clinic_id, patient_id, amount_p, reason, note, adjusted_on, created_by) VALUES (?,?,?,?,?,?,?)",
        (cid, patient_id, amount_p, reason, note, now_local(clinic["timezone"]).date().isoformat(), user_id))
    audit.log(db, cid, user_id, "adjustment.add", "adjustment", cur.lastrowid, reason)
    return cur.lastrowid


def statement(db, clinic_id, patient_id, lang="en"):
    """Every money event for one patient, oldest first, with a running balance. The wording is in the given language."""
    from .i18n import t
    events = []
    for r in db.execute("""SELECT pr.id, pr.done_on AS day, pr.fee_p, pr.tooth, c.name FROM procedures pr
                           JOIN procedure_codes c ON c.id = pr.code_id
                           WHERE pr.clinic_id=? AND pr.patient_id=? AND pr.status='done'""", (clinic_id, patient_id)):
        label = r["name"] + (f", {t(lang, 'tooth')} {teeth.show(r['tooth'])}" if r["tooth"] else "")
        events.append({"day": r["day"], "order": 0, "id": r["id"], "kind": "charge", "label": label,
                       "delta": r["fee_p"], "counts": True})
    for r in db.execute("SELECT * FROM adjustments WHERE clinic_id=? AND patient_id=?", (clinic_id, patient_id)):
        label = t(lang, ADJUSTMENT_REASONS[r["reason"]]) + (f": {r['note']}" if r["note"] else "")
        events.append({"day": r["adjusted_on"], "order": 1, "id": r["id"], "kind": "adjustment", "label": label,
                       "delta": r["amount_p"], "counts": True})
    for r in db.execute("SELECT * FROM payments WHERE clinic_id=? AND patient_id=?", (clinic_id, patient_id)):
        what = t(lang, "Membership fee") if r["purpose"] == "membership" else t(lang, "Payment")
        label = f"{what}, {t(lang, METHODS[r['method']])}" + (f" ({r['reference']})" if r["reference"] else "")
        # A membership fee is income for the clinic but does not pay for treatment, so it is listed without moving the balance.
        events.append({"day": r["received_on"], "order": 2, "id": r["id"], "kind": "payment", "label": label,
                       "delta": -r["amount_p"], "counts": not r["voided"] and r["purpose"] == "treatment",
                       "voided": bool(r["voided"]), "receipt_no": r["receipt_no"]})
    for r in db.execute("SELECT * FROM refunds WHERE clinic_id=? AND patient_id=?", (clinic_id, patient_id)):
        label = f"{t(lang, 'Refund')} {r['refund_no']}, {t(lang, METHODS[r['method']])}" + (f" ({r['reference']})" if r["reference"] else "")
        events.append({"day": r["refunded_on"], "order": 3, "id": r["id"], "kind": "refund", "label": label,
                       "delta": r["amount_p"], "counts": not r["voided"], "voided": bool(r["voided"])})
    events.sort(key=lambda e: (e["day"], e["order"], e["id"]))
    running = 0
    for e in events:
        if e["counts"]:
            running += e["delta"]
        e["balance"] = running
    return events


def get_receipt(db, clinic_id, payment_id):
    return db.execute(
        """SELECT pay.*, p.first_name, p.last_name, p.phone, u.display_name AS received_by_name
           FROM payments pay JOIN patients p ON p.id = pay.patient_id
           LEFT JOIN users u ON u.id = pay.received_by
           WHERE pay.id=? AND pay.clinic_id=?""", (payment_id, clinic_id)).fetchone()


def daily_takings(db, clinic_id, day):
    rows = db.execute(
        """SELECT pay.*, p.first_name, p.last_name FROM payments pay JOIN patients p ON p.id = pay.patient_id
           WHERE pay.clinic_id=? AND pay.received_on=? ORDER BY pay.receipt_no""", (clinic_id, day)).fetchall()
    by_method = {m: 0 for m in METHODS}
    for r in rows:
        if not r["voided"]:
            by_method[r["method"]] += r["amount_p"]
    from . import refunds
    back = refunds.by_method(db, clinic_id, day, day)
    for m, v in back.items():
        by_method[m] -= v
    return {"rows": rows, "by_method": by_method, "total": sum(by_method.values()), "refunds": refunds.on_day(db, clinic_id, day),
            "refunded": sum(back.values())}


def all_balances(db, clinic_id):
    """{patient id: (balance, unpaid)} for every patient with any money on the account, in a handful of queries.

    balance is what totals() gives. unpaid is what the ageing report would split into buckets: positive charges less
    the credit held, never below zero. Patients with nothing on the account are left out. (0.57: the lists below used to
    work out every patient one at a time, which took minutes at a clinic with 20,000 patients.)"""
    acc = {}

    def add(pid, key, value):
        acc.setdefault(pid, {"charges": 0, "pos": 0, "credit": 0, "adjust": 0, "paid": 0, "back": 0})[key] += value or 0

    for r in db.execute("""SELECT patient_id, SUM(fee_p), SUM(CASE WHEN fee_p > 0 THEN fee_p ELSE 0 END) FROM procedures
                           WHERE clinic_id = ? AND status = 'done' GROUP BY patient_id""", (clinic_id,)):
        add(r[0], "charges", r[1])
        add(r[0], "pos", r[2])
    for r in db.execute("""SELECT patient_id, SUM(amount_p), SUM(CASE WHEN amount_p > 0 THEN amount_p ELSE 0 END),
                                  SUM(CASE WHEN amount_p < 0 THEN -amount_p ELSE 0 END) FROM adjustments WHERE clinic_id = ? GROUP BY patient_id""",
                        (clinic_id,)):
        add(r[0], "adjust", r[1])
        add(r[0], "pos", r[2])
        add(r[0], "credit", r[3])
    for r in db.execute("""SELECT patient_id, SUM(amount_p) FROM payments WHERE clinic_id = ? AND voided = 0 AND purpose = 'treatment'
                           GROUP BY patient_id""", (clinic_id,)):
        add(r[0], "paid", r[1])
        add(r[0], "credit", r[1])
    for r in db.execute("SELECT patient_id, SUM(amount_p) FROM refunds WHERE clinic_id = ? AND voided = 0 GROUP BY patient_id", (clinic_id,)):
        add(r[0], "back", r[1])
        add(r[0], "credit", -(r[1] or 0))
    out = {}
    for pid, a in acc.items():
        balance = a["charges"] + a["adjust"] - a["paid"] + a["back"]
        unpaid = max(a["pos"] - a["credit"], 0) if a["pos"] > 0 else 0
        if balance or unpaid:
            out[pid] = (balance, unpaid)
    return out


def outstanding(db, clinic_id, limit=50):
    """Patients who owe money, biggest first."""
    owed = {pid: bal for pid, (bal, _u) in all_balances(db, clinic_id).items() if bal > 0}
    if not owed:
        return []
    top = sorted(owed, key=lambda pid: (-owed[pid], pid))
    out = []
    for i in range(0, len(top), 500):
        chunk = top[i:i + 500]
        rows = {r["id"]: r for r in db.execute(
            f"SELECT id, first_name, last_name FROM patients WHERE clinic_id = ? AND status = 'active' AND id IN ({','.join('?' * len(chunk))})",
            (clinic_id, *chunk))}
        out += [(owed[pid], rows[pid]) for pid in chunk if pid in rows]
        if len(out) >= limit:
            break
    return out[:limit]
