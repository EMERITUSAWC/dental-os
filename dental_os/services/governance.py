"""Complaints and patient safety incidents (version 0.37).

Complaints: who complained, how, about what, and every step after: acknowledged, looked into, answered, closed, with what was
learned. The acknowledgement is due three working days after the complaint arrives; that is what the NHS complaints rules in England
ask, and it is a fair standard anywhere. Working days here are Monday to Friday, less the days the clinic has
listed as closed (version 0.47); a public holiday not on that list is counted, so the due date can only be early, never late. A response date is agreed with the person and kept. Complaint files stay out of the clinical record.

Incidents: anything that went wrong or nearly did (a sharps injury, the wrong tooth, a faint in the chair, an allergic reaction,
a fall, a failed autoclave), how much harm it did, what was done at once, why it happened, and what will stop it happening again.
For harm that is moderate or worse, a clinic in England may owe the patient the statutory duty of candour: tell them in person as soon
as reasonably practicable, apologise, give a true account, and follow up in writing. The register keeps each of those steps.

Nothing is deleted. No web framework here."""
import datetime

from . import audit
from . import closeddays as CD

CHANNELS = {"in_person": "In person", "phone": "Phone", "email": "Email", "letter": "Letter", "whatsapp": "WhatsApp", "online_review": "Online review",
            "other": "Other"}
CATEGORIES = {"treatment": "Treatment or its result", "cost": "Cost or billing", "attitude": "Staff attitude", "waiting": "Waiting time",
              "communication": "Communication or information", "appointments": "Appointments", "privacy": "Privacy or records",
              "premises": "Premises or cleanliness", "other": "Other"}
OUTCOMES = {"upheld": "Upheld", "partly": "Partly upheld", "not_upheld": "Not upheld", "withdrawn": "Withdrawn", "resolved_early": "Resolved on the spot"}
C_STATUSES = {"open": "Open, not yet acknowledged", "acknowledged": "Acknowledged, being looked into", "responded": "Answered, waiting to close",
              "closed": "Closed"}

KINDS = {"sharps": "Sharps or needlestick injury", "wrong_site": "Wrong tooth or wrong site", "medical_emergency": "Medical emergency in the clinic",
         "allergy": "Allergic or drug reaction", "medication": "Medicine or prescribing error", "fall": "Slip, trip or fall",
         "equipment": "Equipment failure", "infection_control": "Infection control or sterilisation failure", "swallowed": "Swallowed or inhaled object",
         "records": "Wrong record or wrong patient", "near_miss": "Near miss (no harm)", "other": "Other"}
HARM = {"none": "No harm", "low": "Low harm", "moderate": "Moderate harm", "severe": "Severe harm", "psychological": "Prolonged psychological harm",
        "death": "Death"}
CANDOUR_HARM = ("moderate", "severe", "psychological", "death")
I_STATUSES = {"open": "Open", "reviewed": "Reviewed, actions agreed", "closed": "Closed"}


class GovError(ValueError):
    pass


def _day(text, label, required=True):
    text = (text or "").strip()
    if not text:
        if required:
            raise GovError(label)
        return None
    try:
        return datetime.date.fromisoformat(text).isoformat()
    except ValueError:
        raise GovError(label)


def add_working_days(day, n, closed=frozenset()):
    """n working days after `day`: Monday to Friday, skipping the clinic's closed days (version 0.47)."""
    d = datetime.date.fromisoformat(day)
    while n > 0:
        d += datetime.timedelta(days=1)
        if d.weekday() < 5 and d.isoformat() not in closed:
            n -= 1
    return d.isoformat()


def _patient(db, cid, pid):
    if not pid:
        return None
    if not db.execute("SELECT 1 FROM patients WHERE id = ? AND clinic_id = ?", (pid, cid)).fetchone():
        raise GovError("That patient was not found.")
    return pid


def _text(v, n):
    return (v or "").strip()[:n] or None


