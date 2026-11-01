"""Who owes what, and for how long. Payments and discounts are applied to the oldest charges first, so what is left
is what is really old. 'Insurers owe' is the part of a balance that sent claims are expected to cover."""
from datetime import date

from .timeutil import now_local

BUCKETS = (("0-30", 0, 30), ("31-60", 31, 60), ("61-90", 61, 90), ("90+", 91, 10**6))


def _bucket(days):
    for name, lo, hi in BUCKETS:
        if lo <= days <= hi:
            return name
    return BUCKETS[-1][0]


def patient_ageing(db, cid, pid, today):
    """{'0-30': minor units, ...} for what is still unpaid, oldest charges paid off first."""
    charges = [(r["done_on"], r["fee_p"]) for r in db.execute(
        "SELECT done_on, fee_p FROM procedures WHERE clinic_id = ? AND patient_id = ? AND status = 'done' AND fee_p > 0", (cid, pid))]
    credit = 0
    for r in db.execute("SELECT adjusted_on, amount_p FROM adjustments WHERE clinic_id = ? AND patient_id = ?", (cid, pid)):
        if r["amount_p"] > 0:
            charges.append((r["adjusted_on"], r["amount_p"]))
        else:
            credit += -r["amount_p"]
    credit += db.execute("SELECT COALESCE(SUM(amount_p),0) FROM payments WHERE clinic_id = ? AND patient_id = ? AND voided = 0 "
                         "AND purpose = 'treatment'", (cid, pid)).fetchone()[0]
    out = {name: 0 for name, _, _ in BUCKETS}
    for day, amount in sorted(charges):
        used = min(credit, amount)
        credit -= used
        left = amount - used
        if left:
            out[_bucket((today - date.fromisoformat(day)).days)] += left
    return out


def insurers_owe(db, cid, pid):
    return db.execute("""SELECT COALESCE(SUM(MAX(estimate_p - paid_p - writeoff_p, 0)), 0) FROM claims
                         WHERE clinic_id = ? AND patient_id = ? AND kind = 'claim' AND status IN ('submitted','partly_paid')""",
                      (cid, pid)).fetchone()[0]


def report(db, clinic):
    cid = clinic["id"]
    today = now_local(clinic["timezone"]).date()
    rows, totals = [], {name: 0 for name, _, _ in BUCKETS}
    for p in db.execute("SELECT id, first_name, last_name, phone FROM patients WHERE clinic_id = ? AND status = 'active'", (cid,)):
        b = patient_ageing(db, cid, p["id"], today)
        total = sum(b.values())
        if not total:
            continue
        for k, v in b.items():
            totals[k] += v
        rows.append({"patient": p, "buckets": b, "total": total, "insurers": min(insurers_owe(db, cid, p["id"]), total)})
    rows.sort(key=lambda r: (-r["buckets"]["90+"], -r["total"]))
    return {"rows": rows, "totals": totals, "grand": sum(totals.values()), "insurers": sum(r["insurers"] for r in rows),
            "buckets": [b[0] for b in BUCKETS]}
