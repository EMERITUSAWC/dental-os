"""Moving a clinic's records in from another system (versions 0.48 and 0.49).

A clinic sends one ZIP of CSV files, or a single CSV. Two layouts are read:

  Open Dental   tables exported as they are (patient, provider, procedurecode, procedurelog, appointment, operatory, payment,
                adjustment, recall, commlog), recognised from their column names. Status codes follow Open Dental's own
                documentation: procedurelog.ProcStatus 1 treatment planned, 2 complete, 3 and 4 existing work, 5 referred out,
                6 deleted, 7 condition, 8 inactive plan; appointment.AptStatus 1 scheduled, 2 complete, 3 unscheduled, 4 ASAP,
                5 broken, 6 planned, 7 and 8 patient notes; patient.PatStatus 0 patient, 1 non-patient, 2 inactive, 3 archived,
                4 deleted, 5 deceased, 6 prospective. The text forms that Open Dental's API uses (TP, C, Scheduled ...) are read too.
                Tooth numbers are Universal; durations come from the appointment's time pattern, one character per 5 minutes.
  Our own       files named for what they hold (patients, providers, treatment, appointments, payments, adjustments, balances,
                recalls, notes) with plain column names. This is the layout for Dentrix, other systems and spreadsheets: export
                from the old system, and on the preview screen match any column the system did not recognise.

Nothing is written until a manager has seen the preview. The preview runs the whole import and then undoes it, so the numbers it
shows are exactly what a real run will do. Every record keeps a link to its number in the old system, so the same files (or newer
ones, the night before going live) can be brought in again: what is already here is skipped, only new records are added.

What is never brought across: consent to messages and offers (it must come from the patient, here); card details; deleted records.
Payments are matched to the patient on the payment, not split across a family. Imported money is history: it is entered as it
was, with its own dates, and the books-closed date does not apply to it. No web framework here."""
import csv
import datetime
import io
import json
import re
import zipfile
from decimal import Decimal, InvalidOperation
from pathlib import Path

from . import audit, phone, teeth
from .money import currency_digits

MAX_BYTES = 100 * 1024 * 1024
KINDS = {"patients": "Patients", "providers": "Dentists and hygienists", "codes": "Treatment list", "chairs": "Chairs (operatories)",
         "treatment": "Treatment, done and planned", "appointments": "Appointments", "payments": "Payments", "adjustments": "Adjustments",
         "balances": "Opening balances", "recalls": "Recalls", "notes": "Notes and contact history"}
ORDER = ("providers", "chairs", "codes", "patients", "appointments", "treatment", "payments", "adjustments", "balances", "recalls", "notes")

