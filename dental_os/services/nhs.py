"""NHS dentistry in England (version 0.24): courses of treatment, bands, patient charges and exemptions, units of dental activity
(UDAs) against the contract, and the FP17 for each course.

Built from published NHS England and NHSBSA guidance for 2026/27 (sources in docs/FEATURES.md):
- Patient charges from 1 April 2026: Band 1 £27.90, Band 2 £76.60, Band 3 £332.10. Urgent care costs one Band 1 charge.
- UDAs: Band 1 = 1, Band 2a = 3, 2b = 5, 2c = 7, Band 3 = 12. The fluoride varnish sub-band (a trained dental nurse, children up to
  16) = 0.5. Denture repair = 2. Urgent (unscheduled) care is now a £75 credit, turned into UDAs at the contract's UDA value.
- Contracts of 100 UDAs or more must deliver 11 unscheduled care courses for each £10,000 of contract value in 2026/27.
- One FP17 per course, sent within 2 months (62 days) of the course ending.

The figures change each April, so they are kept as data the clinic can update, each with its date. Scotland, Wales and Northern
Ireland have their own rules and are not covered.

Sending FP17s straight to the NHS Business Services Authority needs this software to be accredited by them. Until it is, each FP17
is prepared here, checked, and keyed into the clinic's NHSBSA route; the reference that comes back is recorded on the course.
No web framework here."""
import datetime
import json

from . import audit, treatment as TR
from .timeutil import now_local, parse_date

COURSES = {"band1": "Band 1: examination, diagnosis, advice, X-rays, scale and polish",
           "urgent": "Urgent care",
           "band1_fv": "Fluoride varnish by a dental nurse (child up to 16)",
           "band2a": "Band 2a", "band2b": "Band 2b", "band2c": "Band 2c",
           "band3": "Band 3: crowns, bridges, dentures and other lab work",
           "denture_repair": "Denture repair",
           "other": "Other (no UDA)"}
CHARGE_BAND = {"band1": "band1", "urgent": "band1", "band1_fv": None, "band2a": "band2", "band2b": "band2", "band2c": "band2",
               "band3": "band3", "denture_repair": "band1", "other": None}
DEFAULT_UDAS = {"band1": 1.0, "band1_fv": 0.5, "band2a": 3.0, "band2b": 5.0, "band2c": 7.0, "band3": 12.0, "denture_repair": 2.0, "other": 0.0}
URGENT_CREDIT_P = 7500                         # £75, turned into UDAs at the contract's UDA value
DEFAULT_CHARGES = {"from": "2026-04-01", "band1": 2790, "band2": 7660, "band3": 33210,
                   "source": "NHS England dental charges poster, 1 April 2026"}
EXEMPTIONS = {"none": "Pays the charge", "under18": "Under 18", "under19_edu": "Under 19 and in full-time education",
              "pregnant": "Pregnant", "baby12": "Had a baby in the last 12 months", "pension_credit": "Pension Credit Guarantee Credit",
              "universal_credit": "Universal Credit (meets the earnings rule)", "income_support": "Income Support",
              "jsa": "Income-based Jobseeker's Allowance", "esa": "Income-related Employment and Support Allowance",
              "tax_credit": "NHS tax credit exemption certificate", "hc2": "HC2 certificate (full help)",
              "hc3": "HC3 certificate (part help)"}
EVIDENCE = {"seen": "Evidence seen", "not_seen": "Evidence not seen", "na": "No evidence needed"}
NHS_CLASSES = {"": "Not NHS", "b1": "Band 1 item (exam, X-ray, scale, advice)", "b2": "Band 2 item (filling, extraction, root canal, gum treatment, sealant)",
               "b3": "Band 3 item (crown, bridge, denture, veneer, inlay)", "urgent": "Urgent care item"}
STATUSES = {"open": "Open", "closed": "Finished, FP17 to send", "submitted": "FP17 sent", "accepted": "Accepted",
            "rejected": "Rejected", "withdrawn": "Withdrawn"}
FP17_DAYS = 62


class NhsError(Exception):
    pass


def _today(clinic):
    return now_local(clinic["timezone"]).date()


def _date(raw, required=True):
    raw = (raw or "").strip()
    if not raw:
        if required:
            raise NhsError("Use a date like 2026-11-30.")
        return None
    try:
        return parse_date(raw)
    except ValueError:
        raise NhsError("Use a date like 2026-11-30.")


