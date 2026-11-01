"""Moving data in and out. Export gives a clinic all of its own data as spreadsheet files. Import reads a patient list
from a spreadsheet saved as CSV, shows what it understood, and only adds anything after a manager confirms."""
import csv
import io
import json
import re
import zipfile
from datetime import date, datetime

from . import audit
from . import patients as P

MAX_IMPORT_BYTES = 1024 * 1024
MAX_IMPORT_ROWS = 5000

EXPORTS = {
    "patients.csv": "SELECT id, first_name, last_name, dob, gender, phone, email, address, guarantor_id, preferred_contact, "
                    "reminders_ok, offers_ok, notes, status, created_at FROM patients WHERE clinic_id = ? ORDER BY id",
    "appointments.csv": "SELECT id, patient_id, provider_id, chair_id, start_at, duration_min, status, confirmation, category, "
                        "break_reason, notes, created_at FROM appointments WHERE clinic_id = ? ORDER BY id",
    "procedures.csv": "SELECT pr.id, pr.patient_id, c.code, c.name AS procedure, pr.tooth, pr.surfaces, pr.status, pr.fee_p AS fee_minor_units, "
                      "pr.planned_on, pr.done_on, pr.notes, pr.void_reason FROM procedures pr JOIN procedure_codes c ON c.id = pr.code_id "
                      "WHERE pr.clinic_id = ? ORDER BY pr.id",
    "payments.csv": "SELECT id, patient_id, receipt_no, amount_p AS amount_minor_units, method, reference, note, received_on, voided, void_reason "
                    "FROM payments WHERE clinic_id = ? ORDER BY id",
    "adjustments.csv": "SELECT id, patient_id, amount_p AS amount_minor_units, reason, note, adjusted_on FROM adjustments WHERE clinic_id = ? ORDER BY id",
    "medical_history.csv": "SELECT patient_id, conditions, medications, allergies, pregnant, smoker, blood_thinners, notes, reviewed_on "
                           "FROM medical_history WHERE clinic_id = ? ORDER BY patient_id",
    "alerts.csv": "SELECT id, patient_id, kind, severity, text, active, created_at, resolved_at FROM patient_alerts WHERE clinic_id = ? ORDER BY id",
    "tooth_conditions.csv": "SELECT id, patient_id, tooth, surfaces, condition, note, status, recorded_on, removed_on FROM tooth_conditions "
                            "WHERE clinic_id = ? ORDER BY id",
    "perio_readings.csv": "SELECT c.patient_id, c.charted_on, r.tooth, r.site, r.pocket_mm, r.recession_mm, r.bleeding FROM perio_readings r "
                          "JOIN perio_charts c ON c.id = r.chart_id WHERE r.clinic_id = ? ORDER BY c.id, r.tooth, r.site",
    "clinical_notes.csv": "SELECT id, patient_id, appointment_id, tooth, title, body, status, addendum_of, author_id, signed_by, signed_at, created_at "
                          "FROM clinical_notes WHERE clinic_id = ? AND status != 'discarded' ORDER BY id",
    "consents.csv": "SELECT id, patient_id, title, body, signer_name, signer_relation, method, signed_at, status, withdrawn_on, withdrawn_reason "
                    "FROM consents WHERE clinic_id = ? ORDER BY id",
    "files_list.csv": "SELECT id, patient_id, kind, original_name, mime, size_bytes, taken_on, note, archived, created_at FROM patient_files "
                      "WHERE clinic_id = ? ORDER BY id",
}
README = """Dental OS data export.
One CSV file per kind of record, for one clinic only. Money is in minor units (for example pesewas, cents or pence), as stored.
Times are the clinic's local time, except created_at columns, which are UTC. The images and documents themselves are not in
this export: files_list.csv lists them. Treat this folder as confidential patient data.
"""


def _safe_cell(value):
    """Stop spreadsheet programs from running a cell as a formula, without breaking phone numbers like +233201234567."""
    if isinstance(value, str) and value and value[0] in "=@\t\r":
        return "'" + value
    if isinstance(value, str) and value and value[0] in "+-" and not re.fullmatch(r"[+\-][\d .()\-]*", value):
        return "'" + value
    return value


