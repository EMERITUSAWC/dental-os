"""The keyed REST API, version 1. JSON in and out. A key belongs to one clinic and can only ever see that clinic.
Sessions and cookies play no part here, so the forms' CSRF protection does not apply: a request either carries a valid key or is refused."""
import re
from datetime import datetime, timedelta

from flask import Blueprint, g, jsonify, request

from .db import get_db
from .services import (apikeys, audit, billing, patients as P, scheduling, serialize, throttle)
from .services.timeutil import parse_date, to_str

bp = Blueprint("api", __name__, url_prefix="/api/v1")

RATE_PER_MINUTE = 300
FLAGS = {"agreed_to_appointment_messages": "reminders_ok", "agreed_to_offers": "offers_ok"}
PATIENT_FIELDS = ("first_name", "last_name", "dob", "gender", "phone", "email", "address", "preferred_contact", "notes")


def fail(status, code, message, **extra):
    resp = jsonify({"error": {"code": code, "message": message, **extra}})
    resp.status_code = status
    return resp


@bp.before_request
def authenticate():
    db = get_db()
    key = apikeys.authenticate(db, request.headers.get("Authorization"))
    g.user = None
    g.clinic = None
    g.api_key = None
    if not key:
        resp = fail(401, "unauthorised", "Send a valid key as 'Authorization: Bearer <key>'.")
        resp.headers["WWW-Authenticate"] = "Bearer"
        return resp
    g.api_key = key
    g.clinic = db.execute("SELECT * FROM clinics WHERE id = ?", (key["clinic_id"],)).fetchone()
    if not throttle.allow(db, f"api:{key['id']}", RATE_PER_MINUTE, 1):
        resp = fail(429, "rate_limited", f"Too many requests. The limit is {RATE_PER_MINUTE} a minute for each key.")
        resp.headers["Retry-After"] = "60"
        return resp
    if request.method not in ("GET", "HEAD"):
        if key["access"] != "write":
            return fail(403, "read_only_key", "This key can only read. Make a read and write key to change data.")
        if not isinstance(request.get_json(silent=True), dict):
            return fail(400, "bad_json", "Send a JSON object with 'Content-Type: application/json'.")


def _body():
    return request.get_json(silent=True)


def _page():
    limit = min(max(request.args.get("limit", 50, type=int) or 50, 1), 200)
    after = max(request.args.get("after", 0, type=int) or 0, 0)
    return limit, after


def _listing(rows, limit, shape):
    more = len(rows) > limit
    rows = rows[:limit]
    return jsonify({"data": [shape(r) for r in rows], "next_after": rows[-1]["id"] if more else None})


def _text_form(body, keys):
    """Plain strings for the patient checker, so a number or null in the JSON cannot crash it."""
    out = {}
    for k in keys:
        if k in body:
            v = body[k]
            out[k] = "" if v is None else str(v).strip() if not isinstance(v, (dict, list)) else ""
    return out


def _flag(v):
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


@bp.get("/clinic")
def clinic():
    return jsonify({"data": serialize.clinic(g.clinic)})


@bp.get("/providers")
def providers():
    rows = get_db().execute("SELECT id, name FROM providers WHERE clinic_id = ? AND active = 1 ORDER BY id", (g.clinic["id"],)).fetchall()
    return jsonify({"data": [{"id": r["id"], "name": r["name"]} for r in rows]})


@bp.get("/chairs")
def chairs():
    rows = get_db().execute("SELECT id, name FROM chairs WHERE clinic_id = ? AND active = 1 ORDER BY id", (g.clinic["id"],)).fetchall()
    return jsonify({"data": [{"id": r["id"], "name": r["name"]} for r in rows]})


# ----------------------------------------------------------------------------- patients
@bp.get("/patients")
def patients_list():
    limit, after = _page()
    sql, args = "SELECT * FROM patients WHERE clinic_id = ? AND id > ?", [g.clinic["id"], after]
    if request.args.get("include_archived") != "1":
        sql += " AND status = 'active'"
    q = (request.args.get("q") or "").strip()
    if q:
        like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        sql += " AND (first_name LIKE ? ESCAPE '\\' OR last_name LIKE ? ESCAPE '\\' OR phone LIKE ? ESCAPE '\\' OR email LIKE ? ESCAPE '\\')"
        args += [like] * 4
    since = (request.args.get("updated_since") or "").strip()
    if since:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}( \d{2}:\d{2}(:\d{2})?)?", since):
            return fail(400, "bad_date", "updated_since must look like 2026-11-30 or 2026-11-30 14:05 (UTC).")
        sql += " AND updated_at >= ?"
        args.append(since)
    rows = get_db().execute(sql + " ORDER BY id LIMIT ?", (*args, limit + 1)).fetchall()
    return _listing(rows, limit, serialize.patient)