def year_of(day):
    """The NHS year runs 1 April to 31 March. 2026 means 1 April 2026 to 31 March 2027."""
    return day.year if day.month >= 4 else day.year - 1


# ------------------------------------------------------------------------------------------------------- the contract
def settings(db, cid):
    row = db.execute("SELECT * FROM nhs_settings WHERE clinic_id = ?", (cid,)).fetchone()
    if row:
        d = dict(row)
        d["charges"] = json.loads(d["charges"]) if d["charges"] else DEFAULT_CHARGES
        d["udas"] = DEFAULT_UDAS | (json.loads(d["udas"]) if d["udas"] else {})
        return d
    return {"enabled": 0, "contract_no": "", "charges": DEFAULT_CHARGES, "udas": dict(DEFAULT_UDAS)}


def save_settings(db, clinic, user_id, enabled, contract_no, charges=None):
    cid = clinic["id"]
    contract_no = (contract_no or "").strip()[:30]
    if enabled and not contract_no:
        raise NhsError("Enter the NHS contract number first.")
    ch = None
    if charges:
        ch = {}
        for k in ("band1", "band2", "band3"):
            try:
                v = round(float(str(charges.get(k)).replace("£", "").strip()) * 100)
            except (TypeError, ValueError):
                raise NhsError("Enter each band charge in pounds, for example 27.90.")
            if not 0 <= v <= 500000:
                raise NhsError("Enter each band charge in pounds, for example 27.90.")
            ch[k] = v
        ch["from"] = _date(charges.get("from")).isoformat()
        ch["source"] = (charges.get("source") or "").strip()[:200] or "Entered by the clinic"
    db.execute("""INSERT INTO nhs_settings(clinic_id, enabled, contract_no) VALUES (?,?,?)
                  ON CONFLICT(clinic_id) DO UPDATE SET enabled = excluded.enabled, contract_no = excluded.contract_no""",
               (cid, 1 if enabled else 0, contract_no or None))
    if ch:
        db.execute("UPDATE nhs_settings SET charges = ? WHERE clinic_id = ?", (json.dumps(ch), cid))
    audit.log(db, cid, user_id, "nhs.settings")


def is_on(db, cid):
    return bool(settings(db, cid)["enabled"])


def save_year(db, clinic, user_id, year, udas, uda_value):
    cid = clinic["id"]
    try:
        year = int(year)
        u = int(str(udas).strip())
        val = round(float(str(uda_value).replace("£", "").strip()) * 100)
    except (TypeError, ValueError):
        raise NhsError("Enter the year, the contracted UDAs as a whole number and the UDA value in pounds.")
    if not 2020 <= year <= 2100 or not 0 < u <= 1000000 or not 100 <= val <= 50000:
        raise NhsError("Enter the year, the contracted UDAs as a whole number and the UDA value in pounds.")
    db.execute("""INSERT INTO nhs_years(clinic_id, year, udas, uda_value_p) VALUES (?,?,?,?)
                  ON CONFLICT(clinic_id, year) DO UPDATE SET udas = excluded.udas, uda_value_p = excluded.uda_value_p""",
               (cid, year, u, val))
    audit.log(db, cid, user_id, "nhs.year", None, None, str(year))


def contract_year(db, cid, year):
    return db.execute("SELECT * FROM nhs_years WHERE clinic_id = ? AND year = ?", (cid, year)).fetchone()


def save_performer(db, clinic, user_id, provider_id, number):
    number = (number or "").strip()[:20]
    if number and not number.isdigit():
        raise NhsError("A performer number is digits only.")
    db.execute("UPDATE providers SET nhs_performer_no = ? WHERE id = ? AND clinic_id = ?", (number or None, provider_id, clinic["id"]))
    audit.log(db, clinic["id"], user_id, "nhs.performer", None, None, str(provider_id))


def save_class(db, clinic, user_id, code_id, cls):
    if cls not in NHS_CLASSES:
        raise NhsError("Pick the NHS band for this procedure.")
    db.execute("UPDATE procedure_codes SET nhs_class = ? WHERE id = ? AND clinic_id = ?", (cls or None, code_id, clinic["id"]))


# --------------------------------------------------------------------------------------------------------- charges
def charges_on(db, cid, day):
    return settings(db, cid)["charges"]


def charge_for(db, cid, course_type, exemption, day, hc3_amount_p=None):
    band = CHARGE_BAND.get(course_type)
    if not band or exemption not in EXEMPTIONS:
        return 0
    full = charges_on(db, cid, day).get(band, 0)
    if exemption == "none":
        return full
    if exemption == "hc3":
        return min(full, max(hc3_amount_p or 0, 0))
    return 0


