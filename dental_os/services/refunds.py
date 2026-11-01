"""Refunds (version 0.42).

Money goes back to a patient only out of credit they hold: when they have paid more than their treatment costs (a deposit for work
that was cancelled, a double payment, an insurer paying after the patient did). A refund can never take a balance into debt, so it
never undoes a payment that has paid for work; to correct a wrong payment, void it as before.

Each refund has a number, the way it was paid back (cash, mobile money, card, bank transfer), a reference and a reason. It shows on
the account and the statement, reduces the day's takings for that way of paying (so the cash-up expects less in the till), and appears
in the accountant's export. A wrong refund is voided with a reason, never deleted. Managers only. No web framework here."""
import datetime

from . import audit

METHODS = ("cash", "mobile_money", "card", "bank_transfer")
REASONS = {"overpaid": "Paid more than the treatment cost", "cancelled": "Treatment cancelled after a deposit", "insurer": "The insurer paid after the patient",
           "goodwill": "Goodwill, after a complaint", "other": "Other"}


class RefundError(ValueError):
    pass


def total(db, cid, pid):
    return db.execute("SELECT COALESCE(SUM(amount_p), 0) FROM refunds WHERE clinic_id = ? AND patient_id = ? AND voided = 0", (cid, pid)).fetchone()[0]


def by_method(db, cid, start, end):
    """{method: minor units refunded} between two dates (both included)."""
    return {r["method"]: r["t"] for r in db.execute("""SELECT method, SUM(amount_p) AS t FROM refunds WHERE clinic_id = ? AND voided = 0
                                                       AND refunded_on BETWEEN ? AND ? GROUP BY method""", (cid, str(start), str(end)))}


NOT_PAID_IN = "A refund can't be more than the patient actually paid in. Part of this credit comes from a discount or a write-off, which is not money the clinic received."


def refund(db, clinic, user_id, pid, amount_p, method, reason, reference=None, note=None, refunded_on=None, today=None):
    from . import billing, periodlock
    cid = clinic["id"]
    if not db.execute("SELECT 1 FROM patients WHERE id = ? AND clinic_id = ?", (pid, cid)).fetchone():
        raise RefundError("That patient was not found.")
    if method not in METHODS:
        raise RefundError("Pick how the money is paid back.")
    if reason not in REASONS:
        raise RefundError("Pick why the money is paid back.")
    if not isinstance(amount_p, int) or amount_p <= 0:
        raise RefundError("The amount must be more than zero.")
    credit = -billing.totals(db, cid, pid)["balance"]
    if credit <= 0:
        raise RefundError("This patient holds no credit, so there is nothing to refund.")
    if amount_p > credit:
        raise RefundError("A refund can't be more than the credit the patient holds.")
    # 0.68: credit made by a discount or write-off is not money the clinic received. Only money paid in can go back out as money.
    t = billing.totals(db, cid, pid)
    if amount_p > t["paid"] - t["refunded"]:
        raise RefundError(NOT_PAID_IN)
    day = (refunded_on or "").strip() or (today or datetime.date.today()).isoformat()
    try:
        day = datetime.date.fromisoformat(day).isoformat()
    except ValueError:
        raise RefundError("Use a real date for the refund.")
    periodlock.check(db, clinic, day)
    no = db.execute("SELECT COALESCE(MAX(refund_no), 0) + 1 FROM refunds WHERE clinic_id = ?", (cid,)).fetchone()[0]
    rid = db.execute("""INSERT INTO refunds(clinic_id, patient_id, refund_no, amount_p, method, reason, reference, note, refunded_on, created_by)
                        VALUES (?,?,?,?,?,?,?,?,?,?)""",
                     (cid, pid, no, amount_p, method, reason, (reference or "").strip()[:120] or None, (note or "").strip()[:500] or None, day, user_id)).lastrowid
    audit.log(db, cid, user_id, "refund.add", "patient", pid, f"refund {no}")
    return rid


def void(db, clinic, user_id, rid, why):
    from . import periodlock
    r = db.execute("SELECT * FROM refunds WHERE id = ? AND clinic_id = ?", (rid, clinic["id"])).fetchone()
    if not r or r["voided"]:
        raise RefundError("That refund was not found, or is already voided.")
    why = (why or "").strip()[:300]
    if not why:
        raise RefundError("Say why the refund is voided.")
    periodlock.check(db, clinic, r["refunded_on"])
    db.execute("UPDATE refunds SET voided = 1, void_reason = ?, voided_by = ? WHERE id = ?", (why, user_id, rid))
    audit.log(db, clinic["id"], user_id, "refund.void", "patient", r["patient_id"], why)


def for_patient(db, cid, pid):
    return db.execute("SELECT * FROM refunds WHERE clinic_id = ? AND patient_id = ? ORDER BY refunded_on, id", (cid, pid)).fetchall()


def on_day(db, cid, day):
    return db.execute("""SELECT r.*, p.first_name, p.last_name FROM refunds r JOIN patients p ON p.id = r.patient_id
                         WHERE r.clinic_id = ? AND r.refunded_on = ? ORDER BY r.refund_no""", (cid, day)).fetchall()


def credits(db, cid):
    """Patients holding credit (paid more than they owe), most first."""
    from . import billing
    held = {pid: -bal for pid, (bal, _u) in billing.all_balances(db, cid).items() if bal < 0}     # 0.57: one pass, not one per patient
    out = []
    ids = sorted(held)
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        for p in db.execute(f"SELECT id, first_name, last_name, phone, status FROM patients WHERE clinic_id = ? AND id IN ({','.join('?' * len(chunk))})",
                            (cid, *chunk)):
            out.append({"p": p, "credit": held[p["id"]]})
    return sorted(out, key=lambda x: (-x["credit"], x["p"]["id"]))