# ------------------------------------------------------------------------------------------------------------- complaints
def add_complaint(db, clinic, user_id, form, today=None):
    cid = clinic["id"]
    pid = _patient(db, cid, int(form["patient_id"]) if str(form.get("patient_id") or "").isdigit() else None)
    who = _text(form.get("complainant"), 120)
    if not who and not pid:
        raise GovError("Say who complained, or pick the patient.")
    channel = form.get("channel") or "in_person"
    category = form.get("category") or "other"
    if channel not in CHANNELS or category not in CATEGORIES:
        raise GovError("Pick how the complaint came in and what it is about.")
    summary = _text(form.get("summary"), 4000)
    if not summary:
        raise GovError("Write down what the complaint is, in the person's own words where you can.")
    received = _day(form.get("received_on"), "Use a real date for when the complaint came in.", False) or (today or datetime.date.today()).isoformat()
    kid = db.execute("""INSERT INTO complaints(clinic_id, patient_id, complainant, contact, channel, category, about_staff, summary, wanted,
                        received_on, ack_due, status, owner_id, created_by) VALUES (?,?,?,?,?,?,?,?,?,?,?, 'open', ?, ?)""",
                     (cid, pid, who, _text(form.get("contact"), 200), channel, category, _text(form.get("about_staff"), 200), summary,
                      _text(form.get("wanted"), 1000), received, add_working_days(received, 3, CD.days_set(db, cid)),
                      int(form["owner_id"]) if str(form.get("owner_id") or "").isdigit() else user_id, user_id)).lastrowid
    audit.log(db, cid, user_id, "complaint.add", "patient" if pid else None, pid, category)
    return kid


def complaint(db, cid, kid):
    return db.execute("""SELECT k.*, p.first_name, p.last_name, u.display_name AS owner_name FROM complaints k LEFT JOIN patients p ON p.id = k.patient_id
                         LEFT JOIN users u ON u.id = k.owner_id WHERE k.id = ? AND k.clinic_id = ?""", (kid, cid)).fetchone()


def complaints(db, cid, status=None):
    sql = """SELECT k.*, p.first_name, p.last_name FROM complaints k LEFT JOIN patients p ON p.id = k.patient_id WHERE k.clinic_id = ?"""
    args = [cid]
    if status == "not_closed":
        sql += " AND k.status != 'closed'"
    elif status:
        sql += " AND k.status = ?"
        args.append(status)
    return db.execute(sql + " ORDER BY k.received_on DESC, k.id DESC", args).fetchall()


def steps(db, kid):
    return db.execute("""SELECT s.*, u.display_name FROM complaint_steps s LEFT JOIN users u ON u.id = s.created_by
                         WHERE s.complaint_id = ? ORDER BY s.id""", (kid,)).fetchall()


def _step(db, kid, user_id, what, note):
    cid = db.execute("SELECT clinic_id FROM complaints WHERE id = ?", (kid,)).fetchone()[0]
    db.execute("INSERT INTO complaint_steps(clinic_id, complaint_id, what, note, created_by) VALUES (?,?,?,?,?)", (cid, kid, what, note, user_id))


def acknowledge(db, clinic, user_id, kid, on, response_due, note=None):
    k = complaint(db, clinic["id"], kid)
    if not k or k["status"] != "open":
        raise GovError("Only a complaint not yet acknowledged can be acknowledged.")
    on = _day(on, "Use a real date for the acknowledgement.")
    if on < k["received_on"]:
        raise GovError("It can't be acknowledged before it came in.")
    due = _day(response_due, "Agree a date for the full answer with the person, and enter it.")
    if due < on:
        raise GovError("The answer can't be due before the acknowledgement.")
    db.execute("UPDATE complaints SET status = 'acknowledged', acknowledged_on = ?, response_due = ? WHERE id = ?", (on, due, kid))
    _step(db, kid, user_id, "acknowledged", _text(note, 2000))
    audit.log(db, clinic["id"], user_id, "complaint.ack", "patient" if k["patient_id"] else None, k["patient_id"])


def add_note(db, clinic, user_id, kid, note):
    k = complaint(db, clinic["id"], kid)
    if not k or k["status"] == "closed":
        raise GovError("Notes can only be added to a complaint that is not closed.")
    note = _text(note, 4000)
    if not note:
        raise GovError("Write the note.")
    _step(db, kid, user_id, "note", note)


