"""The implant and materials register (version 0.35).

Every implant, abutment, graft, membrane or other traceable item put in a patient is written down with its maker, product,
catalogue code, lot (batch) number, size and expiry, the tooth and the date. When a maker or a regulator recalls a lot, the clinic
types the maker and lot and sees every patient who has one, then works through them, marking each as contacted. Removing an item
keeps the line, with the date and the reason. The patient can be given a printed implant card. Nothing is deleted. No web framework."""
import datetime
import json

from . import audit, teeth

KINDS = {"implant": "Implant", "abutment": "Abutment", "crown": "Implant crown or bridge", "bone_graft": "Bone graft",
         "membrane": "Membrane", "screw": "Screw or pin", "other": "Other traceable item"}
REASONS = {"failed": "Failed to integrate", "infection": "Infection", "fracture": "Fracture", "replaced": "Replaced", "recall": "Maker's recall",
           "other": "Other"}


class ImplantError(ValueError):
    pass


def _date(text, label):
    text = (text or "").strip()
    if not text:
        return None
    try:
        return datetime.date.fromisoformat(text).isoformat()
    except ValueError:
        raise ImplantError(label)


def add(db, clinic, user_id, pid, form, today=None):
    cid = clinic["id"]
    if not db.execute("SELECT 1 FROM patients WHERE id = ? AND clinic_id = ?", (pid, cid)).fetchone():
        raise ImplantError("That patient was not found.")
    kind = form.get("kind") or "implant"
    if kind not in KINDS:
        raise ImplantError("Pick what kind of item it is.")
    maker = (form.get("maker") or "").strip()[:120]
    lot = (form.get("lot") or "").strip()[:60]
    if not maker or not lot:
        raise ImplantError("The maker and the lot number are both needed, so a recall can find this patient.")
    try:
        tooth = teeth.parse(form.get("tooth"))
    except teeth.ToothError as exc:
        raise ImplantError(str(exc))
    placed = _date(form.get("placed_on"), "Use a real date for when it was placed.") or (today or datetime.date.today()).isoformat()
    expires = _date(form.get("expires_on"), "Use a real date for the expiry.")
    if expires and expires < placed:
        raise ImplantError("That item was past its expiry date when it was placed. Check the dates.")
    provider = form.get("provider_id")
    provider = int(provider) if str(provider or "").isdigit() else None
    if provider and not db.execute("SELECT 1 FROM providers WHERE id = ? AND clinic_id = ?", (provider, cid)).fetchone():
        provider = None
    iid = db.execute("""INSERT INTO implant_items(clinic_id, patient_id, tooth, kind, maker, product, ref_code, lot, size, expires_on, placed_on,
                        provider_id, notes, created_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                     (cid, pid, tooth, kind, maker, (form.get("product") or "").strip()[:120] or None, (form.get("ref_code") or "").strip()[:60] or None,
                      lot, (form.get("size") or "").strip()[:60] or None, expires, placed, provider, (form.get("notes") or "").strip()[:1000] or None,
                      user_id)).lastrowid
    audit.log(db, cid, user_id, "implant.add", "patient", pid, f"{maker} lot {lot}")
    return iid


def get(db, cid, iid):
    return db.execute("SELECT * FROM implant_items WHERE id = ? AND clinic_id = ?", (iid, cid)).fetchone()


def remove(db, clinic, user_id, iid, reason, removed_on=None, note=None):
    it = get(db, clinic["id"], iid)
    if not it:
        raise ImplantError("That register line was not found.")
    if it["removed_on"]:
        raise ImplantError("That item is already marked as removed.")
    if reason not in REASONS:
        raise ImplantError("Pick why it was removed.")
    when = _date(removed_on, "Use a real date for when it was removed.") or datetime.date.today().isoformat()
    if when < it["placed_on"]:
        raise ImplantError("It can't be removed before it was placed.")
    db.execute("UPDATE implant_items SET removed_on = ?, removed_reason = ?, removed_note = ?, removed_by = ? WHERE id = ?",
               (when, reason, (note or "").strip()[:500] or None, user_id, iid))
    audit.log(db, clinic["id"], user_id, "implant.remove", "patient", it["patient_id"], reason)


def for_patient(db, cid, pid, in_place_only=False):
    return db.execute("""SELECT i.*, pr.name AS provider_name FROM implant_items i LEFT JOIN providers pr ON pr.id = i.provider_id
                         WHERE i.clinic_id = ? AND i.patient_id = ?""" + (" AND i.removed_on IS NULL" if in_place_only else "") +
                      " ORDER BY i.placed_on DESC, i.id DESC", (cid, pid)).fetchall()


def find(db, cid, maker=None, lot=None, product=None, placed_from=None, placed_to=None):
    """Every item matching what was typed. Maker and product match anywhere in the name, ignoring case; the lot must match exactly."""
    sql = ["""SELECT i.*, p.first_name, p.last_name, p.phone, p.status AS patient_status FROM implant_items i JOIN patients p ON p.id = i.patient_id
              WHERE i.clinic_id = ?"""]
    args = [cid]
    if maker:
        sql.append("AND LOWER(i.maker) LIKE ?")
        args.append("%" + maker.strip().lower() + "%")
    if product:
        sql.append("AND LOWER(COALESCE(i.product, '')) LIKE ?")
        args.append("%" + product.strip().lower() + "%")
    if lot:
        sql.append("AND UPPER(i.lot) = ?")
        args.append(lot.strip().upper())
    if placed_from:
        sql.append("AND i.placed_on >= ?")
        args.append(_date(placed_from, "Use a real date."))
    if placed_to:
        sql.append("AND i.placed_on <= ?")
        args.append(_date(placed_to, "Use a real date."))
    if len(args) == 1:
        raise ImplantError("Type at least a maker, a product or a lot number.")
    return db.execute(" ".join(sql) + " ORDER BY p.last_name, p.first_name, i.placed_on", args).fetchall()


# ------------------------------------------------------------------------------------------------------------- recalls
def open_recall(db, clinic, user_id, maker, lot, reason, product=None):
    cid = clinic["id"]
    reason = (reason or "").strip()[:1000]
    if not reason:
        raise ImplantError("Say what the recall is about, in a few words.")
    hits = find(db, cid, maker=maker, lot=lot, product=product)
    rid = db.execute("INSERT INTO material_recalls(clinic_id, maker, product, lot, reason, created_by) VALUES (?,?,?,?,?,?)",
                     (cid, (maker or "").strip()[:120] or None, (product or "").strip()[:120] or None, (lot or "").strip()[:60] or None,
                      reason, user_id)).lastrowid
    for h in hits:
        db.execute("INSERT INTO material_recall_patients(clinic_id, recall_id, item_id, patient_id) VALUES (?,?,?,?)", (cid, rid, h["id"], h["patient_id"]))
    audit.log(db, cid, user_id, "implant.recall", None, None, f"{len(hits)} found")
    return rid, len(hits)


def recalls(db, cid):
    return db.execute("""SELECT r.*, (SELECT COUNT(*) FROM material_recall_patients x WHERE x.recall_id = r.id) AS found,
                         (SELECT COUNT(*) FROM material_recall_patients x WHERE x.recall_id = r.id AND x.contacted_at IS NOT NULL) AS contacted
                         FROM material_recalls r WHERE r.clinic_id = ? ORDER BY r.id DESC""", (cid,)).fetchall()


def recall(db, cid, rid):
    return db.execute("SELECT * FROM material_recalls WHERE id = ? AND clinic_id = ?", (rid, cid)).fetchone()


def recall_patients(db, cid, rid):
    return db.execute("""SELECT x.*, i.maker, i.product, i.lot, i.tooth, i.placed_on, i.removed_on, p.first_name, p.last_name, p.phone
                         FROM material_recall_patients x JOIN material_recalls r ON r.id = x.recall_id JOIN implant_items i ON i.id = x.item_id
                         JOIN patients p ON p.id = x.patient_id WHERE x.recall_id = ? AND r.clinic_id = ? ORDER BY p.last_name, p.first_name""",
                      (rid, cid)).fetchall()


def mark_contacted(db, clinic, user_id, rid, row_id, note=None):
    row = db.execute("""SELECT x.* FROM material_recall_patients x JOIN material_recalls r ON r.id = x.recall_id
                        WHERE x.id = ? AND x.recall_id = ? AND r.clinic_id = ?""", (row_id, rid, clinic["id"])).fetchone()
    if not row:
        raise ImplantError("That patient is not on this recall.")
    if row["contacted_at"]:
        raise ImplantError("That patient is already marked as contacted.")
    db.execute("UPDATE material_recall_patients SET contacted_at = datetime('now'), contacted_by = ?, note = ? WHERE id = ?",
               (user_id, (note or "").strip()[:500] or None, row_id))
    audit.log(db, clinic["id"], user_id, "implant.recall_contacted", "patient", row["patient_id"])


def close_recall(db, clinic, user_id, rid):
    r = recall(db, clinic["id"], rid)
    if not r or r["closed_at"]:
        raise ImplantError("That recall is not open.")
    db.execute("UPDATE material_recalls SET closed_at = datetime('now'), closed_by = ? WHERE id = ?", (user_id, rid))
    audit.log(db, clinic["id"], user_id, "implant.recall_closed", None, None, str(rid))
