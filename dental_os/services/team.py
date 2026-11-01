"""Messages between staff, and the equipment register (version 0.29).

Staff messages: to one person or to everyone, optionally about a patient (a link to the record goes with it). Each person sees what is
unread for them. Messages are never edited or deleted, so they stand as a record of who was told what.

Equipment: each item (chairs, compressors, suction, X-ray units, scanners, sterilisers and so on) with its serial number, where it is,
warranty, and how often it must be serviced. Services, repairs, checks and faults are logged with who did the work and what it cost.
Items due for service show on the Today screen. No web framework here."""
import calendar
import datetime

from . import audit
from .timeutil import now_local, parse_date

EQUIPMENT_KINDS = {"chair": "Dental chair", "compressor": "Compressor", "suction": "Suction", "xray": "X-ray unit",
                   "opg": "Panoramic or CBCT unit", "sensor": "X-ray sensor or plate scanner", "steriliser": "Steriliser",
                   "washer": "Washer-disinfector or ultrasonic bath", "handpiece": "Handpiece", "light": "Curing light",
                   "scanner": "Intraoral scanner", "it": "Computer or network", "other": "Other"}
EVENT_KINDS = {"service": "Service", "repair": "Repair", "check": "Check or test", "fault": "Fault reported"}


class TeamError(Exception):
    pass


def _today(clinic):
    return now_local(clinic["timezone"]).date()


# --------------------------------------------------------------------------------------------------------- staff messages
def send(db, clinic, user, to_user, body, patient_id=None, reply_to=None):
    cid = clinic["id"]
    body = (body or "").strip()
    if not body:
        raise TeamError("Write the message.")
    if len(body) > 2000:
        raise TeamError("That message is too long. Keep it under 2000 characters.")
    if to_user and not db.execute("SELECT 1 FROM users WHERE id = ? AND clinic_id = ? AND active = 1", (to_user, cid)).fetchone():
        raise TeamError("Pick someone from this clinic.")
    if to_user == user["id"]:
        raise TeamError("Pick someone other than yourself.")
    if patient_id and not db.execute("SELECT 1 FROM patients WHERE id = ? AND clinic_id = ?", (patient_id, cid)).fetchone():
        raise TeamError("That patient was not found.")
    if reply_to and not db.execute("SELECT 1 FROM staff_messages WHERE id = ? AND clinic_id = ?", (reply_to, cid)).fetchone():
        reply_to = None
    mid = db.execute("INSERT INTO staff_messages(clinic_id, from_user, to_user, patient_id, body, reply_to) VALUES (?,?,?,?,?,?)",
                     (cid, user["id"], to_user or None, patient_id or None, body, reply_to)).lastrowid
    db.execute("INSERT INTO staff_message_reads(message_id, user_id, read_at) VALUES (?,?, datetime('now'))", (mid, user["id"]))
    audit.log(db, cid, user["id"], "team.message", "patient" if patient_id else None, patient_id)
    return mid


def _mine(where):
    return f"""SELECT m.*, f.display_name AS from_name, t.display_name AS to_name, p.first_name, p.last_name,
               (SELECT 1 FROM staff_message_reads r WHERE r.message_id = m.id AND r.user_id = ?) AS seen
               FROM staff_messages m JOIN users f ON f.id = m.from_user LEFT JOIN users t ON t.id = m.to_user
               LEFT JOIN patients p ON p.id = m.patient_id WHERE m.clinic_id = ? AND {where} ORDER BY m.id DESC LIMIT 200"""


def inbox(db, user):
    return db.execute(_mine("(m.to_user = ? OR (m.to_user IS NULL AND m.from_user != ?))"),
                      (user["id"], user["clinic_id"], user["id"], user["id"])).fetchall()


def sent(db, user):
    return db.execute(_mine("m.from_user = ?"), (user["id"], user["clinic_id"], user["id"])).fetchall()


def about_patient(db, user, pid):
    return db.execute(_mine("m.patient_id = ? AND (m.to_user IS NULL OR m.to_user = ? OR m.from_user = ?)"),
                      (user["id"], user["clinic_id"], pid, user["id"], user["id"])).fetchall()


def unread(db, user):
    return db.execute("""SELECT COUNT(*) FROM staff_messages m WHERE m.clinic_id = ? AND (m.to_user = ? OR (m.to_user IS NULL AND m.from_user != ?))
                         AND NOT EXISTS (SELECT 1 FROM staff_message_reads r WHERE r.message_id = m.id AND r.user_id = ?)""",
                      (user["clinic_id"], user["id"], user["id"], user["id"])).fetchone()[0]


def mark_read(db, user, message_ids):
    for mid in message_ids:
        if not db.execute("SELECT 1 FROM staff_messages WHERE id = ? AND clinic_id = ?", (mid, user["clinic_id"])).fetchone():
            continue
        if db.execute("SELECT 1 FROM staff_message_reads WHERE message_id = ? AND user_id = ?", (mid, user["id"])).fetchone():
            continue
        db.execute("INSERT INTO staff_message_reads(message_id, user_id, read_at) VALUES (?,?, datetime('now'))", (mid, user["id"]))


# ------------------------------------------------------------------------------------------------------------ equipment
def _date(raw):
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return parse_date(raw)
    except ValueError:
        raise TeamError("Use a date like 2026-11-30.")


