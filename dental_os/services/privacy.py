"""Data rights, in plain terms: a full copy of one person's record, a register of requests with deadlines, removing a person's identity
from the record, a log of data breaches, and a review of records kept longer than the clinic meant to.

Nothing here is legal advice. Which rights exist, how fast a clinic must answer, how long records must be kept, and whom to tell about
a breach all differ by country. The screens say so, and the periods they show (30 days, 72 hours, 10 years) are starting points the
clinic's own lawyer should confirm."""
import io
import json
import re
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

from . import audit, billing, files as filesvc, patients as P, serialize, webhooks
from .timeutil import now_local, parse_date

REQUEST_KINDS = {
    "access": "See their data (a copy)", "correction": "Correct something", "erasure": "Erase their data",
    "restriction": "Pause use of their data", "objection": "Object to how it is used", "portability": "Take their data elsewhere",
    "other": "Something else",
}
DEFAULT_REPLY_DAYS = 30
BREACH_CLOCK_HOURS = 72
ANONYMISE_WORD = "ANONYMISE"
MAX_ZIP_FILE_BYTES = 150 * 1024 * 1024


class PrivacyError(Exception):
    pass


def _today(clinic):
    return now_local(clinic["timezone"]).date()


# ============================================================================================ a full copy of one record
# Every table that has a patient_id column belongs here, unless it is listed in NOT_RECORDS. A test checks this against the schema,
# so a table added later cannot be forgotten by mistake.
DIRECT = ["aftercare_given", "refunds", "reactivation_holds", "complaints", "safety_incidents", "implant_items", "material_recall_patients", "appointments", "messages", "procedures", "payments", "adjustments", "recalls", "medical_history", "patient_alerts",
          "tooth_conditions", "perio_charts", "clinical_notes", "consents", "patient_files", "treatment_plans", "memberships",
          "payment_plans", "patient_cover", "claims", "invoices", "outbox", "booking_requests", "intake_submissions", "lab_cases",
          "data_requests", "prescriptions", "referrals_out", "vitals", "letters", "contact_log", "image_mounts", "tasks", "pay_links",
          "calls", "steri_packs", "nhs_courses", "imaging_requests", "ortho_cases", "statement_items", "staff_messages", "saved_cards", "card_charges", "form_responses"]
# child table -> (column that points at the parent, parent table). Rows are found through the parent.
CHILD = {"kit_uses": ("procedure_id", "procedures"), "complaint_steps": ("complaint_id", "complaints"), "perio_readings": ("chart_id", "perio_charts"), "plan_items": ("plan_id", "treatment_plans"),
         "plan_events": ("plan_id", "treatment_plans"), "membership_charges": ("membership_id", "memberships"),
         "membership_uses": ("membership_id", "memberships"), "instalments": ("payment_plan_id", "payment_plans"),
         "claim_items": ("claim_id", "claims"), "invoice_lines": ("invoice_id", "invoices"), "invoice_tax": ("invoice_id", "invoices"),
         "lab_events": ("case_id", "lab_cases"), "prescription_items": ("prescription_id", "prescriptions"),
         "image_mount_slots": ("mount_id", "image_mounts"), "cover_benefits": ("cover_id", "patient_cover"),
         "cover_limits": ("cover_id", "patient_cover"), "ortho_visits": ("case_id", "ortho_cases"),
         "ortho_contracts": ("case_id", "ortho_cases"), "ortho_charges": ("contract_id", "ortho_contracts")}
NOT_RECORDS = {"aftercare_sheets": "the clinic's own printed advice for each kind of treatment; nothing about any patient",
               "patient_merges": "which duplicate record was merged into which, and how many rows moved; no details about the person",
               "patient_not_duplicates": "two record numbers a manager marked as different people; nothing else",
               "patient_links": "access links; only a hash of the secret is kept, and it says nothing about the person",
               "staff_message_reads": "who has read a staff message, and when; it holds nothing about the patient",
               "custom_forms": "the clinic's own blank forms; the answers are in form_responses",
               "edi_files": "US claim files as sent and received; one file can carry several patients, and the claims in it are in the copy"}
HIDE_COLUMNS = {"patient_files": {"stored_name"}, "saved_cards": {"token"}, "form_responses": {"token_hash"}}                  # the private name a file has on disk


