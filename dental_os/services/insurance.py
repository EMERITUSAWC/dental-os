"""Payers (insurers, employers, schemes), the patient's cover, estimates, and claims.

How an estimate works. Each payer can have its own price list. For a procedure the allowed amount is the payer's price,
or the clinic's price if the payer has none. The payer is expected to pay the cover's percentage of the allowed amount,
never more than what is left of the yearly maximum. If the payer is contracted, the clinic accepts the allowed amount and
the difference from its own price is written off. Otherwise the patient pays everything the payer does not.

How claims touch the account. Finished work is already a charge. When a payer pays, we record a normal payment by
'insurance' and match it to the claim. A write-off, if any, is a normal adjustment. So the patient's balance, the receipts
and the reports all keep working without special cases."""
from datetime import date

from . import audit, billing
from .timeutil import now_local

PAYER_KINDS = {"insurer": "Insurer", "employer": "Employer", "government": "Government scheme", "other": "Other"}
OPEN_CLAIM = ("draft", "submitted", "partly_paid", "approved")
CLAIM_STATUS = {"draft": "Draft", "submitted": "Sent, waiting", "partly_paid": "Part paid", "paid": "Paid", "approved": "Approved",
                "rejected": "Rejected", "cancelled": "Cancelled"}


class InsuranceError(Exception):
    pass


def _today(clinic):
    return now_local(clinic["timezone"]).date()


# ----------------------------------------------------------------------------- payers and their prices
def list_payers(db, cid, active_only=False):
    sql = "SELECT * FROM payers WHERE clinic_id = ?" + (" AND active = 1" if active_only else "") + " ORDER BY name"
    return db.execute(sql, (cid,)).fetchall()


def get_payer(db, cid, payer_id):
    return db.execute("SELECT * FROM payers WHERE id = ? AND clinic_id = ?", (payer_id, cid)).fetchone()


def save_payer(db, clinic, user_id, payer_id, name, kind, contracted, phone, email, notes, active=True):
    cid = clinic["id"]
    name = (name or "").strip()
    if not name or len(name) > 80:
        raise InsuranceError("Enter the payer's name.")
    if kind not in PAYER_KINDS:
        raise InsuranceError("Pick what kind of payer it is.")
    if db.execute("SELECT 1 FROM payers WHERE clinic_id = ? AND name = ? COLLATE NOCASE AND id != ?", (cid, name, payer_id or 0)).fetchone():
        raise InsuranceError("There is already a payer with that name.")
    vals = (name, kind, 1 if contracted else 0, (phone or "").strip()[:40] or None, (email or "").strip()[:120] or None,
            (notes or "").strip()[:300] or None, 1 if active else 0)
    if payer_id:
        if not get_payer(db, cid, payer_id):
            raise LookupError("payer")
        db.execute("UPDATE payers SET name=?, kind=?, contracted=?, phone=?, email=?, notes=?, active=? WHERE id=? AND clinic_id=?",
                   (*vals, payer_id, cid))
        audit.log(db, cid, user_id, "payer.edit", "payer", payer_id)
        return payer_id
    cur = db.execute("INSERT INTO payers(name, kind, contracted, phone, email, notes, active, clinic_id) VALUES (?,?,?,?,?,?,?,?)", (*vals, cid))
    audit.log(db, cid, user_id, "payer.add", "payer", cur.lastrowid)
    return cur.lastrowid


def payer_fee_table(db, cid, payer_id):
    """Every active procedure with the clinic's price and the payer's price (or None)."""
    return db.execute(
        """SELECT c.id, c.code, c.name, c.category, c.fee_p AS clinic_fee_p, f.fee_p AS payer_fee_p FROM procedure_codes c
           LEFT JOIN payer_fees f ON f.code_id = c.id AND f.payer_id = ? WHERE c.clinic_id = ? AND c.active = 1
           ORDER BY c.category, c.name""", (payer_id, cid)).fetchall()


