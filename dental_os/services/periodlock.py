"""Closing the books up to a date (version 0.43).

Once the accountant has done a month, nobody should be able to add, void or change money dated in it. A manager sets the date the
books are closed up to. After that, a payment, refund, discount or write-off dated on or before it, treatment marked done or voided
on or before it, or a void of anything dated on or before it, is refused, with the date in the message. The date can be moved
forward, or back by a manager with a reason; every change is kept. No web framework here."""
import datetime

from . import audit


CLOSED = "The books are closed for that date. Money and finished treatment dated on or before the closing date can't be added or changed. Date it later, or ask a manager."


class LockedError(ValueError):
    pass


def lock_date(db, cid):
    row = db.execute("SELECT locked_to FROM period_locks WHERE clinic_id = ? ORDER BY id DESC LIMIT 1", (cid,)).fetchone()
    return row["locked_to"] if row and row["locked_to"] else None


def check(db, clinic, day, error=None):
    """Raise LockedError (or `error`, the caller's own kind) when `day` (a date or 'YYYY-MM-DD...' text) falls on or before the
    lock date. `clinic` is the clinic row or its id."""
    locked = lock_date(db, clinic if isinstance(clinic, int) else clinic["id"])
    if not locked or not day:
        return
    d = day.isoformat() if isinstance(day, datetime.date) else str(day)[:10]
    if d <= locked:
        raise (error or LockedError)(CLOSED)


def set_lock(db, clinic, user_id, day, reason=None, today=None):
    cid = clinic["id"]
    day = (day or "").strip()
    if day:
        try:
            day = datetime.date.fromisoformat(day).isoformat()
        except ValueError:
            raise LockedError("Use a real date.")
        if day >= (today or datetime.date.today()).isoformat():
            raise LockedError("The books can only be closed up to yesterday or earlier.")
    current = lock_date(db, cid)
    reason = (reason or "").strip()[:300] or None
    if current and (not day or day < current) and not reason:
        raise LockedError("Opening closed days again needs a reason.")
    db.execute("INSERT INTO period_locks(clinic_id, locked_to, reason, created_by) VALUES (?,?,?,?)", (cid, day or None, reason, user_id))
    audit.log(db, cid, user_id, "books.lock", None, None, day or "open")


def history(db, cid):
    return db.execute("""SELECT l.*, u.display_name FROM period_locks l LEFT JOIN users u ON u.id = l.created_by
                         WHERE l.clinic_id = ? ORDER BY l.id DESC""", (cid,)).fetchall()