def _clean(row, table):
    hide = HIDE_COLUMNS.get(table, ())
    return {k: row[k] for k in row.keys() if k not in hide}


def record_data(db, cid, pid):
    """Everything held about one patient, as plain data: {table: [rows]}. Ordered so a reader sees the story in time."""
    p = db.execute("SELECT * FROM patients WHERE id = ? AND clinic_id = ?", (pid, cid)).fetchone()
    if not p:
        raise LookupError("patient")
    out = {"patients": [_clean(p, "patients")]}
    extra = {
        "procedures": """SELECT pr.*, c.code AS code, c.name AS procedure FROM procedures pr JOIN procedure_codes c ON c.id = pr.code_id
                         WHERE pr.clinic_id = ? AND pr.patient_id = ? ORDER BY pr.id""",
        "appointments": """SELECT a.*, pv.name AS dentist FROM appointments a LEFT JOIN providers pv ON pv.id = a.provider_id
                           WHERE a.clinic_id = ? AND a.patient_id = ? ORDER BY a.id""",
        "steri_packs": "SELECT * FROM steri_packs WHERE clinic_id = ? AND used_on = ? ORDER BY id",    # instrument packs used on them
    }
    for table in DIRECT:
        sql = extra.get(table) or f"SELECT * FROM {table} WHERE clinic_id = ? AND patient_id = ? ORDER BY id"
        out[table] = [_clean(r, table) for r in db.execute(sql, (cid, pid)).fetchall()]
    for table, (col, parent) in CHILD.items():
        ids = [r["id"] for r in out.get(parent, [])]
        rows = []
        if ids:
            marks = ",".join("?" * len(ids))
            rows = db.execute(f"SELECT * FROM {table} WHERE clinic_id = ? AND {col} IN ({marks}) ORDER BY id", [cid] + ids).fetchall()
        out[table] = [_clean(r, table) for r in rows]
    return out


# What the printable copy shows, in order: (table, heading, [(column, heading, how to show it)]). "money" is minor units.
SECTIONS = [
    ("appointments", "Appointments", [("start_at", "When", None), ("dentist", "Dentist", None), ("category", "Reason", None), ("status", "Status", None)]),
    ("medical_history", "Medical history", [("conditions", "Conditions", None), ("medications", "Medicines", None), ("allergies", "Allergies", None),
                                             ("notes", "Notes", None), ("reviewed_on", "Reviewed", None)]),
    ("patient_alerts", "Alerts", [("kind", "Kind", None), ("severity", "Level", None), ("text", "Alert", None), ("created_at", "Added", None)]),
    ("procedures", "Treatment", [("done_on", "Done", None), ("procedure", "Treatment", None), ("tooth", "Tooth", None), ("status", "Status", None),
                                 ("fee_p", "Fee", "money")]),
    ("clinical_notes", "Clinical notes", [("created_at", "Written", None), ("title", "Title", None), ("body", "Note", None), ("status", "Status", None)]),
    ("consents", "Consents", [("signed_at", "Signed", None), ("title", "Form", None), ("signer_name", "Signed by", None), ("status", "Status", None)]),
    ("payments", "Payments", [("received_on", "Date", None), ("receipt_no", "Receipt", None), ("method", "Method", None), ("amount_p", "Amount", "money"),
                              ("voided", "Cancelled", "yes")]),
    ("invoices", "Invoices", [("invoice_no", "No.", None), ("issued_on", "Issued", None), ("total_p", "Total", "money"), ("status", "Status", None)]),
    ("messages", "Messages", [("created_at", "When (UTC)", None), ("direction", "Direction", None), ("body", "Message", None)]),
    ("patient_files", "Files held", [("original_name", "File", None), ("kind", "Kind", None), ("taken_on", "Taken", None), ("size_bytes", "Size (bytes)", None)]),
]

README = """A copy of one person's record from {clinic}, made on {day}.
record.json holds everything the system keeps about this person, table by table. Money is in minor units (for example pesewas,
cents or pence). Times are the clinic's local time unless a column says UTC. The folder files/ holds their uploaded documents and
images, if any are available. This copy is confidential personal and health information. Send it only to the person it is about, or to
someone the law allows, and check who is asking before you do.
"""