def uda_for(db, cid, course_type, day):
    """UDAs a course earns. Urgent care is a £75 credit turned into UDAs at that year's UDA value."""
    if course_type == "urgent":
        y = contract_year(db, cid, year_of(day))
        if not y:
            return None
        return round(URGENT_CREDIT_P / y["uda_value_p"], 2)
    return settings(db, cid)["udas"].get(course_type, 0.0)


# --------------------------------------------------------------------------------------------------------- courses
def _course(db, cid, course_id):
    c = db.execute("SELECT * FROM nhs_courses WHERE id = ? AND clinic_id = ?", (course_id, cid)).fetchone()
    if not c:
        raise NhsError("That course of treatment was not found.")
    return c


def open_course(db, clinic, user_id, pid, provider_id, opened_on=None, exemption="none", evidence="na", hc3_amount=None):
    cid = clinic["id"]
    if not is_on(db, cid):
        raise NhsError("NHS work is not switched on for this clinic.")
    p = db.execute("SELECT * FROM patients WHERE id = ? AND clinic_id = ? AND status = 'active'", (pid, cid)).fetchone()
    if not p:
        raise NhsError("That patient was not found.")
    pv = db.execute("SELECT * FROM providers WHERE id = ? AND clinic_id = ? AND active = 1", (provider_id, cid)).fetchone()
    if not pv:
        raise NhsError("Pick the dentist who leads this course.")
    if not pv["nhs_performer_no"]:
        raise NhsError("This dentist has no NHS performer number. A manager adds it in NHS set-up.")
    if db.execute("SELECT 1 FROM nhs_courses WHERE clinic_id = ? AND patient_id = ? AND status = 'open'", (cid, pid)).fetchone():
        raise NhsError("This patient already has an open course of treatment. Finish it first.")
    day = _date(opened_on, required=False) or _today(clinic)
    if day > _today(clinic):
        raise NhsError("A course cannot start in the future.")
    exemption, evidence = _check_exemption(exemption, evidence, p, day)
    hc3 = _pounds(hc3_amount) if exemption == "hc3" else None
    cur = db.execute("""INSERT INTO nhs_courses(clinic_id, patient_id, provider_id, opened_on, exemption, evidence, hc3_amount_p, status, created_by)
                        VALUES (?,?,?,?,?,?,?, 'open', ?)""", (cid, pid, provider_id, day.isoformat(), exemption, evidence, hc3, user_id))
    audit.log(db, cid, user_id, "nhs.open", "patient", pid)
    return cur.lastrowid


def _pounds(raw):
    try:
        v = round(float(str(raw or "0").replace("£", "").strip()) * 100)
    except ValueError:
        raise NhsError("Enter the amount on the HC3 certificate in pounds.")
    if v < 0:
        raise NhsError("Enter the amount on the HC3 certificate in pounds.")
    return v


def _age_on(dob, day):
    if not dob:
        return None
    b = datetime.date.fromisoformat(dob)
    return day.year - b.year - ((day.month, day.day) < (b.month, b.day))


def _check_exemption(exemption, evidence, p, day):
    if exemption not in EXEMPTIONS:
        raise NhsError("Pick why the patient does or does not pay.")
    if evidence not in EVIDENCE:
        raise NhsError("Say whether evidence was seen.")
    age = _age_on(p["dob"], day)
    if exemption == "under18":
        if age is None:
            raise NhsError("Add the patient's date of birth to use the under 18 exemption.")
        if age >= 18:
            raise NhsError("The patient was 18 or over when the course started, so the under 18 exemption does not apply.")
        evidence = "na"
    elif exemption == "under19_edu" and age is not None and age >= 19:
        raise NhsError("The patient was 19 or over when the course started.")
    if exemption == "none":
        evidence = "na"
    return exemption, evidence


def update_course(db, clinic, user_id, course_id, exemption, evidence, hc3_amount=None, notes=None):
    c = _course(db, clinic["id"], course_id)
    if c["status"] not in ("open", "closed", "rejected"):
        raise NhsError("An FP17 has been sent for this course. It cannot be changed now.")
    p = db.execute("SELECT * FROM patients WHERE id = ?", (c["patient_id"],)).fetchone()
    exemption, evidence = _check_exemption(exemption, evidence, p, parse_date(c["opened_on"]))
    hc3 = _pounds(hc3_amount) if exemption == "hc3" else None
    db.execute("UPDATE nhs_courses SET exemption = ?, evidence = ?, hc3_amount_p = ?, notes = ? WHERE id = ?",
               (exemption, evidence, hc3, (notes or "").strip()[:500] or None, course_id))
    audit.log(db, clinic["id"], user_id, "nhs.update", "patient", c["patient_id"])


