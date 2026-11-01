"""Orthodontics (version 0.26): ortho cases, the ortho chart kept at each visit, and the ortho contract with its monthly fees.

A case holds the diagnosis (molar class, overjet, overbite, crowding), the appliance, the start date and the expected length.
Each adjustment visit adds a line to the ortho chart: wires, elastics, aligner number, breakages, and when to come back.
The contract is the fee the patient agreed: a deposit, then equal monthly amounts. The schedule is written out when the contract is
made, and each amount goes on the patient's account on its due date as one line, so statements, receipts and reports work as
before. Posting is safe to run any number of times: an amount already posted is never posted again.

Nothing is deleted. Stopping a case stops the amounts not yet due. No web framework here."""
import calendar
import datetime

from . import audit, treatment as TR
from .timeutil import now_local, parse_date

APPLIANCES = {"fixed_metal": "Fixed braces, metal", "fixed_ceramic": "Fixed braces, tooth-coloured", "lingual": "Lingual braces",
              "aligners": "Clear aligners", "removable": "Removable appliance", "functional": "Functional appliance", "other": "Other"}
ARCHES = {"both": "Both arches", "upper": "Upper only", "lower": "Lower only"}
MOLAR = {"": "Not recorded", "I": "Class I", "II1": "Class II division 1", "II2": "Class II division 2", "III": "Class III"}
CROWDING = {"": "Not recorded", "none": "None", "mild": "Mild", "moderate": "Moderate", "severe": "Severe", "spacing": "Spacing"}
STATUSES = {"planned": "Planned", "active": "In treatment", "retention": "In retention", "finished": "Finished", "stopped": "Stopped"}
CODES = {"deposit": ("ORTHO-DEP", "Orthodontic treatment, deposit"), "monthly": ("ORTHO-MTH", "Orthodontic treatment, monthly fee")}


class OrthoError(Exception):
    pass


def _today(clinic):
    return now_local(clinic["timezone"]).date()


def _date(raw, required=True):
    raw = (raw or "").strip()
    if not raw:
        if required:
            raise OrthoError("Use a date like 2026-11-30.")
        return None
    try:
        return parse_date(raw)
    except ValueError:
        raise OrthoError("Use a date like 2026-11-30.")


def _mm(raw):
    raw = (raw or "").strip().replace(",", ".")
    if not raw:
        return None
    try:
        v = float(raw)
    except ValueError:
        raise OrthoError("Write overjet and overbite in millimetres, for example 4 or 2.5.")
    if not -15 <= v <= 20:
        raise OrthoError("Write overjet and overbite in millimetres, for example 4 or 2.5.")
    return round(v * 10)


def add_months(d, n, day=None):
    m = d.month - 1 + n
    y, m = d.year + m // 12, m % 12 + 1
    return datetime.date(y, m, min(day or d.day, calendar.monthrange(y, m)[1]))


# ---------------------------------------------------------------------------------------------------------------- cases
def _case(db, cid, case_id):
    c = db.execute("SELECT * FROM ortho_cases WHERE id = ? AND clinic_id = ?", (case_id, cid)).fetchone()
    if not c:
        raise OrthoError("That ortho case was not found.")
    return c


def open_case(db, clinic, user_id, pid, provider_id, form):
    cid = clinic["id"]
    if not db.execute("SELECT 1 FROM patients WHERE id = ? AND clinic_id = ? AND status = 'active'", (pid, cid)).fetchone():
        raise OrthoError("That patient was not found.")
    if not db.execute("SELECT 1 FROM providers WHERE id = ? AND clinic_id = ? AND active = 1", (provider_id, cid)).fetchone():
        raise OrthoError("Pick the orthodontist or dentist for this case.")
    if db.execute("SELECT 1 FROM ortho_cases WHERE clinic_id = ? AND patient_id = ? AND status IN ('planned','active','retention')",
                  (cid, pid)).fetchone():
        raise OrthoError("This patient already has an ortho case that is not finished.")
    v = _clean(form)
    cur = db.execute("""INSERT INTO ortho_cases(clinic_id, patient_id, provider_id, appliance, arches, molar_class, overjet_tenths,
                        overbite_tenths, crowding, est_months, notes, status, created_by) VALUES (?,?,?,?,?,?,?,?,?,?,?, 'planned', ?)""",
                     (cid, pid, provider_id, v["appliance"], v["arches"], v["molar_class"], v["overjet"], v["overbite"], v["crowding"],
                      v["est_months"], v["notes"], user_id))
    audit.log(db, cid, user_id, "ortho.open", "patient", pid)
    return cur.lastrowid


