"""A private link for one patient. It opens a page with their own visits, quotes and balance, lets them confirm a visit, accept a
quote and fill in their health form. The link is the only secret, so: it is long and random, only a hash of it is stored, it
expires, a new one replaces the old one, and staff can cancel it. The page never shows clinical notes, files or other people."""
import hashlib
import json
import secrets
from datetime import timedelta

from . import audit, billing, i18n, medical, plans as PL, scheduling
from .timeutil import now_local, parse_dt

LINK_DAYS = 30
MAX_PENDING_FORMS = 1


class LinkError(Exception):
    pass


def _hash(token):
    return hashlib.sha256((token or "").encode()).hexdigest()


def create(db, clinic, user_id, pid, days=LINK_DAYS):
    """Returns the raw token. It is shown once. Any earlier link for this patient stops working."""
    cid = clinic["id"]
    p = db.execute("SELECT * FROM patients WHERE id = ? AND clinic_id = ? AND status = 'active'", (pid, cid)).fetchone()
    if not p:
        raise LinkError("That patient was not found, or is archived.")
    if not isinstance(days, int) or not 1 <= days <= 90:
        raise LinkError("A link can last from 1 to 90 days.")
    db.execute("UPDATE patient_links SET revoked = 1 WHERE clinic_id = ? AND patient_id = ? AND revoked = 0", (cid, pid))
    token = secrets.token_urlsafe(24)
    expires = (now_local(clinic["timezone"]).date() + timedelta(days=days)).isoformat()
    cur = db.execute("INSERT INTO patient_links(clinic_id, patient_id, token_hash, expires_on, created_by) VALUES (?,?,?,?,?)",
                     (cid, pid, _hash(token), expires, user_id))
    audit.log(db, cid, user_id, "link.create", "patient", pid)
    return token


def revoke(db, clinic, user_id, pid):
    n = db.execute("UPDATE patient_links SET revoked = 1 WHERE clinic_id = ? AND patient_id = ? AND revoked = 0", (clinic["id"], pid)).rowcount
    if n:
        audit.log(db, clinic["id"], user_id, "link.revoke", "patient", pid)
    return n


def current(db, clinic, pid):
    today = now_local(clinic["timezone"]).date().isoformat()
    return db.execute("""SELECT * FROM patient_links WHERE clinic_id = ? AND patient_id = ? AND revoked = 0 AND expires_on >= ?
                         ORDER BY id DESC LIMIT 1""", (clinic["id"], pid, today)).fetchone()


def resolve(db, token):
    """(link, patient, clinic) for a working link, else None. Looks the same for a wrong, expired or cancelled link."""
    if not token or len(token) > 100:
        return None
    link = db.execute("SELECT * FROM patient_links WHERE token_hash = ? AND revoked = 0", (_hash(token),)).fetchone()
    if not link:
        return None
    clinic = db.execute("SELECT * FROM clinics WHERE id = ?", (link["clinic_id"],)).fetchone()
    if link["expires_on"] < now_local(clinic["timezone"]).date().isoformat():
        return None
    patient = db.execute("SELECT * FROM patients WHERE id = ? AND clinic_id = ? AND status = 'active'", (link["patient_id"], link["clinic_id"])).fetchone()
    if not patient:
        return None
    db.execute("UPDATE patient_links SET last_used_at = datetime('now') WHERE id = ?", (link["id"],))
    return link, patient, clinic


def portal(db, clinic, patient, lang="en"):
    cid, pid = clinic["id"], patient["id"]
    now = now_local(clinic["timezone"]).strftime("%Y-%m-%d %H:%M")
    visits = []
    for a in db.execute("""SELECT a.*, pr.name AS provider_name FROM appointments a LEFT JOIN providers pr ON pr.id = a.provider_id
                           WHERE a.clinic_id = ? AND a.patient_id = ? AND a.status = 'scheduled' AND a.start_at >= ?
                           ORDER BY a.start_at LIMIT 10""", (cid, pid, now)).fetchall():
        start = parse_dt(a["start_at"])
        visits.append({"id": a["id"], "day": i18n.fmt_day_date(start, lang), "time": i18n.fmt_time(start, lang), "dentist": a["provider_name"] or i18n.t(lang, "Our team"),
                       "confirmation": a["confirmation"]})
    quotes = []
    for entry in PL.for_patient(db, cid, pid):
        p = entry["plan"]
        if p["status"] == "presented":
            rows = PL.items(db, cid, p["id"])
            quotes.append({"plan": p, "lines": rows, "total": PL.total(rows)})
    pending = db.execute("SELECT COUNT(*) FROM intake_submissions WHERE clinic_id = ? AND patient_id = ? AND status = 'pending'", (cid, pid)).fetchone()[0]
    h = medical.get_history(db, cid, pid)
    return {"visits": visits, "quotes": quotes, "balance": billing.totals(db, cid, pid)["balance"], "form_pending": pending,
            "history_on_file": bool(h)}