def record_zip(db, clinic, pid, files_root):
    data = record_data(db, clinic["id"], pid)
    buf = io.BytesIO()
    notes = []
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("README.txt", README.format(clinic=clinic["name"], day=_today(clinic).isoformat()))
        z.writestr("record.json", json.dumps(data, indent=2, ensure_ascii=False, default=str))
        total = 0
        for f in data["patient_files"]:
            row = db.execute("SELECT * FROM patient_files WHERE id = ? AND clinic_id = ?", (f["id"], clinic["id"])).fetchone()
            try:
                path = filesvc.path_for(files_root, row)
            except filesvc.FileError:
                path = None
            if not path or not path.exists():
                notes.append(f"File {f['id']} ({f['original_name']}) was not available.")
                continue
            size = path.stat().st_size
            if total + size > MAX_ZIP_FILE_BYTES:
                notes.append(f"File {f['id']} ({f['original_name']}) was left out because the copy would be too large. Send it separately.")
                continue
            total += size
            safe = re.sub(r"[^A-Za-z0-9._-]+", "_", f["original_name"] or "file")[:80]
            z.write(path, f"files/{f['id']}-{safe}")
        if notes:
            z.writestr("NOT_INCLUDED.txt", "\n".join(notes) + "\n")
    return buf.getvalue(), notes


# =================================================================================================== anonymise a person
def anonymise_check(db, clinic, pid):
    """The reasons this person cannot be anonymised yet, as a list of sentences. Empty means it can go ahead."""
    cid = clinic["id"]
    p = P.get(db, cid, pid)
    if not p:
        raise LookupError("patient")
    why = []
    if p["anonymised_on"]:
        why.append("This person has already been anonymised.")
        return why
    bal = billing.totals(db, cid, pid)["balance"]
    if bal != 0:
        why.append("The account is not settled. Collect, refund or write off the balance first, because the clinic needs to know who owes or is owed.")
    future = db.execute("""SELECT COUNT(*) FROM appointments WHERE clinic_id = ? AND patient_id = ? AND status = 'scheduled'
                           AND start_at >= ?""", (cid, pid, _today(clinic).isoformat())).fetchone()[0]
    if future:
        why.append("There are appointments still booked. Cancel or complete them first.")
    deps = db.execute("SELECT COUNT(*) FROM patients WHERE clinic_id = ? AND guarantor_id = ?", (cid, pid)).fetchone()[0]
    if deps:
        why.append("Other patients list this person as their family head. Change that first.")
    open_claims = db.execute("SELECT COUNT(*) FROM claims WHERE clinic_id = ? AND patient_id = ? AND status IN ('draft','submitted','partly_paid')",
                             (cid, pid)).fetchone()[0]
    if open_claims:
        why.append("An insurance claim is still open. Close it first.")
    return why