def _clean(form):
    appliance = form.get("appliance") or ""
    if appliance not in APPLIANCES:
        raise OrthoError("Pick the appliance.")
    arches = form.get("arches") or "both"
    if arches not in ARCHES:
        raise OrthoError("Pick which arches are treated.")
    molar = form.get("molar_class") or ""
    if molar not in MOLAR:
        raise OrthoError("Pick the molar class from the list.")
    crowding = form.get("crowding") or ""
    if crowding not in CROWDING:
        raise OrthoError("Pick the crowding from the list.")
    try:
        months = int(str(form.get("est_months") or "0").strip() or 0)
    except ValueError:
        raise OrthoError("Write the expected length in whole months.")
    if not 1 <= months <= 60:
        raise OrthoError("Write the expected length in whole months, from 1 to 60.")
    return {"appliance": appliance, "arches": arches, "molar_class": molar, "crowding": crowding, "est_months": months,
            "overjet": _mm(form.get("overjet_mm")), "overbite": _mm(form.get("overbite_mm")), "notes": (form.get("notes") or "").strip()[:1000] or None}


def update_case(db, clinic, user_id, case_id, form):
    c = _case(db, clinic["id"], case_id)
    if c["status"] in ("finished", "stopped"):
        raise OrthoError("This case is closed.")
    v = _clean(form)
    db.execute("""UPDATE ortho_cases SET appliance = ?, arches = ?, molar_class = ?, overjet_tenths = ?, overbite_tenths = ?, crowding = ?,
                  est_months = ?, notes = ? WHERE id = ?""",
               (v["appliance"], v["arches"], v["molar_class"], v["overjet"], v["overbite"], v["crowding"], v["est_months"], v["notes"], case_id))
    audit.log(db, clinic["id"], user_id, "ortho.update", "patient", c["patient_id"])


def start(db, clinic, user_id, case_id, started_on=None):
    c = _case(db, clinic["id"], case_id)
    if c["status"] != "planned":
        raise OrthoError("Only a planned case can be started.")
    day = _date(started_on, required=False) or _today(clinic)
    if day > _today(clinic):
        raise OrthoError("Treatment cannot start in the future.")
    db.execute("UPDATE ortho_cases SET status = 'active', started_on = ? WHERE id = ?", (day.isoformat(), case_id))
    audit.log(db, clinic["id"], user_id, "ortho.start", "patient", c["patient_id"])


def debond(db, clinic, user_id, case_id, retention, debonded_on=None):
    c = _case(db, clinic["id"], case_id)
    if c["status"] != "active":
        raise OrthoError("Only a case in treatment can move to retention.")
    retention = (retention or "").strip()[:200]
    if not retention:
        raise OrthoError("Say what retainers were given, for example upper and lower bonded retainers.")
    day = _date(debonded_on, required=False) or _today(clinic)
    if c["started_on"] and day < parse_date(c["started_on"]):
        raise OrthoError("Braces cannot come off before treatment started.")
    db.execute("UPDATE ortho_cases SET status = 'retention', debonded_on = ?, retention = ? WHERE id = ?", (day.isoformat(), retention, case_id))
    audit.log(db, clinic["id"], user_id, "ortho.debond", "patient", c["patient_id"])


