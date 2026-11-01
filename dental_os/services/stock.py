"""Stock of materials and consumables. The level is the sum of the moves, so there is always a trail. Stock that expires first
is counted as used first (the usual way materials are used), which tells us how much is left of each batch and what has expired."""
from datetime import timedelta

from . import audit
from .timeutil import now_local, parse_date

KINDS = {"receive": "Received", "use": "Used", "waste": "Wasted or broken", "adjust": "Corrected", "count": "Stock count"}
UNITS = ["each", "box", "pack", "bottle", "tube", "syringe", "cartridge", "pair", "roll", "kit", "litre", "gram"]
CATEGORIES = ["General", "Anaesthetic", "Restorative", "Impression and lab", "Endodontic", "Surgical", "Hygiene",
              "Infection control", "Disposables", "Imaging", "Office"]


class StockError(Exception):
    pass


def _today(clinic):
    return now_local(clinic["timezone"]).date()


def _text(value, limit, what, required=False):
    value = (value or "").strip()
    if required and not value:
        raise StockError(f"Enter {what}.")
    if len(value) > limit:
        raise StockError(f"{what[0].upper()}{what[1:]} is too long (up to {limit} characters).")
    return value or None


def _begin(db):
    if not db.in_transaction:
        db.execute("BEGIN IMMEDIATE")


# ---------------------------------------------------------------- suppliers and items
def save_supplier(db, clinic, user_id, supplier_id, name, phone=None, email=None, notes=None, active=True):
    cid = clinic["id"]
    name = _text(name, 80, "the supplier's name", required=True)
    phone, email, notes = _text(phone, 40, "the phone number"), _text(email, 120, "the email"), _text(notes, 500, "the notes")
    if email and "@" not in email:
        raise StockError("That email address does not look right.")
    if supplier_id:
        if not db.execute("SELECT 1 FROM suppliers WHERE id=? AND clinic_id=?", (supplier_id, cid)).fetchone():
            raise LookupError("supplier")
        db.execute("UPDATE suppliers SET name=?, phone=?, email=?, notes=?, active=? WHERE id=? AND clinic_id=?",
                   (name, phone, email, notes, 1 if active else 0, supplier_id, cid))
    else:
        supplier_id = db.execute("INSERT INTO suppliers(clinic_id, name, phone, email, notes) VALUES (?,?,?,?,?)",
                                 (cid, name, phone, email, notes)).lastrowid
    audit.log(db, cid, user_id, "stock.supplier.save", "supplier", supplier_id)
    return supplier_id


def suppliers(db, cid, active_only=False):
    return db.execute("SELECT * FROM suppliers WHERE clinic_id=? " + ("AND active=1 " if active_only else "") + "ORDER BY active DESC, name",
                      (cid,)).fetchall()