# Plain column names for our own layout, and the other names people use for them.
FIELDS = {
    "patients": {"id": {"id", "patient id", "patient number", "chart number", "chart no", "patnum"},
                 "first_name": {"first name", "firstname", "given name", "forename", "fname"},
                 "last_name": {"last name", "lastname", "surname", "family name", "lname"},
                 "phone": {"phone", "mobile", "mobile phone", "cell", "cell phone", "wirelessphone", "whatsapp"},
                 "phone2": {"home phone", "hmphone", "telephone", "tel"}, "phone3": {"work phone", "wkphone"},
                 "dob": {"dob", "date of birth", "birthdate", "birth date", "born"}, "gender": {"gender", "sex"},
                 "email": {"email", "e mail", "email address"}, "address": {"address", "street", "address1"},
                 "address2": {"address2", "address 2"}, "city": {"city", "town"},
                 "notes": {"notes", "note", "comments"}, "medical_alert": {"medical alert", "medical note", "medurgnote", "alert"},
                 "family_head": {"family head", "guarantor", "guarantor id", "head of household"}, "status": {"status", "patstatus"}},
    "providers": {"id": {"id", "provider id", "provnum", "provider number"}, "name": {"name", "provider", "dentist", "full name"},
                  "first_name": {"first name", "fname"}, "last_name": {"last name", "lname"}, "abbr": {"abbr", "short name", "initials"}},
    "chairs": {"id": {"id", "chair id", "operatorynum", "op"}, "name": {"name", "chair", "opname", "operatory", "room"}},
    "codes": {"id": {"id", "codenum", "code id"}, "code": {"code", "proccode", "procedure code", "item code"},
              "name": {"name", "description", "descript", "procedure", "treatment"}, "fee": {"fee", "price", "standard fee"}},
    "treatment": {"id": {"id", "procnum", "treatment id", "procedure id"}, "patient_id": {"patient id", "patnum", "patient", "chart number"},
                  "code": {"code", "proccode", "procedure code", "codenum"}, "description": {"description", "descript", "procedure", "treatment"},
                  "tooth": {"tooth", "toothnum", "tooth number"}, "surfaces": {"surfaces", "surface", "surf"},
                  "status": {"status", "procstatus"}, "date": {"date", "procdate", "date done", "service date"},
                  "fee": {"fee", "procfee", "amount", "charge"}, "provider_id": {"provider id", "provnum", "provider", "dentist"},
                  "note": {"note", "notes"}},
    "appointments": {"id": {"id", "aptnum", "appointment id"}, "patient_id": {"patient id", "patnum", "patient", "chart number"},
                     "start": {"start", "aptdatetime", "date and time", "appointment date", "date"}, "time": {"time", "start time"},
                     "minutes": {"minutes", "length", "duration"}, "pattern": {"pattern"}, "status": {"status", "aptstatus"},
                     "provider_id": {"provider id", "provnum", "provider", "dentist"}, "chair": {"chair", "op", "operatory", "room"},
                     "note": {"note", "notes", "procdescript", "reason"}},
    "payments": {"id": {"id", "paynum", "payment id", "receipt"}, "patient_id": {"patient id", "patnum", "patient", "chart number"},
                 "date": {"date", "paydate", "payment date"}, "amount": {"amount", "payamt", "paid"},
                 "method": {"method", "paytype", "payment type", "how paid"}, "reference": {"reference", "checknum", "cheque number", "ref"},
                 "note": {"note", "paynote", "notes"}},
    "adjustments": {"id": {"id", "adjnum", "adjustment id"}, "patient_id": {"patient id", "patnum", "patient", "chart number"},
                    "date": {"date", "adjdate"}, "amount": {"amount", "adjamt"}, "note": {"note", "adjnote", "reason"}},
    "balances": {"patient_id": {"patient id", "patnum", "patient", "chart number"}, "balance": {"balance", "baltotal", "amount owed", "owes"},
                 "date": {"date", "as of", "balance date"}},
    "recalls": {"id": {"id", "recallnum"}, "patient_id": {"patient id", "patnum", "patient", "chart number"}, "due": {"due", "datedue", "due date"}},
    "notes": {"id": {"id", "commlognum", "note id"}, "patient_id": {"patient id", "patnum", "patient", "chart number"},
              "date": {"date", "commdatetime", "when"}, "note": {"note", "notes", "text", "summary"}},
}
NEEDED = {"patients": ("id", "first_name", "last_name"), "providers": ("id",), "chairs": ("id", "name"), "codes": ("id", "code"),
          "treatment": ("patient_id", "status"), "appointments": ("patient_id", "start"), "payments": ("patient_id", "amount", "date"),
          "adjustments": ("patient_id", "amount", "date"), "balances": ("patient_id", "balance"), "recalls": ("patient_id", "due"),
          "notes": ("patient_id", "note")}

# Open Dental tables, recognised by two of their columns, and the kind each becomes.
OPEN_DENTAL = [({"patnum", "lname", "fname"}, "patients"), ({"provnum", "abbr"}, "providers"), ({"codenum", "proccode"}, "codes"),
               ({"procnum", "procstatus"}, "treatment"), ({"aptnum", "aptstatus"}, "appointments"), ({"operatorynum", "opname"}, "chairs"),
               ({"paynum", "payamt"}, "payments"), ({"adjnum", "adjamt"}, "adjustments"), ({"recallnum", "datedue"}, "recalls"),
               ({"commlognum", "note"}, "notes")]
FILE_NAMES = {"patient": "patients", "patients": "patients", "provider": "providers", "providers": "providers", "dentists": "providers",
              "chairs": "chairs", "operatory": "chairs", "codes": "codes", "procedurecode": "codes", "treatment": "treatment",
              "procedures": "treatment", "procedurelog": "treatment", "appointments": "appointments", "appointment": "appointments",
              "payments": "payments", "payment": "payments", "adjustments": "adjustments", "adjustment": "adjustments",
              "balances": "balances", "recalls": "recalls", "recall": "recalls", "notes": "notes", "commlog": "notes"}

OD_PROC = {"1": "planned", "2": "done", "3": "existing", "4": "existing", "5": "referred", "6": "deleted", "7": "condition", "8": "planned",
           "tp": "planned", "c": "done", "ec": "existing", "eo": "existing", "r": "referred", "d": "deleted", "cn": "condition", "tpi": "planned"}