def anonymise(db, clinic, user_id, pid, reason, confirm, request_id=None):
    """Remove the person's identity from the record, keeping the clinical and money history that clinics must usually keep.
    This cannot be undone. Free text written in notes may still mention the person, so it is not a guarantee of anonymity."""
    cid = clinic["id"]
    reason = (reason or "").strip()
    if len(reason) < 5:
        raise PrivacyError("Say why the person is being anonymised.")
    if (confirm or "").strip().upper() != ANONYMISE_WORD:
        raise PrivacyError(f"Type {ANONYMISE_WORD} to confirm. It cannot be undone.")
    why = anonymise_check(db, clinic, pid)
    if why:
        raise PrivacyError(why[0])
    if request_id:                                                       # check this before changing anything
        req = get_request(db, cid, request_id)
        if not req or req["status"] != "open" or (req["patient_id"] and req["patient_id"] != pid):
            raise PrivacyError("That request is not open for this person.")
        if not req["identity_checked"]:
            raise PrivacyError("Check who is asking, and tick that you did, before acting on the request.")
    p = P.get(db, cid, pid)
    year = (p["dob"] or "")[:4]
    dob = f"{year}-01-01" if re.fullmatch(r"\d{4}", year) else None          # only the year of birth is kept
    db.execute("""UPDATE patients SET first_name = 'Anonymised', last_name = ?, dob = ?, gender = '', phone = '', email = NULL, address = NULL,
                  guarantor_id = NULL, notes = NULL, reminders_ok = 0, offers_ok = 0, status = 'archived', anonymised_on = ?,
                  updated_at = datetime('now') WHERE id = ? AND clinic_id = ?""",
               (f"#{pid}", dob, _today(clinic).isoformat(), pid, cid))
    db.execute("UPDATE appointments SET notes = NULL WHERE clinic_id = ? AND patient_id = ?", (cid, pid))
    db.execute("UPDATE messages SET body = '[removed]' WHERE clinic_id = ? AND patient_id = ?", (cid, pid))
    db.execute("UPDATE staff_messages SET body = '[removed]' WHERE clinic_id = ? AND patient_id = ?", (cid, pid))
    db.execute("UPDATE saved_cards SET status = 'removed', token = '', email = NULL, removed_reason = 'Record anonymised' WHERE clinic_id = ? AND patient_id = ?", (cid, pid))
    db.execute("UPDATE calls SET from_number = CASE WHEN direction = 'in' THEN '' ELSE from_number END, "
               "to_number = CASE WHEN direction = 'out' THEN '' ELSE to_number END WHERE clinic_id = ? AND patient_id = ?", (cid, pid))
    db.execute("UPDATE consents SET signer_name = 'Anonymised' WHERE clinic_id = ? AND patient_id = ?", (cid, pid))
    db.execute("UPDATE patient_cover SET holder_name = NULL, member_no = NULL WHERE clinic_id = ? AND patient_id = ?", (cid, pid))
    db.execute("UPDATE treatment_plans SET signer_name = NULL WHERE clinic_id = ? AND patient_id = ?", (cid, pid))
    db.execute("UPDATE patient_files SET archived = 1 WHERE clinic_id = ? AND patient_id = ?", (cid, pid))
    db.execute("""UPDATE booking_requests SET first_name = 'Anonymised', last_name = '', phone = '', email = NULL, note = NULL, reason = '[removed]'
                  WHERE clinic_id = ? AND patient_id = ?""", (cid, pid))
    db.execute("UPDATE intake_submissions SET answers = '{}' WHERE clinic_id = ? AND patient_id = ?", (cid, pid))
    db.execute("UPDATE patient_links SET revoked = 1 WHERE clinic_id = ? AND patient_id = ?", (cid, pid))
    db.execute("UPDATE data_requests SET requester = 'Anonymised', details = NULL WHERE clinic_id = ? AND patient_id = ?", (cid, pid))
    db.execute("UPDATE letters SET body = '[removed]', addressee = NULL WHERE clinic_id = ? AND patient_id = ?", (cid, pid))
    db.execute("UPDATE contact_log SET summary = '[removed]', outcome = NULL WHERE clinic_id = ? AND patient_id = ?", (cid, pid))
    db.execute("UPDATE tasks SET title = '[removed]', detail = NULL, close_note = NULL WHERE clinic_id = ? AND patient_id = ?", (cid, pid))
    webhooks.redact_patient(db, cid, pid)
    audit.log(db, cid, user_id, "patient.anonymise", "patient", pid, reason[:120])
    if request_id:
        close_request(db, clinic, user_id, request_id, "done", "Person anonymised. " + reason)
    row = db.execute("SELECT * FROM patients WHERE id = ?", (pid,)).fetchone()
    webhooks.emit(db, cid, "patient.updated", serialize.patient(row))


# ====================================================================================================== requests register
def add_request(db, clinic, user_id, patient_id, requester, kind, received_on=None, due_on=None, details=None, identity_checked=False):
    cid = clinic["id"]
    if kind not in REQUEST_KINDS:
        raise PrivacyError("Pick what the person is asking for.")
    requester = (requester or "").strip()[:120]
    if patient_id:
        p = P.get(db, cid, patient_id)
        if not p:
            raise PrivacyError("That patient was not found.")
        requester = requester or f"{p['first_name']} {p['last_name']}"
    if not requester:
        raise PrivacyError("Say who is asking.")
    try:
        got = parse_date(received_on) if received_on else _today(clinic)
        due = parse_date(due_on) if due_on else got + timedelta(days=DEFAULT_REPLY_DAYS)
    except ValueError:
        raise PrivacyError("Enter the dates like 2026-10-24.")
    if due < got:
        raise PrivacyError("The reply date cannot be before the day the request arrived.")
    cur = db.execute("""INSERT INTO data_requests(clinic_id, patient_id, requester, kind, received_on, due_on, identity_checked, details, created_by)
                        VALUES (?,?,?,?,?,?,?,?,?)""",
                     (cid, patient_id or None, requester, kind, got.isoformat(), due.isoformat(), 1 if identity_checked else 0,
                      (details or "").strip()[:1000] or None, user_id))
    audit.log(db, cid, user_id, "request.add", "data_request", cur.lastrowid, kind)
    return cur.lastrowid


