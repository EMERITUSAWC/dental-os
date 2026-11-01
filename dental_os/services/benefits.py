"""How much a payer is expected to pay for each line of treatment, and what the patient pays.

For one cover and one line, in this order:
1. The cover must be in force on the day, and past any waiting period for that kind of treatment.
2. The payer pays nothing for a treatment it has already paid for as often as its limit allows (for example 2 in 12 months).
3. The allowed amount is the payer's own price, or the clinic's price if the payer has none.
4. The patient pays the excess (deductible) first, once per benefit year. Check-ups, cleaning and X-rays skip it unless the cover says not.
5. The payer pays its share for that kind of treatment (or the cover's general share) of what is left.
6. Never more than what is left of the yearly maximum, or for orthodontics of the lifetime orthodontic maximum.
7. A contracted payer's difference between the clinic's price and the allowed amount is written off.

A second payer pays after the first: the lesser of what it would normally pay and what the patient still owes after the first payer.
Everything is an estimate. The payer decides."""
from datetime import date, timedelta

from .timeutil import parse_date

PREVENTIVE = ("Examination", "Preventive", "Imaging")
ORTHO = ("Orthodontics",)
OPEN = ("rejected", "cancelled")


def _add_months(d, months):
    y, m = divmod(d.month - 1 + months, 12)
    y += d.year
    m += 1
    last = [31, 29 if (y % 4 == 0 and (y % 100 != 0 or y % 400 == 0)) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m - 1]
    return date(y, m, min(d.day, last))


def benefit_year(cover, day):
    m = cover["year_start_month"] if "year_start_month" in cover.keys() and cover["year_start_month"] else 1
    start = date(day.year if day.month >= m else day.year - 1, m, 1)
    return start, _add_months(start, 12) - timedelta(days=1)


def in_force(cover, day):
    iso = day.isoformat()
    return bool(cover["active"]) and (not cover["valid_from"] or cover["valid_from"] <= iso) and (not cover["valid_to"] or cover["valid_to"] >= iso)


def _claims_in(db, cid, cover_id, start, end):
    return ("""FROM claims c WHERE c.clinic_id = ? AND c.cover_id = ? AND c.kind = 'claim' AND c.status NOT IN ('rejected','cancelled')
               AND date(c.created_at) BETWEEN ? AND ?""", (cid, cover_id, start.isoformat(), end.isoformat()))


def max_used(db, cid, cover, day):
    start, end = benefit_year(cover, day)
    sql, args = _claims_in(db, cid, cover["id"], start, end)
    return db.execute("SELECT COALESCE(SUM(CASE WHEN c.paid_p > 0 THEN c.paid_p ELSE c.estimate_p END), 0) " + sql, args).fetchone()[0]


def deductible_used(db, cid, cover, day):
    start, end = benefit_year(cover, day)
    return db.execute("""SELECT COALESCE(SUM(i.deductible_p), 0) FROM claim_items i JOIN claims c ON c.id = i.claim_id
                         WHERE c.clinic_id = ? AND c.cover_id = ? AND c.kind = 'claim' AND c.status NOT IN ('rejected','cancelled')
                         AND date(c.created_at) BETWEEN ? AND ?""", (cid, cover["id"], start.isoformat(), end.isoformat())).fetchone()[0]


def ortho_used(db, cid, cover):
    return db.execute("""SELECT COALESCE(SUM(i.estimate_p), 0) FROM claim_items i JOIN claims c ON c.id = i.claim_id
                         JOIN procedures pr ON pr.id = i.procedure_id JOIN procedure_codes pc ON pc.id = pr.code_id
                         WHERE c.clinic_id = ? AND c.cover_id = ? AND c.kind = 'claim' AND c.status NOT IN ('rejected','cancelled')
                         AND pc.category IN ('Orthodontics')""", (cid, cover["id"])).fetchone()[0]


