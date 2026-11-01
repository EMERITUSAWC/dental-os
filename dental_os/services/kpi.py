"""The numbers a practice owner looks at: production, collections, acceptance, no-shows, new patients, recall health,
chair use and who owes what. One clinic at a time. Money is in minor units of the clinic's own currency, so figures
from clinics with different currencies are never added together.

Definitions (kept here so every screen and the API agree):
- Gross production: the fees of treatment marked done in the period.
- Net production: gross production plus adjustments (discounts and write-offs are negative).
- Collections: every payment taken in the period that was not voided, including insurers and membership fees.
- Collection rate: treatment payments taken divided by net production. Over 100% means old debts were paid down.
- Plan acceptance: of the treatment plans presented in the period, how many (and how much money) are now accepted.
- No-show rate: visits marked missed divided by visits done plus visits missed. Cancellations are counted apart.
- Chair use: booked minutes divided by open minutes, counting working days and active chairs.
- Recall health: of patients on recall, the share not overdue.
"""
from datetime import date, timedelta

from . import ageing
from .timeutil import hhmm_to_minutes, now_local

MAX_DAYS = 400


def pct(part, whole):
    return round(100 * part / whole, 1) if whole else None


def change(now, before):
    """Percent change from `before` to `now`. None when there is nothing to compare with."""
    if before in (None, 0) or now is None:
        return None
    return round(100 * (now - before) / abs(before), 1)


