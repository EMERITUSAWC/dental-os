"""Payers (insurers, employers, schemes), the patient's cover, estimates, and claims.

How an estimate works. Each payer can have its own price list. For a procedure the allowed amount is the payer's price,
or the clinic's price if the payer has none. The payer is expected to pay the cover's percentage of the allowed amount,
never more than what is left of the yearly maximum. If the payer is contracted, the clinic accepts the allowed amount and
the difference from its own price is written off. Otherwise the patient pays everything the payer does not.

How claims touch the account. Finished work is already a charge. When a payer pays, we record a normal payment by
'insurance' and match it to the claim. A write-off, if any, is a normal adjustment. So the patient's balance, the receipts
and the reports all keep working without special cases."""
from datetime import date

from . import audit, benefits as BEN, billing
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
           "WHERE pc.clinic_id = ? AND pc.patient_id = ?" + (" AND pc.active = 1" if active_only else "") + " ORDER BY pc.active DESC, pc.cover_rank, pc.id")
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
    rank = db.execute("SELECT COALESCE(MAX(cover_rank), 0) + 1 FROM patient_cover WHERE clinic_id = ? AND patient_id = ? AND active = 1",
                      (cid, pid)).fetchone()[0]
    cur = db.execute("""INSERT INTO patient_cover(clinic_id, patient_id, payer_id, member_no, holder_name, coverage_pct, annual_max_p,
                        valid_from, valid_to, cover_rank) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                     (cid, pid, payer_id, (member_no or "").strip()[:60] or None, (holder_name or "").strip()[:100] or None,
                      coverage_pct, annual_max_p, valid_from or None, valid_to or None, min(rank, 9)))
    audit.log(db, cid, user_id, "cover.add", "patient", pid, payer["name"][:60])
    return cur.lastrowid


def end_cover(db, clinic, user_id, cover_id):
    cid = clinic["id"]
    row = db.execute("SELECT * FROM patient_cover WHERE id = ? AND clinic_id = ? AND active = 1", (cover_id, cid)).fetchone()
    if not row:
        raise LookupError("cover")
    db.execute("UPDATE patient_cover SET active = 0 WHERE id = ?", (cover_id,))
    _renumber(db, cid, row["patient_id"])
    audit.log(db, cid, user_id, "cover.end", "patient", row["patient_id"])
    return row["patient_id"]


def _renumber(db, cid, pid, first=None):
    rows = db.execute("SELECT id FROM patient_cover WHERE clinic_id = ? AND patient_id = ? AND active = 1 ORDER BY cover_rank, id",
                      (cid, pid)).fetchall()
    ids = [r["id"] for r in rows]
    if first in ids:
        ids.remove(first)
        ids.insert(0, first)
    for n, i in enumerate(ids, start=1):
        db.execute("UPDATE patient_cover SET cover_rank = ? WHERE id = ?", (n, i))


def make_main(db, clinic, user_id, cover_id):
    cid = clinic["id"]
    row = db.execute("SELECT * FROM patient_cover WHERE id = ? AND clinic_id = ? AND active = 1", (cover_id, cid)).fetchone()
    if not row:
        raise LookupError("cover")
    _renumber(db, cid, row["patient_id"], first=cover_id)
    audit.log(db, cid, user_id, "cover.main", "patient", row["patient_id"])
    return row["patient_id"]


def get_cover(db, cid, cover_id):
    return db.execute("""SELECT pc.*, p.name AS payer_name, p.contracted FROM patient_cover pc JOIN payers p ON p.id = pc.payer_id
                         WHERE pc.id = ? AND pc.clinic_id = ?""", (cover_id, cid)).fetchone()


def save_benefit_terms(db, clinic, user_id, cover_id, deductible_p, on_preventive, year_start_month, ortho_max_p, waiting_from,
                       categories, limits_rows):
    """categories: {category: (pct or None to use the general share, waiting_months)}. limits_rows: [(code_id, times, months)]."""
    cid = clinic["id"]
    cover = get_cover(db, cid, cover_id)
    if not cover or not cover["active"]:
        raise InsuranceError("That cover was not found.")
    if deductible_p is not None and deductible_p < 0:
        raise InsuranceError("The excess cannot be negative.")
    if ortho_max_p is not None and ortho_max_p <= 0:
        raise InsuranceError("The orthodontic limit must be more than zero, or left empty for none.")
    if year_start_month not in range(1, 13):
        raise InsuranceError("Pick the month the benefit year starts.")
    clean_cats = {}
    for cat, (pct, waiting) in categories.items():
        if pct is None and not waiting:
            continue
        if pct is not None and not 0 <= pct <= 100:
            raise InsuranceError("Each share must be from 0 to 100.")
        if not 0 <= (waiting or 0) <= 60:
            raise InsuranceError("A waiting period is from 0 to 60 months.")
        clean_cats[cat] = (cover["coverage_pct"] if pct is None else pct, waiting or 0)
    clean_lims = {}
    for code_id, times, months in limits_rows:
        if not db.execute("SELECT 1 FROM procedure_codes WHERE id = ? AND clinic_id = ?", (code_id, cid)).fetchone():
            raise InsuranceError("Pick a procedure from the list.")
        if not (1 <= times <= 50 and 1 <= months <= 120):
            raise InsuranceError("A limit is 1 to 50 times in 1 to 120 months.")
        clean_lims[code_id] = (times, months)
    db.execute("""UPDATE patient_cover SET deductible_p = ?, deductible_on_preventive = ?, year_start_month = ?, ortho_max_p = ?,
                  waiting_from = ? WHERE id = ?""",
               (deductible_p or 0, 1 if on_preventive else 0, year_start_month, ortho_max_p, waiting_from or None, cover_id))
    db.execute("DELETE FROM cover_benefits WHERE clinic_id = ? AND cover_id = ?", (cid, cover_id))
    for cat, (pct, waiting) in clean_cats.items():
        db.execute("INSERT INTO cover_benefits(clinic_id, cover_id, category, pct, waiting_months) VALUES (?,?,?,?,?)",
                   (cid, cover_id, cat, pct, waiting))
    db.execute("DELETE FROM cover_limits WHERE clinic_id = ? AND cover_id = ?", (cid, cover_id))
    for code_id, (times, months) in clean_lims.items():
        db.execute("INSERT INTO cover_limits(clinic_id, cover_id, code_id, times, months) VALUES (?,?,?,?,?)",
                   (cid, cover_id, code_id, times, months))
    audit.log(db, cid, user_id, "cover.benefits", "patient", cover["patient_id"])
    return cover["patient_id"]


def verify(db, clinic, user_id, cover_id, note):
    cid = clinic["id"]
    cover = get_cover(db, cid, cover_id)
    if not cover or not cover["active"]:
        raise InsuranceError("That cover was not found.")
    note = (note or "").strip()[:300]
    if not note:
        raise InsuranceError("Write who you spoke to or what the payer confirmed.")
    db.execute("UPDATE patient_cover SET verified_on = ?, verified_by = ?, verify_note = ? WHERE id = ?",
               (_today(clinic).isoformat(), user_id, note, cover_id))
    audit.log(db, cid, user_id, "cover.verify", "patient", cover["patient_id"])
    return cover["patient_id"]


def _in_force(cover, today):
    return BEN.in_force(cover, today)


def used_this_year(db, cid, cover_id, today):
    """How much of the yearly maximum is spoken for in the benefit year: what payers paid, or are expected to pay on open claims."""
    cover = db.execute("SELECT * FROM patient_cover WHERE id = ? AND clinic_id = ?", (cover_id, cid)).fetchone()
    return BEN.max_used(db, cid, cover, today) if cover else 0


def estimate(db, clinic, cover, code_id, fee_p, today=None, remaining_p=None):
    """What one payer is expected to pay for one line, on its own. Returns minor units. See services/benefits.py."""
    today = today or _today(clinic)
    code = db.execute("SELECT * FROM procedure_codes WHERE id = ? AND clinic_id = ?", (code_id, clinic["id"])).fetchone()
    pool = BEN.pools(db, clinic["id"], cover, today)
    if remaining_p is not None:
        pool["max_left"] = remaining_p
    return BEN.line(db, clinic, cover, code, fee_p, today, pool, cover["patient_id"])


def estimate_lines(db, clinic, pid, lines):
    """lines: [{'code_id', 'fee_p'}]. Uses the patient's covers in force, the main one first and a second payer after it.
    Returns (main cover, [result per line]) or (None, []). Each result has payer_p (both payers together) and patient_p."""
    covers, results = BEN.estimate_plan(db, clinic, pid, lines, _today(clinic))
    if not covers:
        return None, []
    out = []
    for r in results:
        first, sec = r["first"], r["second"]
        out.append({"allowed_p": first["allowed_p"], "payer_p": first["payer_p"] + (sec["payer_p"] if sec else 0),
                    "first_p": first["payer_p"], "second_p": sec["payer_p"] if sec else 0, "writeoff_p": first["writeoff_p"],
                    "patient_p": r["patient_p"], "notes": r["notes"], "in_force": first["in_force"]})
    return covers[0], out


def estimate_covers(db, clinic, pid):
    return BEN.covers_in_order(db, clinic["id"], pid, _today(clinic))[:2]


# ----------------------------------------------------------------------------- claims
def get_claim(db, cid, claim_id):
    return db.execute("""SELECT c.*, p.name AS payer_name, pa.first_name, pa.last_name, pc.member_no FROM claims c
                         JOIN payers p ON p.id = c.payer_id JOIN patients pa ON pa.id = c.patient_id
                         LEFT JOIN patient_cover pc ON pc.id = c.cover_id WHERE c.id = ? AND c.clinic_id = ?""", (claim_id, cid)).fetchone()


def claim_items(db, cid, claim_id):
    return db.execute("""SELECT i.*, c.name AS code_name, pr.tooth, pr.done_on, pr.status AS work_status FROM claim_items i
                         JOIN procedures pr ON pr.id = i.procedure_id JOIN procedure_codes c ON c.id = pr.code_id
                         WHERE i.claim_id = ? AND i.clinic_id = ? ORDER BY i.id""", (claim_id, cid)).fetchall()


def unclaimed(db, cid, pid, kind, cover_id=None):
    """Finished (for a claim) or planned (for a pre-authorisation) work not already on an open or paid claim to this cover.
    With no cover given: not on any such claim at all."""
    status = "done" if kind == "claim" else "planned"
    same = "AND cl.cover_id = ?" if cover_id else ""
    args = [cid, pid, status, kind] + ([cover_id] if cover_id else [])
    return db.execute(
        f"""SELECT pr.*, c.name AS code_name FROM procedures pr JOIN procedure_codes c ON c.id = pr.code_id
           WHERE pr.clinic_id = ? AND pr.patient_id = ? AND pr.status = ? AND NOT EXISTS (
             SELECT 1 FROM claim_items ci JOIN claims cl ON cl.id = ci.claim_id
             WHERE ci.procedure_id = pr.id AND cl.kind = ? AND cl.status NOT IN ('rejected','cancelled') {same})
           ORDER BY pr.id""", args).fetchall()


def _first_payer_share(db, clinic, pid, cover, proc, day):
    """For a second payer: what the main payer is expected to pay on this line, and what the patient then owes."""
    cid = clinic["id"]
    row = db.execute("""SELECT ci.estimate_p, ci.allowed_p, p.contracted, c.paid_p, c.status FROM claim_items ci
                        JOIN claims c ON c.id = ci.claim_id JOIN payers p ON p.id = c.payer_id
                        WHERE ci.procedure_id = ? AND ci.clinic_id = ? AND c.kind = 'claim' AND c.cover_id != ?
                        AND c.status NOT IN ('rejected','cancelled') ORDER BY c.id LIMIT 1""", (proc["id"], cid, cover["id"])).fetchone()
    if row:
        writeoff = max(proc["fee_p"] - row["allowed_p"], 0) if row["contracted"] else 0
        return {"payer_p": row["estimate_p"], "writeoff_p": writeoff, "patient_p": max(proc["fee_p"] - row["estimate_p"] - writeoff, 0)}
    main = [c for c in BEN.covers_in_order(db, cid, pid, day) if c["id"] != cover["id"] and c["cover_rank"] < cover["cover_rank"]]
    if not main:
        return None
    code = db.execute("SELECT * FROM procedure_codes WHERE id = ?", (proc["code_id"],)).fetchone()
    return BEN.line(db, clinic, main[0], code, proc["fee_p"], day, BEN.pools(db, cid, main[0], day), pid, exclude=(proc["id"],))


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
    rows = [r for r in unclaimed(db, cid, pid, kind, cover_id) if r["id"] in wanted]
    if len(rows) != len(wanted):
        raise InsuranceError("Some of that treatment is not available to claim. It may already be on another claim.")
    if kind == "claim" and db.execute("SELECT needs_dx FROM payers WHERE id = ?", (cover["payer_id"],)).fetchone()["needs_dx"]:
        if any(not db.execute("SELECT dx_code FROM procedures WHERE id = ?", (r["id"],)).fetchone()["dx_code"] for r in rows):
            raise InsuranceError("This payer needs a diagnosis on every line. Add it on the treatment list first.")
    billing._begin(db)
    claim_no = db.execute("SELECT COALESCE(MAX(claim_no), 0) + 1 FROM claims WHERE clinic_id = ?", (cid,)).fetchone()[0]
    cur = db.execute("INSERT INTO claims(clinic_id, claim_no, patient_id, payer_id, cover_id, kind, created_by) VALUES (?,?,?,?,?,?,?)",
                     (cid, claim_no, pid, cover["payer_id"], cover_id, kind, user_id))
    today = _today(clinic)
    pool = BEN.pools(db, cid, cover, today)
    rules, lims = BEN.category_rules(db, cid, cover_id), BEN.limits(db, cid, cover_id)
    others = tuple(r["id"] for r in rows)
    total = est_total = 0
    for r in rows:
        code = db.execute("SELECT * FROM procedure_codes WHERE id = ?", (r["code_id"],)).fetchone()
        day = BEN.parse_date(r["done_on"]) if (kind == "claim" and r["done_on"]) else today
        est = BEN.line(db, clinic, cover, code, r["fee_p"], day, pool, pid, exclude=others, rules=rules, lims=lims)
        if cover["cover_rank"] > 1:
            first = _first_payer_share(db, clinic, pid, cover, r, day)
            if first is not None:
                pays = min(est["payer_p"], first["patient_p"])
                if pool["max_left"] is not None:
                    pool["max_left"] += est["payer_p"] - pays
                est["payer_p"] = pays
        dx = db.execute("SELECT dx_code FROM procedures WHERE id = ?", (r["id"],)).fetchone()["dx_code"]
        db.execute("""INSERT INTO claim_items(clinic_id, claim_id, procedure_id, fee_p, allowed_p, estimate_p, deductible_p, note, dx_code)
                      VALUES (?,?,?,?,?,?,?,?,?)""",
                   (cid, cur.lastrowid, r["id"], r["fee_p"], est["allowed_p"], est["payer_p"], est["deductible_p"],
                    " ".join(est["notes"])[:300] or None, dx))
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


WRITEOFF_TOO_BIG = "The write-off is more than is left to pay on this claim. Write off only the part the payer will not pay."


def record_payer_payment(db, clinic, user_id, claim_id, amount_p, reference, writeoff_p=0, writeoff_note=None, final=False, allow_zero=False,
                         from_remittance=False):
    """The payer has paid. Records a payment by insurance matched to the claim, and an optional write-off for the
    part that will never be paid. 'final' closes the claim even if less than the estimate was paid."""
    cid = clinic["id"]
    c = _claim(db, cid, claim_id, ("submitted", "partly_paid"))
    if c["kind"] != "claim":
        raise InsuranceError("A pre-authorisation is not paid. Raise a claim once the work is done.")
    if amount_p < 0 or (amount_p == 0 and not (allow_zero and (writeoff_p > 0 or final))):
        raise InsuranceError("The amount must be more than zero.")
    if writeoff_p < 0:
        raise InsuranceError("The write-off cannot be negative.")
    if writeoff_p and not from_remittance and writeoff_p > max(0, c["estimate_p"] - c["paid_p"] - c["writeoff_p"] - amount_p):
        raise InsuranceError(WRITEOFF_TOO_BIG)                 # 0.68: more than is left on the claim would make credit from nothing
    pay_id = None
    try:
        if amount_p:
            pay_id = billing.record_payment(db, clinic, user_id, c["patient_id"], amount_p, "insurance", reference,
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
    return pay_id


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