def add_months(d, n):
    m = d.month - 1 + n
    y, m = d.year + m // 12, m % 12 + 1
    return datetime.date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def save_item(db, clinic, user_id, form, item_id=None):
    cid = clinic["id"]
    name = (form.get("name") or "").strip()[:80]
    if not name:
        raise TeamError("Give the item a name, for example Chair 1.")
    kind = form.get("kind") or "other"
    if kind not in EQUIPMENT_KINDS:
        raise TeamError("Pick what kind of equipment it is.")
    try:
        every = int(str(form.get("service_months") or "0").strip() or 0)
    except ValueError:
        raise TeamError("Write how often it is serviced in whole months, or 0 for never.")
    if not 0 <= every <= 120:
        raise TeamError("Write how often it is serviced in whole months, or 0 for never.")
    vals = {"name": name, "kind": kind, "maker": (form.get("maker") or "").strip()[:80] or None,
            "model": (form.get("model") or "").strip()[:80] or None, "serial": (form.get("serial") or "").strip()[:80] or None,
            "location": (form.get("location") or "").strip()[:80] or None, "bought_on": _date(form.get("bought_on")),
            "warranty_until": _date(form.get("warranty_until")), "service_months": every or None,
            "supplier": (form.get("supplier") or "").strip()[:120] or None, "notes": (form.get("notes") or "").strip()[:1000] or None,
            "retired": 1 if form.get("retired") == "1" else 0}
    for k in ("bought_on", "warranty_until"):
        vals[k] = vals[k].isoformat() if vals[k] else None
    if item_id:
        if not get_item(db, cid, item_id):
            raise TeamError("That item was not found.")
        db.execute("UPDATE equipment SET " + ", ".join(f"{k} = ?" for k in vals) + " WHERE id = ? AND clinic_id = ?",
                   list(vals.values()) + [item_id, cid])
    else:
        item_id = db.execute("INSERT INTO equipment(clinic_id, " + ", ".join(vals) + ") VALUES (?" + ",?" * len(vals) + ")",
                             [cid] + list(vals.values())).lastrowid
    audit.log(db, cid, user_id, "equipment.save", None, None, name)
    return item_id


def get_item(db, cid, item_id):
    return db.execute("SELECT * FROM equipment WHERE id = ? AND clinic_id = ?", (item_id, cid)).fetchone()


def log_event(db, clinic, user_id, item_id, kind, done_on, by, cost, notes):
    cid = clinic["id"]
    item = get_item(db, cid, item_id)
    if not item:
        raise TeamError("That item was not found.")
    if kind not in EVENT_KINDS:
        raise TeamError("Pick what happened.")
    day = _date(done_on) or _today(clinic)
    if day > _today(clinic):
        raise TeamError("The date cannot be in the future.")
    notes = (notes or "").strip()[:1000]
    if not notes:
        raise TeamError("Write what was done or what is wrong.")
    cost_p = None
    if (cost or "").strip():
        try:
            cost_p = round(float(cost.replace(",", "").strip()) * 100)
        except ValueError:
            raise TeamError("Write the cost as a number.")
        if cost_p < 0:
            raise TeamError("Write the cost as a number.")
    eid = db.execute("""INSERT INTO equipment_events(clinic_id, equipment_id, kind, done_on, done_by, cost_p, notes, created_by)
                        VALUES (?,?,?,?,?,?,?,?)""", (cid, item_id, kind, day.isoformat(), (by or "").strip()[:120] or None, cost_p, notes, user_id)).lastrowid
    if kind == "fault":
        db.execute("UPDATE equipment SET fault_open = 1 WHERE id = ?", (item_id,))
    elif kind in ("repair", "service"):
        db.execute("UPDATE equipment SET fault_open = 0 WHERE id = ?", (item_id,))
    if kind == "service" and (not item["last_service_on"] or day.isoformat() > item["last_service_on"]):
        db.execute("UPDATE equipment SET last_service_on = ? WHERE id = ?", (day.isoformat(), item_id))
    audit.log(db, cid, user_id, "equipment.event", None, None, f"{item['name']}: {kind}")
    return eid


def next_service(item):
    if not item["service_months"]:
        return None
    base = item["last_service_on"] or item["bought_on"]
    if not base:
        return None
    return add_months(parse_date(base), item["service_months"])


def register(db, clinic, include_retired=False):
    today = _today(clinic)
    rows = db.execute("SELECT * FROM equipment WHERE clinic_id = ?" + ("" if include_retired else " AND retired = 0") + " ORDER BY kind, name",
                      (clinic["id"],)).fetchall()
    out = []
    for r in rows:
        nxt = next_service(r)
        out.append({"item": r, "next": nxt, "overdue": bool(nxt and nxt < today), "soon": bool(nxt and today <= nxt <= today + datetime.timedelta(days=30)),
                    "no_date": bool(r["service_months"] and not nxt),
                    "warranty_left": bool(r["warranty_until"] and r["warranty_until"] >= today.isoformat())})
    return out


def events(db, cid, item_id):
    return db.execute("""SELECT e.*, u.display_name AS by_user FROM equipment_events e LEFT JOIN users u ON u.id = e.created_by
                         WHERE e.clinic_id = ? AND e.equipment_id = ? ORDER BY e.done_on DESC, e.id DESC""", (cid, item_id)).fetchall()


def due_summary(db, clinic):
    reg = register(db, clinic)
    return {"overdue": [x for x in reg if x["overdue"] or x["no_date"]], "faults": [x for x in reg if x["item"]["fault_open"]]}