def export_zip(db, clinic_id):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("README.txt", README)
        for name, sql in EXPORTS.items():
            cur = db.execute(sql, (clinic_id,))
            out = io.StringIO()
            writer = csv.writer(out)
            writer.writerow([d[0] for d in cur.description])
            for row in cur:
                writer.writerow([_safe_cell(v) for v in tuple(row)])
            z.writestr(name, out.getvalue())
    return buffer.getvalue()


# ----------------------------------------------------------------------------- import
ALIASES = {
    "first_name": {"first name", "firstname", "given name", "forename", "first"},
    "last_name": {"last name", "lastname", "surname", "family name", "last"},
    "phone": {"phone", "phone number", "mobile", "mobile number", "cell", "telephone", "tel", "whatsapp", "contact"},
    "dob": {"dob", "date of birth", "birthday", "birth date", "born"},
    "gender": {"gender", "sex"},
    "email": {"email", "e mail", "email address"},
    "address": {"address", "home address", "street"},
    "notes": {"notes", "note", "comments"},
}


def _norm_header(h):
    return re.sub(r"\s+", " ", re.sub(r"[_\-]+", " ", (h or "").replace("﻿", "").strip().lower()))


def map_headers(headers):
    mapping = {}
    for i, h in enumerate(headers):
        n = _norm_header(h)
        for field, names in ALIASES.items():
            if (n == field.replace("_", " ") or n in names) and field not in mapping.values():
                mapping[i] = field
                break
    return mapping


def parse_dob(text, order):
    """ISO dates always work. A date with slashes, dots or dashes is read day-first or month-first as the clinic's
    country does. Two-digit years are refused because they cannot be read safely."""
    t = (text or "").strip()
    if not t:
        return None
    if re.fullmatch(r"\d{4}-\d{1,2}-\d{1,2}", t):
        y, m, d = map(int, t.split("-"))
    else:
        parts = re.split(r"[/.\-]", t)
        if len(parts) != 3 or not all(p.isdigit() for p in parts) or len(parts[2]) != 4:
            raise ValueError("Use a date like 2001-04-23, or 23/04/2001 with a four-digit year.")
        a, b, y = int(parts[0]), int(parts[1]), int(parts[2])
        d, m = (a, b) if order == "DMY" else (b, a)
    try:
        return date(y, m, d).isoformat()
    except ValueError:
        raise ValueError("That date does not exist.")


def date_order_for(country):
    return "MDY" if (country or "").upper() in ("US", "CA") else "DMY"


def _gender(v):
    v = (v or "").strip().lower()
    return "Female" if v in ("f", "female", "woman", "girl") else "Male" if v in ("m", "male", "man", "boy") else ""