def close(db, clinic, user_id, case_id, outcome, reason=None):
    """Finish (after retention or treatment) or stop (the patient left, moved away, stopped paying). Amounts not yet due are stopped."""
    c = _case(db, clinic["id"], case_id)
    if c["status"] in ("finished", "stopped"):
        raise OrthoError("This case is already closed.")
    if outcome not in ("finished", "stopped"):
        raise OrthoError("Pick finished or stopped.")
    reason = (reason or "").strip()[:300]
    if outcome == "stopped" and not reason:
        raise OrthoError("Say why the case stopped, for example moved away.")
    today = _today(clinic).isoformat()
    db.execute("UPDATE ortho_cases SET status = ?, closed_on = ?, close_reason = ? WHERE id = ?", (outcome, today, reason or None, case_id))
    k = contract_for(db, clinic["id"], case_id)
    if k and k["status"] == "active":
        db.execute("UPDATE ortho_charges SET cancelled = 1 WHERE contract_id = ? AND procedure_id IS NULL AND due_on > ?", (k["id"], today))
        db.execute("UPDATE ortho_contracts SET status = ? WHERE id = ?", ("finished" if outcome == "finished" else "cancelled", k["id"]))
    audit.log(db, clinic["id"], user_id, "ortho.close", "patient", c["patient_id"], outcome)


def get(db, cid, case_id):
    return db.execute("""SELECT o.*, p.first_name, p.last_name, pv.name AS dentist FROM ortho_cases o JOIN patients p ON p.id = o.patient_id
                         JOIN providers pv ON pv.id = o.provider_id WHERE o.id = ? AND o.clinic_id = ?""", (case_id, cid)).fetchone()


def for_patient(db, cid, pid):
    return db.execute("SELECT * FROM ortho_cases WHERE clinic_id = ? AND patient_id = ? ORDER BY id DESC", (cid, pid)).fetchall()


def months_in(case, today):
    if not case["started_on"]:
        return 0
    s = parse_date(case["started_on"])
    end = parse_date(case["debonded_on"]) if case["debonded_on"] else today
    return max((end.year - s.year) * 12 + end.month - s.month - (1 if end.day < s.day else 0), 0)