def presets(today):
    first = today.replace(day=1)
    last_month_end = first - timedelta(days=1)
    q_start = date(today.year, 3 * ((today.month - 1) // 3) + 1, 1)
    return [("This month", first, today), ("Last month", last_month_end.replace(day=1), last_month_end),
            ("Last 30 days", today - timedelta(days=29), today), ("This quarter", q_start, today),
            ("This year", date(today.year, 1, 1), today)]


def clamp_range(start, end):
    if end < start:
        start, end = end, start
    if (end - start).days >= MAX_DAYS:
        start = end - timedelta(days=MAX_DAYS - 1)
    return start, end


def previous_range(start, end):
    n = (end - start).days + 1
    return start - timedelta(days=n), start - timedelta(days=1)


def _one(db, q, *a):
    return db.execute(q, a).fetchone()[0] or 0


def _production(db, cid, s, e, provider_id=None):
    extra, args = ("AND provider_id = ?", [provider_id]) if provider_id else ("", [])
    return _one(db, f"SELECT SUM(fee_p) FROM procedures WHERE clinic_id=? AND status='done' AND done_on BETWEEN ? AND ? {extra}",
                cid, s, e, *args)


def chair_use(db, clinic, start, end, provider_id=None):
    open_m, close_m = hhmm_to_minutes(clinic["open_time"]), hhmm_to_minutes(clinic["close_time"])
    days = {int(d) for d in clinic["working_days"].split(",") if d.strip()}
    n_days = sum(1 for i in range((end - start).days + 1) if (start + timedelta(days=i)).isoweekday() in days)
    if provider_id:
        units = 1
    else:
        units = _one(db, "SELECT COUNT(*) FROM chairs WHERE clinic_id=? AND active=1", clinic["id"])
    available = n_days * units * max(close_m - open_m, 0)
    extra, args = ("AND provider_id = ?", [provider_id]) if provider_id else ("", [])
    booked = _one(db, f"""SELECT SUM(duration_min) FROM appointments WHERE clinic_id=? AND status IN ('scheduled','complete')
                          AND start_at >= ? AND start_at < ? {extra}""",
                  clinic["id"], f"{start} 00:00", f"{end + timedelta(days=1)} 00:00", *args)
    return {"available_min": available, "booked_min": booked, "rate": min(pct(booked, available), 100.0) if available else None}


def recall_health(db, cid, today):
    t = today.isoformat()
    live = "status != 'disabled' AND (snoozed_until IS NULL OR snoozed_until < ?)"
    total = _one(db, f"SELECT COUNT(*) FROM recalls WHERE clinic_id=? AND {live}", cid, t)
    late = _one(db, f"SELECT COUNT(*) FROM recalls WHERE clinic_id=? AND {live} AND due_on < ?", cid, t, t)
    late30 = _one(db, f"SELECT COUNT(*) FROM recalls WHERE clinic_id=? AND {live} AND due_on < ?", cid, t,
                  (today - timedelta(days=30)).isoformat())
    late90 = _one(db, f"SELECT COUNT(*) FROM recalls WHERE clinic_id=? AND {live} AND due_on < ?", cid, t,
                  (today - timedelta(days=90)).isoformat())
    return {"total": total, "overdue": late, "overdue_30": late30, "overdue_90": late90,
            "on_time_rate": pct(total - late, total)}


def plan_acceptance(db, cid, s, e):
    rows = db.execute(
        """SELECT p.status, COALESCE((SELECT SUM(fee_p) FROM plan_items i WHERE i.plan_id = p.id AND i.clinic_id = p.clinic_id AND i.removed = 0), 0) AS value
           FROM treatment_plans p WHERE p.clinic_id = ? AND p.presented_on BETWEEN ? AND ? AND p.status != 'draft'""",
        (cid, s, e)).fetchall()
    total = len(rows)
    acc = [r for r in rows if r["status"] == "accepted"]
    value, acc_value = sum(r["value"] for r in rows), sum(r["value"] for r in acc)
    return {"presented": total, "accepted": len(acc), "declined": sum(1 for r in rows if r["status"] == "declined"),
            "waiting": sum(1 for r in rows if r["status"] == "presented"),
            "presented_p": value, "accepted_p": acc_value,
            "count_rate": pct(len(acc), total), "value_rate": pct(acc_value, value)}


def visits(db, cid, s, e):
    rows = db.execute(
        """SELECT status, break_reason, COUNT(*) AS n FROM appointments WHERE clinic_id=? AND start_at >= ? AND start_at < ?
           AND status IN ('complete','broken') GROUP BY status, break_reason""",
        (cid, f"{s} 00:00", f"{date.fromisoformat(e) + timedelta(days=1)} 00:00")).fetchall()
    done = sum(r["n"] for r in rows if r["status"] == "complete")
    missed = sum(r["n"] for r in rows if r["break_reason"] == "missed")
    cancelled = sum(r["n"] for r in rows if r["break_reason"] == "cancelled")
    return {"done": done, "missed": missed, "cancelled": cancelled,
            "no_show_rate": pct(missed, done + missed), "cancel_rate": pct(cancelled, done + missed + cancelled)}


def period(db, clinic, start, end):
    """Every headline figure for one stretch of dates."""
    cid, s, e = clinic["id"], str(start), str(end)
    gross = _production(db, cid, s, e)
    adjust = _one(db, "SELECT SUM(amount_p) FROM adjustments WHERE clinic_id=? AND adjusted_on BETWEEN ? AND ?", cid, s, e)
    net = gross + adjust
    collected = _one(db, "SELECT SUM(amount_p) FROM payments WHERE clinic_id=? AND voided=0 AND received_on BETWEEN ? AND ?", cid, s, e)
    treat = _one(db, """SELECT SUM(amount_p) FROM payments WHERE clinic_id=? AND voided=0 AND purpose='treatment'
                        AND received_on BETWEEN ? AND ?""", cid, s, e)
    v = visits(db, cid, s, e)
    new_patients = _one(db, "SELECT COUNT(*) FROM patients WHERE clinic_id=? AND date(created_at) BETWEEN ? AND ?", cid, s, e)
    lab_spend = _one(db, """SELECT SUM(cost_p) FROM lab_cases WHERE clinic_id=? AND status != 'cancelled' AND sent_on BETWEEN ? AND ?""",
                     cid, s, e)
    return {"gross_p": gross, "adjust_p": adjust, "net_p": net, "collected_p": collected, "collected_treatment_p": treat,
            "collection_rate": pct(treat, net) if net > 0 else None, "visits": v,
            "per_visit_p": (gross // v["done"]) if v["done"] else None, "new_patients": new_patients,
            "plans": plan_acceptance(db, cid, s, e), "chairs": chair_use(db, clinic, start, end), "lab_spend_p": lab_spend}


HEADLINES = ("gross_p", "net_p", "collected_p", "new_patients")


def dashboard(db, clinic, start, end, today=None):
    """The full KPI page: this period, the one before it, by dentist, over time, and today's snapshot."""
    start, end = clamp_range(start, end)
    today = today or now_local(clinic["timezone"]).date()
    cur = period(db, clinic, start, end)
    p_start, p_end = previous_range(start, end)
    prev = period(db, clinic, p_start, p_end)
    deltas = {
        "gross_p": change(cur["gross_p"], prev["gross_p"]), "net_p": change(cur["net_p"], prev["net_p"]),
        "collected_p": change(cur["collected_p"], prev["collected_p"]),
        "new_patients": change(cur["new_patients"], prev["new_patients"]),
        "visits": change(cur["visits"]["done"], prev["visits"]["done"]),
    }
    return {"start": start, "end": end, "prev_start": p_start, "prev_end": p_end, "cur": cur, "prev": prev, "deltas": deltas,
            "dentists": by_dentist(db, clinic, start, end), "series": series(db, clinic["id"], start, end),
            "recall": recall_health(db, clinic["id"], today), "ageing": ageing_totals(db, clinic),
            "top_procedures": top_procedures(db, clinic["id"], start, end)}


def by_dentist(db, clinic, start, end):
    cid, s, e = clinic["id"], str(start), str(end)
    out = []
    for p in db.execute("SELECT id, name FROM providers WHERE clinic_id=? ORDER BY active DESC, id", (cid,)):
        gross = _production(db, cid, s, e, p["id"])
        done = _one(db, """SELECT COUNT(*) FROM appointments WHERE clinic_id=? AND provider_id=? AND status='complete'
                           AND start_at >= ? AND start_at < ?""", cid, p["id"], f"{s} 00:00", f"{end + timedelta(days=1)} 00:00")
        missed = _one(db, """SELECT COUNT(*) FROM appointments WHERE clinic_id=? AND provider_id=? AND status='broken' AND break_reason='missed'
                             AND start_at >= ? AND start_at < ?""", cid, p["id"], f"{s} 00:00", f"{end + timedelta(days=1)} 00:00")
        use = chair_use(db, clinic, start, end, p["id"])
        if gross or done or missed or use["booked_min"]:
            out.append({"id": p["id"], "name": p["name"], "gross_p": gross, "visits": done, "missed": missed,
                        "no_show_rate": pct(missed, done + missed), "use": use["rate"],
                        "per_visit_p": gross // done if done else None})
    return out


def series(db, cid, start, end):
    """Production and collections over time: by day for up to ten weeks, otherwise by month."""
    by_month = (end - start).days > 70
    cut = 7 if by_month else 10
    prod = {r[0]: r[1] for r in db.execute(
        f"SELECT substr(done_on,1,{cut}), SUM(fee_p) FROM procedures WHERE clinic_id=? AND status='done' AND done_on BETWEEN ? AND ? GROUP BY 1",
        (cid, str(start), str(end)))}
    coll = {r[0]: r[1] for r in db.execute(
        f"SELECT substr(received_on,1,{cut}), SUM(amount_p) FROM payments WHERE clinic_id=? AND voided=0 AND received_on BETWEEN ? AND ? GROUP BY 1",
        (cid, str(start), str(end)))}
    labels, d = [], start
    while d <= end:
        key = d.isoformat()[:cut]
        if not labels or labels[-1] != key:
            labels.append(key)
        d += timedelta(days=1)
    rows = [{"label": k, "production_p": prod.get(k, 0), "collected_p": coll.get(k, 0)} for k in labels]
    peak = max([r["production_p"] for r in rows] + [r["collected_p"] for r in rows] + [1])
    for r in rows:
        r["production_w"] = round(100 * r["production_p"] / peak)
        r["collected_w"] = round(100 * r["collected_p"] / peak)
    return {"by": "month" if by_month else "day", "rows": rows}


def top_procedures(db, cid, start, end, limit=8):
    return db.execute(
        """SELECT c.code, c.name, COUNT(*) AS n, SUM(pr.fee_p) AS total_p FROM procedures pr JOIN procedure_codes c ON c.id = pr.code_id
           WHERE pr.clinic_id=? AND pr.status='done' AND pr.done_on BETWEEN ? AND ? GROUP BY c.id ORDER BY total_p DESC, n DESC LIMIT ?""",
        (cid, str(start), str(end), limit)).fetchall()


def ageing_totals(db, clinic):
    r = ageing.report(db, clinic)
    return {"grand_p": r["grand"], "totals": r["totals"], "buckets": r["buckets"], "insurers_p": r["insurers"]}


def csv_rows(db, clinic, start, end):
    """The headline figures as plain rows for a spreadsheet."""
    d = dashboard(db, clinic, start, end)
    c = d["cur"]
    rows = [("Measure", "Value", "Unit"),
            ("From", str(d["start"]), "date"), ("To", str(d["end"]), "date"),
            ("Gross production", c["gross_p"], "minor units"), ("Adjustments", c["adjust_p"], "minor units"),
            ("Net production", c["net_p"], "minor units"), ("Collections", c["collected_p"], "minor units"),
            ("Collection rate", c["collection_rate"], "percent"), ("Visits done", c["visits"]["done"], "count"),
            ("Visits missed", c["visits"]["missed"], "count"), ("Visits cancelled", c["visits"]["cancelled"], "count"),
            ("No-show rate", c["visits"]["no_show_rate"], "percent"), ("New patients", c["new_patients"], "count"),
            ("Plans presented", c["plans"]["presented"], "count"), ("Plans accepted", c["plans"]["accepted"], "count"),
            ("Plan acceptance by value", c["plans"]["value_rate"], "percent"), ("Chair use", c["chairs"]["rate"], "percent"),
            ("Recalls not overdue", d["recall"]["on_time_rate"], "percent"), ("Owed by patients", d["ageing"]["grand_p"], "minor units")]
    return rows