def items(db, cid, course):
    """The NHS treatment done for this patient while the course was open."""
    end = course["closed_on"] or "9999-12-31"
    return db.execute("""SELECT pr.*, pc.code, pc.name, pc.nhs_class FROM procedures pr JOIN procedure_codes pc ON pc.id = pr.code_id
                         WHERE pr.clinic_id = ? AND pr.patient_id = ? AND pr.status = 'done' AND pc.nhs_class IS NOT NULL
                         AND pr.done_on >= ? AND pr.done_on <= ? ORDER BY pr.done_on, pr.id""",
                      (cid, course["patient_id"], course["opened_on"], end)).fetchall()


def suggest(rows):
    """Which band the work done points to. Band 2 needs the dentist to choose 2a, 2b or 2c from the rules."""
    classes = {r["nhs_class"] for r in rows}
    if "b3" in classes:
        return "band3"
    if "b2" in classes:
        return "band2"
    if "urgent" in classes and classes <= {"urgent", "b1"}:
        return "urgent"
    if "b1" in classes:
        return "band1"
    return None


CHARGE_CODES = {"band1": ("NHSB1", "NHS charge, Band 1"), "band2": ("NHSB2", "NHS charge, Band 2"), "band3": ("NHSB3", "NHS charge, Band 3")}


def _charge_code(db, cid, band):
    """The patient's NHS charge goes on their account as one line, so receipts, invoices and reports work as before."""
    code, name = CHARGE_CODES[band]
    row = db.execute("SELECT id FROM procedure_codes WHERE clinic_id = ? AND code = ?", (cid, code)).fetchone()
    if row:
        return row["id"]
    return db.execute("INSERT INTO procedure_codes(clinic_id, code, name, category, fee_p) VALUES (?,?,?, 'NHS charges', 0)",
                      (cid, code, name)).lastrowid


def close_course(db, clinic, user_id, course_id, course_type, closed_on=None, charge_account=True):
    cid = clinic["id"]
    c = _course(db, cid, course_id)
    if c["status"] != "open":
        raise NhsError("Only an open course can be finished.")
    if course_type not in COURSES:
        raise NhsError("Pick the band for this course.")
    day = _date(closed_on, required=False) or _today(clinic)
    if day < parse_date(c["opened_on"]):
        raise NhsError("A course cannot end before it started.")
    if day > _today(clinic):
        raise NhsError("A course cannot end in the future.")
    if course_type == "band1_fv":
        p = db.execute("SELECT dob FROM patients WHERE id = ?", (c["patient_id"],)).fetchone()
        age = _age_on(p["dob"], parse_date(c["opened_on"]))
        if age is None or age > 16:
            raise NhsError("The fluoride varnish course is for children up to 16.")
    uda = uda_for(db, cid, course_type, day)
    if uda is None:
        raise NhsError("Enter this year's contract (UDAs and UDA value) in NHS set-up first: urgent care UDAs depend on the UDA value.")
    charge = charge_for(db, cid, course_type, c["exemption"], parse_date(c["opened_on"]), c["hc3_amount_p"])
    adj = None
    if charge and charge_account:
        try:
            pr = TR.add_planned(db, clinic, user_id, c["patient_id"], _charge_code(db, cid, CHARGE_BAND[course_type]), fee_p=charge,
                                provider_id=c["provider_id"], notes="NHS patient charge, course started " + c["opened_on"])
            TR.mark_done(db, clinic, user_id, pr)
            adj = pr
        except TR.TreatmentError as exc:
            raise NhsError(str(exc))
    db.execute("""UPDATE nhs_courses SET status = 'closed', course_type = ?, closed_on = ?, uda = ?, charge_p = ?, charge_proc_id = ?,
                  fp17_due = ? WHERE id = ?""",
               (course_type, day.isoformat(), uda, charge, adj, (day + datetime.timedelta(days=FP17_DAYS)).isoformat(), course_id))
    audit.log(db, cid, user_id, "nhs.close", "patient", c["patient_id"], course_type)


