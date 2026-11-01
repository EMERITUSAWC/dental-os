"""Fee schedules (version 0.39): more than one price list.

The clinic's standard price for each treatment stays on the procedure list. A fee schedule is another price list (staff and family,
a company that sends its workers, students, a partner clinic's referrals, senior citizens) that a patient is put on. Its price for a
treatment is the one written in the schedule, or, where none is written, the standard price less the schedule's percentage off.

When treatment is planned for a patient on a schedule, the schedule's price is used. Prices already on a patient's treatment or plan
never change when a schedule does. Insurers' own agreed fees (payer fees) and membership discounts work as before, on top.
Nothing is deleted: a schedule no longer used is switched off. No web framework here."""
from decimal import ROUND_HALF_UP, Decimal

from . import audit


class FeeError(ValueError):
    pass


def _bp(text):
    try:
        v = Decimal(str(text or "0").strip().rstrip("%") or "0")
    except Exception:
        raise FeeError("The percentage off is a number from 0 to 100.")
    if not v.is_finite() or v < 0 or v > 100 or v != v.quantize(Decimal("0.01")):
        raise FeeError("The percentage off is a number from 0 to 100.")
    return int(v * 100)


def save(db, clinic, user_id, name, percent_off, notes=None, sid=None, active=True):
    cid = clinic["id"]
    name = (name or "").strip()[:80]
    if not name:
        raise FeeError("Give the fee schedule a name.")
    bp = _bp(percent_off)
    clash = db.execute("SELECT id FROM fee_schedules WHERE clinic_id = ? AND LOWER(name) = LOWER(?)", (cid, name)).fetchone()
    if clash and clash["id"] != sid:
        raise FeeError("There is already a fee schedule with that name.")
    notes = (notes or "").strip()[:500] or None
    if sid:
        if not get(db, cid, sid):
            raise FeeError("That fee schedule was not found.")
        db.execute("UPDATE fee_schedules SET name = ?, discount_bp = ?, notes = ?, active = ? WHERE id = ?", (name, bp, notes, 1 if active else 0, sid))
    else:
        sid = db.execute("INSERT INTO fee_schedules(clinic_id, name, discount_bp, notes, active, created_by) VALUES (?,?,?,?,?,?)",
                         (cid, name, bp, notes, 1 if active else 0, user_id)).lastrowid
    audit.log(db, cid, user_id, "fees.schedule", None, None, name)
    return sid


def get(db, cid, sid):
    return db.execute("SELECT * FROM fee_schedules WHERE id = ? AND clinic_id = ?", (sid, cid)).fetchone()


def schedules(db, cid, active_only=False):
    return db.execute("""SELECT s.*, (SELECT COUNT(*) FROM patients p WHERE p.fee_schedule_id = s.id AND p.status = 'active') AS patients,
                         (SELECT COUNT(*) FROM fee_schedule_items i WHERE i.schedule_id = s.id) AS prices
                         FROM fee_schedules s WHERE s.clinic_id = ?""" + (" AND s.active = 1" if active_only else "") + " ORDER BY s.name", (cid,)).fetchall()


def price(standard_p, discount_bp, own_p=None):
    if own_p is not None:
        return own_p
    return int((Decimal(standard_p) * Decimal(10000 - discount_bp) / Decimal(10000)).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def table(db, cid, sid):
    s = get(db, cid, sid)
    rows = db.execute("""SELECT c.id, c.code, c.name, c.category, c.fee_p AS standard_p, i.fee_p AS own_p FROM procedure_codes c
                         LEFT JOIN fee_schedule_items i ON i.code_id = c.id AND i.schedule_id = ? WHERE c.clinic_id = ? AND c.active = 1
                         ORDER BY c.category, c.name""", (sid, cid)).fetchall()
    return [{"r": r, "price": price(r["standard_p"], s["discount_bp"], r["own_p"])} for r in rows]


def set_prices(db, clinic, user_id, sid, prices):
    """prices: {code_id: minor units, or None to go back to the standard price less the percentage}."""
    cid = clinic["id"]
    if not get(db, cid, sid):
        raise FeeError("That fee schedule was not found.")
    valid = {r["id"] for r in db.execute("SELECT id FROM procedure_codes WHERE clinic_id = ?", (cid,))}
    for code_id, fee in prices.items():
        if code_id not in valid:
            continue
        if fee is None:
            db.execute("DELETE FROM fee_schedule_items WHERE schedule_id = ? AND code_id = ?", (sid, code_id))
        elif fee < 0:
            raise FeeError("A fee cannot be negative.")
        else:
            db.execute("""INSERT INTO fee_schedule_items(clinic_id, schedule_id, code_id, fee_p) VALUES (?,?,?,?)
                          ON CONFLICT(schedule_id, code_id) DO UPDATE SET fee_p = excluded.fee_p""", (cid, sid, code_id, fee))
    audit.log(db, cid, user_id, "fees.prices", None, None, str(sid))


def assign(db, clinic, user_id, pid, sid):
    cid = clinic["id"]
    if not db.execute("SELECT 1 FROM patients WHERE id = ? AND clinic_id = ?", (pid, cid)).fetchone():
        raise FeeError("That patient was not found.")
    if sid:
        s = get(db, cid, sid)
        if not s or not s["active"]:
            raise FeeError("Pick one of the clinic's fee schedules in use.")
    db.execute("UPDATE patients SET fee_schedule_id = ? WHERE id = ?", (sid or None, pid))
    audit.log(db, cid, user_id, "fees.assign", "patient", pid, str(sid or "standard"))


def fee_for(db, cid, pid, code):
    """The price of one treatment for one patient. code is a procedure_codes row."""
    row = db.execute("""SELECT s.id, s.discount_bp FROM patients p JOIN fee_schedules s ON s.id = p.fee_schedule_id AND s.active = 1
                        WHERE p.id = ? AND p.clinic_id = ?""", (pid, cid)).fetchone()
    if not row:
        return code["fee_p"]
    own = db.execute("SELECT fee_p FROM fee_schedule_items WHERE schedule_id = ? AND code_id = ?", (row["id"], code["id"])).fetchone()
    return price(code["fee_p"], row["discount_bp"], own["fee_p"] if own else None)


def of_patient(db, cid, pid):
    return db.execute("""SELECT s.* FROM patients p JOIN fee_schedules s ON s.id = p.fee_schedule_id WHERE p.id = ? AND p.clinic_id = ?""",
                      (pid, cid)).fetchone()
