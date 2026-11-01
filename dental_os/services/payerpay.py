"""One payment from a payer that covers several claims, the way insurers and schemes usually pay: one bank transfer and a list
(a remittance or statement) of which claims it is for. Each claim's share becomes a normal payment by insurance on that patient's
account, linked to the payer's payment, so receipts, balances and reports work as before. The shares must add up to the amount
received, so nothing is left unexplained."""
from . import audit, insurance as INS
from .timeutil import now_local, parse_date


class PayerPayError(Exception):
    pass


def open_claims(db, cid, payer_id):
    rows = db.execute("""SELECT c.*, pa.first_name, pa.last_name FROM claims c JOIN patients pa ON pa.id = c.patient_id
                         WHERE c.clinic_id = ? AND c.payer_id = ? AND c.kind = 'claim' AND c.status IN ('submitted','partly_paid')
                         ORDER BY c.submitted_on, c.claim_no""", (cid, payer_id)).fetchall()
    return [{"claim": r, "owed": max(r["estimate_p"] - r["paid_p"] - r["writeoff_p"], 0)} for r in rows]


def post(db, clinic, user_id, payer_id, received_on, amount_p, reference, allocations, note=None):
    """allocations: [{"claim_id", "amount_p", "writeoff_p", "final"}]. Returns the payer payment id."""
    cid = clinic["id"]
    payer = db.execute("SELECT * FROM payers WHERE id = ? AND clinic_id = ?", (payer_id, cid)).fetchone()
    if not payer:
        raise PayerPayError("Pick a payer from the list.")
    reference = (reference or "").strip()[:80]
    if not reference:
        raise PayerPayError("Enter the payer's reference, for example the transfer or cheque number.")
    try:
        day = parse_date((received_on or "").strip()) if received_on else now_local(clinic["timezone"]).date()
    except ValueError:
        raise PayerPayError("Use a date like 2026-11-30.")
    if day > now_local(clinic["timezone"]).date():
        raise PayerPayError("The date cannot be in the future.")
    if amount_p is None or amount_p < 0:
        raise PayerPayError("Enter the amount received.")
    open_ids = {x["claim"]["id"] for x in open_claims(db, cid, payer_id)}
    used = [a for a in allocations if a.get("amount_p") or a.get("writeoff_p") or a.get("final")]
    if not used:
        raise PayerPayError("Enter what this payment pays on at least one claim.")
    for a in used:
        if a["claim_id"] not in open_ids:
            raise PayerPayError("One of those claims is not open with this payer.")
        if (a.get("amount_p") or 0) < 0 or (a.get("writeoff_p") or 0) < 0:
            raise PayerPayError("Amounts cannot be negative.")
    if sum(a.get("amount_p") or 0 for a in used) != amount_p:
        raise PayerPayError("The amounts on the claims must add up to the amount received.")
    if db.execute("SELECT 1 FROM payer_payments WHERE clinic_id = ? AND payer_id = ? AND reference = ?", (cid, payer_id, reference)).fetchone():
        raise PayerPayError("A payment with that reference from this payer is already recorded.")
    pp_id = db.execute("""INSERT INTO payer_payments(clinic_id, payer_id, received_on, amount_p, reference, note, created_by)
                          VALUES (?,?,?,?,?,?,?)""", (cid, payer_id, day.isoformat(), amount_p, reference, (note or "").strip()[:300] or None, user_id)).lastrowid
    for a in used:
        try:
            pay_id = INS.record_payer_payment(db, clinic, user_id, a["claim_id"], a.get("amount_p") or 0, reference, a.get("writeoff_p") or 0,
                                              f"{payer['name']} {reference}", final=bool(a.get("final")), allow_zero=True,
                                              from_remittance=bool(a.get("era")))
        except INS.InsuranceError as exc:
            raise PayerPayError(str(exc))
        if pay_id:
            db.execute("UPDATE payments SET payer_payment_id = ? WHERE id = ?", (pp_id, pay_id))
    audit.log(db, cid, user_id, "payer_payment.post", "payer_payment", pp_id, f"{len(used)} claims")
    return pp_id


def get(db, cid, pp_id):
    return db.execute("""SELECT x.*, p.name AS payer_name, u.display_name AS by_name FROM payer_payments x JOIN payers p ON p.id = x.payer_id
                         LEFT JOIN users u ON u.id = x.created_by WHERE x.id = ? AND x.clinic_id = ?""", (pp_id, cid)).fetchone()


def lines(db, cid, pp_id):
    return db.execute("""SELECT pm.*, c.claim_no, c.status AS claim_status, pa.first_name, pa.last_name FROM payments pm
                         JOIN claims c ON c.id = pm.claim_id JOIN patients pa ON pa.id = pm.patient_id
                         WHERE pm.clinic_id = ? AND pm.payer_payment_id = ? ORDER BY c.claim_no""", (cid, pp_id)).fetchall()


def recent(db, cid, limit=50):
    return db.execute("""SELECT x.*, p.name AS payer_name FROM payer_payments x JOIN payers p ON p.id = x.payer_id
                         WHERE x.clinic_id = ? ORDER BY x.received_on DESC, x.id DESC LIMIT ?""", (cid, limit)).fetchall()


def ageing_by_payer(db, clinic):
    """What each payer still owes on sent claims, by how long ago they were sent."""
    today = now_local(clinic["timezone"]).date()
    out = {}
    for r in db.execute("""SELECT c.*, p.name AS payer_name FROM claims c JOIN payers p ON p.id = c.payer_id
                           WHERE c.clinic_id = ? AND c.kind = 'claim' AND c.status IN ('submitted','partly_paid')""", (clinic["id"],)):
        owed = max(r["estimate_p"] - r["paid_p"] - r["writeoff_p"], 0)
        if not owed:
            continue
        days = (today - parse_date(r["submitted_on"])).days if r["submitted_on"] else 0
        band = "0_30" if days <= 30 else "31_60" if days <= 60 else "61_90" if days <= 90 else "over_90"
        row = out.setdefault(r["payer_id"], {"payer": r["payer_name"], "0_30": 0, "31_60": 0, "61_90": 0, "over_90": 0, "total": 0, "claims": 0})
        row[band] += owed
        row["total"] += owed
        row["claims"] += 1
    return sorted(out.values(), key=lambda x: -x["total"])