def get_request(db, cid, rid):
    return db.execute("""SELECT r.*, p.first_name, p.last_name, u.display_name AS handler FROM data_requests r
                         LEFT JOIN patients p ON p.id = r.patient_id LEFT JOIN users u ON u.id = r.handled_by
                         WHERE r.id = ? AND r.clinic_id = ?""", (rid, cid)).fetchone()


def list_requests(db, clinic, status=None):
    rows = db.execute("""SELECT r.*, p.first_name, p.last_name FROM data_requests r LEFT JOIN patients p ON p.id = r.patient_id
                         WHERE r.clinic_id = ? AND (CAST(? AS TEXT) IS NULL OR r.status = ?)
                         ORDER BY CASE r.status WHEN 'open' THEN 0 ELSE 1 END, r.due_on, r.id DESC""",
                      (clinic["id"], status, status)).fetchall()
    today = _today(clinic).isoformat()
    return [dict(r, overdue=(r["status"] == "open" and r["due_on"] < today),
                 days_left=(parse_date(r["due_on"]) - parse_date(today)).days if r["status"] == "open" else None) for r in rows]


def set_identity_checked(db, clinic, user_id, rid, value=True):
    row = get_request(db, clinic["id"], rid)
    if not row:
        raise LookupError("request")
    db.execute("UPDATE data_requests SET identity_checked = ? WHERE id = ?", (1 if value else 0, rid))
    audit.log(db, clinic["id"], user_id, "request.identity", "data_request", rid)


def close_request(db, clinic, user_id, rid, status, outcome):
    row = get_request(db, clinic["id"], rid)
    if not row:
        raise LookupError("request")
    if status not in ("done", "refused"):
        raise PrivacyError("Choose done or refused.")
    outcome = (outcome or "").strip()
    if len(outcome) < 5:
        raise PrivacyError("Write what was done, or why it was refused.")
    if row["status"] != "open":
        raise PrivacyError("That request is already closed.")
    if status == "done" and row["kind"] in ("access", "erasure", "portability", "correction") and not row["identity_checked"]:
        raise PrivacyError("Check who is asking before you act on or close this kind of request, and tick that you did.")
    db.execute("UPDATE data_requests SET status = ?, outcome = ?, closed_on = ?, handled_by = ? WHERE id = ?",
               (status, outcome[:1000], _today(clinic).isoformat(), user_id, rid))
    audit.log(db, clinic["id"], user_id, "request." + status, "data_request", rid)


def reopen_request(db, clinic, user_id, rid):
    row = get_request(db, clinic["id"], rid)
    if not row:
        raise LookupError("request")
    if row["status"] == "open":
        raise PrivacyError("That request is already open.")
    db.execute("UPDATE data_requests SET status = 'open', closed_on = NULL WHERE id = ?", (rid,))
    audit.log(db, clinic["id"], user_id, "request.reopen", "data_request", rid)


# ================================================================================================================ incidents
def _now_text(clinic):
    return now_local(clinic["timezone"]).strftime("%Y-%m-%d %H:%M")


def deadline(found_at):
    """When a 72-hour reporting clock started at `found_at` runs out (clinic local time, as text)."""
    return (datetime.strptime(found_at, "%Y-%m-%d %H:%M") + timedelta(hours=BREACH_CLOCK_HOURS)).strftime("%Y-%m-%d %H:%M")