def analyse(db, clinic, text):
    """Read CSV text. Returns {'rows': [...], 'errors': [...], 'duplicates': [...], 'headers': {...}}. Adds nothing."""
    if len(text.encode("utf-8", "ignore")) > MAX_IMPORT_BYTES:
        raise ValueError("That file is bigger than 1 MB. Split it into smaller files.")
    text = text.lstrip("﻿")
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.reader(io.StringIO(text), dialect)
    try:
        header = next(reader)
    except StopIteration:
        raise ValueError("The file is empty.")
    mapping = map_headers(header)
    fields = set(mapping.values())
    missing = [f for f in ("first_name", "last_name", "phone") if f not in fields]
    if missing:
        raise ValueError("The first row must name these columns: first name, last name, phone. Missing: "
                         + ", ".join(m.replace("_", " ") for m in missing) + ".")
    order = date_order_for(clinic["country"])
    rows, errors, duplicates, seen = [], [], [], set()
    for line_no, cells in enumerate(reader, start=2):
        if not any((c or "").strip() for c in cells):
            continue
        if len(rows) + len(errors) + len(duplicates) >= MAX_IMPORT_ROWS:
            raise ValueError(f"More than {MAX_IMPORT_ROWS} rows. Split the file.")
        raw = {mapping[i]: (cells[i] if i < len(cells) else "") for i in mapping}
        problems = {}
        try:
            raw["dob"] = parse_dob(raw.get("dob"), order) or ""
        except ValueError as exc:
            problems["dob"] = str(exc)
            raw["dob"] = ""
        raw["gender"] = _gender(raw.get("gender"))
        try:
            data = P.clean(raw, db, clinic["id"], country=clinic["country"])
        except P.ValidationError as exc:
            problems.update(exc.errors)
            data = None
        if problems or data is None:
            errors.append({"line": line_no, "problems": problems})
            continue
        key = (data["phone"], data["last_name"].lower(), data["first_name"].lower())
        if key in seen or P.duplicates(db, clinic["id"], data):
            duplicates.append({"line": line_no, "name": f"{data['first_name']} {data['last_name']}"})
            continue
        seen.add(key)
        data["reminders_ok"] = 0      # consent is never imported. It has to come from the patient.
        data["offers_ok"] = 0
        rows.append({"line": line_no, "data": data})
    return {"rows": rows, "errors": errors, "duplicates": duplicates,
            "columns": sorted(fields), "order": order}


EMPTY_PAYLOAD = json.dumps({"rows": [], "errors": [], "duplicates": [], "columns": [], "order": "DMY"})


def _scrub(db, batch_id):
    """Once an import is finished or cancelled the uploaded list (names, phones, birth dates) is not needed. Only the counts stay."""
    db.execute("UPDATE import_batches SET payload = ? WHERE id = ?", (EMPTY_PAYLOAD, batch_id))


def stage(db, clinic, user_id, text):
    stale = db.execute("SELECT id FROM import_batches WHERE status = 'preview' AND created_at < datetime('now', '-2 days')").fetchall()
    for r in stale:                                   # a preview nobody confirmed within two days is dropped
        db.execute("UPDATE import_batches SET status = 'cancelled' WHERE id = ?", (r["id"],))
        _scrub(db, r["id"])
    result = analyse(db, clinic, text)
    summary = {"ok": len(result["rows"]), "errors": len(result["errors"]), "duplicates": len(result["duplicates"])}
    cur = db.execute("INSERT INTO import_batches(clinic_id, created_by, kind, payload, summary) VALUES (?,?, 'patients', ?, ?)",
                     (clinic["id"], user_id, json.dumps(result), json.dumps(summary)))
    audit.log(db, clinic["id"], user_id, "import.preview", detail=f"{summary['ok']} ok")
    return cur.lastrowid


def get_batch(db, clinic_id, batch_id):
    row = db.execute("SELECT * FROM import_batches WHERE id = ? AND clinic_id = ?", (batch_id, clinic_id)).fetchone()
    if not row:
        return None, None
    return row, json.loads(row["payload"])


def commit(db, clinic, user_id, batch_id):
    row, payload = get_batch(db, clinic["id"], batch_id)
    if not row or row["status"] != "preview":
        raise ValueError("That import is not waiting to be confirmed.")
    added = skipped = 0
    for item in payload["rows"]:
        data = item["data"]
        if P.duplicates(db, clinic["id"], data):
            skipped += 1
            continue
        P.create(db, clinic["id"], user_id, data, allow_duplicates=True)
        added += 1
    db.execute("UPDATE import_batches SET status = 'done' WHERE id = ?", (batch_id,))
    _scrub(db, batch_id)
    audit.log(db, clinic["id"], user_id, "import.commit", detail=f"{added} added")
    return added, skipped


def cancel(db, clinic, user_id, batch_id):
    cur = db.execute("UPDATE import_batches SET status = 'cancelled' WHERE id = ? AND clinic_id = ? AND status = 'preview'",
                     (batch_id, clinic["id"]))
    if not cur.rowcount:
        raise ValueError("That import is not waiting to be confirmed.")
    _scrub(db, batch_id)
    audit.log(db, clinic["id"], user_id, "import.cancel")