def fp17_problems(db, clinic, course):
    """What stops the FP17 for this course going in. Empty: ready."""
    out = []
    s = settings(db, clinic["id"])
    if not s.get("contract_no"):
        out.append("The clinic's NHS contract number is missing.")
    p = db.execute("SELECT * FROM patients WHERE id = ?", (course["patient_id"],)).fetchone()
    if not p["dob"]:
        out.append("The patient's date of birth is missing.")
    if not p["gender"]:
        out.append("The patient's sex is missing.")
    if not p["address"]:
        out.append("The patient's address is missing.")
    if course["exemption"] not in ("none", "under18") and course["evidence"] == "na":
        out.append("Say whether evidence of the exemption was seen.")
    if not course["declaration_on"]:
        out.append("The patient's signed declaration is not recorded.")
    if not items(db, clinic["id"], course) and course["course_type"] not in ("urgent", "other"):
        out.append("No NHS treatment is recorded for this course. Mark the procedures that are NHS work in NHS set-up.")
    return out


def record_declaration(db, clinic, user_id, course_id, signed_on=None):
    c = _course(db, clinic["id"], course_id)
    day = _date(signed_on, required=False) or _today(clinic)
    db.execute("UPDATE nhs_courses SET declaration_on = ? WHERE id = ?", (day.isoformat(), course_id))
    audit.log(db, clinic["id"], user_id, "nhs.declaration", "patient", c["patient_id"])


def mark_sent(db, clinic, user_id, course_id, reference, sent_on=None):
    c = _course(db, clinic["id"], course_id)
    if c["status"] not in ("closed", "rejected"):
        raise NhsError("Only a finished course needs an FP17.")
    problems = fp17_problems(db, clinic, c)
    if problems:
        raise NhsError(" ".join(problems))
    reference = (reference or "").strip()[:40]
    if not reference:
        raise NhsError("Enter the reference the NHSBSA gave for this FP17.")
    day = _date(sent_on, required=False) or _today(clinic)
    db.execute("UPDATE nhs_courses SET status = 'submitted', fp17_ref = ?, fp17_sent_on = ? WHERE id = ?", (reference, day.isoformat(), course_id))
    audit.log(db, clinic["id"], user_id, "nhs.fp17", "patient", c["patient_id"])


def mark_outcome(db, clinic, user_id, course_id, outcome, note=None):
    c = _course(db, clinic["id"], course_id)
    if c["status"] != "submitted":
        raise NhsError("Only a sent FP17 can be accepted or rejected.")
    if outcome not in ("accepted", "rejected"):
        raise NhsError("Pick accepted or rejected.")
    note = (note or "").strip()[:300]
    if outcome == "rejected" and not note:
        raise NhsError("Write why the NHSBSA rejected it, so it can be put right.")
    db.execute("UPDATE nhs_courses SET status = ?, outcome_note = ? WHERE id = ?", (outcome, note or None, course_id))
    audit.log(db, clinic["id"], user_id, "nhs.outcome", "patient", c["patient_id"], outcome)


def withdraw(db, clinic, user_id, course_id, reason):
    c = _course(db, clinic["id"], course_id)
    if c["status"] not in ("open", "closed"):
        raise NhsError("Only a course with no FP17 sent can be withdrawn.")
    reason = (reason or "").strip()[:300]
    if not reason:
        raise NhsError("Say why, for example: opened by mistake.")
    if c["charge_proc_id"]:
        try:
            TR.void(db, clinic["id"], user_id, c["charge_proc_id"], "NHS course withdrawn: " + reason)
        except (TR.TreatmentError, LookupError) as exc:
            raise NhsError(str(exc))
    db.execute("UPDATE nhs_courses SET status = 'withdrawn', outcome_note = ? WHERE id = ?", (reason, course_id))
    audit.log(db, clinic["id"], user_id, "nhs.withdraw", "patient", c["patient_id"])


def get(db, cid, course_id):
    return db.execute("""SELECT n.*, p.first_name, p.last_name, p.dob, p.gender, p.address, pv.name AS dentist, pv.nhs_performer_no
                         FROM nhs_courses n JOIN patients p ON p.id = n.patient_id JOIN providers pv ON pv.id = n.provider_id
                         WHERE n.id = ? AND n.clinic_id = ?""", (course_id, cid)).fetchone()


def for_patient(db, cid, pid):
    return db.execute("""SELECT n.*, pv.name AS dentist FROM nhs_courses n JOIN providers pv ON pv.id = n.provider_id
                         WHERE n.clinic_id = ? AND n.patient_id = ? ORDER BY n.id DESC""", (cid, pid)).fetchall()


