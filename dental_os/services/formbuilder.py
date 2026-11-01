"""The form builder (version 0.34): clinics make their own forms, and patients fill them in on their phone or on the check-in tablet.

A form is a title, a short introduction and up to 40 questions. A question has a label, a kind (short answer, long answer, yes or no,
one choice, several choices, date, number, or a signature by typed full name), whether it must be answered, and choices where needed.
Editing a form makes a new version; each answer set keeps a copy of the questions exactly as the patient saw them.

A form goes to a patient as a private link that works once, for a set number of days. Only a hash of the link is kept. Answers are
checked against the questions, then wait for staff to review them and file them on the record. Nothing is deleted. No web framework here."""
import datetime
import hashlib
import json
import re
import secrets

from . import audit

KINDS = {"text": "Short answer", "long": "Long answer", "yesno": "Yes or no", "choice": "One choice", "multi": "Several choices",
         "date": "Date", "number": "Number", "signature": "Signature (typed full name)", "heading": "Heading (no answer)"}
STATUSES = {"sent": "Sent, not filled in", "submitted": "Filled in, to review", "filed": "Reviewed and filed", "expired": "Expired", "cancelled": "Cancelled"}
MAX_FIELDS = 40
LINK_DAYS = 14


class FormError(Exception):
    def __init__(self, message, labels=None):
        super().__init__(message)
        self.labels = labels or []


def _hash(token):
    return hashlib.sha256((token or "").encode()).hexdigest()


# ------------------------------------------------------------------------------------------------------------ the forms
def parse_fields(form):
    """Questions from the builder screen: q0_label, q0_kind, q0_required, q0_options ... Empty labels are dropped."""
    out, keys = [], set()
    for i in range(MAX_FIELDS + 20):
        label = (form.get(f"q{i}_label") or "").strip()[:300]
        if not label:
            continue
        kind = form.get(f"q{i}_kind") or "text"
        if kind not in KINDS:
            raise FormError("Pick a kind for each question.")
        options = [o.strip()[:100] for o in re.split(r"[\n;]", form.get(f"q{i}_options") or "") if o.strip()]
        if kind in ("choice", "multi") and len(options) < 2:
            raise FormError("A choice question needs at least two choices, one per line.")
        if len(options) > 30:
            raise FormError("Use at most 30 choices in a question.")
        key = "f%d" % (len(out) + 1)
        while key in keys:
            key += "x"
        keys.add(key)
        out.append({"key": key, "label": label, "kind": kind, "required": form.get(f"q{i}_required") == "1" and kind != "heading",
                    "options": options if kind in ("choice", "multi") else []})
    if not [f for f in out if f["kind"] != "heading"]:
        raise FormError("Add at least one question.")
    if len(out) > MAX_FIELDS:
        raise FormError("Use at most 40 questions in a form.")
    return out


def save(db, clinic, user_id, title, intro, fields, form_id=None, active=True):
    cid = clinic["id"]
    title = (title or "").strip()[:120]
    if not title:
        raise FormError("Give the form a title.")
    intro = (intro or "").strip()[:2000] or None
    body = json.dumps(fields)
    if form_id:
        f = get(db, cid, form_id)
        if not f:
            raise FormError("That form was not found.")
        version = f["version"] + (1 if f["fields"] != body else 0)
        db.execute("UPDATE custom_forms SET title = ?, intro = ?, fields = ?, version = ?, active = ?, updated_at = datetime('now') WHERE id = ?",
                   (title, intro, body, version, 1 if active else 0, form_id))
    else:
        form_id = db.execute("INSERT INTO custom_forms(clinic_id, title, intro, fields, version, active, created_by) VALUES (?,?,?,?,1,?,?)",
                             (cid, title, intro, body, 1 if active else 0, user_id)).lastrowid
    audit.log(db, cid, user_id, "form.save", None, None, title)
    return form_id


def get(db, cid, form_id):
    return db.execute("SELECT * FROM custom_forms WHERE id = ? AND clinic_id = ?", (form_id, cid)).fetchone()


def forms(db, cid, active_only=False):
    return db.execute("SELECT * FROM custom_forms WHERE clinic_id = ?" + (" AND active = 1" if active_only else "") + " ORDER BY title", (cid,)).fetchall()


def fields_of(row):
    return json.loads(row["fields"]) if row and row["fields"] else []


# -------------------------------------------------------------------------------------------------- sending to a patient
def send(db, clinic, user_id, pid, form_id, days=LINK_DAYS):
    """Make a one-time link for this patient to fill in this form. Returns the raw token, shown once."""
    cid = clinic["id"]
    if not db.execute("SELECT 1 FROM patients WHERE id = ? AND clinic_id = ? AND status = 'active'", (pid, cid)).fetchone():
        raise FormError("That patient was not found.")
    f = get(db, cid, form_id)
    if not f or not f["active"]:
        raise FormError("Pick one of the clinic's forms.")
    if not isinstance(days, int) or not 1 <= days <= 60:
        raise FormError("A form link can last from 1 to 60 days.")
    token = "frm_" + secrets.token_urlsafe(24)
    expires = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    rid = db.execute("""INSERT INTO form_responses(clinic_id, form_id, patient_id, version, fields, token_hash, status, expires_at, sent_by)
                        VALUES (?,?,?,?,?,?, 'sent', ?, ?)""", (cid, form_id, pid, f["version"], f["fields"], _hash(token), expires, user_id)).lastrowid
    audit.log(db, cid, user_id, "form.send", "patient", pid)
    return token, rid


