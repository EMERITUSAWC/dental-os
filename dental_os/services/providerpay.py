"""Paying dentists and hygienists their share (version 0.36).

Many associates are paid a percentage of what they earn the practice, less part of the lab bills for their work. A manager sets
each provider's terms: the basis (gross production, net production or money collected), the percentage, the share of lab fees the
provider bears, and an optional fixed amount per day worked. Terms have a start date, so a change never rewrites past pay.

Collections need a rule, because a patient pays the clinic, not a dentist. Each patient's payments and discounts are matched to
their completed treatment oldest first (first in, first out). Money counts as collected for a provider on the later of the day it
was paid and the day the work was done, so a deposit counts once the work is done. Lines with no provider (late fees, for example)
stay with the clinic.

A pay run works out one provider's pay for a period, keeps a copy of the figures, takes extra lines (an advance, a bonus), and is
approved, then marked paid. Approved runs never change. Nothing is deleted. No web framework here."""
import datetime
import json
from decimal import ROUND_HALF_UP, Decimal

from . import audit

BASES = {"gross_production": "Gross production (fees of work done)", "net_production": "Net production (fees less discounts and write-offs)",
         "collections": "Collections (money received for their work)"}
STATUSES = {"draft": "Draft", "approved": "Approved", "paid": "Paid", "cancelled": "Cancelled"}


class PayError(ValueError):
    pass


def _bp(text, label):
    """'45' or '45.5' per cent -> basis points (4500 or 4550)."""
    try:
        v = Decimal(str(text or "0").strip().rstrip("%") or "0")
    except Exception:
        raise PayError(label)
    if not v.is_finite() or v < 0 or v > 100 or v != v.quantize(Decimal("0.01")):
        raise PayError(label)
    return int(v * 100)


