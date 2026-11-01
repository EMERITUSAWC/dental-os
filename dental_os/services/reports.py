"""Numbers for the manager. The confirmation block answers: do patients who confirm by message miss fewer visits?"""
from . import billing


def _pct(part, whole):
    return round(100 * part / whole, 1) if whole else None


def pilot(db, clinic_id, start, end):
    """Appointments that were due between `start` and `end` (dates, inclusive) and are no longer open."""
    rows = db.execute(
        """SELECT status, break_reason, confirmation FROM appointments
           WHERE clinic_id = ? AND start_at >= ? AND start_at < ? AND status IN ('complete','broken')""",
        (clinic_id, f"{start} 00:00", f"{end} 23:59")).fetchall()
    booked_open = db.execute(
        """SELECT COUNT(*) FROM appointments WHERE clinic_id = ? AND start_at >= ? AND start_at < ?
           AND status = 'scheduled'""", (clinic_id, f"{start} 00:00", f"{end} 23:59")).fetchone()[0]

    def split(subset):
        done = sum(1 for r in subset if r["status"] == "complete")
        missed = sum(1 for r in subset if r["status"] == "broken" and r["break_reason"] == "missed")
        return {"done": done, "missed": missed, "rate": _pct(missed, done + missed)}

    confirmed = [r for r in rows if r["confirmation"] == "confirmed"]
    others = [r for r in rows if r["confirmation"] != "confirmed"]
    everyone = split(rows)
    cancelled = sum(1 for r in rows if r["status"] == "broken" and r["break_reason"] == "cancelled")
    messages = db.execute(
        "SELECT COUNT(*) FROM messages WHERE clinic_id = ? AND date(created_at) BETWEEN ? AND ?",
        (clinic_id, str(start), str(end))).fetchone()[0]
    return {"closed": len(rows), "still_booked": booked_open, "cancelled": cancelled, "messages": messages,
            "all": everyone, "confirmed": {**split(confirmed), "n": len(confirmed)},
            "not_confirmed": {**split(others), "n": len(others)},
            "confirmed_share": _pct(len(confirmed), len(rows))}


def money(db, clinic_id, start, end):
    charges = db.execute(
        "SELECT COALESCE(SUM(fee_p),0) FROM procedures WHERE clinic_id=? AND status='done' AND done_on BETWEEN ? AND ?",
        (clinic_id, str(start), str(end))).fetchone()[0]
    adjust = db.execute(
        "SELECT COALESCE(SUM(amount_p),0) FROM adjustments WHERE clinic_id=? AND adjusted_on BETWEEN ? AND ?",
        (clinic_id, str(start), str(end))).fetchone()[0]
    by_method = {m: 0 for m in billing.METHODS}
    for r in db.execute(
            """SELECT method, SUM(amount_p) AS total FROM payments
               WHERE clinic_id=? AND voided=0 AND received_on BETWEEN ? AND ? GROUP BY method""",
            (clinic_id, str(start), str(end))):
        by_method[r["method"]] = r["total"]
    from . import refunds
    back = refunds.by_method(db, clinic_id, start, end)
    for m, v in back.items():
        by_method[m] -= v
    new_patients = db.execute(
        "SELECT COUNT(*) FROM patients WHERE clinic_id=? AND date(created_at) BETWEEN ? AND ?",
        (clinic_id, str(start), str(end))).fetchone()[0]
    return {"charges": charges, "adjustments": adjust, "by_method": by_method,
            "collected": sum(by_method.values()), "refunded": sum(back.values()), "new_patients": new_patients}
