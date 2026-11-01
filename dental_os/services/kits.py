"""Treatment kits (version 0.45): the stock each treatment uses, taken off the shelf by itself.

For each treatment on its list, the clinic says what it uses from stock: a crown uses one crown cement capsule and one impression
tray, a filling uses one composite capsule and one bonding brush. When the treatment is marked done, those amounts are recorded as
used, so the stock level and the reorder list stay right without anyone counting by hand.

Clinical work is never held up by stock: if the shelf shows too little, what is there is taken and the shortfall is listed for the
manager, because it usually means a delivery was not entered or the count is off. If the treatment is voided, what was taken goes
back with a note. The material cost of each treatment is kept, so the clinic can see what each one really costs. No web framework."""
from . import audit, stock


class KitError(ValueError):
    pass


def lines(db, cid, code_id):
    return db.execute("""SELECT k.*, s.name, s.unit, s.cost_p FROM kit_lines k JOIN stock_items s ON s.id = k.item_id
                         WHERE k.clinic_id = ? AND k.code_id = ? ORDER BY s.name""", (cid, code_id)).fetchall()


def save(db, clinic, user_id, code_id, wanted):
    """wanted: {stock item id: quantity}. A quantity of 0 removes the line."""
    cid = clinic["id"]
    if not db.execute("SELECT 1 FROM procedure_codes WHERE id = ? AND clinic_id = ?", (code_id, cid)).fetchone():
        raise KitError("That treatment was not found.")
    for item_id, qty in wanted.items():
        if not db.execute("SELECT 1 FROM stock_items WHERE id = ? AND clinic_id = ?", (item_id, cid)).fetchone():
            continue
        if not isinstance(qty, int) or qty < 0 or qty > 1000:
            raise KitError("Use whole numbers from 0 to 1000.")
        if qty == 0:
            db.execute("DELETE FROM kit_lines WHERE clinic_id = ? AND code_id = ? AND item_id = ?", (cid, code_id, item_id))
        else:
            db.execute("""INSERT INTO kit_lines(clinic_id, code_id, item_id, qty) VALUES (?,?,?,?)
                          ON CONFLICT(code_id, item_id) DO UPDATE SET qty = excluded.qty""", (cid, code_id, item_id, qty))
    audit.log(db, cid, user_id, "kit.save", None, None, str(code_id))


def kits(db, cid):
    return db.execute("""SELECT c.id, c.code, c.name, c.category, COUNT(k.id) AS n, COALESCE(SUM(k.qty * s.cost_p), 0) AS cost_p
                         FROM procedure_codes c LEFT JOIN kit_lines k ON k.code_id = c.id LEFT JOIN stock_items s ON s.id = k.item_id
                         WHERE c.clinic_id = ? AND c.active = 1 GROUP BY c.id, c.code, c.name, c.category ORDER BY c.category, c.name""", (cid,)).fetchall()


def use_for(db, clinic, user_id, proc_id):
    """Take the kit for one finished treatment off the shelf. Returns the shortfalls (item name, wanted, taken)."""
    cid = clinic["id"]
    proc = db.execute("SELECT * FROM procedures WHERE id = ? AND clinic_id = ?", (proc_id, cid)).fetchone()
    if not proc or db.execute("SELECT 1 FROM kit_uses WHERE procedure_id = ? AND returned = 0", (proc_id,)).fetchone():
        return []
    short = []
    for ln in lines(db, cid, proc["code_id"]):
        item = stock.get_item(db, cid, ln["item_id"])
        if not item or not item["active"]:
            continue
        have = stock.level(db, cid, ln["item_id"])
        take = min(ln["qty"], max(have, 0))
        move_id = stock.move(db, clinic, user_id, ln["item_id"], "use", take, note=f"Treatment {proc_id}") if take else None
        db.execute("""INSERT INTO kit_uses(clinic_id, procedure_id, item_id, wanted, taken, cost_p, move_id) VALUES (?,?,?,?,?,?,?)""",
                   (cid, proc_id, ln["item_id"], ln["qty"], take, take * item["cost_p"], move_id))
        if take < ln["qty"]:
            short.append((item["name"], ln["qty"], take))
    return short


def give_back(db, clinic, user_id, proc_id):
    """The treatment was voided: put back what its kit took."""
    cid = clinic["id"]
    for u in db.execute("SELECT * FROM kit_uses WHERE procedure_id = ? AND clinic_id = ? AND returned = 0", (proc_id, cid)).fetchall():
        if u["taken"]:
            item = stock.get_item(db, cid, u["item_id"])
            if item and item["active"]:
                stock.move(db, clinic, user_id, u["item_id"], "adjust", u["taken"], note=f"Treatment {proc_id} voided: kit put back")
        db.execute("UPDATE kit_uses SET returned = 1 WHERE id = ?", (u["id"],))


def shortfalls(db, cid, limit=100):
    return db.execute("""SELECT u.*, s.name, s.unit, pr.done_on FROM kit_uses u JOIN stock_items s ON s.id = u.item_id
                         JOIN procedures pr ON pr.id = u.procedure_id WHERE u.clinic_id = ? AND u.taken < u.wanted AND u.returned = 0
                         ORDER BY u.id DESC LIMIT ?""", (cid, limit)).fetchall()


def material_costs(db, cid, start, end):
    """Per treatment: how many were done, their fees, and the stock they used."""
    return db.execute("""SELECT c.code, c.name, COUNT(*) AS n,
                         (SELECT COALESCE(SUM(p2.fee_p), 0) FROM procedures p2 WHERE p2.code_id = c.id AND p2.clinic_id = ? AND p2.status = 'done'
                          AND substr(p2.done_on, 1, 10) BETWEEN ? AND ?) AS fees_p,
                         (SELECT COALESCE(SUM(u.cost_p), 0) FROM kit_uses u JOIN procedures p3 ON p3.id = u.procedure_id WHERE p3.code_id = c.id
                          AND u.clinic_id = ? AND u.returned = 0 AND p3.status = 'done' AND substr(p3.done_on, 1, 10) BETWEEN ? AND ?) AS cost_p
                         FROM procedures pr JOIN procedure_codes c ON c.id = pr.code_id
                         WHERE pr.clinic_id = ? AND pr.status = 'done' AND substr(pr.done_on, 1, 10) BETWEEN ? AND ?
                         GROUP BY c.id, c.code, c.name ORDER BY cost_p DESC, c.name""",
                      (cid, start, end, cid, start, end, cid, start, end)).fetchall()