@bp.get("/patients/<int:pid>")
def patient_get(pid):
    row = P.get(get_db(), g.clinic["id"], pid)
    return jsonify({"data": serialize.patient(row)}) if row else fail(404, "not_found", "No such patient.")


@bp.get("/patients/<int:pid>/balance")
def patient_balance(pid):
    db = get_db()
    if not P.get(db, g.clinic["id"], pid):
        return fail(404, "not_found", "No such patient.")
    t = billing.totals(db, g.clinic["id"], pid)
    return jsonify({"data": {"patient_id": pid, "currency": g.clinic["currency"], "charges_p": t["charges"], "adjustments_p": t["adjustments"],
                             "paid_p": t["paid"], "balance_p": t["balance"]}})


def _patient_form(body, base=None):
    form = dict(base or {})
    form.update(_text_form(body, PATIENT_FIELDS))
    for api_name, field in FLAGS.items():
        if api_name in body:
            form[field] = "1" if _flag(body[api_name]) else ""
    return form


@bp.post("/patients")
def patient_create():
    db, body, clinic = get_db(), _body(), g.clinic
    try:
        data = P.clean(_patient_form(body), db, clinic["id"], country=clinic["country"])
        pid = P.create(db, clinic["id"], None, data, allow_duplicates=_flag(body.get("allow_duplicate", False)))
    except P.ValidationError as exc:
        db.rollback()
        return fail(422, "invalid", "Some details need fixing.", fields=exc.errors)
    except P.DuplicateWarning as exc:
        db.rollback()
        return fail(409, "possible_duplicate", "A patient like this already exists. Send allow_duplicate: true to add anyway.",
                    matches=[m["id"] for m in exc.matches])
    audit.log(db, clinic["id"], None, "api.write", "api_key", g.api_key["id"], "POST /patients")
    resp = jsonify({"data": serialize.patient(P.get(db, clinic["id"], pid))})
    resp.status_code = 201
    return resp


@bp.patch("/patients/<int:pid>")
def patient_update(pid):
    db, body, clinic = get_db(), _body(), g.clinic
    row = P.get(db, clinic["id"], pid)
    if not row:
        return fail(404, "not_found", "No such patient.")
    base = {k: ("" if row[k] is None else str(row[k])) for k in PATIENT_FIELDS}
    base["guarantor_id"] = "" if row["guarantor_id"] is None else str(row["guarantor_id"])
    base["reminders_ok"] = "1" if row["reminders_ok"] else ""
    base["offers_ok"] = "1" if row["offers_ok"] else ""
    try:
        data = P.clean(_patient_form(body, base), db, clinic["id"], patient_id=pid, country=clinic["country"])
        P.update(db, clinic["id"], None, pid, data)
    except P.ValidationError as exc:
        db.rollback()
        return fail(422, "invalid", "Some details need fixing.", fields=exc.errors)
    audit.log(db, clinic["id"], None, "api.write", "api_key", g.api_key["id"], f"PATCH /patients/{pid}")
    return jsonify({"data": serialize.patient(P.get(db, clinic["id"], pid))})


# ----------------------------------------------------------------------------- appointments
_APPT_STATUSES = ("planned", "unscheduled", "asap", "scheduled", "complete", "broken")


@bp.get("/appointments")
def appointments_list():
    limit, after = _page()
    sql, args = "SELECT * FROM appointments WHERE clinic_id = ? AND id > ?", [g.clinic["id"], after]
    for name, op in (("from", ">="), ("to", "<")):
        raw = request.args.get(name)
        if raw:
            try:
                d = parse_date(raw)
            except ValueError:
                return fail(400, "bad_date", f"'{name}' must look like 2026-11-30.")
            if name == "to":
                d = d + timedelta(days=1)
            sql += f" AND start_at {op} ?"
            args.append(f"{d} 00:00")
    if request.args.get("patient_id", type=int):
        sql += " AND patient_id = ?"
        args.append(request.args.get("patient_id", type=int))
    status = request.args.get("status")
    if status:
        if status not in _APPT_STATUSES:
            return fail(400, "bad_status", "Unknown status.")
        sql += " AND status = ?"
        args.append(status)
    rows = get_db().execute(sql + " ORDER BY id LIMIT ?", (*args, limit + 1)).fetchall()
    return _listing(rows, limit, serialize.appointment)