def listing(db, cid, status=None):
    q = """SELECT n.*, p.first_name, p.last_name, pv.name AS dentist FROM nhs_courses n JOIN patients p ON p.id = n.patient_id
           JOIN providers pv ON pv.id = n.provider_id WHERE n.clinic_id = ?"""
    args = [cid]
    if status:
        q += " AND n.status = ?"
        args.append(status)
    return db.execute(q + " ORDER BY COALESCE(n.fp17_due, '9999'), n.id LIMIT 500", args).fetchall()


# --------------------------------------------------------------------------------------------------- the year so far
def progress(db, clinic, year=None):
    """UDAs done this NHS year against the contract, by month and by dentist, and the urgent care target."""
    cid = clinic["id"]
    today = _today(clinic)
    year = year or year_of(today)
    start, end = datetime.date(year, 4, 1), datetime.date(year + 1, 3, 31)
    y = contract_year(db, cid, year)
    rows = db.execute("""SELECT n.*, pv.name AS dentist FROM nhs_courses n JOIN providers pv ON pv.id = n.provider_id
                         WHERE n.clinic_id = ? AND n.status IN ('closed','submitted','accepted') AND n.closed_on >= ? AND n.closed_on <= ?""",
                      (cid, start.isoformat(), end.isoformat())).fetchall()
    done = round(sum(r["uda"] or 0 for r in rows), 2)
    months = {}
    by_dentist = {}
    for r in rows:
        m = r["closed_on"][:7]
        months[m] = round(months.get(m, 0) + (r["uda"] or 0), 2)
        by_dentist[r["dentist"]] = round(by_dentist.get(r["dentist"], 0) + (r["uda"] or 0), 2)
    urgent = sum(1 for r in rows if r["course_type"] == "urgent")
    out = {"year": year, "start": start, "end": end, "contract": y, "done": done, "months": sorted(months.items()),
           "by_dentist": sorted(by_dentist.items()), "urgent": urgent, "courses": len(rows)}
    if y:
        value_p = y["udas"] * y["uda_value_p"]
        elapsed = min(max((today - start).days + 1, 0), (end - start).days + 1)
        share = elapsed / ((end - start).days + 1)
        out.update({"target": y["udas"], "pct": round(100 * done / y["udas"], 1) if y["udas"] else 0,
                    "expected": round(y["udas"] * share, 1), "value_p": value_p,
                    "urgent_target": round(value_p / 100 * 11 / 10000) if y["udas"] >= 100 else None})
    late = db.execute("SELECT COUNT(*) FROM nhs_courses WHERE clinic_id = ? AND status IN ('closed','rejected') AND fp17_due < ?",
                      (cid, today.isoformat())).fetchone()[0]
    due_soon = db.execute("""SELECT COUNT(*) FROM nhs_courses WHERE clinic_id = ? AND status IN ('closed','rejected') AND fp17_due >= ?
                             AND fp17_due <= ?""", (cid, today.isoformat(), (today + datetime.timedelta(days=14)).isoformat())).fetchone()[0]
    out.update({"late": late, "due_soon": due_soon})
    return out


def fp17_rows(db, clinic, status="closed"):
    """One line per course, with what the FP17 needs, for keying in or for the clinic's own records."""
    s = settings(db, clinic["id"])
    head = ["Contract", "Performer", "Patient surname", "Forename", "Date of birth", "Sex", "Address", "Course start", "Course end",
            "Band", "UDA", "Exemption", "Evidence", "Charge (£)", "Declaration signed", "Treatment"]
    out = [head]
    for c in listing(db, clinic["id"], status):
        full = get(db, clinic["id"], c["id"])
        its = "; ".join(f"{r['code']}{' ' + str(r['tooth']) if r['tooth'] else ''}" for r in items(db, clinic["id"], full))
        out.append([s.get("contract_no") or "", full["nhs_performer_no"] or "", full["last_name"], full["first_name"], full["dob"] or "",
                    full["gender"], (full["address"] or "").replace("\n", ", "), full["opened_on"], full["closed_on"] or "",
                    full["course_type"] or "", full["uda"] if full["uda"] is not None else "", EXEMPTIONS[full["exemption"]],
                    EVIDENCE[full["evidence"]], f"{(full['charge_p'] or 0) / 100:.2f}", full["declaration_on"] or "", its])
    return out