def set_payer_fees(db, clinic, user_id, payer_id, fees):
    """fees: {code_id: fee in minor units, or None to use the clinic's price}."""
    cid = clinic["id"]
    if not get_payer(db, cid, payer_id):
        raise LookupError("payer")
    valid = {r["id"] for r in db.execute("SELECT id FROM procedure_codes WHERE clinic_id = ?", (cid,))}
    for code_id, fee in fees.items():
        if code_id not in valid:
            continue
        if fee is None:
            db.execute("DELETE FROM payer_fees WHERE payer_id = ? AND code_id = ? AND clinic_id = ?", (payer_id, code_id, cid))
        elif fee < 0:
            raise InsuranceError("A fee cannot be negative.")
        else:
            db.execute("""INSERT INTO payer_fees(clinic_id, payer_id, code_id, fee_p) VALUES (?,?,?,?)
                          ON CONFLICT(payer_id, code_id) DO UPDATE SET fee_p = excluded.fee_p""", (cid, payer_id, code_id, fee))
    audit.log(db, cid, user_id, "payer.fees", "payer", payer_id)


def allowed_fee(db, cid, payer_id, code_id, clinic_fee_p):
    row = db.execute("SELECT fee_p FROM payer_fees WHERE payer_id = ? AND code_id = ? AND clinic_id = ?", (payer_id, code_id, cid)).fetchone()
    return row["fee_p"] if row else clinic_fee_p


# ----------------------------------------------------------------------------- patient cover
def covers_for(db, cid, pid, active_only=True):
    sql = ("SELECT pc.*, p.name AS payer_name, p.contracted FROM patient_cover pc JOIN payers p ON p.id = pc.payer_id "
           "WHERE pc.clinic_id = ? AND pc.patient_id = ?" + (" AND pc.active = 1" if active_only else "") + " ORDER BY pc.id")
    return db.execute(sql, (cid, pid)).fetchall()


def add_cover(db, clinic, user_id, pid, payer_id, member_no, holder_name, coverage_pct, annual_max_p, valid_from, valid_to):
    cid = clinic["id"]
    if not db.execute("SELECT 1 FROM patients WHERE id = ? AND clinic_id = ? AND status = 'active'", (pid, cid)).fetchone():
        raise InsuranceError("That patient was not found, or is archived.")
    payer = db.execute("SELECT * FROM payers WHERE id = ? AND clinic_id = ? AND active = 1", (payer_id, cid)).fetchone()
    if not payer:
        raise InsuranceError("Pick a payer from the list.")
    if not isinstance(coverage_pct, int) or not 0 <= coverage_pct <= 100:
        raise InsuranceError("The percentage the payer covers must be from 0 to 100.")
    if annual_max_p is not None and annual_max_p <= 0:
        raise InsuranceError("The yearly maximum must be more than zero, or left empty for no limit.")
    if valid_from and valid_to and valid_to < valid_from:
        raise InsuranceError("The end date is before the start date.")
    cur = db.execute("""INSERT INTO patient_cover(clinic_id, patient_id, payer_id, member_no, holder_name, coverage_pct, annual_max_p,
                        valid_from, valid_to) VALUES (?,?,?,?,?,?,?,?,?)""",
                     (cid, pid, payer_id, (member_no or "").strip()[:60] or None, (holder_name or "").strip()[:100] or None,
                      coverage_pct, annual_max_p, valid_from or None, valid_to or None))
    audit.log(db, cid, user_id, "cover.add", "patient", pid, payer["name"][:60])
    return cur.lastrowid


def end_cover(db, clinic, user_id, cover_id):
    cid = clinic["id"]
    row = db.execute("SELECT * FROM patient_cover WHERE id = ? AND clinic_id = ? AND active = 1", (cover_id, cid)).fetchone()
    if not row:
        raise LookupError("cover")
    db.execute("UPDATE patient_cover SET active = 0 WHERE id = ?", (cover_id,))
    audit.log(db, cid, user_id, "cover.end", "patient", row["patient_id"])
    return row["patient_id"]


def _in_force(cover, today):
    iso = today.isoformat()
    return cover["active"] and (not cover["valid_from"] or cover["valid_from"] <= iso) and (not cover["valid_to"] or cover["valid_to"] >= iso)