def resolve(db, token):
    if not (token or "").startswith("frm_"):
        return None
    r = db.execute("SELECT * FROM form_responses WHERE token_hash = ?", (_hash(token),)).fetchone()
    if not r:
        return None
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    if r["status"] == "sent" and r["expires_at"] < now:
        db.execute("UPDATE form_responses SET status = 'expired' WHERE id = ?", (r["id"],))
        r = db.execute("SELECT * FROM form_responses WHERE id = ?", (r["id"],)).fetchone()
    return r


def check_answers(fields, form, patient_name=None):
    """The patient's answers, checked against the questions as they saw them. Raises FormError naming the first problem."""
    answers, problems = {}, []
    for f in fields:
        if f["kind"] == "heading":
            continue
        k = f["key"]
        if f["kind"] == "multi":
            vals = [v for v in form.getlist(k) if v in f["options"]] if hasattr(form, "getlist") else [v for v in (form.get(k) or []) if v in f["options"]]
            val = vals
            empty = not vals
        else:
            val = (form.get(k) or "").strip()[:4000]
            empty = val == ""
            if not empty:
                if f["kind"] == "yesno" and val not in ("yes", "no"):
                    problems.append(f["label"])
                elif f["kind"] == "choice" and val not in f["options"]:
                    problems.append(f["label"])
                elif f["kind"] == "date" and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", val):
                    problems.append(f["label"])
                elif f["kind"] == "number" and not re.fullmatch(r"-?\d+([.,]\d+)?", val):
                    problems.append(f["label"])
                elif f["kind"] == "signature" and len(val) < 3:
                    problems.append(f["label"])
        if empty and f["required"]:
            problems.append(f["label"])
        answers[k] = val
    if problems:
        raise FormError("Please check the questions marked below.", problems)
    return answers


def submit(db, token, form):
    r = resolve(db, token)
    if not r or r["status"] != "sent":
        raise FormError("This form link can no longer be used. Please ask the clinic for a new one.")
    answers = check_answers(json.loads(r["fields"]), form)
    db.execute("UPDATE form_responses SET answers = ?, status = 'submitted', submitted_at = datetime('now') WHERE id = ?", (json.dumps(answers), r["id"]))
    audit.log(db, r["clinic_id"], None, "form.submitted", "patient", r["patient_id"])
    return r["id"]


def file_response(db, clinic, user_id, response_id, note=None):
    r = response(db, clinic["id"], response_id)
    if not r or r["status"] != "submitted":
        raise FormError("Only a filled-in form waiting for review can be filed.")
    db.execute("UPDATE form_responses SET status = 'filed', reviewed_by = ?, reviewed_at = datetime('now'), review_note = ? WHERE id = ?",
               (user_id, (note or "").strip()[:500] or None, response_id))
    audit.log(db, clinic["id"], user_id, "form.filed", "patient", r["patient_id"])


def cancel(db, clinic, user_id, response_id):
    r = response(db, clinic["id"], response_id)
    if not r or r["status"] != "sent":
        raise FormError("Only a form not yet filled in can be cancelled.")
    db.execute("UPDATE form_responses SET status = 'cancelled' WHERE id = ?", (response_id,))
    audit.log(db, clinic["id"], user_id, "form.cancel", "patient", r["patient_id"])


def response(db, cid, response_id):
    return db.execute("""SELECT r.*, f.title, p.first_name, p.last_name FROM form_responses r JOIN custom_forms f ON f.id = r.form_id
                         JOIN patients p ON p.id = r.patient_id WHERE r.id = ? AND r.clinic_id = ?""", (response_id, cid)).fetchone()


def for_patient(db, cid, pid):
    return db.execute("""SELECT r.*, f.title FROM form_responses r JOIN custom_forms f ON f.id = r.form_id
                         WHERE r.clinic_id = ? AND r.patient_id = ? ORDER BY r.id DESC""", (cid, pid)).fetchall()


def to_review(db, cid):
    return db.execute("""SELECT r.*, f.title, p.first_name, p.last_name FROM form_responses r JOIN custom_forms f ON f.id = r.form_id
                         JOIN patients p ON p.id = r.patient_id WHERE r.clinic_id = ? AND r.status = 'submitted' ORDER BY r.submitted_at""", (cid,)).fetchall()


def shown(r):
    """Questions and answers side by side, for the review and print screens."""
    answers = json.loads(r["answers"]) if r["answers"] else {}
    out = []
    for f in json.loads(r["fields"]):
        v = answers.get(f["key"])
        out.append({"f": f, "value": ", ".join(v) if isinstance(v, list) else v})
    return out