def respond(db, clinic, user_id, kid, on, outcome, note):
    k = complaint(db, clinic["id"], kid)
    if not k or k["status"] not in ("open", "acknowledged"):
        raise GovError("Only an open complaint can be answered.")
    on = _day(on, "Use a real date for the answer.")
    if outcome not in OUTCOMES:
        raise GovError("Pick the outcome.")
    if on < k["received_on"]:
        raise GovError("It can't be answered before it came in.")
    note = _text(note, 4000)
    if not note:
        raise GovError("Write down what the answer said.")
    db.execute("UPDATE complaints SET status = 'responded', responded_on = ?, outcome = ?, acknowledged_on = COALESCE(acknowledged_on, ?) WHERE id = ?",
               (on, outcome, on, kid))
    _step(db, kid, user_id, "responded", note)
    audit.log(db, clinic["id"], user_id, "complaint.respond", "patient" if k["patient_id"] else None, k["patient_id"], outcome)


def close(db, clinic, user_id, kid, learning, escalated_to=None):
    k = complaint(db, clinic["id"], kid)
    if not k or k["status"] != "responded":
        raise GovError("A complaint is closed after it has been answered.")
    learning = _text(learning, 4000)
    if not learning:
        raise GovError("Write what the clinic learned or changed, even if it is 'nothing to change, and why'.")
    db.execute("UPDATE complaints SET status = 'closed', closed_on = date('now'), learning = ?, escalated_to = ? WHERE id = ?",
               (learning, _text(escalated_to, 200), kid))
    _step(db, kid, user_id, "closed", learning)
    audit.log(db, clinic["id"], user_id, "complaint.close", "patient" if k["patient_id"] else None, k["patient_id"])


def complaint_summary(db, cid, start, end):
    rows = db.execute("SELECT * FROM complaints WHERE clinic_id = ? AND received_on BETWEEN ? AND ?", (cid, start, end)).fetchall()
    acked = [r for r in rows if r["acknowledged_on"]]
    on_time = [r for r in acked if r["acknowledged_on"] <= r["ack_due"]]
    answered = [r for r in rows if r["responded_on"] and r["response_due"]]
    by_cat = {}
    for r in rows:
        by_cat[r["category"]] = by_cat.get(r["category"], 0) + 1
    by_outcome = {}
    for r in rows:
        if r["outcome"]:
            by_outcome[r["outcome"]] = by_outcome.get(r["outcome"], 0) + 1
    return {"total": len(rows), "acknowledged": len(acked), "ack_on_time": len(on_time),
            "answered_on_time": len([r for r in answered if r["responded_on"] <= r["response_due"]]), "answered": len(answered),
            "open": len([r for r in rows if r["status"] != "closed"]), "by_category": by_cat, "by_outcome": by_outcome}


def late(db, cid, today=None):
    """Complaints not acknowledged by their due date, or not answered by the agreed date."""
    today = (today or datetime.date.today()).isoformat()
    return db.execute("""SELECT k.*, p.first_name, p.last_name FROM complaints k LEFT JOIN patients p ON p.id = k.patient_id WHERE k.clinic_id = ?
                         AND ((k.status = 'open' AND k.ack_due < ?) OR (k.status = 'acknowledged' AND k.response_due < ?)) ORDER BY k.received_on""",
                      (cid, today, today)).fetchall()


# -------------------------------------------------------------------------------------------------------------- incidents
def add_incident(db, clinic, user_id, form, today=None):
    cid = clinic["id"]
    kind, harm = form.get("kind"), form.get("harm") or "none"
    if kind not in KINDS:
        raise GovError("Pick what kind of incident it was.")
    if harm not in HARM:
        raise GovError("Pick how much harm was done.")
    pid = _patient(db, cid, int(form["patient_id"]) if str(form.get("patient_id") or "").isdigit() else None)
    what = _text(form.get("what_happened"), 4000)
    if not what:
        raise GovError("Write down what happened.")
    occurred = _day(form.get("occurred_on"), "Use a real date for when it happened.", False) or (today or datetime.date.today()).isoformat()
    if occurred > (today or datetime.date.today()).isoformat():
        raise GovError("An incident can't be in the future.")
    iid = db.execute("""INSERT INTO safety_incidents(clinic_id, patient_id, kind, harm, occurred_on, place, people, what_happened, immediate_action,
                        status, created_by) VALUES (?,?,?,?,?,?,?,?,?, 'open', ?)""",
                     (cid, pid, kind, harm, occurred, _text(form.get("place"), 120), _text(form.get("people"), 300), what,
                      _text(form.get("immediate_action"), 4000), user_id)).lastrowid
    audit.log(db, cid, user_id, "incident.add", "patient" if pid else None, pid, f"{kind} {harm}")
    return iid