def used_this_year(db, cid, cover_id, today):
    """How much of the yearly maximum is spoken for: what payers paid, or are expected to pay on open claims, this calendar year."""
    start, end = f"{today.year}-01-01", f"{today.year}-12-31"
    return db.execute(
        """SELECT COALESCE(SUM(CASE WHEN paid_p > 0 THEN paid_p ELSE estimate_p END), 0) FROM claims
           WHERE clinic_id = ? AND cover_id = ? AND kind = 'claim' AND status NOT IN ('rejected','cancelled')
           AND date(created_at) BETWEEN ? AND ?""", (cid, cover_id, start, end)).fetchone()[0]


def estimate(db, clinic, cover, code_id, fee_p, today=None, remaining_p=None):
    """What the payer and the patient are each expected to pay for one line. Returns minor units.
    remaining_p is the yearly maximum still available, passed in so a list of lines shares one pool."""
    cid = clinic["id"]
    today = today or _today(clinic)
    allowed = allowed_fee(db, cid, cover["payer_id"], code_id, fee_p)
    contracted = bool(db.execute("SELECT contracted FROM payers WHERE id = ?", (cover["payer_id"],)).fetchone()["contracted"])
    if not _in_force(cover, today):
        return {"allowed_p": allowed, "payer_p": 0, "patient_p": fee_p, "writeoff_p": 0, "in_force": False}
    payer = allowed * cover["coverage_pct"] // 100
    if cover["annual_max_p"] is not None:
        if remaining_p is None:
            remaining_p = max(cover["annual_max_p"] - used_this_year(db, cid, cover["id"], today), 0)
        payer = min(payer, remaining_p)
    writeoff = max(fee_p - allowed, 0) if contracted else 0
    patient = max(fee_p - payer - writeoff, 0)
    return {"allowed_p": allowed, "payer_p": payer, "patient_p": patient, "writeoff_p": writeoff, "in_force": True}


def estimate_lines(db, clinic, pid, lines):
    """lines: [{'code_id', 'fee_p'}]. Uses the patient's first cover in force. Returns (cover, [estimate per line]) or (None, [])."""
    today = _today(clinic)
    covers = [c for c in covers_for(db, clinic["id"], pid) if _in_force(c, today)]
    if not covers:
        return None, []
    cover = covers[0]
    remaining = None
    if cover["annual_max_p"] is not None:
        remaining = max(cover["annual_max_p"] - used_this_year(db, clinic["id"], cover["id"], today), 0)
    out = []
    for ln in lines:
        est = estimate(db, clinic, cover, ln["code_id"], ln["fee_p"], today, remaining)
        if remaining is not None:
            remaining = max(remaining - est["payer_p"], 0)
        out.append(est)
    return cover, out


# ----------------------------------------------------------------------------- claims
def get_claim(db, cid, claim_id):
    return db.execute("""SELECT c.*, p.name AS payer_name, pa.first_name, pa.last_name, pc.member_no FROM claims c
                         JOIN payers p ON p.id = c.payer_id JOIN patients pa ON pa.id = c.patient_id
                         LEFT JOIN patient_cover pc ON pc.id = c.cover_id WHERE c.id = ? AND c.clinic_id = ?""", (claim_id, cid)).fetchone()


def claim_items(db, cid, claim_id):
    return db.execute("""SELECT i.*, c.name AS code_name, pr.tooth, pr.done_on, pr.status AS work_status FROM claim_items i
                         JOIN procedures pr ON pr.id = i.procedure_id JOIN procedure_codes c ON c.id = pr.code_id
                         WHERE i.claim_id = ? AND i.clinic_id = ? ORDER BY i.id""", (claim_id, cid)).fetchall()


def unclaimed(db, cid, pid, kind):
    """Finished (for a claim) or planned (for a pre-authorisation) work that is not already on an open or paid claim."""
    status = "done" if kind == "claim" else "planned"
    return db.execute(
        """SELECT pr.*, c.name AS code_name FROM procedures pr JOIN procedure_codes c ON c.id = pr.code_id
           WHERE pr.clinic_id = ? AND pr.patient_id = ? AND pr.status = ? AND NOT EXISTS (
             SELECT 1 FROM claim_items ci JOIN claims cl ON cl.id = ci.claim_id
             WHERE ci.procedure_id = pr.id AND cl.kind = ? AND cl.status NOT IN ('rejected','cancelled'))
           ORDER BY pr.id""", (cid, pid, status, kind)).fetchall()


