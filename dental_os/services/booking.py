"""Online booking. The public page offers real free times, but what comes out of it is a request. Nothing lands in the diary until
a member of staff accepts it, so a stranger can never take a slot, double-book a dentist or put a made-up name on a record."""
import secrets
from datetime import datetime, timedelta

from . import audit, i18n, outbox, patients as P, scheduling, serialize, webhooks
from . import throttle
from .timeutil import fmt_day_date, fmt_time, now_local, parse_dt, to_str

MAX_PER_ADDRESS_HOUR = 3
MAX_PER_CLINIC_HOUR = 25
MAX_NEW_OPEN = 60
MAX_SLOTS_PER_DAY = 8


class BookingError(Exception):
    def __init__(self, message, parts=None):
        super().__init__(message)
        self.parts = parts                 # several sentences, each of which can be translated on its own


def new_token():
    return secrets.token_urlsafe(9)


def save_settings(db, clinic, user_id, on, days_ahead, notice_hours, minutes, note, regenerate=False):
    c = outbox.comms(db, clinic["id"])
    if not isinstance(days_ahead, int) or not 1 <= days_ahead <= 60:
        raise BookingError("Show from 1 to 60 days ahead.")
    if not isinstance(notice_hours, int) or not 0 <= notice_hours <= 168:
        raise BookingError("Notice must be from 0 to 168 hours.")
    if minutes not in scheduling.DURATIONS:
        raise BookingError("Pick a visit length from the list.")
    note = (note or "").strip()
    if len(note) > 300:
        raise BookingError("Keep the note under 300 characters.")
    token = c["booking_token"]
    if not token or regenerate:
        token = new_token()
    db.execute("""UPDATE clinic_comms SET booking_on = ?, booking_token = ?, booking_days_ahead = ?, booking_notice_hours = ?,
                  booking_minutes = ?, booking_note = ? WHERE clinic_id = ?""",
               (1 if on else 0, token, days_ahead, notice_hours, minutes, note or None, clinic["id"]))
    audit.log(db, clinic["id"], user_id, "booking.settings", None, None, "new link" if regenerate else None)


def clinic_for_token(db, token):
    """(clinic row, comms row) for an open booking page, or (None, None)."""
    row = db.execute("""SELECT cc.*, c.id AS cid FROM clinic_comms cc JOIN clinics c ON c.id = cc.clinic_id
                        WHERE cc.booking_token = ? AND cc.booking_on = 1""", (token or "",)).fetchone()
    if not row:
        return None, None
    return db.execute("SELECT * FROM clinics WHERE id = ?", (row["cid"],)).fetchone(), row


def providers(db, cid):
    return db.execute("SELECT * FROM providers WHERE clinic_id = ? AND active = 1 ORDER BY name", (cid,)).fetchall()


def offered(db, clinic, c, provider_id=None, now=None, lang="en"):
    """Days with free times: [{'day': date, 'slots': [{'value': 'YYYY-MM-DD HH:MM', 'label': '9:30 am', 'provider_id': n}]}]."""
    now = now or now_local(clinic["timezone"])
    earliest = now + timedelta(hours=c["booking_notice_hours"])
    people = [p for p in providers(db, clinic["id"]) if provider_id in (None, p["id"])]
    days = []
    for offset in range(c["booking_days_ahead"] + 1):
        day = (now + timedelta(days=offset)).date()
        by_time = {}
        for p in people:
            for s in scheduling.free_slots(db, clinic, p["id"], day, c["booking_minutes"], now=earliest):
                by_time.setdefault(s["time"], {"value": f"{day.isoformat()} {s['time']}", "label": s["label"], "provider_id": p["id"]})
        slots = [by_time[t] for t in sorted(by_time)]
        if len(slots) > MAX_SLOTS_PER_DAY:
            step = len(slots) / MAX_SLOTS_PER_DAY
            slots = [slots[int(i * step)] for i in range(MAX_SLOTS_PER_DAY)]
        if slots:
            if lang != "en":
                for s in slots:
                    s["label"] = i18n.fmt_time(datetime.strptime(s["value"], "%Y-%m-%d %H:%M"), lang)
            days.append({"day": day, "label": i18n.fmt_day_date(day, lang), "slots": slots})
    return days