def save_item(db, clinic, user_id, item_id, name, sku=None, unit="each", category="General", supplier_id=None, reorder_level=0,
              reorder_qty=0, cost_p=0, location=None, active=True):
    cid = clinic["id"]
    name = _text(name, 100, "the item's name", required=True)
    sku, location = _text(sku, 60, "the code"), _text(location, 60, "the location")
    unit = _text(unit, 20, "the unit", required=True)
    category = _text(category, 40, "the category") or "General"
    if min(reorder_level, reorder_qty, cost_p) < 0:
        raise StockError("Levels and costs cannot be negative.")
    if supplier_id and not db.execute("SELECT 1 FROM suppliers WHERE id=? AND clinic_id=?", (supplier_id, cid)).fetchone():
        raise StockError("Pick one of your suppliers.")
    if item_id:
        if not db.execute("SELECT 1 FROM stock_items WHERE id=? AND clinic_id=?", (item_id, cid)).fetchone():
            raise LookupError("item")
        db.execute("""UPDATE stock_items SET name=?, sku=?, unit=?, category=?, supplier_id=?, reorder_level=?, reorder_qty=?, cost_p=?,
                      location=?, active=? WHERE id=? AND clinic_id=?""",
                   (name, sku, unit, category, supplier_id or None, reorder_level, reorder_qty, cost_p, location, 1 if active else 0, item_id, cid))
    else:
        item_id = db.execute(
            """INSERT INTO stock_items(clinic_id, name, sku, unit, category, supplier_id, reorder_level, reorder_qty, cost_p, location)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (cid, name, sku, unit, category, supplier_id or None, reorder_level, reorder_qty, cost_p, location)).lastrowid
    audit.log(db, cid, user_id, "stock.item.save", "stock_item", item_id)
    return item_id


def get_item(db, cid, item_id):
    return db.execute("""SELECT i.*, s.name AS supplier_name FROM stock_items i LEFT JOIN suppliers s ON s.id = i.supplier_id
                         WHERE i.id = ? AND i.clinic_id = ?""", (item_id, cid)).fetchone()


def level(db, cid, item_id):
    return db.execute("SELECT COALESCE(SUM(qty),0) FROM stock_moves WHERE clinic_id=? AND item_id=?", (cid, item_id)).fetchone()[0]


# ---------------------------------------------------------------- moves
def move(db, clinic, user_id, item_id, kind, qty, unit_cost_p=None, expires_on=None, note=None, moved_on=None):
    """Record stock coming in, going out, or being corrected. Returns the move id, or None when a count matched exactly."""
    cid = clinic["id"]
    if kind not in KINDS:
        raise StockError("Pick what happened.")
    if not isinstance(qty, int):
        raise StockError("Enter a whole number.")
    note = _text(note, 200, "the note")
    today = _today(clinic)
    try:
        day = parse_date(moved_on).isoformat() if moved_on else today.isoformat()
    except ValueError:
        raise StockError("The date must look like 2026-11-30.")
    if day > today.isoformat():
        raise StockError("The date cannot be in the future.")
    _begin(db)
    item = get_item(db, cid, item_id)
    if not item:
        raise LookupError("item")
    if not item["active"]:
        raise StockError("That item is switched off. Switch it on first.")
    have = level(db, cid, item_id)
    expiry = None
    cost = item["cost_p"]
    if kind == "receive":
        if qty <= 0:
            raise StockError("Enter how many came in.")
        if expires_on:
            try:
                expiry = parse_date(expires_on).isoformat()
            except ValueError:
                raise StockError("The expiry date must look like 2026-11-30.")
            if expiry < today.isoformat():
                raise StockError("That stock has already expired.")
        if unit_cost_p is not None:
            if unit_cost_p < 0:
                raise StockError("The cost cannot be negative.")
            cost = unit_cost_p
            db.execute("UPDATE stock_items SET cost_p=? WHERE id=? AND clinic_id=?", (cost, item_id, cid))
        delta = qty
    elif kind in ("use", "waste"):
        if qty <= 0:
            raise StockError("Enter how many were used.")
        delta = -qty
    elif kind == "adjust":
        if qty == 0:
            raise StockError("Enter how many to add (+) or take away (-).")
        if not note:
            raise StockError("Say why the level is being corrected.")
        delta = qty
    else:   # count: qty is what was actually counted on the shelf
        if qty < 0:
            raise StockError("A count cannot be negative.")
        delta = qty - have
        if delta == 0:
            audit.log(db, cid, user_id, "stock.count", "stock_item", item_id, "no change")
            return None
    if have + delta < 0:
        raise StockError(f"Only {have} {item['unit']} in stock. That would take it below zero.")
    cur = db.execute(
        """INSERT INTO stock_moves(clinic_id, item_id, qty, kind, unit_cost_p, expires_on, note, moved_on, created_by)
           VALUES (?,?,?,?,?,?,?,?,?)""", (cid, item_id, delta, kind, cost, expiry, note, day, user_id))
    audit.log(db, cid, user_id, f"stock.{kind}", "stock_item", item_id, str(delta))
    return cur.lastrowid


def history(db, cid, item_id, limit=100):
    return db.execute("""SELECT m.*, u.display_name FROM stock_moves m LEFT JOIN users u ON u.id = m.created_by
                         WHERE m.clinic_id=? AND m.item_id=? ORDER BY m.id DESC LIMIT ?""", (cid, item_id, limit)).fetchall()


def _batches(moves):
    """What is left of each delivery, taking the earliest expiry first. `moves` are rows in id order."""
    batches = [{"qty": m["qty"], "expires_on": m["expires_on"], "cost_p": m["unit_cost_p"], "id": m["id"]} for m in moves if m["qty"] > 0]
    used = -sum(m["qty"] for m in moves if m["qty"] < 0)
    batches.sort(key=lambda b: (b["expires_on"] is None, b["expires_on"] or "", b["id"]))
    for b in batches:
        take = min(b["qty"], used)
        b["qty"] -= take
        used -= take
    return [b for b in batches if b["qty"] > 0]


def batches(db, cid, item_id):
    rows = db.execute("SELECT id, qty, unit_cost_p, expires_on FROM stock_moves WHERE clinic_id=? AND item_id=? ORDER BY id", (cid, item_id)).fetchall()
    return _batches(rows)


def items(db, clinic, q=None, include_off=False, category=None):
    cid, today = clinic["id"], _today(clinic).isoformat()
    soon = (parse_date(today) + timedelta(days=60)).isoformat()
    moves = {}
    for m in db.execute("SELECT id, item_id, qty, unit_cost_p, expires_on FROM stock_moves WHERE clinic_id=? ORDER BY id", (cid,)):
        moves.setdefault(m["item_id"], []).append(m)
    sql = """SELECT i.*, s.name AS supplier_name FROM stock_items i LEFT JOIN suppliers s ON s.id = i.supplier_id WHERE i.clinic_id = ?"""
    args = [cid]
    if not include_off:
        sql += " AND i.active = 1"
    if q:
        sql += " AND (i.name LIKE ? OR i.sku LIKE ?)"
        args += [f"%{q}%", f"%{q}%"]
    if category:
        sql += " AND i.category = ?"
        args.append(category)
    out = []
    for r in db.execute(sql + " ORDER BY i.category, i.name", args).fetchall():
        bs = _batches(moves.get(r["id"], []))
        have = sum(b["qty"] for b in bs)
        expired = sum(b["qty"] for b in bs if b["expires_on"] and b["expires_on"] < today)
        expiring = sum(b["qty"] for b in bs if b["expires_on"] and today <= b["expires_on"] <= soon)
        nxt = next((b["expires_on"] for b in bs if b["expires_on"]), None)
        out.append({"item": r, "level": have, "value_p": sum(b["qty"] * b["cost_p"] for b in bs), "expired": expired,
                    "expiring": expiring, "next_expiry": nxt,
                    "low": bool(r["active"] and r["reorder_level"] and have <= r["reorder_level"])})
    return out


def to_order(db, clinic):
    """Items at or below their reorder level, grouped by supplier, with a suggested quantity."""
    groups = {}
    for row in items(db, clinic):
        if not row["low"]:
            continue
        it = row["item"]
        want = it["reorder_qty"] or max(it["reorder_level"] * 2 - row["level"], 1)
        key = it["supplier_id"] or 0
        g = groups.setdefault(key, {"supplier": it["supplier_name"] or "No supplier set", "supplier_id": it["supplier_id"], "lines": [], "total_p": 0})
        g["lines"].append({"item": it, "level": row["level"], "want": want, "cost_p": want * it["cost_p"]})
        g["total_p"] += want * it["cost_p"]
    return sorted(groups.values(), key=lambda g: (g["supplier_id"] is None, g["supplier"]))


def expiring(db, clinic, days=60):
    """Batches that have expired or will within `days`, with how many are left."""
    cid, today = clinic["id"], _today(clinic)
    cutoff = (today + timedelta(days=days)).isoformat()
    out = []
    for row in items(db, clinic):
        for b in batches(db, cid, row["item"]["id"]):
            if b["expires_on"] and b["expires_on"] <= cutoff:
                out.append({"item": row["item"], "qty": b["qty"], "expires_on": b["expires_on"], "expired": b["expires_on"] < today.isoformat()})
    return sorted(out, key=lambda r: r["expires_on"])


def summary(db, clinic):
    rows = items(db, clinic)
    return {"items": len(rows), "value_p": sum(r["value_p"] for r in rows), "low": sum(1 for r in rows if r["low"]),
            "expired": sum(1 for r in rows if r["expired"]), "expiring": sum(1 for r in rows if r["expiring"])}