def add_incident(db, clinic, user_id, title, what_happened, data_involved=None, occurred_on=None, found_at=None, people_affected=None, risk="low"):
    title = (title or "").strip()[:120]
    what = (what_happened or "").strip()
    if not title or len(what) < 10:
        raise PrivacyError("Give it a short title and say what happened.")
    if risk not in ("low", "medium", "high"):
        raise PrivacyError("Pick the risk level.")
    found = _now_text(clinic)
    if found_at:
        try:
            found = datetime.strptime(found_at.replace("T", " "), "%Y-%m-%d %H:%M").strftime("%Y-%m-%d %H:%M")
        except ValueError:
            raise PrivacyError("Enter when you found out like 2026-10-24 14:30.")
    if found > _now_text(clinic):
        raise PrivacyError("You cannot have found out in the future.")
    if occurred_on:
        try:
            parse_date(occurred_on)
        except ValueError:
            raise PrivacyError("Enter the date it happened like 2026-10-24.")
    try:
        people = int(people_affected) if str(people_affected or "").strip() else None
        if people is not None and people < 0:
            raise ValueError
    except ValueError:
        raise PrivacyError("The number of people must be a whole number, or blank if you do not know.")
    cur = db.execute("""INSERT INTO incidents(clinic_id, title, occurred_on, found_at, what_happened, data_involved, people_affected, risk, created_by)
                        VALUES (?,?,?,?,?,?,?,?,?)""",
                     (clinic["id"], title, occurred_on or None, found, what[:2000], (data_involved or "").strip()[:500] or None, people, risk, user_id))
    audit.log(db, clinic["id"], user_id, "incident.add", "incident", cur.lastrowid, risk)
    return cur.lastrowid


def get_incident(db, cid, iid):
    return db.execute("SELECT * FROM incidents WHERE id = ? AND clinic_id = ?", (iid, cid)).fetchone()


def incident_view(clinic, row):
    """The row with its reporting clock worked out."""
    d = dict(row)
    d["clock_ends"] = deadline(row["found_at"])
    now = _now_text(clinic)
    needs_report = row["risk"] != "low" and not row["regulator_told"] and row["status"] == "open"
    d["clock_running"] = needs_report
    d["clock_late"] = needs_report and now > d["clock_ends"]
    hours = (datetime.strptime(d["clock_ends"], "%Y-%m-%d %H:%M") - datetime.strptime(now, "%Y-%m-%d %H:%M")).total_seconds() / 3600
    d["hours_left"] = round(hours)
    return d


def list_incidents(db, clinic):
    rows = db.execute("SELECT * FROM incidents WHERE clinic_id = ? ORDER BY CASE status WHEN 'open' THEN 0 ELSE 1 END, found_at DESC",
                      (clinic["id"],)).fetchall()
    return [incident_view(clinic, r) for r in rows]


def update_incident(db, clinic, user_id, iid, risk, people_affected, regulator_told, regulator_on, patients_told, patients_on, actions):
    row = get_incident(db, clinic["id"], iid)
    if not row:
        raise LookupError("incident")
    if row["status"] == "closed":
        raise PrivacyError("That incident is closed. Reopen it to change it.")
    if risk not in ("low", "medium", "high"):
        raise PrivacyError("Pick the risk level.")
    try:
        people = int(people_affected) if str(people_affected or "").strip() else None
        if people is not None and people < 0:
            raise ValueError
    except ValueError:
        raise PrivacyError("The number of people must be a whole number, or blank if you do not know.")
    today = _today(clinic).isoformat()
    for flag, day in ((regulator_told, regulator_on), (patients_told, patients_on)):
        if day:
            try:
                parse_date(day)
            except ValueError:
                raise PrivacyError("Enter dates like 2026-10-24.")
    db.execute("""UPDATE incidents SET risk = ?, people_affected = ?, regulator_told = ?, regulator_on = ?, patients_told = ?, patients_on = ?, actions = ?
                  WHERE id = ?""",
               (risk, people, 1 if regulator_told else 0, (regulator_on or (today if regulator_told else None)) or None,
                1 if patients_told else 0, (patients_on or (today if patients_told else None)) or None, (actions or "").strip()[:2000] or None, iid))
    audit.log(db, clinic["id"], user_id, "incident.update", "incident", iid, risk)


def close_incident(db, clinic, user_id, iid, closing=True):
    row = get_incident(db, clinic["id"], iid)
    if not row:
        raise LookupError("incident")
    if closing:
        if row["status"] == "closed":
            raise PrivacyError("That incident is already closed.")
        if not (row["actions"] or "").strip():
            raise PrivacyError("Write down what was done about it before closing it.")
        db.execute("UPDATE incidents SET status = 'closed', closed_on = ? WHERE id = ?", (_today(clinic).isoformat(), iid))
        audit.log(db, clinic["id"], user_id, "incident.close", "incident", iid)
    else:
        db.execute("UPDATE incidents SET status = 'open', closed_on = NULL WHERE id = ?", (iid,))
        audit.log(db, clinic["id"], user_id, "incident.reopen", "incident", iid)


