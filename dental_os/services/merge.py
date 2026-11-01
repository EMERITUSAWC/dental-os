"""Finding and merging duplicate patient records (version 0.40).

Two records for one person happen: a new receptionist does not find the old one, a parent books a child under a nickname. The
finder lists likely pairs: the same phone number, or the same name and date of birth. A manager compares the two side by side and
merges the duplicate into the record to keep.

Everything that belongs to the duplicate moves across: visits, treatment, payments, notes, X-rays, messages, claims, forms, all of
it. Two things cannot simply be doubled, so they stay on the old record and the merge lists them for a person to look at: a second
medical history (the kept record has its own) and a recall of a kind the kept record already has. Empty details on the kept record
(email, date of birth, address) are filled from the duplicate. The duplicate is archived, marked as merged, and keeps a note of where
it went. A merge cannot be undone, so it is kept in full: what moved, how many rows, and what stayed. No web framework here."""
import json
import re
from pathlib import Path

from . import audit


class MergeError(ValueError):
    pass


def _patient_tables():
    """Every table with a patient_id column, read from the schema so a new table is never missed."""
    text = (Path(__file__).resolve().parent.parent / "schema.sql").read_text()
    out = []
    for m in re.finditer(r"CREATE TABLE IF NOT EXISTS (\w+) \((.*?)\n\);", text, re.S):
        if m.group(1) != "patients" and re.search(r"^\s*patient_id\s", m.group(2), re.M):
            out.append(m.group(1))
    return out


TABLES = _patient_tables()
ONE_EACH = {"medical_history"}                      # only one per patient
LABELS = {"appointments": "Visits", "procedures": "Treatment", "payments": "Payments", "adjustments": "Discounts and corrections",
          "clinical_notes": "Clinical notes", "patient_files": "Files and X-rays", "messages": "Messages", "claims": "Claims",
          "treatment_plans": "Treatment plans", "recalls": "Recalls", "medical_history": "Medical history", "consents": "Consent forms",
          "prescriptions": "Prescriptions", "invoices": "Invoices"}
LEFT = {"medical_history": "A second medical history: copy anything new across by hand, then archive it",
        "recalls": "Recalls of a kind the kept record already has: check which due date is right"}
FILL = ("email", "dob", "address", "gender", "language", "referrer_id", "referral_source")


GROUP_CAP = 40       # a phone or name on more records than this is a placeholder (the clinic's own number), not one person


def candidates(db, cid, limit=100):
    """Likely pairs of records for the same person: the same phone, or the same name and date of birth.

    0.57: grouped in one pass. The old version joined every patient with every other one, which took about a
    minute and a half at a clinic with 20,000 patients."""
    people = {r["id"]: r for r in db.execute(
        "SELECT id, first_name, last_name, phone, dob FROM patients WHERE clinic_id = ? AND status = 'active'", (cid,))}
    by_phone, by_name = {}, {}
    for r in people.values():
        if r["phone"]:
            by_phone.setdefault(r["phone"], []).append(r["id"])
        if r["dob"]:
            key = ((r["first_name"] or "").lower(), (r["last_name"] or "").lower(), r["dob"])
            by_name.setdefault(key, []).append(r["id"])
    pairs = {}
    for groups, why in ((by_name, "name"), (by_phone, "phone")):       # a phone match wins when both match, as before
        for ids in groups.values():
            if len(ids) < 2 or len(ids) > GROUP_CAP:
                continue
            ids.sort()
            for i, a in enumerate(ids):
                for b in ids[i + 1:]:
                    pairs[(a, b)] = why
    if not pairs:
        return []
    skip = {(r["a_id"], r["b_id"]) for r in db.execute("SELECT a_id, b_id FROM patient_not_duplicates WHERE clinic_id = ?", (cid,))}
    out = []
    for (a, b), why in pairs.items():
        if (a, b) in skip:
            continue
        pa, pb = people[a], people[b]
        out.append({"a_id": a, "b_id": b, "a_first": pa["first_name"], "a_last": pa["last_name"], "b_first": pb["first_name"],
                    "b_last": pb["last_name"], "phone": pa["phone"], "a_dob": pa["dob"], "b_dob": pb["dob"],
                    "why": "phone" if pa["phone"] and pa["phone"] == pb["phone"] else why})
    out.sort(key=lambda r: (r["a_last"] or "", r["a_first"] or "", r["a_id"], r["b_id"]))
    return out[:limit]


def not_duplicate(db, clinic, user_id, a, b):
    """Two records that look alike but are different people (twins sharing a parent's phone, for example)."""
    if not a or not b or a == b:
        raise MergeError("That patient was not found.")
    a, b = sorted((a, b))
    for pid in (a, b):
        if not db.execute("SELECT 1 FROM patients WHERE id = ? AND clinic_id = ?", (pid, clinic["id"])).fetchone():
            raise MergeError("That patient was not found.")
    if not db.execute("SELECT 1 FROM patient_not_duplicates WHERE clinic_id = ? AND a_id = ? AND b_id = ?", (clinic["id"], a, b)).fetchone():
        db.execute("INSERT INTO patient_not_duplicates(clinic_id, a_id, b_id, created_by) VALUES (?,?,?,?)", (clinic["id"], a, b, user_id))
    audit.log(db, clinic["id"], user_id, "patient.not_duplicate", "patient", a, str(b))