def create_claim(db, clinic, user_id, pid, cover_id, procedure_ids, kind="claim"):
    cid = clinic["id"]
    if kind not in ("claim", "preauth"):
        raise InsuranceError("Unknown kind of claim.")
    cover = db.execute("SELECT * FROM patient_cover WHERE id = ? AND clinic_id = ? AND patient_id = ? AND active = 1",
                       (cover_id, cid, pid)).fetchone()
    if not cover:
        raise InsuranceError("Pick one of this patient's covers.")
    wanted = {int(x) for x in procedure_ids}
    if not wanted:
        raise InsuranceError("Tick the treatment to claim for.")
    rows = [r for r in unclaimed(db, cid, pid, kind) if r["id"] in wanted]
    if len(rows) != len(wanted):
        raise InsuranceError("Some of that treatment is not available to claim. It may already be on another claim.")
    billing._begin(db)
    claim_no = db.execute("SELECT COALESCE(MAX(claim_no), 0) + 1 FROM claims WHERE clinic_id = ?", (cid,)).fetchone()[0]
    cur = db.execute("INSERT INTO claims(clinic_id, claim_no, patient_id, payer_id, cover_id, kind, created_by) VALUES (?,?,?,?,?,?,?)",
                     (cid, claim_no, pid, cover["payer_id"], cover_id, kind, user_id))
    remaining = None
    today = _today(clinic)
    if cover["annual_max_p"] is not None:
        remaining = max(cover["annual_max_p"] - used_this_year(db, cid, cover_id, today), 0)
    total = est_total = 0
    for r in rows:
        est = estimate(db, clinic, cover, r["code_id"], r["fee_p"], today, remaining)
        if remaining is not None:
            remaining = max(remaining - est["payer_p"], 0)
        db.execute("INSERT INTO claim_items(clinic_id, claim_id, procedure_id, fee_p, allowed_p, estimate_p) VALUES (?,?,?,?,?,?)",
                   (cid, cur.lastrowid, r["id"], r["fee_p"], est["allowed_p"], est["payer_p"]))
        total += r["fee_p"]
        est_total += est["payer_p"]
    db.execute("UPDATE claims SET total_fee_p = ?, estimate_p = ? WHERE id = ?", (total, est_total, cur.lastrowid))
    audit.log(db, cid, user_id, f"{kind}.create", "claim", cur.lastrowid)
    return cur.lastrowid


def _claim(db, cid, claim_id, allowed_status):
    c = db.execute("SELECT * FROM claims WHERE id = ? AND clinic_id = ?", (claim_id, cid)).fetchone()
    if not c:
        raise LookupError("claim")
    if c["status"] not in allowed_status:
        raise InsuranceError(f"That cannot be done to a claim that is {CLAIM_STATUS[c['status']].lower()}.")
    return c


def submit(db, clinic, user_id, claim_id, sent_on=None, payer_ref=None):
    cid = clinic["id"]
    c = _claim(db, cid, claim_id, ("draft", "rejected"))
    sent = sent_on or _today(clinic).isoformat()
    db.execute("UPDATE claims SET status = 'submitted', submitted_on = ?, payer_ref = COALESCE(?, payer_ref) WHERE id = ?",
               (sent, (payer_ref or "").strip()[:80] or None, claim_id))
    audit.log(db, cid, user_id, "claim.submit", "claim", claim_id)


def reject(db, clinic, user_id, claim_id, reason):
    cid = clinic["id"]
    _claim(db, cid, claim_id, ("submitted", "partly_paid"))
    reason = (reason or "").strip()
    if len(reason) < 3:
        raise InsuranceError("Write why it was rejected.")
    db.execute("UPDATE claims SET status = 'rejected', note = ? WHERE id = ?", (reason[:300], claim_id))
    audit.log(db, cid, user_id, "claim.reject", "claim", claim_id)