def pools(db, cid, cover, day):
    """What is left to share between the lines of one estimate or claim."""
    deductible = cover["deductible_p"] if "deductible_p" in cover.keys() else 0
    ortho = cover["ortho_max_p"] if "ortho_max_p" in cover.keys() else None
    return {
        "max_left": None if cover["annual_max_p"] is None else max(cover["annual_max_p"] - max_used(db, cid, cover, day), 0),
        "ded_left": max((deductible or 0) - deductible_used(db, cid, cover, day), 0),
        "ortho_left": None if ortho is None else max(ortho - ortho_used(db, cid, cover), 0),
        "freq": {},
    }


def category_rules(db, cid, cover_id):
    return {r["category"]: r for r in db.execute("SELECT * FROM cover_benefits WHERE clinic_id = ? AND cover_id = ?", (cid, cover_id))}


def limits(db, cid, cover_id):
    return {r["code_id"]: r for r in db.execute("SELECT * FROM cover_limits WHERE clinic_id = ? AND cover_id = ?", (cid, cover_id))}


def _waiting_start(cover):
    raw = (cover["waiting_from"] if "waiting_from" in cover.keys() else None) or cover["valid_from"] or (cover["created_at"] or "")[:10]
    try:
        return parse_date(raw)
    except (TypeError, ValueError):
        return None


def _times_done(db, cid, pid, code_id, day, months, exclude):
    since = _add_months(day, -months)
    q = ("SELECT COUNT(*) FROM procedures WHERE clinic_id = ? AND patient_id = ? AND code_id = ? AND status = 'done' "
         "AND done_on > ? AND done_on <= ?")
    args = [cid, pid, code_id, since.isoformat(), day.isoformat()]
    if exclude:
        q += f" AND id NOT IN ({','.join('?' * len(exclude))})"
        args += list(exclude)
    return db.execute(q, args).fetchone()[0]


def line(db, clinic, cover, code, fee_p, day, pool, pid, exclude=(), rules=None, lims=None, contracted=None):
    """One line for one cover. code: a procedure_codes row. Changes pool as it goes."""
    cid = clinic["id"]
    notes = []
    from .insurance import allowed_fee
    allowed = allowed_fee(db, cid, cover["payer_id"], code["id"], fee_p)
    if contracted is None:
        contracted = bool(db.execute("SELECT contracted FROM payers WHERE id = ?", (cover["payer_id"],)).fetchone()["contracted"])
    writeoff = max(fee_p - allowed, 0) if contracted else 0
    base = {"allowed_p": allowed, "pct": 0, "deductible_p": 0, "payer_p": 0, "writeoff_p": writeoff, "notes": notes, "in_force": True}
    if not in_force(cover, day):
        base.update(writeoff_p=0, in_force=False, patient_p=fee_p)
        notes.append("Cover not in force on that day.")
        return base
    from . import ghclaims
    if not ghclaims.covers_code(db, cid, cover["payer_id"], code["id"]):        # 0.52: the payer keeps a list, and this is not on it
        notes.append("Not covered by this payer.")
        base.update(writeoff_p=0, patient_p=fee_p)
        return base
    rules = category_rules(db, cid, cover["id"]) if rules is None else rules
    lims = limits(db, cid, cover["id"]) if lims is None else lims
    rule = rules.get(code["category"])
    pct = rule["pct"] if rule is not None else cover["coverage_pct"]
    base["pct"] = pct
    if rule is not None and rule["waiting_months"]:
        start = _waiting_start(cover)
        if start and day < _add_months(start, rule["waiting_months"]):
            notes.append("Waiting period: covered from %s." % _add_months(start, rule["waiting_months"]).isoformat())
            base["patient_p"] = fee_p - writeoff
            return base
    lim = lims.get(code["id"])
    if lim is not None:
        done = _times_done(db, cid, pid, code["id"], day, lim["months"], exclude) + pool["freq"].get(code["id"], 0)
        pool["freq"][code["id"]] = pool["freq"].get(code["id"], 0) + 1
        if done >= lim["times"]:
            notes.append("Limit reached: %d in %d months." % (lim["times"], lim["months"]))
            base["patient_p"] = fee_p - writeoff
            return base
    payable = allowed
    skip_ded = code["category"] in PREVENTIVE and not (cover["deductible_on_preventive"] if "deductible_on_preventive" in cover.keys() else 0)
    if pool["ded_left"] and not skip_ded:
        ded = min(pool["ded_left"], payable)
        pool["ded_left"] -= ded
        payable -= ded
        base["deductible_p"] = ded
        if ded:
            notes.append("Excess applied.")
    payer = payable * pct // 100
    if code["category"] in ORTHO and pool["ortho_left"] is not None:
        if payer > pool["ortho_left"]:
            notes.append("Orthodontic lifetime limit reached.")
        payer = min(payer, pool["ortho_left"])
        pool["ortho_left"] -= payer
    if pool["max_left"] is not None:
        if payer > pool["max_left"]:
            notes.append("Yearly maximum reached.")
        payer = min(payer, pool["max_left"])
        pool["max_left"] -= payer
    base["payer_p"] = payer
    base["patient_p"] = max(fee_p - payer - writeoff, 0)
    return base