def confirm_visit(db, clinic, patient, appt_id, value):
    if value not in ("confirmed", "declined"):
        raise LinkError("Pick one of the two answers.")
    a = scheduling.get_appointment(db, clinic["id"], appt_id)
    if not a or a["patient_id"] != patient["id"] or a["status"] != "scheduled":
        raise LinkError("That visit was not found.")
    scheduling.set_confirmation(db, clinic["id"], None, appt_id, value)
    audit.log(db, clinic["id"], None, f"portal.visit.{value}", "appointment", appt_id)


def accept_quote(db, clinic, patient, plan_id, signer_name, relation, agreed):
    plan = PL.get(db, clinic["id"], plan_id)
    if not plan or plan["patient_id"] != patient["id"] or plan["status"] != "presented":
        raise LinkError("That quote was not found, or it is not waiting for an answer.")
    try:
        PL.accept(db, clinic, {"id": None}, plan_id, signer_name, relation, "typed", agreed)
    except PL.PlanError as exc:
        raise LinkError(str(exc))
    audit.log(db, clinic["id"], None, "portal.quote.accept", "plan", plan_id)


# ----------------------------------------------------------------------------- health form
FORM_FIELDS = ("conditions", "medications", "allergies", "notes")


def submit_form(db, clinic, patient, link_id, form):
    cid = clinic["id"]
    if db.execute("SELECT COUNT(*) FROM intake_submissions WHERE clinic_id = ? AND patient_id = ? AND status = 'pending'",
                  (cid, patient["id"])).fetchone()[0] >= MAX_PENDING_FORMS:
        raise LinkError("Your last form is still waiting to be checked by the clinic. Thank you.")
    answers = {}
    for f in FORM_FIELDS:
        v = (form.get(f) or "").strip()
        if len(v) > 1500:
            raise LinkError("One of your answers is too long. Please shorten it.")
        answers[f] = v
    answers["pregnant"] = form.get("pregnant") if form.get("pregnant") in ("yes", "no") else ""
    answers["smoker"] = form.get("smoker") if form.get("smoker") in ("never", "former", "current") else ""
    answers["blood_thinners"] = 1 if form.get("blood_thinners") else 0
    answers["agrees_messages"] = 1 if form.get("agrees_messages") else 0
    if not any(answers[f] for f in FORM_FIELDS) and not answers["blood_thinners"] and not answers["pregnant"] and not answers["smoker"]:
        raise LinkError("Please answer at least one question. If nothing applies, write 'none' under conditions.")
    cur = db.execute("INSERT INTO intake_submissions(clinic_id, patient_id, link_id, answers) VALUES (?,?,?,?)",
                     (cid, patient["id"], link_id, json.dumps(answers)))
    audit.log(db, cid, None, "portal.form", "patient", patient["id"])
    return cur.lastrowid


def pending_forms(db, cid, status="pending"):
    return db.execute("""SELECT s.*, p.first_name, p.last_name FROM intake_submissions s JOIN patients p ON p.id = s.patient_id
                         WHERE s.clinic_id = ? AND s.status = ? ORDER BY s.id DESC LIMIT 100""", (cid, status)).fetchall()


def get_form(db, cid, sid):
    row = db.execute("""SELECT s.*, p.first_name, p.last_name FROM intake_submissions s JOIN patients p ON p.id = s.patient_id
                        WHERE s.id = ? AND s.clinic_id = ?""", (sid, cid)).fetchone()
    return (row, json.loads(row["answers"])) if row else (None, None)


def accept_form(db, clinic, user_id, sid):
    """A clinician checks the answers and they become the patient's medical history."""
    row, answers = get_form(db, clinic["id"], sid)
    if not row:
        raise LookupError("form")
    if row["status"] != "pending":
        raise LinkError("That form has already been dealt with.")
    today = now_local(clinic["timezone"]).date().isoformat()
    medical.save_history(db, clinic, user_id, row["patient_id"], answers, today)
    if answers.get("agrees_messages"):
        db.execute("UPDATE patients SET reminders_ok = 1, updated_at = datetime('now') WHERE id = ? AND clinic_id = ?", (row["patient_id"], clinic["id"]))
        audit.log(db, clinic["id"], user_id, "consent.messages", "patient", row["patient_id"], "Health form")
    db.execute("UPDATE intake_submissions SET status = 'accepted', reviewed_by = ?, reviewed_at = datetime('now') WHERE id = ?", (user_id, sid))
    audit.log(db, clinic["id"], user_id, "intake.accept", "patient", row["patient_id"])


def dismiss_form(db, clinic, user_id, sid):
    row, _ = get_form(db, clinic["id"], sid)
    if not row:
        raise LookupError("form")
    if row["status"] != "pending":
        raise LinkError("That form has already been dealt with.")
    db.execute("UPDATE intake_submissions SET status = 'dismissed', reviewed_by = ?, reviewed_at = datetime('now') WHERE id = ?", (user_id, sid))
    audit.log(db, clinic["id"], user_id, "intake.dismiss", "patient", row["patient_id"])