# ===================================================================================================== keeping records too long
def set_retention(db, clinic, user_id, years):
    try:
        n = int(years)
    except (TypeError, ValueError):
        raise PrivacyError("Enter the number of years.")
    if not 1 <= n <= 60:
        raise PrivacyError("Enter a number of years from 1 to 60.")
    db.execute("UPDATE clinics SET retention_years = ? WHERE id = ?", (n, clinic["id"]))
    audit.log(db, clinic["id"], user_id, "settings.retention", detail=str(n))


def retention_candidates(db, clinic, limit=300):
    """People whose last activity is older than the clinic's chosen period and whose account is settled. For review, never automatic."""
    years = clinic["retention_years"] or 10
    today = _today(clinic)
    try:
        cutoff = today.replace(year=today.year - years)
    except ValueError:
        cutoff = today.replace(year=today.year - years, day=28)
    cid = clinic["id"]
    # 0.57: one grouped query per kind of activity instead of five lookups per patient (over four minutes at 20,000 patients).
    last = {}
    for sql in ("SELECT patient_id, MAX(substr(start_at, 1, 10)) FROM appointments WHERE clinic_id = ? AND start_at IS NOT NULL GROUP BY patient_id",
                "SELECT patient_id, MAX(done_on) FROM procedures WHERE clinic_id = ? AND status = 'done' GROUP BY patient_id",
                "SELECT patient_id, MAX(received_on) FROM payments WHERE clinic_id = ? GROUP BY patient_id",
                "SELECT patient_id, MAX(substr(created_at, 1, 10)) FROM clinical_notes WHERE clinic_id = ? GROUP BY patient_id",
                "SELECT patient_id, MAX(substr(created_at, 1, 10)) FROM messages WHERE clinic_id = ? GROUP BY patient_id"):
        for pid, day in db.execute(sql, (cid,)):
            if day and day > last.get(pid, ""):
                last[pid] = day
    found = []
    for r in db.execute("""SELECT id, first_name, last_name, status, created_at FROM patients
                           WHERE clinic_id = ? AND anonymised_on IS NULL""", (cid,)):
        last_on = max(last.get(r["id"], "0000"), (r["created_at"] or "0000")[:10])
        if last_on < cutoff.isoformat():
            found.append((last_on, r))
    found.sort(key=lambda t: (t[0], t[1]["id"]))
    balances = billing.all_balances(db, cid)
    out = []
    for last_on, r in found[:limit]:
        bal = balances.get(r["id"], (0, 0))[0]
        out.append(dict(r, last_on=last_on, balance=bal, ready=(bal == 0)))
    return out, cutoff.isoformat()


# =============================================================================================================== who looked
def access_log(db, cid, pid, limit=300):
    return db.execute("""SELECT a.*, u.display_name AS user_name FROM audit_log a LEFT JOIN users u ON u.id = a.user_id
                         WHERE a.clinic_id = ? AND a.object_type = 'patient' AND a.object_id = ? ORDER BY a.id DESC LIMIT ?""",
                      (cid, pid, limit)).fetchall()


def counts(db, clinic):
    """What needs a manager's eye: open requests (and late ones) and open incidents."""
    today = _today(clinic).isoformat()
    cid = clinic["id"]
    open_req = db.execute("SELECT COUNT(*) FROM data_requests WHERE clinic_id = ? AND status = 'open'", (cid,)).fetchone()[0]
    late = db.execute("SELECT COUNT(*) FROM data_requests WHERE clinic_id = ? AND status = 'open' AND due_on < ?", (cid, today)).fetchone()[0]
    inc = [incident_view(clinic, r) for r in db.execute("SELECT * FROM incidents WHERE clinic_id = ? AND status = 'open'", (cid,))]
    return {"requests_open": open_req, "requests_late": late, "incidents_open": len(inc),
            "incidents_late": sum(1 for i in inc if i["clock_late"])}