TREAT_STATUS = {"done": "done", "complete": "done", "completed": "done", "planned": "planned", "plan": "planned", "treatment plan": "planned",
                "to do": "planned"}
OD_APT = {"1": "scheduled", "2": "complete", "3": "unscheduled", "4": "asap", "5": "broken", "6": "planned", "7": None, "8": None,
          "scheduled": "scheduled", "complete": "complete", "unschedlist": "unscheduled", "asap": "asap", "broken": "broken",
          "planned": "planned", "ptnote": None, "ptnotecompleted": None, "completed": "complete", "attended": "complete",
          "missed": "broken", "no show": "broken", "cancelled": "broken", "unscheduled": "unscheduled", "waiting list": "asap"}
OD_PAT = {"0": "active", "1": "active", "2": "archived", "3": "archived", "4": None, "5": "archived", "6": "active",
          "patient": "active", "nonpatient": "active", "inactive": "archived", "archived": "archived", "deleted": None,
          "deceased": "archived", "prospective": "active", "active": "active"}
METHOD_WORDS = [("mobile_money", ("momo", "mobile", "mtn", "vodafone", "telecel", "airtel", "mpesa", "m-pesa")),
                ("card", ("card", "visa", "master", "credit", "debit", "pos")),
                ("bank_transfer", ("bank", "transfer", "cheque", "check", "eft", "ach", "wire")),
                ("insurance", ("insur", "claim", "nhis")), ("cash", ("cash",))]
SURFACE = {"B": "B", "F": "B", "V": "B", "M": "M", "O": "O", "I": "I", "D": "D", "L": "L"}


class MigrateError(ValueError):
    pass


# --------------------------------------------------------------------------------------------------------- reading files
def _norm(h):
    return re.sub(r"\s+", " ", re.sub(r"[_\-]+", " ", (h or "").replace("﻿", "").strip().lower()))


def _read_csv(text):
    text = text.lstrip("﻿")
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    rows = list(csv.reader(io.StringIO(text), dialect))
    return (rows[0], rows[1:]) if rows else ([], [])


def _decode(raw):
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    raise MigrateError("A file could not be read as text.")


def files_in(name, data):
    """[(file name, header, rows)] from a ZIP or a single CSV."""
    if len(data) > MAX_BYTES:
        raise MigrateError("That file is bigger than 100 MB. Split it, or use the command line import.")
    out = []
    if name.lower().endswith(".zip") or data[:2] == b"PK":
        try:
            z = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile:
            raise MigrateError("That ZIP file is damaged.")
        total = 0
        for info in z.infolist():
            if info.is_dir() or not info.filename.lower().endswith((".csv", ".txt", ".tsv")) or "__macosx" in info.filename.lower():
                continue
            total += info.file_size
            if total > MAX_BYTES * 3:
                raise MigrateError("The files in that ZIP are too large once unpacked.")
            header, rows = _read_csv(_decode(z.read(info)))
            out.append((Path(info.filename).name, header, rows))
    else:
        header, rows = _read_csv(_decode(data))
        out.append((Path(name).name, header, rows))
    if not out:
        raise MigrateError("No CSV files were found.")
    return out


def detect(file_name, header):
    """(kind, source) for one file: source is 'opendental' or 'plain'. kind is None when it cannot be told."""
    cols = {_norm(h).replace(" ", "") for h in header}
    for need, kind in OPEN_DENTAL:
        if need <= cols:
            return kind, "opendental"
    stem = re.sub(r"[^a-z]", "", Path(file_name).stem.lower())
    for key, kind in FILE_NAMES.items():
        if stem == key or stem.endswith(key) or stem.startswith(key):
            return kind, "plain"
    return None, "plain"


def auto_map(kind, header):
    """{column index: field} from the header."""
    mapping, used = {}, set()
    names = FIELDS.get(kind, {})
    for i, h in enumerate(header):
        n, flat = _norm(h), _norm(h).replace(" ", "")
        for field, aliases in names.items():
            if field in used:
                continue
            if n == field.replace("_", " ") or n in aliases or flat in {a.replace(" ", "") for a in aliases}:
                mapping[i] = field
                used.add(field)
                break
    return mapping