# ------------------------------------------------------------------------------------------------------- the ortho chart
def add_visit(db, clinic, user_id, case_id, form):
    c = _case(db, clinic["id"], case_id)
    if c["status"] not in ("active", "retention"):
        raise OrthoError("Visits are recorded once treatment has started.")
    day = _date(form.get("visit_on"), required=False) or _today(clinic)
    if day > _today(clinic):
        raise OrthoError("A visit cannot be in the future.")
    fields = {k: (form.get(k) or "").strip()[:80] or None for k in ("upper_wire", "lower_wire", "elastics", "aligner_no", "repairs")}
    notes = (form.get("notes") or "").strip()[:1000] or None
    if not any(fields.values()) and not notes:
        raise OrthoError("Write what was done at this visit.")
    try:
        weeks = int(str(form.get("next_weeks") or "0").strip() or 0)
    except ValueError:
        raise OrthoError("Write when to come back in whole weeks.")
    if not 0 <= weeks <= 52:
        raise OrthoError("Write when to come back in whole weeks, up to 52.")
    appt = form.get("appointment_id")
    appt = int(appt) if str(appt or "").isdigit() else None
    if appt and not db.execute("SELECT 1 FROM appointments WHERE id = ? AND clinic_id = ? AND patient_id = ?",
                               (appt, clinic["id"], c["patient_id"])).fetchone():
        raise OrthoError("That visit does not belong to this patient.")
    vid = db.execute("""INSERT INTO ortho_visits(clinic_id, case_id, visit_on, appointment_id, upper_wire, lower_wire, elastics, aligner_no,
                        repairs, notes, next_weeks, created_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                     (clinic["id"], case_id, day.isoformat(), appt, fields["upper_wire"], fields["lower_wire"], fields["elastics"],
                      fields["aligner_no"], fields["repairs"], notes, weeks or None, user_id)).lastrowid
    audit.log(db, clinic["id"], user_id, "ortho.visit", "patient", c["patient_id"])
    return vid


def visits(db, cid, case_id):
    return db.execute("""SELECT v.*, u.display_name AS by_name FROM ortho_visits v LEFT JOIN users u ON u.id = v.created_by
                         WHERE v.clinic_id = ? AND v.case_id = ? ORDER BY v.visit_on DESC, v.id DESC""", (cid, case_id)).fetchall()


def next_due(db, case):
    v = db.execute("SELECT * FROM ortho_visits WHERE case_id = ? ORDER BY visit_on DESC, id DESC LIMIT 1", (case["id"],)).fetchone()
    if not v or not v["next_weeks"]:
        return None
    return parse_date(v["visit_on"]) + datetime.timedelta(weeks=v["next_weeks"])


# ----------------------------------------------------------------------------------------------------------- contracts
def contract_for(db, cid, case_id):
    return db.execute("SELECT * FROM ortho_contracts WHERE clinic_id = ? AND case_id = ? AND status != 'replaced' ORDER BY id DESC LIMIT 1",
                      (cid, case_id)).fetchone()


def _money(raw, message):
    t = str(raw or "").strip().replace(",", "")
    try:
        v = round(float(t) * 100)
    except ValueError:
        raise OrthoError(message)
    if v < 0 or v > 10**10:
        raise OrthoError(message)
    return v


def make_contract(db, clinic, user_id, case_id, total, deposit, months, first_due):
    """The agreed fee: a deposit due now, then equal monthly amounts from first_due. The last month takes any rounding."""
    cid = clinic["id"]
    c = _case(db, cid, case_id)
    if c["status"] in ("finished", "stopped"):
        raise OrthoError("This case is closed.")
    total_p = _money(total, "Write the total fee as a number.")
    deposit_p = _money(deposit or "0", "Write the deposit as a number.")
    if total_p <= 0:
        raise OrthoError("The total fee must be more than zero.")
    if deposit_p > total_p:
        raise OrthoError("The deposit cannot be more than the total fee.")
    try:
        n = int(str(months).strip())
    except ValueError:
        raise OrthoError("Write the number of monthly payments as a whole number.")
    if not (0 <= n <= 60) or (n == 0 and deposit_p != total_p):
        raise OrthoError("Use from 1 to 60 monthly payments, or 0 when the deposit is the whole fee.")
    first = _date(first_due, required=n > 0) if n else None
    old = contract_for(db, cid, case_id)
    if old and db.execute("SELECT 1 FROM ortho_charges WHERE contract_id = ? AND procedure_id IS NOT NULL", (old["id"],)).fetchone():
        raise OrthoError("Amounts from the current contract are already on the account. Stop it first, then make a new one.")
    if old:
        db.execute("UPDATE ortho_contracts SET status = 'replaced' WHERE id = ?", (old["id"],))
    rest = total_p - deposit_p
    each = rest // n if n else 0
    kid = db.execute("""INSERT INTO ortho_contracts(clinic_id, case_id, total_p, deposit_p, months, monthly_p, first_due, status, created_by)
                        VALUES (?,?,?,?,?,?,?, 'active', ?)""",
                     (cid, case_id, total_p, deposit_p, n, each, first.isoformat() if first else None, user_id)).lastrowid
    today = _today(clinic)
    if deposit_p:
        db.execute("INSERT INTO ortho_charges(clinic_id, contract_id, kind, due_on, amount_p) VALUES (?,?, 'deposit', ?, ?)",
                   (cid, kid, today.isoformat(), deposit_p))
    for i in range(n):
        amt = each if i < n - 1 else rest - each * (n - 1)
        db.execute("INSERT INTO ortho_charges(clinic_id, contract_id, kind, due_on, amount_p) VALUES (?,?, 'monthly', ?, ?)",
                   (cid, kid, add_months(first, i, first.day).isoformat(), amt))
    audit.log(db, cid, user_id, "ortho.contract", "patient", c["patient_id"])
    post_due(db, clinic, user_id)
    return kid


def stop_contract(db, clinic, user_id, case_id, reason):
    c = _case(db, clinic["id"], case_id)
    k = contract_for(db, clinic["id"], case_id)
    if not k or k["status"] != "active":
        raise OrthoError("There is no active contract to stop.")
    reason = (reason or "").strip()[:300]
    if not reason:
        raise OrthoError("Say why the contract is stopped.")
    db.execute("UPDATE ortho_charges SET cancelled = 1 WHERE contract_id = ? AND procedure_id IS NULL", (k["id"],))
    db.execute("UPDATE ortho_contracts SET status = 'cancelled', stop_reason = ? WHERE id = ?", (reason, k["id"]))
    audit.log(db, clinic["id"], user_id, "ortho.contract_stop", "patient", c["patient_id"])


def charges(db, cid, contract_id):
    return db.execute("SELECT * FROM ortho_charges WHERE clinic_id = ? AND contract_id = ? ORDER BY due_on, id", (cid, contract_id)).fetchall()


def _code(db, cid, kind):
    code, name = CODES[kind]
    row = db.execute("SELECT id FROM procedure_codes WHERE clinic_id = ? AND code = ?", (cid, code)).fetchone()
    if row:
        return row["id"]
    return db.execute("INSERT INTO procedure_codes(clinic_id, code, name, category, fee_p) VALUES (?,?,?, 'Orthodontics', 0)",
                      (cid, code, name)).lastrowid


def post_due(db, clinic, user_id=None, today=None):
    """Put every amount that is due and not yet posted on the patient's account. Returns how many were posted."""
    cid = clinic["id"]
    today = today or _today(clinic)
    rows = db.execute("""SELECT ch.*, k.case_id, o.patient_id, o.provider_id FROM ortho_charges ch JOIN ortho_contracts k ON k.id = ch.contract_id
                         JOIN ortho_cases o ON o.id = k.case_id WHERE ch.clinic_id = ? AND ch.procedure_id IS NULL AND ch.cancelled = 0
                         AND k.status = 'active' AND ch.due_on <= ? ORDER BY ch.due_on, ch.id""", (cid, today.isoformat())).fetchall()
    n = 0
    for ch in rows:
        try:
            pr = TR.add_planned(db, clinic, user_id, ch["patient_id"], _code(db, cid, ch["kind"]), fee_p=ch["amount_p"],
                                provider_id=ch["provider_id"], notes=f"Ortho contract, due {ch['due_on']}")
            TR.mark_done(db, clinic, user_id, pr)
        except TR.TreatmentError:
            continue                                     # an archived patient: left for staff to sort out
        db.execute("UPDATE ortho_charges SET procedure_id = ?, posted_on = ? WHERE id = ?", (pr, today.isoformat(), ch["id"]))
        n += 1
    left = db.execute("""SELECT k.id FROM ortho_contracts k WHERE k.clinic_id = ? AND k.status = 'active' AND NOT EXISTS
                         (SELECT 1 FROM ortho_charges ch WHERE ch.contract_id = k.id AND ch.procedure_id IS NULL AND ch.cancelled = 0)""",
                      (cid,)).fetchall()
    for k in left:
        db.execute("UPDATE ortho_contracts SET status = 'finished' WHERE id = ?", (k["id"],))
    return n


# ------------------------------------------------------------------------------------------------------------- the board
def board(db, clinic):
    cid = clinic["id"]
    today = _today(clinic)
    rows = db.execute("""SELECT o.*, p.first_name, p.last_name, pv.name AS dentist FROM ortho_cases o JOIN patients p ON p.id = o.patient_id
                         JOIN providers pv ON pv.id = o.provider_id WHERE o.clinic_id = ? AND o.status IN ('planned','active','retention')
                         ORDER BY o.status, p.last_name""", (cid,)).fetchall()
    out = []
    for o in rows:
        nd = next_due(db, o)
        k = contract_for(db, cid, o["id"])
        posted = left = 0
        if k:
            posted = db.execute("SELECT COALESCE(SUM(amount_p), 0) FROM ortho_charges WHERE contract_id = ? AND procedure_id IS NOT NULL", (k["id"],)).fetchone()[0]
            left = db.execute("SELECT COALESCE(SUM(amount_p), 0) FROM ortho_charges WHERE contract_id = ? AND procedure_id IS NULL AND cancelled = 0",
                              (k["id"],)).fetchone()[0]
        m = months_in(o, today)
        out.append({"o": o, "months": m, "over": o["status"] == "active" and m > o["est_months"], "next_due": nd,
                    "late": bool(nd and nd < today), "contract": k, "posted_p": posted, "left_p": left})
    return out