def counts(db, cid, pid):
    out = {}
    for t in TABLES:
        n = db.execute(f"SELECT COUNT(*) FROM {t} WHERE patient_id = ? AND clinic_id = ?", (pid, cid)).fetchone()[0] if _has_clinic(t) else \
            db.execute(f"SELECT COUNT(*) FROM {t} WHERE patient_id = ?", (pid,)).fetchone()[0]
        if n:
            out[t] = n
    return out


_CLINIC = {}


def _has_clinic(t):
    if t not in _CLINIC:
        text = (Path(__file__).resolve().parent.parent / "schema.sql").read_text()
        m = re.search(r"CREATE TABLE IF NOT EXISTS %s \((.*?)\n\);" % t, text, re.S)
        _CLINIC[t] = bool(m and re.search(r"^\s*clinic_id\s", m.group(1), re.M))
    return _CLINIC[t]


def merge(db, clinic, user_id, keep_id, dup_id, confirm):
    cid = clinic["id"]
    if keep_id == dup_id:
        raise MergeError("Pick two different records.")
    keep = db.execute("SELECT * FROM patients WHERE id = ? AND clinic_id = ?", (keep_id, cid)).fetchone()
    dup = db.execute("SELECT * FROM patients WHERE id = ? AND clinic_id = ?", (dup_id, cid)).fetchone()
    if not keep or not dup:
        raise MergeError("That patient was not found.")
    if keep["status"] != "active" or dup["status"] != "active":
        raise MergeError("Both records must be active to merge them.")
    if not confirm:
        raise MergeError("Tick that you have checked these are the same person. A merge cannot be undone.")
    moved, left = {}, []
    for t in TABLES:
        where = "patient_id = ? AND clinic_id = ?" if _has_clinic(t) else "patient_id = ?"
        args = (dup_id, cid) if _has_clinic(t) else (dup_id,)
        if t in ONE_EACH:
            has_keep = db.execute(f"SELECT 1 FROM {t} WHERE patient_id = ?", (keep_id,)).fetchone()
            if has_keep:
                if db.execute(f"SELECT 1 FROM {t} WHERE {where}", args).fetchone():
                    left.append({"what": "medical_history", "n": 1})
                continue
        if t == "recalls":
            n = db.execute(f"""UPDATE recalls SET patient_id = ? WHERE {where} AND type_id NOT IN
                               (SELECT type_id FROM recalls WHERE patient_id = ? AND clinic_id = ?)""", (keep_id, *args, keep_id, cid)).rowcount
            rest = db.execute(f"SELECT COUNT(*) FROM recalls WHERE {where}", args).fetchone()[0]
            if rest:
                left.append({"what": "recalls", "n": rest})
        else:
            n = db.execute(f"UPDATE {t} SET patient_id = ? WHERE {where}", (keep_id, *args)).rowcount
        if n:
            moved[t] = n
    db.execute("UPDATE patients SET guarantor_id = ? WHERE guarantor_id = ? AND clinic_id = ? AND id != ?", (keep_id, dup_id, cid, keep_id))
    if keep["guarantor_id"] == dup_id:
        db.execute("UPDATE patients SET guarantor_id = NULL WHERE id = ?", (keep_id,))
    if db.execute("SELECT 1 FROM steri_packs WHERE used_on = ?", (dup_id,)).fetchone():
        moved["steri_packs"] = db.execute("UPDATE steri_packs SET used_on = ? WHERE used_on = ? AND clinic_id = ?", (keep_id, dup_id, cid)).rowcount
    filled = []
    for f in FILL:
        if f in keep.keys() and not keep[f] and dup[f]:
            db.execute(f"UPDATE patients SET {f} = ? WHERE id = ?", (dup[f], keep_id))
            filled.append(f)
    if not keep["guarantor_id"] and dup["guarantor_id"] and dup["guarantor_id"] != keep_id:
        db.execute("UPDATE patients SET guarantor_id = ? WHERE id = ?", (dup["guarantor_id"], keep_id))
        filled.append("guarantor_id")
    note = ((dup["notes"] or "") + f"\nMerged into patient {keep_id}.").strip()
    db.execute("UPDATE patients SET status = 'archived', merged_into = ?, guarantor_id = NULL, notes = ?, updated_at = datetime('now') WHERE id = ?",
               (keep_id, note[:4000], dup_id))
    mid = db.execute("INSERT INTO patient_merges(clinic_id, keep_id, dup_id, moved, left_behind, filled, created_by) VALUES (?,?,?,?,?,?,?)",
                     (cid, keep_id, dup_id, json.dumps(moved), json.dumps(left), json.dumps(filled), user_id)).lastrowid
    audit.log(db, cid, user_id, "patient.merge", "patient", keep_id, f"from {dup_id}: {sum(moved.values())} rows")
    return mid


def merges(db, cid):
    return db.execute("""SELECT m.*, k.first_name, k.last_name, u.display_name FROM patient_merges m JOIN patients k ON k.id = m.keep_id
                         LEFT JOIN users u ON u.id = m.created_by WHERE m.clinic_id = ? ORDER BY m.id DESC""", (cid,)).fetchall()


def get_merge(db, cid, mid):
    return db.execute("SELECT * FROM patient_merges WHERE id = ? AND clinic_id = ?", (mid, cid)).fetchone()
