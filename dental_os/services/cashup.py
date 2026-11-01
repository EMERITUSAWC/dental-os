"""End-of-day cash-up. The system works out what should be in the till and the other accounts for a day, by way of paying; someone
counts what is really there; the differences are recorded with a reason, and the day is closed. A payment entered for a day after it
was closed is shown, so nothing slips by. A manager can reopen a day, and the earlier close is kept."""
import json

from . import audit
from .timeutil import now_local, parse_date


class CashUpError(Exception):
    pass


def expected(db, cid, day):
    rows = db.execute("""SELECT method, COALESCE(SUM(amount_p), 0) AS total, COUNT(*) AS n FROM payments
                         WHERE clinic_id = ? AND received_on = ? AND voided = 0 GROUP BY method""", (cid, day)).fetchall()
    out = {r["method"]: {"total": r["total"], "n": r["n"]} for r in rows}
    from . import refunds
    for m, v in refunds.by_method(db, cid, day, day).items():          # money paid back out of the till or the account
        out.setdefault(m, {"total": 0, "n": 0})
        out[m]["total"] -= v
    return out


def current(db, cid, day):
    return db.execute("SELECT * FROM day_closes WHERE clinic_id = ? AND day = ? AND status = 'closed' ORDER BY id DESC LIMIT 1",
                      (cid, day)).fetchone()


def history(db, cid, day):
    return db.execute("""SELECT d.*, u.display_name AS by_name FROM day_closes d LEFT JOIN users u ON u.id = d.closed_by
                         WHERE d.clinic_id = ? AND d.day = ? ORDER BY d.id DESC""", (cid, day)).fetchall()


def close(db, clinic, user_id, day, counted, note=None, methods=()):
    """counted: {method: minor units counted}. Insurance is not counted at the desk."""
    cid = clinic["id"]
    try:
        d = parse_date(day)
    except (TypeError, ValueError):
        raise CashUpError("Use a date like 2026-11-30.")
    if d > now_local(clinic["timezone"]).date():
        raise CashUpError("A day in the future cannot be closed.")
    if current(db, cid, d.isoformat()):
        raise CashUpError("That day is already closed. A manager can reopen it.")
    exp = expected(db, cid, d.isoformat())
    lines, off = {}, False
    for m in methods:
        if m == "insurance":
            continue
        e = exp.get(m, {}).get("total", 0)
        c = counted.get(m)
        if c is None:
            raise CashUpError("Enter what was counted for each way of paying. Enter 0 if there was none.")
        if c < 0:
            raise CashUpError("A count cannot be negative.")
        lines[m] = {"expected": e, "counted": c, "difference": c - e}
        off = off or c != e
    note = (note or "").strip()[:500] or None
    if off and not note:
        raise CashUpError("The count does not match. Write what you think happened before closing the day.")
    cl_id = db.execute("""INSERT INTO day_closes(clinic_id, day, lines, note, status, closed_by, closed_at)
                          VALUES (?,?,?,?, 'closed', ?, datetime('now'))""", (cid, d.isoformat(), json.dumps(lines, sort_keys=True), note, user_id)).lastrowid
    audit.log(db, cid, user_id, "cashup.close", "day_close", cl_id, d.isoformat() + (" with a difference" if off else ""))
    return cl_id


def reopen(db, clinic, user_id, day, reason):
    cid = clinic["id"]
    row = current(db, cid, day)
    if not row:
        raise CashUpError("That day is not closed.")
    reason = (reason or "").strip()[:300]
    if not reason:
        raise CashUpError("Say why the day is being reopened.")
    db.execute("UPDATE day_closes SET status = 'reopened', reopened_by = ?, reopened_at = datetime('now'), reopen_reason = ? WHERE id = ?",
               (user_id, reason, row["id"]))
    audit.log(db, cid, user_id, "cashup.reopen", "day_close", row["id"], reason[:80])


def lines_of(row):
    return json.loads(row["lines"]) if row else {}


def after_close(db, cid, day, row):
    """Payments for the day entered (or voided) after it was closed."""
    if not row:
        return []
    return db.execute("""SELECT p.*, pa.first_name, pa.last_name FROM payments p JOIN patients pa ON pa.id = p.patient_id
                         WHERE p.clinic_id = ? AND p.received_on = ? AND p.created_at > ? ORDER BY p.id""",
                      (cid, day, row["closed_at"])).fetchall()


def recent(db, cid, limit=30):
    return db.execute("""SELECT d.*, u.display_name AS by_name FROM day_closes d LEFT JOIN users u ON u.id = d.closed_by
                         WHERE d.clinic_id = ? AND d.status = 'closed' ORDER BY d.day DESC LIMIT ?""", (cid, limit)).fetchall()