def describe(files, overrides=None):
    """What the preview screen shows for each file before anything runs."""
    overrides = overrides or {}
    out = []
    for idx, (name, header, rows) in enumerate(files):
        o = overrides.get(str(idx), {})
        kind, source = detect(name, header)
        kind = o.get("kind", kind)
        mapping = auto_map(kind, header) if kind else {}
        for col, field in (o.get("map") or {}).items():
            col = int(col)
            mapping = {k: v for k, v in mapping.items() if v != field or not field}
            if field:
                mapping[col] = field
            else:
                mapping.pop(col, None)
        missing = [f for f in NEEDED.get(kind, ())if f not in mapping.values()] if kind else []
        out.append({"index": idx, "name": name, "kind": kind, "source": source, "header": header, "rows": len(rows),
                    "mapping": mapping, "missing": missing, "fields": list(FIELDS.get(kind, {}))})
    return out


# ------------------------------------------------------------------------------------------------------- reading values
def _blank(v):
    v = (v or "").strip()
    return "" if v in ("0001-01-01", "0001-01-01 00:00:00", "1900-01-01", "0000-00-00", "NULL", "null") else v


def _date(v, order):
    v = _blank(v)
    if not v:
        return None
    v = v.replace("T", " ")
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})", v)
    try:
        if m:
            return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
        parts = re.split(r"[/.\-]", v.split(" ")[0])
        if len(parts) == 3 and all(p.isdigit() for p in parts) and len(parts[2]) == 4:
            a, b, y = int(parts[0]), int(parts[1]), int(parts[2])
            d, mth = (a, b) if order == "DMY" else (b, a)
            return datetime.date(y, mth, d).isoformat()
    except ValueError:
        return None
    return None


def _time(v):
    m = re.search(r"(\d{1,2}):(\d{2})(?::\d{2})?\s*([AaPp][Mm])?", v or "")
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    if m.group(3):
        h = h % 12 + (12 if m.group(3).lower() == "pm" else 0)
    return f"{h:02d}:{mi:02d}" if h < 24 and mi < 60 else None


def _money(v, digits):
    v = re.sub(r"[^\d.,\-()]", "", _blank(v))
    if not v:
        return None
    neg = v.startswith("(") and v.endswith(")") or v.startswith("-")
    v = v.strip("()-")
    if re.fullmatch(r"\d+,\d{1,2}", v):
        v = v.replace(",", ".")
    v = v.replace(",", "")
    try:
        d = Decimal(v)
    except InvalidOperation:
        return None
    minor = int((d * (10 ** digits)).to_integral_value())
    return -minor if neg else minor


def _phone(values, country):
    for v in values:
        v = _blank(v)
        if not v:
            continue
        try:
            return phone.normalise_phone(v, country)
        except ValueError:
            continue
    return ""


def _gender(v):
    v = _blank(v).lower()
    return "Male" if v in ("0", "m", "male", "man", "boy") else "Female" if v in ("1", "f", "female", "woman", "girl") else ""


def _tooth(v, notation):
    v = _blank(v)
    if not v:
        return None
    try:
        return teeth.parse(v, notation)
    except teeth.ToothError:
        return None


def _surfaces(v):
    letters = {SURFACE[c] for c in (_blank(v) or "").upper() if c in SURFACE}
    return "".join(s for s in "MODBLI" if s in letters) or None


def _method(v):
    t = _blank(v).lower()
    for method, words in METHOD_WORDS:
        if any(w in t for w in words):
            return method
    return None