def _slot_is_free(db, clinic, c, start, provider_id):
    day = start.date()
    now = now_local(clinic["timezone"]) + timedelta(hours=c["booking_notice_hours"])
    ids = [provider_id] if provider_id else [p["id"] for p in providers(db, clinic["id"])]
    for pid in ids:
        if any(s["time"] == start.strftime("%H:%M") for s in scheduling.free_slots(db, clinic, pid, day, c["booking_minutes"], now=now)):
            return True
    return False


def submit(db, clinic, c, form, address_key):
    """Take a request from the public page. Returns the id, or None when it was quietly dropped as spam."""
    if (form.get("website") or "").strip():            # a hidden field only a program fills in
        return None
    cid = clinic["id"]
    if not throttle.allow(db, f"book-ip:{address_key}", MAX_PER_ADDRESS_HOUR, 60):
        raise BookingError("Too many requests from this connection. Please try again later, or contact the clinic directly.")
    if not throttle.allow(db, f"book-clinic:{cid}", MAX_PER_CLINIC_HOUR, 60):
        raise BookingError("We are getting a lot of requests right now. Please try again later, or contact the clinic directly.")
    if db.execute("SELECT COUNT(*) FROM booking_requests WHERE clinic_id = ? AND status = 'new'", (cid,)).fetchone()[0] >= MAX_NEW_OPEN:
        raise BookingError("We cannot take more requests online right now. Please contact the clinic directly.")
    try:
        data = P.clean({"first_name": form.get("first_name"), "last_name": form.get("last_name"), "phone": form.get("phone"),
                        "email": form.get("email")}, None, None, country=clinic["country"])
    except P.ValidationError as exc:
        raise BookingError(" ".join(exc.errors.values()), parts=list(exc.errors.values()))
    reason = form.get("reason")
    if reason not in scheduling.CATEGORIES:
        raise BookingError("Pick what the visit is for.")
    try:
        start = datetime.strptime((form.get("slot") or "").strip(), "%Y-%m-%d %H:%M")
    except ValueError:
        raise BookingError("Pick one of the times shown.")
    provider_id = None
    raw = (form.get("provider_id") or "").strip()
    if raw:
        if not raw.isdigit() or not db.execute("SELECT 1 FROM providers WHERE id = ? AND clinic_id = ? AND active = 1", (int(raw), cid)).fetchone():
            raise BookingError("Pick a dentist from the list.")
        provider_id = int(raw)
    if not _slot_is_free(db, clinic, c, start, provider_id):
        raise BookingError("Sorry, that time has just been taken or is no longer offered. Please pick another.")
    if db.execute("""SELECT 1 FROM booking_requests WHERE clinic_id = ? AND phone = ? AND wanted_start = ? AND status = 'new'""",
                  (cid, data["phone"], to_str(start))).fetchone():
        raise BookingError("We already have a request from this number for that time.")
    note = (form.get("note") or "").strip()[:300]
    cur = db.execute(
        """INSERT INTO booking_requests(clinic_id, first_name, last_name, phone, email, reason, wanted_start, provider_id, note, agrees_messages)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (cid, data["first_name"], data["last_name"], data["phone"], data["email"], reason, to_str(start), provider_id, note or None,
         1 if form.get("agrees_messages") else 0))
    audit.log(db, cid, None, "booking.request", "booking_request", cur.lastrowid)
    webhooks.emit(db, cid, "booking.requested", serialize.booking_request(get(db, cid, cur.lastrowid)))
    return cur.lastrowid


# ----------------------------------------------------------------------------- staff side
def requests(db, cid, status="new"):
    sql = ("SELECT r.*, pr.name AS provider_name FROM booking_requests r LEFT JOIN providers pr ON pr.id = r.provider_id "
           "WHERE r.clinic_id = ?")
    args = [cid]
    if status != "all":
        sql += " AND r.status = ?"
        args.append(status)
    return db.execute(sql + " ORDER BY r.wanted_start, r.id LIMIT 200", args).fetchall()


def get(db, cid, rid):
    return db.execute("""SELECT r.*, pr.name AS provider_name FROM booking_requests r LEFT JOIN providers pr ON pr.id = r.provider_id
                         WHERE r.id = ? AND r.clinic_id = ?""", (rid, cid)).fetchone()


def matches(db, cid, req):
    """Patients who might be the same person: same phone, or same name."""
    return db.execute(
        """SELECT * FROM patients WHERE clinic_id = ? AND status = 'active' AND (phone = ?
             OR (lower(first_name) = lower(?) AND lower(last_name) = lower(?))) ORDER BY last_name, first_name LIMIT 6""",
        (cid, req["phone"], req["first_name"], req["last_name"])).fetchall()


def accept(db, clinic, user_id, rid, patient_id=None, provider_id=None, start=None):
    """Make the booking. patient_id None creates a new patient. start is a datetime, default the time asked for."""
    cid = clinic["id"]
    req = get(db, cid, rid)
    if not req:
        raise LookupError("request")
    if req["status"] != "new":
        raise BookingError("That request has already been dealt with.")
    c = outbox.comms(db, cid)
    start = start or parse_dt(req["wanted_start"])
    provider_id = provider_id or req["provider_id"]
    people = [provider_id] if provider_id else [p["id"] for p in providers(db, cid)]
    chosen = None
    for pid in people:
        for s in scheduling.free_slots(db, clinic, pid, start.date(), c["booking_minutes"], now=now_local(clinic["timezone"])):
            if s["time"] == start.strftime("%H:%M"):
                chosen = (pid, s["chair_id"])
                break
        if chosen:
            break
    if not chosen:
        raise BookingError("That time is not free any more. Pick another time and dentist, or decline the request.")
    if patient_id is not None:
        patient = P.get(db, cid, patient_id)
        if not patient or patient["status"] != "active":
            raise BookingError("Pick a patient from this clinic.")
        if req["agrees_messages"] and not patient["reminders_ok"]:
            db.execute("UPDATE patients SET reminders_ok = 1, updated_at = datetime('now') WHERE id = ? AND clinic_id = ?", (patient_id, cid))
            audit.log(db, cid, user_id, "consent.messages", "patient", patient_id, "Online booking form")
    else:
        patient_id = P.create(db, cid, user_id, {
            "first_name": req["first_name"], "last_name": req["last_name"], "dob": None, "gender": "", "phone": req["phone"],
            "email": req["email"], "address": None, "guarantor_id": None, "preferred_contact": "whatsapp",
            "reminders_ok": req["agrees_messages"], "offers_ok": 0, "notes": "Asked for a visit on the online booking page"},
            allow_duplicates=True)
    appt = scheduling.book(db, clinic, user_id, patient_id, chosen[0], chosen[1], start, c["booking_minutes"], category=req["reason"],
                           notes=("Online request: " + req["note"])[:300] if req["note"] else "Online request")
    db.execute("""UPDATE booking_requests SET status = 'accepted', patient_id = ?, appointment_id = ?, decided_by = ?, decided_at = datetime('now')
                  WHERE id = ?""", (patient_id, appt, user_id, rid))
    audit.log(db, cid, user_id, "booking.accept", "booking_request", rid)
    return appt


def decline(db, clinic, user_id, rid, reason):
    req = get(db, clinic["id"], rid)
    if not req:
        raise LookupError("request")
    if req["status"] != "new":
        raise BookingError("That request has already been dealt with.")
    reason = (reason or "").strip()
    if len(reason) < 3:
        raise BookingError("Write a short reason, for example: no dentist that day.")
    db.execute("UPDATE booking_requests SET status = 'declined', decline_reason = ?, decided_by = ?, decided_at = datetime('now') WHERE id = ?",
               (reason[:200], user_id, rid))
    audit.log(db, clinic["id"], user_id, "booking.decline", "booking_request", rid)