def _pct(amount, bp):
    return int((Decimal(amount) * Decimal(bp) / Decimal(10000)).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def _day(text, label):
    try:
        return datetime.date.fromisoformat((text or "").strip()).isoformat()
    except ValueError:
        raise PayError(label)


# ---------------------------------------------------------------------------------------------------------------- terms
def set_terms(db, clinic, user_id, provider_id, basis, percent, lab_percent, daily_p, effective_from):
    cid = clinic["id"]
    if not db.execute("SELECT 1 FROM providers WHERE id = ? AND clinic_id = ?", (provider_id, cid)).fetchone():
        raise PayError("That provider was not found.")
    if basis not in BASES:
        raise PayError("Pick what the share is worked out on.")
    pct = _bp(percent, "The share is a percentage from 0 to 100, with at most two decimal places.")
    lab = _bp(lab_percent, "The lab share is a percentage from 0 to 100, with at most two decimal places.")
    if not isinstance(daily_p, int) or daily_p < 0:
        raise PayError("The daily amount can't be below zero.")
    if pct == 0 and daily_p == 0:
        raise PayError("Give a share, a daily amount, or both.")
    start = _day(effective_from, "Use a real date for when these terms start.")
    db.execute("""INSERT INTO provider_pay_terms(clinic_id, provider_id, basis, percent_bp, lab_bp, daily_p, effective_from, created_by)
                  VALUES (?,?,?,?,?,?,?,?)""", (cid, provider_id, basis, pct, lab, daily_p, start, user_id))
    audit.log(db, cid, user_id, "pay.terms", "provider", provider_id, f"{basis} {pct / 100}%")


def terms_on(db, cid, provider_id, day):
    return db.execute("""SELECT * FROM provider_pay_terms WHERE clinic_id = ? AND provider_id = ? AND effective_from <= ?
                         ORDER BY effective_from DESC, id DESC LIMIT 1""", (cid, provider_id, day)).fetchone()


def terms_history(db, cid, provider_id):
    return db.execute("SELECT * FROM provider_pay_terms WHERE clinic_id = ? AND provider_id = ? ORDER BY effective_from DESC, id DESC",
                      (cid, provider_id)).fetchall()


def providers(db, cid):
    rows = db.execute("SELECT * FROM providers WHERE clinic_id = ? ORDER BY active DESC, name", (cid,)).fetchall()
    today = datetime.date.today().isoformat()
    return [{"p": p, "terms": terms_on(db, cid, p["id"], today)} for p in rows]


# ---------------------------------------------------------------------------------------------------------- the figures
def matched(db, cid, patient_id=None):
    """Every patient's credits matched oldest first to their completed work.

    Returns a list of dicts: provider_id, patient_id, procedure_id, done_on, amount (minor units), kind ('payment' or 'discount')
    and day (when it counts). Credits left over after all work is matched are not returned: they are money on account."""
    where, args = "clinic_id = ?", [cid]
    if patient_id:
        where += " AND patient_id = ?"
        args.append(patient_id)
    charges, credits = {}, {}
    for r in db.execute(f"""SELECT id, patient_id, provider_id, fee_p, done_on FROM procedures WHERE {where} AND status = 'done' AND fee_p > 0
                            ORDER BY patient_id, done_on, id""", args):
        charges.setdefault(r["patient_id"], []).append({"id": r["id"], "provider_id": r["provider_id"], "left": r["fee_p"], "done_on": r["done_on"][:10]})
    for r in db.execute(f"""SELECT patient_id, amount_p, received_on AS day, id, 'payment' AS kind FROM payments
                            WHERE {where} AND voided = 0 AND purpose = 'treatment'
                            UNION ALL SELECT patient_id, -amount_p, adjusted_on, id, 'discount' FROM adjustments WHERE {where} AND amount_p < 0
                            ORDER BY 1, 3, 4""", args + args):
        credits.setdefault(r["patient_id"], []).append({"amount": r["amount_p"], "day": r["day"][:10], "kind": r["kind"]})
    for r in db.execute(f"""SELECT patient_id, amount_p, refunded_on FROM refunds WHERE {where} AND voided = 0 ORDER BY 1, 3""", args):
        back = r["amount_p"]                                  # a refund gives back the most recent money first
        for c in reversed(credits.get(r["patient_id"], [])):
            if c["kind"] != "payment" or c["day"] > r["refunded_on"][:10]:
                continue
            take = min(back, c["amount"])
            c["amount"] -= take
            back -= take
            if not back:
                break
    out = []
    for pid, queue in charges.items():
        i = 0
        for c in credits.get(pid, []):
            left = c["amount"]
            while left > 0 and i < len(queue):
                ch = queue[i]
                take = min(left, ch["left"])
                out.append({"provider_id": ch["provider_id"], "patient_id": pid, "procedure_id": ch["id"], "done_on": ch["done_on"],
                            "amount": take, "kind": c["kind"], "day": max(c["day"], ch["done_on"])})
                ch["left"] -= take
                left -= take
                if ch["left"] == 0:
                    i += 1
    return out


def figures(db, cid, provider_id, start, end):
    """What a provider earned the practice between two dates (both included), on each basis, and their lab bills."""
    gross = db.execute("""SELECT COALESCE(SUM(fee_p), 0) AS t, COUNT(*) AS n FROM procedures WHERE clinic_id = ? AND provider_id = ?
                          AND status = 'done' AND substr(done_on, 1, 10) BETWEEN ? AND ?""", (cid, provider_id, start, end)).fetchone()
    pids = [r[0] for r in db.execute("SELECT DISTINCT patient_id FROM procedures WHERE clinic_id = ? AND provider_id = ? AND status = 'done'",
                                     (cid, provider_id))]
    discounts = collected = 0
    for pid in pids:
        for m in matched(db, cid, pid):
            if m["provider_id"] != provider_id:
                continue
            if m["kind"] == "discount" and start <= m["done_on"] <= end:
                discounts += m["amount"]
            if m["kind"] == "payment" and start <= m["day"] <= end:
                collected += m["amount"]
    lab = db.execute("""SELECT COALESCE(SUM(cost_p), 0) AS t, COUNT(*) AS n FROM lab_cases WHERE clinic_id = ? AND provider_id = ?
                        AND status != 'cancelled' AND substr(COALESCE(sent_on, created_at), 1, 10) BETWEEN ? AND ?""",
                     (cid, provider_id, start, end)).fetchone()
    days = db.execute("""SELECT COUNT(DISTINCT substr(done_on, 1, 10)) FROM procedures WHERE clinic_id = ? AND provider_id = ?
                         AND status = 'done' AND substr(done_on, 1, 10) BETWEEN ? AND ?""", (cid, provider_id, start, end)).fetchone()[0]
    return {"gross_production": gross["t"], "procedures": gross["n"], "discounts": discounts, "net_production": gross["t"] - discounts,
            "collections": collected, "lab_fees": lab["t"], "lab_cases": lab["n"], "days": days}


def work_out(db, cid, provider_id, start, end):
    t = terms_on(db, cid, provider_id, end)
    if not t:
        raise PayError("Set this provider's pay terms first.")
    f = figures(db, cid, provider_id, start, end)
    base = f[t["basis"]]
    share = _pct(base, t["percent_bp"])
    lab_cut = _pct(f["lab_fees"], t["lab_bp"])
    daily = t["daily_p"] * f["days"]
    return {"terms": dict(t), "figures": f, "base": base, "share": share, "lab_deduction": lab_cut, "daily": daily,
            "total": share - lab_cut + daily}


# -------------------------------------------------------------------------------------------------------------- pay runs
def new_run(db, clinic, user_id, provider_id, start, end):
    cid = clinic["id"]
    start = _day(start, "Use a real date for the start of the period.")
    end = _day(end, "Use a real date for the end of the period.")
    if end < start:
        raise PayError("The period ends before it starts.")
    if db.execute("""SELECT 1 FROM pay_runs WHERE clinic_id = ? AND provider_id = ? AND status IN ('approved','paid')
                     AND period_from <= ? AND period_to >= ?""", (cid, provider_id, end, start)).fetchone():
        raise PayError("This provider already has an approved pay run covering some of those days.")
    w = work_out(db, cid, provider_id, start, end)
    rid = db.execute("""INSERT INTO pay_runs(clinic_id, provider_id, period_from, period_to, basis, percent_bp, lab_bp, base_p, share_p,
                        lab_fees_p, lab_deduction_p, daily_p, extras, total_p, figures, status, created_by)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?, '[]', ?, ?, 'draft', ?)""",
                     (cid, provider_id, start, end, w["terms"]["basis"], w["terms"]["percent_bp"], w["terms"]["lab_bp"], w["base"], w["share"],
                      w["figures"]["lab_fees"], w["lab_deduction"], w["daily"], w["total"], json.dumps(w["figures"]), user_id)).lastrowid
    audit.log(db, cid, user_id, "pay.run", "provider", provider_id, f"{start} to {end}")
    return rid


def run(db, cid, rid):
    return db.execute("""SELECT r.*, p.name AS provider_name FROM pay_runs r JOIN providers p ON p.id = r.provider_id
                         WHERE r.id = ? AND r.clinic_id = ?""", (rid, cid)).fetchone()


def runs(db, cid, provider_id=None):
    return db.execute("""SELECT r.*, p.name AS provider_name FROM pay_runs r JOIN providers p ON p.id = r.provider_id WHERE r.clinic_id = ?"""
                      + (" AND r.provider_id = ?" if provider_id else "") + " ORDER BY r.period_to DESC, r.id DESC",
                      (cid, provider_id) if provider_id else (cid,)).fetchall()


def extras(r):
    return json.loads(r["extras"] or "[]")


def add_extra(db, clinic, user_id, rid, label, amount_p):
    """A line added by hand: a bonus (positive) or an advance or other deduction (negative)."""
    r = run(db, clinic["id"], rid)
    if not r or r["status"] != "draft":
        raise PayError("Only a draft pay run can be changed.")
    label = (label or "").strip()[:120]
    if not label:
        raise PayError("Say what the extra line is for.")
    if not isinstance(amount_p, int) or amount_p == 0:
        raise PayError("Give an amount for the extra line.")
    lines = extras(r) + [{"label": label, "amount": amount_p}]
    db.execute("UPDATE pay_runs SET extras = ?, total_p = total_p + ? WHERE id = ?", (json.dumps(lines), amount_p, rid))
    audit.log(db, clinic["id"], user_id, "pay.extra", "provider", r["provider_id"], label)


def approve(db, clinic, user_id, rid):
    r = run(db, clinic["id"], rid)
    if not r or r["status"] != "draft":
        raise PayError("Only a draft pay run can be approved.")
    if db.execute("""SELECT 1 FROM pay_runs WHERE clinic_id = ? AND provider_id = ? AND status IN ('approved','paid') AND id != ?
                     AND period_from <= ? AND period_to >= ?""", (clinic["id"], r["provider_id"], rid, r["period_to"], r["period_from"])).fetchone():
        raise PayError("This provider already has an approved pay run covering some of those days.")
    db.execute("UPDATE pay_runs SET status = 'approved', approved_by = ?, approved_at = datetime('now') WHERE id = ?", (user_id, rid))
    audit.log(db, clinic["id"], user_id, "pay.approve", "provider", r["provider_id"])


def mark_paid(db, clinic, user_id, rid, paid_on, reference=None):
    r = run(db, clinic["id"], rid)
    if not r or r["status"] != "approved":
        raise PayError("Only an approved pay run can be marked paid.")
    day = _day(paid_on, "Use a real date for when it was paid.")
    db.execute("UPDATE pay_runs SET status = 'paid', paid_on = ?, paid_ref = ?, paid_by = ? WHERE id = ?",
               (day, (reference or "").strip()[:120] or None, user_id, rid))
    audit.log(db, clinic["id"], user_id, "pay.paid", "provider", r["provider_id"])


def cancel(db, clinic, user_id, rid, reason):
    r = run(db, clinic["id"], rid)
    if not r or r["status"] != "draft":
        raise PayError("Only a draft pay run can be cancelled.")
    reason = (reason or "").strip()[:300]
    if not reason:
        raise PayError("Say why the pay run is cancelled.")
    db.execute("UPDATE pay_runs SET status = 'cancelled', cancel_reason = ? WHERE id = ?", (reason, rid))
    audit.log(db, clinic["id"], user_id, "pay.cancel", "provider", r["provider_id"], reason)