@bp.get("/appointments/<int:aid>")
def appointment_get(aid):
    row = get_db().execute("SELECT * FROM appointments WHERE id = ? AND clinic_id = ?", (aid, g.clinic["id"])).fetchone()
    return jsonify({"data": serialize.appointment(row)}) if row else fail(404, "not_found", "No such appointment.")


@bp.get("/free-slots")
def free_slots():
    db, clinic = get_db(), g.clinic
    provider_id, duration = request.args.get("provider_id", type=int), request.args.get("duration", 30, type=int)
    try:
        day = parse_date(request.args.get("date", ""))
    except ValueError:
        return fail(400, "bad_date", "date must look like 2026-11-30.")
    if duration not in scheduling.DURATIONS:
        return fail(400, "bad_duration", f"duration must be one of {', '.join(map(str, scheduling.DURATIONS))}.")
    if not provider_id:
        return fail(400, "missing", "provider_id is needed.")
    slots = scheduling.free_slots(db, clinic, provider_id, day, duration)
    return jsonify({"data": [{"start_at": f"{day} {s['time']}", "chair_id": s["chair_id"]} for s in slots]})


@bp.post("/appointments")
def appointment_create():
    db, body, clinic = get_db(), _body(), g.clinic

    def num(key, default=None):
        v = body.get(key, default)
        return v if isinstance(v, int) and not isinstance(v, bool) else None
    patient_id, provider_id, chair_id, duration = num("patient_id"), num("provider_id"), num("chair_id"), num("duration_min", 30)
    if not patient_id or not provider_id or duration is None:
        return fail(422, "invalid", "patient_id, provider_id and start_at are needed. duration_min must be a whole number.")
    try:
        start = datetime.strptime(str(body.get("start_at", "")), "%Y-%m-%d %H:%M")
    except ValueError:
        return fail(422, "invalid", "start_at must look like '2026-11-30 14:30' in the clinic's own time.")
    category = body.get("category", "Consultation")
    if category not in scheduling.CATEGORIES:
        return fail(422, "invalid", "category must be one of the clinic's visit reasons.", allowed=scheduling.CATEGORIES)
    notes = body.get("notes")
    notes = str(notes).strip()[:300] if isinstance(notes, str) and notes.strip() else None
    try:
        if chair_id is None:
            free = [s for s in scheduling.free_slots(db, clinic, provider_id, start.date(), duration) if s["time"] == start.strftime("%H:%M")]
            if not free:
                raise scheduling.SchedulingError("That time is not free for that dentist.")
            chair_id = free[0]["chair_id"]
        aid = scheduling.book(db, clinic, None, patient_id, provider_id, chair_id, start, duration, category=category, notes=notes)
    except scheduling.SchedulingError as exc:
        db.rollback()
        return fail(409, "cannot_book", str(exc))
    audit.log(db, clinic["id"], None, "api.write", "api_key", g.api_key["id"], "POST /appointments")
    resp = jsonify({"data": serialize.appointment(db.execute("SELECT * FROM appointments WHERE id = ?", (aid,)).fetchone())})
    resp.status_code = 201
    return resp


@bp.post("/appointments/<int:aid>/status")
def appointment_status(aid):
    db, body, clinic = get_db(), _body(), g.clinic
    try:
        scheduling.set_status(db, clinic["id"], None, aid, body.get("status"), body.get("break_reason"))
    except LookupError:
        db.rollback()
        return fail(404, "not_found", "No such appointment.")
    except scheduling.SchedulingError as exc:
        db.rollback()
        return fail(409, "cannot_change", str(exc))
    audit.log(db, clinic["id"], None, "api.write", "api_key", g.api_key["id"], f"POST /appointments/{aid}/status")
    return jsonify({"data": serialize.appointment(db.execute("SELECT * FROM appointments WHERE id = ?", (aid,)).fetchone())})
