"""Dental laboratory jobs: sent out, due back, received, fitted. Nothing is deleted. A case is cancelled with a reason,
and a remake is a new case that points at the old one, so the lab's record stays honest."""
from datetime import timedelta

from . import audit
from .timeutil import now_local, parse_date

STATUSES = ("draft", "sent", "received", "fitted", "cancelled")
LABELS = {"draft": "Not sent yet", "sent": "At the lab", "received": "Back, ready to fit", "fitted": "Fitted", "cancelled": "Cancelled"}
KINDS = ["Crown", "Bridge", "Veneer", "Inlay or onlay", "Complete denture", "Partial denture", "Night guard", "Retainer",
         "Implant crown", "Whitening tray", "Repair", "Other"]


class LabError(Exception):
    pass


def _today(clinic):
    return now_local(clinic["timezone"]).date()


def _date(value, what, required=False):
    value = (value or "").strip() if isinstance(value, str) or value is None else value
    if not value:
        if required:
            raise LabError(f"Enter {what}.")
        return None
    try:
        return parse_date(str(value)).isoformat()
    except ValueError:
        raise LabError(f"{what[0].upper()}{what[1:]} must be a date like 2026-11-30.")


def _text(value, limit, what, required=False):
    value = (value or "").strip()
    if required and not value:
        raise LabError(f"Enter {what}.")
    if len(value) > limit:
        raise LabError(f"{what[0].upper()}{what[1:]} is too long (up to {limit} characters).")
    return value or None


# ---------------------------------------------------------------- laboratories
def save_lab(db, clinic, user_id, lab_id, name, phone=None, email=None, notes=None, active=True):
    cid = clinic["id"]
    name = _text(name, 80, "the lab's name", required=True)
    phone, email, notes = _text(phone, 40, "the phone number"), _text(email, 120, "the email"), _text(notes, 500, "the notes")
    if email and "@" not in email:
        raise LabError("That email address does not look right.")
    if lab_id:
        if not db.execute("SELECT 1 FROM labs WHERE id=? AND clinic_id=?", (lab_id, cid)).fetchone():
            raise LookupError("lab")
        db.execute("UPDATE labs SET name=?, phone=?, email=?, notes=?, active=? WHERE id=? AND clinic_id=?",
                   (name, phone, email, notes, 1 if active else 0, lab_id, cid))
    else:
        lab_id = db.execute("INSERT INTO labs(clinic_id, name, phone, email, notes) VALUES (?,?,?,?,?)",
                            (cid, name, phone, email, notes)).lastrowid
    audit.log(db, cid, user_id, "lab.save", "lab", lab_id)
    return lab_id


def list_labs(db, cid, active_only=False):
    return db.execute("SELECT * FROM labs WHERE clinic_id=? " + ("AND active=1 " if active_only else "") + "ORDER BY active DESC, name",
                      (cid,)).fetchall()


# ---------------------------------------------------------------- cases
def get(db, cid, case_id):
    return db.execute(
        """SELECT k.*, l.name AS lab_name, l.phone AS lab_phone, p.first_name, p.last_name, pr.name AS provider_name
           FROM lab_cases k JOIN labs l ON l.id = k.lab_id JOIN patients p ON p.id = k.patient_id
           LEFT JOIN providers pr ON pr.id = k.provider_id WHERE k.id = ? AND k.clinic_id = ?""", (case_id, cid)).fetchone()


def events(db, cid, case_id):
    return db.execute("""SELECT e.*, u.display_name FROM lab_events e LEFT JOIN users u ON u.id = e.by_user
                         WHERE e.clinic_id=? AND e.case_id=? ORDER BY e.id""", (cid, case_id)).fetchall()


def _event(db, cid, case_id, user_id, kind, note=None):
    db.execute("INSERT INTO lab_events(clinic_id, case_id, kind, note, by_user) VALUES (?,?,?,?,?)", (cid, case_id, kind, note, user_id))


def _begin(db):
    if not db.in_transaction:
        db.execute("BEGIN IMMEDIATE")


def _checked(db, cid, patient_id, lab_id, procedure_id, provider_id):
    if not db.execute("SELECT 1 FROM patients WHERE id=? AND clinic_id=?", (patient_id, cid)).fetchone():
        raise LookupError("patient")
    if not db.execute("SELECT 1 FROM labs WHERE id=? AND clinic_id=? AND active=1", (lab_id, cid)).fetchone():
        raise LabError("Pick one of your labs.")
    if procedure_id and not db.execute("SELECT 1 FROM procedures WHERE id=? AND clinic_id=? AND patient_id=?",
                                       (procedure_id, cid, patient_id)).fetchone():
        raise LabError("That treatment is not on this patient's record.")
    if provider_id and not db.execute("SELECT 1 FROM providers WHERE id=? AND clinic_id=?", (provider_id, cid)).fetchone():
        raise LabError("Pick one of your dentists.")