# --------------------------------------------------------------------------------------------------------------- the run
class _Run:
    def __init__(self, db, clinic, user_id, files, overrides, notation):
        self.db, self.clinic, self.user_id = db, clinic, user_id
        self.cid = clinic["id"]
        self.files = files
        self.info = describe(files, overrides)
        self.order = "MDY" if (clinic["country"] or "").upper() in ("US", "CA") else "DMY"
        self.digits = currency_digits(clinic["currency"])
        self.notation = notation
        self.counts = {k: 0 for k in KINDS}
        self.skipped = {}
        self.already = {k: 0 for k in KINDS}
        self.problems = []
        self.no_phone = 0
        self.cache = {}

    def skip(self, why, n=1):
        self.skipped[why] = self.skipped.get(why, 0) + n

    def problem(self, file_name, line, text):
        if len(self.problems) < 300:
            self.problems.append({"file": file_name, "line": line, "text": text})

    def link(self, kind, source_id):
        if not source_id:
            return None
        key = (kind, source_id)
        if key not in self.cache:
            r = self.db.execute("SELECT new_id FROM migration_links WHERE clinic_id = ? AND kind = ? AND source_id = ?",
                                (self.cid, kind, source_id)).fetchone()
            self.cache[key] = r["new_id"] if r else None
        return self.cache[key]

    def remember(self, kind, source_id, new_id):
        if source_id:
            self.db.execute("INSERT INTO migration_links(clinic_id, kind, source_id, new_id) VALUES (?,?,?,?)", (self.cid, kind, source_id, new_id))
            self.cache[(kind, source_id)] = new_id

    def records(self, kind):
        for f in self.info:
            if f["kind"] != kind or f["missing"]:
                continue
            for line, cells in enumerate(self.files[f["index"]][2], start=2):
                if not any((c or "").strip() for c in cells):
                    continue
                yield f, line, {field: (cells[i] if i < len(cells) else "").strip() for i, field in f["mapping"].items()}

    # ----- each kind
    def providers(self):
        for f, line, r in self.records("providers"):
            sid = r.get("id")
            if self.link("providers", sid):
                self.already["providers"] += 1
                continue
            name = r.get("name") or " ".join(x for x in (r.get("first_name"), r.get("last_name")) if x) or r.get("abbr")
            if not name:
                self.problem(f["name"], line, "No name.")
                continue
            same = self.db.execute("SELECT id FROM providers WHERE clinic_id = ? AND LOWER(name) = LOWER(?)", (self.cid, name[:80])).fetchone()
            pid = same["id"] if same else self.db.execute("INSERT INTO providers(clinic_id, name) VALUES (?,?)", (self.cid, name[:80])).lastrowid
            self.remember("providers", sid, pid)
            self.counts["providers"] += 1

    def chairs(self):
        for f, line, r in self.records("chairs"):
            sid = r.get("id")
            if self.link("chairs", sid):
                self.already["chairs"] += 1
                continue
            name = (r.get("name") or "").strip()[:60] or f"Chair {sid}"
            same = self.db.execute("SELECT id FROM chairs WHERE clinic_id = ? AND LOWER(name) = LOWER(?)", (self.cid, name)).fetchone()
            new = same["id"] if same else self.db.execute("INSERT INTO chairs(clinic_id, name) VALUES (?,?)", (self.cid, name)).lastrowid
            self.remember("chairs", sid, new)
            self.counts["chairs"] += 1

    def codes(self):
        for f, line, r in self.records("codes"):
            sid, code = r.get("id"), (r.get("code") or "").strip()[:20]
            if self.link("codes", sid):
                self.already["codes"] += 1
                continue
            self.remember("codes", sid, self._code(code, r.get("name"), _money(r.get("fee"), self.digits)))
            self.counts["codes"] += 1

    def _code(self, code, name, fee=None):
        code = (code or "").strip()[:20]
        if not code:
            code = "IMP-" + re.sub(r"[^A-Z0-9]", "", (name or "X").upper())[:12]
        row = self.db.execute("SELECT id FROM procedure_codes WHERE clinic_id = ? AND code = ?", (self.cid, code)).fetchone()
        if row:
            return row["id"]
        return self.db.execute("INSERT INTO procedure_codes(clinic_id, code, name, category, fee_p) VALUES (?,?,?, 'Imported', ?)",
                               (self.cid, code, ((name or "").strip() or code)[:120], max(fee or 0, 0))).lastrowid

    def patients(self):
        heads = []
        for f, line, r in self.records("patients"):
            sid = r.get("id")
            if self.link("patients", sid):
                self.already["patients"] += 1
                continue
            status = OD_PAT.get((r.get("status") or "").strip().lower(), "active")
            if status is None:
                self.skip("Deleted in the old system")
                continue
            first, last = (r.get("first_name") or "").strip()[:60], (r.get("last_name") or "").strip()[:60]
            if not first or not last:
                self.problem(f["name"], line, "A first name and a last name are both needed.")
                continue
            tel = _phone([r.get("phone"), r.get("phone2"), r.get("phone3")], self.clinic["country"])
            if not tel:
                self.no_phone += 1
            email = (r.get("email") or "").strip().lower()
            if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
                email = ""
            address = ", ".join(x for x in (r.get("address"), r.get("address2"), r.get("city")) if x and x.strip())[:300] or None
            dob = _date(r.get("dob"), self.order)
            if dob and (dob > datetime.date.today().isoformat() or dob < "1900-01-01"):
                dob = None
            pid = self.db.execute("""INSERT INTO patients(clinic_id, first_name, last_name, dob, gender, phone, email, address, notes, status)
                                     VALUES (?,?,?,?,?,?,?,?,?,?)""",
                                  (self.cid, first, last, dob, _gender(r.get("gender")), tel, email or None, address,
                                   (r.get("notes") or "").strip()[:4000] or None, status)).lastrowid
            if (r.get("medical_alert") or "").strip():
                self.db.execute("INSERT INTO patient_alerts(clinic_id, patient_id, kind, severity, text, created_by) VALUES (?,?, 'medical', 'warning', ?, ?)",
                                (self.cid, pid, r["medical_alert"].strip()[:500], self.user_id))
            self.remember("patients", sid, pid)
            self.counts["patients"] += 1
            if r.get("family_head") and r["family_head"] != sid:
                heads.append((pid, r["family_head"]))
        for pid, head_sid in heads:
            head = self.link("patients", head_sid)
            if head and head != pid:
                self.db.execute("UPDATE patients SET guarantor_id = ? WHERE id = ?", (head, pid))

    def _patient(self, f, line, r):
        pid = self.link("patients", r.get("patient_id"))
        if not pid:
            self.problem(f["name"], line, "The patient is not in the patient file.")
        return pid

    def _provider(self, sid):
        new = self.link("providers", sid) if sid else None
        if not new and sid:
            row = self.db.execute("SELECT id FROM providers WHERE clinic_id = ? AND LOWER(name) = LOWER(?)", (self.cid, sid)).fetchone()
            new = row["id"] if row else None
        return new

    def _fallback(self, table, name):
        row = self.db.execute(f"SELECT id FROM {table} WHERE clinic_id = ? AND active = 1 ORDER BY id LIMIT 1", (self.cid,)).fetchone()
        return row["id"] if row else self.db.execute(f"INSERT INTO {table}(clinic_id, name) VALUES (?,?)", (self.cid, name)).lastrowid

    def appointments(self):
        for f, line, r in self.records("appointments"):
            sid = r.get("id")
            if sid and self.link("appointments", sid):
                self.already["appointments"] += 1
                continue
            status = OD_APT.get((r.get("status") or "scheduled").strip().lower(), "scheduled")
            if status is None:
                self.skip("Appointment notes, not visits")
                continue
            pid = self._patient(f, line, r)
            if not pid:
                continue
            day = _date(r.get("start"), self.order)
            hhmm = _time(r.get("time") or r.get("start"))
            start = f"{day} {hhmm or '09:00'}" if day else None
            if status in ("scheduled", "complete") and not start:
                self.problem(f["name"], line, "A booked or finished visit needs a date.")
                continue
            if status in ("unscheduled", "asap", "planned"):
                start = None
            minutes = int(r["minutes"]) if (r.get("minutes") or "").isdigit() else (len(r["pattern"]) * 5 if r.get("pattern") else 30)
            minutes = min(max(minutes, 5), 480)
            prov = self._provider(r.get("provider_id"))
            chair = self.link("chairs", r.get("chair")) if r.get("chair") else None
            if not chair and r.get("chair"):
                row = self.db.execute("SELECT id FROM chairs WHERE clinic_id = ? AND LOWER(name) = LOWER(?)", (self.cid, r["chair"])).fetchone()
                chair = row["id"] if row else None
            if status in ("scheduled", "complete"):
                prov = prov or self._fallback("providers", "Dentist (imported)")
                chair = chair or self._fallback("chairs", "Chair (imported)")
            aid = self.db.execute("""INSERT INTO appointments(clinic_id, patient_id, provider_id, chair_id, start_at, duration_min, status, notes)
                                     VALUES (?,?,?,?,?,?,?,?)""", (self.cid, pid, prov, chair, start, minutes, status,
                                                                 (r.get("note") or "").strip()[:500] or None)).lastrowid
            self.remember("appointments", sid, aid)
            self.counts["appointments"] += 1

    def treatment(self):
        for f, line, r in self.records("treatment"):
            sid = r.get("id")
            if sid and self.link("treatment", sid):
                self.already["treatment"] += 1
                continue
            raw = (r.get("status") or "").strip().lower()
            status = OD_PROC.get(raw) if f["source"] == "opendental" or raw in OD_PROC else TREAT_STATUS.get(raw)
            status = status or TREAT_STATUS.get(raw)
            if status in ("existing", "referred", "deleted", "condition"):
                self.skip({"existing": "Existing work from before (not charged here)", "referred": "Referred to someone else",
                           "deleted": "Deleted in the old system", "condition": "Conditions, not treatment"}[status])
                continue
            if status not in ("done", "planned"):
                self.problem(f["name"], line, "The status should say done or planned.")
                continue
            pid = self._patient(f, line, r)
            if not pid:
                continue
            code_id = self.link("codes", r.get("code")) or self._code(r.get("code"), r.get("description"))
            day = _date(r.get("date"), self.order) or datetime.date.today().isoformat()
            fee = _money(r.get("fee"), self.digits) or 0
            if fee < 0:
                self.problem(f["name"], line, "A fee can't be below zero.")
                continue
            pr = self.db.execute("""INSERT INTO procedures(clinic_id, patient_id, code_id, tooth, surfaces, status, fee_p, provider_id, planned_on,
                                    done_on, notes, created_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                                 (self.cid, pid, code_id, _tooth(r.get("tooth"), self.notation), _surfaces(r.get("surfaces")), status, fee,
                                  self._provider(r.get("provider_id")), day, day if status == "done" else None,
                                  (r.get("note") or "").strip()[:500] or None, self.user_id)).lastrowid
            self.remember("treatment", sid, pr)
            self.counts["treatment"] += 1

    def _next_receipt(self):
        if "receipt" not in self.cache:
            self.cache["receipt"] = self.db.execute("SELECT COALESCE(MAX(receipt_no), 0) FROM payments WHERE clinic_id = ?", (self.cid,)).fetchone()[0]
        self.cache["receipt"] += 1
        return self.cache["receipt"]

    def _correction(self, pid, amount, day, note):
        self.db.execute("INSERT INTO adjustments(clinic_id, patient_id, amount_p, reason, note, adjusted_on, created_by) VALUES (?,?,?, 'correction', ?, ?, ?)",
                        (self.cid, pid, amount, note[:500], day, self.user_id))

    def payments(self):
        for f, line, r in self.records("payments"):
            sid = r.get("id")
            if sid and self.link("payments", sid):
                self.already["payments"] += 1
                continue
            pid = self._patient(f, line, r)
            if not pid:
                continue
            amount = _money(r.get("amount"), self.digits)
            day = _date(r.get("date"), self.order)
            if amount is None or not day:
                self.problem(f["name"], line, "A payment needs an amount and a date.")
                continue
            if amount == 0:
                self.skip("Payments of nothing")
                continue
            old = f"Old system payment {sid}" if sid else "Old system payment"
            if amount < 0:                                  # money paid back in the old system
                self._correction(pid, -amount, day, f"{old}: money paid back")
                self.remember("payments", sid, -1)            # entered as an adjustment, not a payment
                self.counts["payments"] += 1
                continue
            method = _method(r.get("method")) or "cash"
            note = "; ".join(x for x in (old, f"paid by {r['method']}" if r.get("method") else "", (r.get("note") or "").strip()) if x)[:500]
            new = self.db.execute("""INSERT INTO payments(clinic_id, patient_id, amount_p, method, reference, note, receipt_no, received_on, received_by)
                                     VALUES (?,?,?,?,?,?,?,?,?)""", (self.cid, pid, amount, method, (r.get("reference") or "").strip()[:60] or None,
                                                                    note, self._next_receipt(), day, self.user_id)).lastrowid
            self.remember("payments", sid, new)
            self.counts["payments"] += 1

    def adjustments(self):
        for f, line, r in self.records("adjustments"):
            sid = r.get("id")
            if sid and self.link("adjustments", sid):
                self.already["adjustments"] += 1
                continue
            pid = self._patient(f, line, r)
            if not pid:
                continue
            amount, day = _money(r.get("amount"), self.digits), _date(r.get("date"), self.order)
            if not amount or not day:
                self.problem(f["name"], line, "An adjustment needs an amount and a date.")
                continue
            self._correction(pid, amount, day, "Old system adjustment" + (f": {r['note'].strip()}" if (r.get("note") or "").strip() else ""))
            self.remember("adjustments", sid, -1)
            self.counts["adjustments"] += 1

    def balances(self):
        from . import billing
        for f, line, r in self.records("balances"):
            pid = self._patient(f, line, r)
            if not pid:
                continue
            if self.link("balances", r.get("patient_id")):
                self.already["balances"] += 1
                continue
            want = _money(r.get("balance"), self.digits)
            if want is None:
                self.problem(f["name"], line, "The balance is not a number.")
                continue
            diff = want - billing.totals(self.db, self.cid, pid)["balance"]
            if diff:
                self._correction(pid, diff, _date(r.get("date"), self.order) or datetime.date.today().isoformat(), "Opening balance from the old system")
            self.remember("balances", r.get("patient_id"), pid)
            self.counts["balances"] += 1

    def recalls(self):
        tid = None
        for f, line, r in self.records("recalls"):
            sid = r.get("id") or f"{r.get('patient_id')}:{r.get('due')}"
            if self.link("recalls", sid):
                self.already["recalls"] += 1
                continue
            pid = self._patient(f, line, r)
            due = _date(r.get("due"), self.order)
            if not pid or not due:
                if pid:
                    self.problem(f["name"], line, "No due date.")
                continue
            if tid is None:
                row = self.db.execute("SELECT id FROM recall_types WHERE clinic_id = ? AND active = 1 ORDER BY id LIMIT 1", (self.cid,)).fetchone()
                tid = row["id"] if row else self.db.execute("INSERT INTO recall_types(clinic_id, name, interval_months) VALUES (?, 'Check-up', 6)",
                                                            (self.cid,)).lastrowid
            if self.db.execute("SELECT 1 FROM recalls WHERE clinic_id = ? AND patient_id = ? AND type_id = ?", (self.cid, pid, tid)).fetchone():
                self.skip("A second recall for the same patient")
                continue
            self.db.execute("INSERT INTO recalls(clinic_id, patient_id, type_id, due_on) VALUES (?,?,?,?)", (self.cid, pid, tid, due))
            self.remember("recalls", sid, pid)
            self.counts["recalls"] += 1

    def notes(self):
        for f, line, r in self.records("notes"):
            sid = r.get("id")
            if sid and self.link("notes", sid):
                self.already["notes"] += 1
                continue
            pid = self._patient(f, line, r)
            text = (r.get("note") or "").strip()
            if not pid or not text:
                continue
            day = _date(r.get("date"), self.order)
            cl = self.db.execute("INSERT INTO contact_log(clinic_id, patient_id, kind, summary, outcome, created_by, created_at) VALUES (?,?, 'other', ?, ?, ?, ?)",
                                 (self.cid, pid, text[:2000], "From the old system", self.user_id,
                                  (day + " 12:00:00") if day else datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))).lastrowid
            self.remember("notes", sid, cl)
            self.counts["notes"] += 1

    def go(self):
        for kind in ORDER:
            getattr(self, kind)()
        return {"counts": self.counts, "already": self.already, "skipped": self.skipped, "problems": self.problems, "no_phone": self.no_phone,
                "files": [{k: v for k, v in f.items() if k != "header"} | {"header": f["header"][:60]} for f in self.info]}


def run(db, clinic, user_id, files, overrides=None, notation="universal", keep=False):
    """Bring the files in. With keep=False everything is undone at the end, so the result is a true preview."""
    if notation not in teeth.NOTATIONS:
        raise MigrateError("Pick how the old system numbers teeth.")
    if not db.in_transaction:
        db.execute("BEGIN")
    try:
        result = _Run(db, clinic, user_id, files, overrides or {}, notation).go()
    except Exception:
        db.rollback()
        raise
    if keep:
        audit.log(db, clinic["id"], user_id, "migrate.import", None, None, json.dumps(result["counts"]))
        db.commit()
    else:
        db.rollback()
    return result


# ----------------------------------------------------------------------------------------------------- staging uploads
def folder(instance_path):
    p = Path(instance_path) / "migrations"
    p.mkdir(parents=True, exist_ok=True)
    return p


def stage(db, clinic, user_id, instance_path, name, data):
    files_in(name, data)                                   # checks it can be read before anything is kept
    rid = db.execute("INSERT INTO migration_runs(clinic_id, file_name, status, overrides, created_by) VALUES (?,?, 'preview', '{}', ?)",
                     (clinic["id"], Path(name).name[:200], user_id)).lastrowid
    (folder(instance_path) / f"{clinic['id']}-{rid}.bin").write_bytes(data)
    audit.log(db, clinic["id"], user_id, "migrate.upload", None, None, Path(name).name[:200])
    return rid


def get_run(db, cid, rid):
    return db.execute("SELECT * FROM migration_runs WHERE id = ? AND clinic_id = ?", (rid, cid)).fetchone()


def load(instance_path, cid, run_row):
    p = folder(instance_path) / f"{cid}-{run_row['id']}.bin"
    if not p.exists():
        raise MigrateError("The uploaded file is no longer here. Upload it again.")
    return files_in(run_row["file_name"], p.read_bytes())


def finish(db, instance_path, cid, rid, status, summary=None):
    db.execute("UPDATE migration_runs SET status = ?, summary = ?, finished_at = datetime('now') WHERE id = ?",
               (status, json.dumps(summary) if summary else None, rid))
    p = folder(instance_path) / f"{cid}-{rid}.bin"
    if p.exists():
        p.unlink()                                         # the uploaded records are not kept once the import is done or cancelled


def runs(db, cid):
    return db.execute("SELECT * FROM migration_runs WHERE clinic_id = ? ORDER BY id DESC LIMIT 30", (cid,)).fetchall()