def incident(db, cid, iid):
    return db.execute("""SELECT i.*, p.first_name, p.last_name, u.display_name AS reported_by FROM safety_incidents i LEFT JOIN patients p ON p.id = i.patient_id
                         LEFT JOIN users u ON u.id = i.created_by WHERE i.id = ? AND i.clinic_id = ?""", (iid, cid)).fetchone()


def incidents(db, cid, status=None):
    return db.execute("""SELECT i.*, p.first_name, p.last_name FROM safety_incidents i LEFT JOIN patients p ON p.id = i.patient_id WHERE i.clinic_id = ?"""
                      + (" AND i.status != 'closed'" if status == "not_closed" else "") + " ORDER BY i.occurred_on DESC, i.id DESC", (cid,)).fetchall()


def candour_needed(i):
    return i["harm"] in CANDOUR_HARM and i["patient_id"] is not None


def review(db, clinic, user_id, iid, cause, actions, harm=None, reported_to=None, report_ref=None):
    i = incident(db, clinic["id"], iid)
    if not i or i["status"] == "closed":
        raise GovError("Only an incident that is not closed can be reviewed.")
    cause, actions = _text(cause, 4000), _text(actions, 4000)
    if not cause or not actions:
        raise GovError("Write why it happened and what will stop it happening again.")
    harm = harm or i["harm"]
    if harm not in HARM:
        raise GovError("Pick how much harm was done.")
    db.execute("""UPDATE safety_incidents SET cause = ?, actions = ?, harm = ?, reported_to = ?, report_ref = ?, status = 'reviewed',
                  reviewed_by = ?, reviewed_at = datetime('now') WHERE id = ?""",
               (cause, actions, harm, _text(reported_to, 200), _text(report_ref, 120), user_id, iid))
    audit.log(db, clinic["id"], user_id, "incident.review", "patient" if i["patient_id"] else None, i["patient_id"], harm)


def candour(db, clinic, user_id, iid, told_on, told_by, apology, written_on=None, note=None):
    i = incident(db, clinic["id"], iid)
    if not i:
        raise GovError("That incident was not found.")
    if not apology:
        raise GovError("The duty of candour needs an apology. Tick it once one has been given.")
    told = _day(told_on, "Use a real date for when the patient was told in person.")
    if told < i["occurred_on"]:
        raise GovError("The patient can't be told before it happened.")
    written = _day(written_on, "Use a real date for the written follow-up.", False)
    if written and written < told:
        raise GovError("The written follow-up comes after telling them in person.")
    db.execute("""UPDATE safety_incidents SET candour_told_on = ?, candour_told_by = ?, candour_apology = 1, candour_written_on = ?, candour_note = ?
                  WHERE id = ?""", (told, _text(told_by, 120), written, _text(note, 2000), iid))
    audit.log(db, clinic["id"], user_id, "incident.candour", "patient", i["patient_id"])


def close_incident(db, clinic, user_id, iid):
    i = incident(db, clinic["id"], iid)
    if not i or i["status"] != "reviewed":
        raise GovError("An incident is closed after it has been reviewed.")
    if candour_needed(i) and not (i["candour_told_on"] and i["candour_written_on"]):
        raise GovError("This incident did moderate harm or worse. Record telling the patient and the written follow-up before closing it.")
    db.execute("UPDATE safety_incidents SET status = 'closed', closed_at = datetime('now') WHERE id = ?", (iid,))
    audit.log(db, clinic["id"], user_id, "incident.close", "patient" if i["patient_id"] else None, i["patient_id"])


def incident_summary(db, cid, start, end):
    rows = db.execute("SELECT kind, harm FROM safety_incidents WHERE clinic_id = ? AND occurred_on BETWEEN ? AND ?", (cid, start, end)).fetchall()
    by_kind, by_harm = {}, {}
    for r in rows:
        by_kind[r["kind"]] = by_kind.get(r["kind"], 0) + 1
        by_harm[r["harm"]] = by_harm.get(r["harm"], 0) + 1
    return {"total": len(rows), "by_kind": by_kind, "by_harm": by_harm}