def create(db, clinic, user_id, patient_id, lab_id, kind, tooth=None, shade=None, instructions=None, due_on=None, cost_p=0,
           procedure_id=None, provider_id=None, remake_of=None):
    cid = clinic["id"]
    kind = _text(kind, 60, "what the job is", required=True)
    tooth, shade, instructions = _text(tooth, 40, "the tooth"), _text(shade, 40, "the shade"), _text(instructions, 1000, "the instructions")
    due = _date(due_on, "the due date")
    if cost_p < 0:
        raise LabError("The cost cannot be negative.")
    _begin(db)
    _checked(db, cid, patient_id, lab_id, procedure_id, provider_id)
    cur = db.execute(
        """INSERT INTO lab_cases(clinic_id, patient_id, lab_id, procedure_id, provider_id, kind, tooth, shade, instructions, due_on,
                                 cost_p, remake_of, created_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (cid, patient_id, lab_id, procedure_id, provider_id, kind, tooth, shade, instructions, due, cost_p, remake_of, user_id))
    _event(db, cid, cur.lastrowid, user_id, "created", f"Remake of case {remake_of}" if remake_of else None)
    audit.log(db, cid, user_id, "lab.case.create", "lab_case", cur.lastrowid)
    return cur.lastrowid


def update(db, clinic, user_id, case_id, lab_id, kind, tooth, shade, instructions, due_on, cost_p, provider_id=None):
    cid = clinic["id"]
    _begin(db)
    case = get(db, cid, case_id)
    if not case:
        raise LookupError("case")
    if case["status"] not in ("draft", "sent"):
        raise LabError("A case that is back, fitted or cancelled can no longer be changed.")
    kind = _text(kind, 60, "what the job is", required=True)
    tooth, shade, instructions = _text(tooth, 40, "the tooth"), _text(shade, 40, "the shade"), _text(instructions, 1000, "the instructions")
    due = _date(due_on, "the due date", required=case["status"] == "sent")
    if cost_p < 0:
        raise LabError("The cost cannot be negative.")
    _checked(db, cid, case["patient_id"], lab_id, case["procedure_id"], provider_id)
    db.execute("""UPDATE lab_cases SET lab_id=?, kind=?, tooth=?, shade=?, instructions=?, due_on=?, cost_p=?, provider_id=?
                  WHERE id=? AND clinic_id=?""", (lab_id, kind, tooth, shade, instructions, due, cost_p, provider_id, case_id, cid))
    _event(db, cid, case_id, user_id, "edited")
    audit.log(db, cid, user_id, "lab.case.edit", "lab_case", case_id)


def _move(db, clinic, user_id, case_id, allowed, new, **fields):
    cid = clinic["id"]
    _begin(db)
    case = get(db, cid, case_id)
    if not case:
        raise LookupError("case")
    if case["status"] not in allowed:
        raise LabError(f"A case that is '{LABELS[case['status']].lower()}' cannot be moved that way.")
    sets = ", ".join(f"{k}=?" for k in fields)
    db.execute(f"UPDATE lab_cases SET status=?{', ' + sets if sets else ''} WHERE id=? AND clinic_id=?",
               (new, *fields.values(), case_id, cid))
    return case


def send(db, clinic, user_id, case_id, sent_on=None, due_on=None):
    today = _today(clinic)
    case = get(db, clinic["id"], case_id)
    if not case:
        raise LookupError("case")
    sent = _date(sent_on, "the date sent") or today.isoformat()
    due = _date(due_on, "the due date") or case["due_on"]
    if not due:
        raise LabError("Enter the date the lab promised it back.")
    if due < sent:
        raise LabError("The due date cannot be before the day it was sent.")
    _move(db, clinic, user_id, case_id, ("draft",), "sent", sent_on=sent, due_on=due)
    _event(db, clinic["id"], case_id, user_id, "sent", f"Due {due}")
    audit.log(db, clinic["id"], user_id, "lab.case.send", "lab_case", case_id)


def receive(db, clinic, user_id, case_id, received_on=None):
    case = get(db, clinic["id"], case_id)
    if not case:
        raise LookupError("case")
    day = _date(received_on, "the date received") or _today(clinic).isoformat()
    if case["sent_on"] and day < case["sent_on"]:
        raise LabError("It cannot come back before it was sent.")
    _move(db, clinic, user_id, case_id, ("sent",), "received", received_on=day)
    _event(db, clinic["id"], case_id, user_id, "received")
    audit.log(db, clinic["id"], user_id, "lab.case.receive", "lab_case", case_id)


def fit(db, clinic, user_id, case_id, fitted_on=None):
    case = get(db, clinic["id"], case_id)
    if not case:
        raise LookupError("case")
    day = _date(fitted_on, "the date fitted") or _today(clinic).isoformat()
    if case["received_on"] and day < case["received_on"]:
        raise LabError("It cannot be fitted before it came back.")
    _move(db, clinic, user_id, case_id, ("received",), "fitted", fitted_on=day)
    _event(db, clinic["id"], case_id, user_id, "fitted")
    audit.log(db, clinic["id"], user_id, "lab.case.fit", "lab_case", case_id)


def cancel(db, clinic, user_id, case_id, reason):
    reason = _text(reason, 200, "a reason", required=True)
    _move(db, clinic, user_id, case_id, ("draft", "sent", "received"), "cancelled", cancel_reason=reason)
    _event(db, clinic["id"], case_id, user_id, "cancelled", reason)
    audit.log(db, clinic["id"], user_id, "lab.case.cancel", "lab_case", case_id)


def remake(db, clinic, user_id, case_id, reason, cost_p=0):
    """The lab or the dentist wants the job done again. The old case stays as it was. A new draft is made from it."""
    cid = clinic["id"]
    reason = _text(reason, 200, "why it is being remade", required=True)
    case = get(db, cid, case_id)
    if not case:
        raise LookupError("case")
    if case["status"] not in ("received", "fitted"):
        raise LabError("Only a case that came back from the lab can be remade.")
    new_id = create(db, clinic, user_id, case["patient_id"], case["lab_id"], case["kind"], case["tooth"], case["shade"],
                    case["instructions"], None, cost_p, case["procedure_id"], case["provider_id"], remake_of=case_id)
    _event(db, cid, case_id, user_id, "remade", f"Remake is case {new_id}: {reason}")
    return new_id


def link_fit_visit(db, clinic, user_id, case_id, appointment_id):
    cid = clinic["id"]
    case = get(db, cid, case_id)
    if not case:
        raise LookupError("case")
    if appointment_id is None:
        db.execute("UPDATE lab_cases SET fit_appointment_id=NULL WHERE id=? AND clinic_id=?", (case_id, cid))
        return
    ok = db.execute("SELECT 1 FROM appointments WHERE id=? AND clinic_id=? AND patient_id=? AND status IN ('scheduled','complete')",
                    (appointment_id, cid, case["patient_id"])).fetchone()
    if not ok:
        raise LabError("Pick a booked visit for this patient.")
    db.execute("UPDATE lab_cases SET fit_appointment_id=? WHERE id=? AND clinic_id=?", (appointment_id, cid, case_id))
    _event(db, cid, case_id, user_id, "fit visit booked")


# ---------------------------------------------------------------- lists
_SELECT = """SELECT k.*, l.name AS lab_name, p.first_name, p.last_name, a.start_at AS fit_at
             FROM lab_cases k JOIN labs l ON l.id = k.lab_id JOIN patients p ON p.id = k.patient_id
             LEFT JOIN appointments a ON a.id = k.fit_appointment_id AND a.clinic_id = k.clinic_id"""


def board(db, clinic):
    cid, today = clinic["id"], _today(clinic).isoformat()
    rows = db.execute(_SELECT + " WHERE k.clinic_id = ? AND k.status IN ('draft','sent','received') ORDER BY k.due_on IS NULL, k.due_on, k.id",
                      (cid,)).fetchall()
    recent = db.execute(_SELECT + " WHERE k.clinic_id = ? AND k.status = 'fitted' AND k.fitted_on >= ? ORDER BY k.fitted_on DESC",
                        (cid, (parse_date(today) - timedelta(days=30)).isoformat())).fetchall()
    return {"draft": [r for r in rows if r["status"] == "draft"],
            "late": [r for r in rows if r["status"] == "sent" and r["due_on"] and r["due_on"] < today],
            "out": [r for r in rows if r["status"] == "sent" and not (r["due_on"] and r["due_on"] < today)],
            "ready": [r for r in rows if r["status"] == "received"], "recent": recent, "today": today}


def for_patient(db, cid, pid):
    return db.execute(_SELECT + " WHERE k.clinic_id = ? AND k.patient_id = ? ORDER BY k.id DESC", (cid, pid)).fetchall()


def counts(db, clinic):
    b = board(db, clinic)
    return {"late": len(b["late"]), "ready": len(b["ready"]), "draft": len(b["draft"]), "out": len(b["out"])}


def performance(db, cid, start, end):
    """How each lab did on cases sent in the period: turnaround, on time, remakes and spend."""
    out = []
    for lab in db.execute("SELECT * FROM labs WHERE clinic_id = ? ORDER BY name", (cid,)):
        rows = db.execute("""SELECT * FROM lab_cases WHERE clinic_id=? AND lab_id=? AND status != 'cancelled' AND status != 'draft'
                             AND sent_on BETWEEN ? AND ?""", (cid, lab["id"], str(start), str(end))).fetchall()
        if not rows:
            continue
        back = [r for r in rows if r["received_on"]]
        days = [(parse_date(r["received_on"]) - parse_date(r["sent_on"])).days for r in back]
        on_time = sum(1 for r in back if r["due_on"] and r["received_on"] <= r["due_on"])
        remakes = db.execute("SELECT COUNT(*) FROM lab_cases WHERE clinic_id=? AND lab_id=? AND remake_of IS NOT NULL AND created_at >= ?",
                             (cid, lab["id"], f"{start} 00:00")).fetchone()[0]
        out.append({"lab": lab, "sent": len(rows), "back": len(back), "avg_days": round(sum(days) / len(days), 1) if days else None,
                    "on_time_rate": round(100 * on_time / len(back), 1) if back else None, "remakes": remakes,
                    "spend_p": sum(r["cost_p"] for r in rows)})
    return out