def approve(db, clinic, user_id, claim_id, approved_p, payer_ref=None):
    """For a pre-authorisation: the payer says what it will pay."""
    cid = clinic["id"]
    c = _claim(db, cid, claim_id, ("submitted",))
    if c["kind"] != "preauth":
        raise InsuranceError("Only a pre-authorisation is approved. A claim is paid.")
    if approved_p < 0:
        raise InsuranceError("The approved amount cannot be negative.")
    db.execute("UPDATE claims SET status = 'approved', estimate_p = ?, payer_ref = COALESCE(?, payer_ref) WHERE id = ?",
               (approved_p, (payer_ref or "").strip()[:80] or None, claim_id))
    audit.log(db, cid, user_id, "preauth.approve", "claim", claim_id)


def cancel(db, clinic, user_id, claim_id):
    cid = clinic["id"]
    c = _claim(db, cid, claim_id, ("draft", "submitted", "approved", "rejected"))
    if c["paid_p"] > 0:
        raise InsuranceError("Money has been received on this claim, so it cannot be cancelled.")
    db.execute("UPDATE claims SET status = 'cancelled' WHERE id = ?", (claim_id,))
    audit.log(db, cid, user_id, "claim.cancel", "claim", claim_id)


def record_payer_payment(db, clinic, user_id, claim_id, amount_p, reference, writeoff_p=0, writeoff_note=None, final=False):
    """The payer has paid. Records a payment by insurance matched to the claim, and an optional write-off for the
    part that will never be paid. 'final' closes the claim even if less than the estimate was paid."""
    cid = clinic["id"]
    c = _claim(db, cid, claim_id, ("submitted", "partly_paid"))
    if c["kind"] != "claim":
        raise InsuranceError("A pre-authorisation is not paid. Raise a claim once the work is done.")
    if amount_p <= 0:
        raise InsuranceError("The amount must be more than zero.")
    if writeoff_p < 0:
        raise InsuranceError("The write-off cannot be negative.")
    try:
        billing.record_payment(db, clinic, user_id, c["patient_id"], amount_p, "insurance", reference,
                               f"Claim {c['claim_no']}", claim_id=claim_id)
        if writeoff_p:
            billing.add_adjustment(db, clinic, user_id, c["patient_id"], -writeoff_p, "write_off",
                                   (writeoff_note or "").strip() or f"Claim {c['claim_no']} difference")
    except billing.BillingError as exc:
        raise InsuranceError(str(exc) + (" Switch on Insurance under Settings if it is off." if "how the patient paid" in str(exc) else ""))
    paid = c["paid_p"] + amount_p
    wo = c["writeoff_p"] + writeoff_p
    status = "paid" if (final or paid + wo >= c["estimate_p"]) else "partly_paid"
    db.execute("UPDATE claims SET paid_p = ?, writeoff_p = ?, status = ? WHERE id = ?", (paid, wo, status, claim_id))
    audit.log(db, cid, user_id, "claim.paid", "claim", claim_id)


def claims_for_patient(db, cid, pid):
    return db.execute("""SELECT c.*, p.name AS payer_name FROM claims c JOIN payers p ON p.id = c.payer_id
                         WHERE c.clinic_id = ? AND c.patient_id = ? ORDER BY c.id DESC""", (cid, pid)).fetchall()


def claim_list(db, clinic, status=None):
    cid, today = clinic["id"], _today(clinic)
    sql = ("SELECT c.*, p.name AS payer_name, pa.first_name, pa.last_name FROM claims c JOIN payers p ON p.id = c.payer_id "
           "JOIN patients pa ON pa.id = c.patient_id WHERE c.clinic_id = ?")
    args = [cid]
    if status == "open":
        sql += " AND c.status IN ('draft','submitted','partly_paid','approved')"
    elif status in CLAIM_STATUS:
        sql += " AND c.status = ?"
        args.append(status)
    out = []
    for r in db.execute(sql + " ORDER BY c.id DESC LIMIT 300", args):
        age = (today - date.fromisoformat(r["submitted_on"])).days if r["submitted_on"] and r["status"] in ("submitted", "partly_paid") else None
        out.append({"claim": r, "age": age})
    return out


def awaiting_total(db, cid):
    """What payers still owe: expected minus paid minus written off, on sent claims."""
    return db.execute("""SELECT COALESCE(SUM(MAX(estimate_p - paid_p - writeoff_p, 0)), 0) FROM claims
                         WHERE clinic_id = ? AND kind = 'claim' AND status IN ('submitted','partly_paid')""", (cid,)).fetchone()[0]