def second(db, clinic, cover2, code, fee_p, day, pool2, pid, first, exclude=()):
    """The second payer, after the first payer's result for the same line."""
    normal = line(db, clinic, cover2, code, fee_p, day, pool2, pid, exclude)
    owed = first["patient_p"]
    pays = min(normal["payer_p"], owed)
    if pool2["max_left"] is not None:
        pool2["max_left"] += normal["payer_p"] - pays          # give back what it did not need
    if code["category"] in ORTHO and pool2["ortho_left"] is not None:
        pool2["ortho_left"] += normal["payer_p"] - pays
    normal["payer_p"] = pays
    normal["writeoff_p"] = 0
    normal["patient_p"] = owed - pays
    return normal


def covers_in_order(db, cid, pid, day):
    rows = db.execute("""SELECT pc.*, p.name AS payer_name, p.contracted FROM patient_cover pc JOIN payers p ON p.id = pc.payer_id
                         WHERE pc.clinic_id = ? AND pc.patient_id = ? AND pc.active = 1 ORDER BY pc.cover_rank, pc.id""", (cid, pid)).fetchall()
    return [c for c in rows if in_force(c, day)]


def estimate_plan(db, clinic, pid, lines, day):
    """lines: [{'code_id', 'fee_p'}]. Uses the first two covers in force. Returns (covers, [result per line])."""
    cid = clinic["id"]
    covers = covers_in_order(db, cid, pid, day)[:2]
    if not covers:
        return [], []
    pool1 = pools(db, cid, covers[0], day)
    pool2 = pools(db, cid, covers[1], day) if len(covers) > 1 else None
    out = []
    for ln in lines:
        code = db.execute("SELECT * FROM procedure_codes WHERE id = ? AND clinic_id = ?", (ln["code_id"], cid)).fetchone()
        first = line(db, clinic, covers[0], code, ln["fee_p"], day, pool1, pid)
        sec = second(db, clinic, covers[1], code, ln["fee_p"], day, pool2, pid, first) if pool2 is not None else None
        out.append({"first": first, "second": sec, "patient_p": sec["patient_p"] if sec else first["patient_p"],
                    "notes": first["notes"] + ((sec["notes"]) if sec else [])})
    return covers, out


def summary(db, clinic, cover, day):
    """For the cover's card: maximum, excess and orthodontic limit used and left."""
    cid = clinic["id"]
    used = max_used(db, cid, cover, day)
    ded_used = deductible_used(db, cid, cover, day)
    start, end = benefit_year(cover, day)
    return {"year_start": start, "year_end": end, "max_used": used,
            "max_left": None if cover["annual_max_p"] is None else max(cover["annual_max_p"] - used, 0),
            "ded_used": min(ded_used, cover["deductible_p"] or 0), "ded_left": max((cover["deductible_p"] or 0) - ded_used, 0),
            "ortho_left": None if cover["ortho_max_p"] is None else max(cover["ortho_max_p"] - ortho_used(db, cid, cover), 0)}
