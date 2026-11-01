"""Staff registration, indemnity and CPD (version 0.38).

For each person: which body they are registered with, the number, their title, when the registration and their indemnity cover run
out, and how their CPD is measured. Two ways are built in:

  gdc     the UK General Dental Council's enhanced CPD: verifiable hours over a five-year cycle (100 for dentists; 75 for dental
          therapists, hygienists, orthodontic therapists and clinical dental technicians; 50 for dental nurses and dental
          technicians), at least 10 hours in any two years, and each activity linked to one or more development outcomes, A to D.
  yearly  a number of hours or points a year, set by the clinic, for bodies such as Ghana's Medical and Dental Council, whose exact
          yearly figure the clinic should take from the body itself.

Each person records their own CPD; the manager sees everyone's, and what runs out soon. A wrong entry is struck out with a reason,
never deleted. No web framework here."""
import datetime
from decimal import Decimal

from . import audit

TITLES = {"dentist": "Dentist", "therapist": "Dental therapist", "hygienist": "Dental hygienist", "ortho_therapist": "Orthodontic therapist",
          "cdt": "Clinical dental technician", "nurse": "Dental nurse", "technician": "Dental technician", "other": "Other"}
GDC_HOURS = {"dentist": 100, "therapist": 75, "hygienist": 75, "ortho_therapist": 75, "cdt": 75, "nurse": 50, "technician": 50}
SCHEMES = {"gdc": "UK General Dental Council, five-year cycle", "yearly": "A yearly target set by the clinic", "none": "Not tracked here"}
OUTCOMES = {"A": "A: communication with patients, the team and others", "B": "B: managing yourself and working with others",
            "C": "C: keeping up and developing knowledge and skills", "D": "D: behaviour that keeps patients' trust"}
BODIES = ["General Dental Council (UK)", "Medical and Dental Council (Ghana)", "Medical and Dental Council of Nigeria",
          "Health Professions Council of South Africa", "Kenya Medical Practitioners and Dentists Council", "State dental board (US)"]
WARN_DAYS = 60


class CpdError(ValueError):
    pass


def _day(text, label, required=True):
    text = (text or "").strip()
    if not text:
        if required:
            raise CpdError(label)
        return None
    try:
        return datetime.date.fromisoformat(text).isoformat()
    except ValueError:
        raise CpdError(label)


def _tenths(text, label):
    try:
        v = Decimal(str(text or "").strip().replace(",", "."))
    except Exception:
        raise CpdError(label)
    if not v.is_finite() or v <= 0 or v > 200 or v != v.quantize(Decimal("0.1")):
        raise CpdError(label)
    return int(v * 10)


def _user(db, cid, uid):
    u = db.execute("SELECT id, display_name, role FROM users WHERE id = ? AND clinic_id = ?", (uid, cid)).fetchone()
    if not u:
        raise CpdError("That person was not found.")
    return u


# -------------------------------------------------------------------------------------------------------- registration
def save_registration(db, clinic, by_user, uid, form):
    cid = clinic["id"]
    _user(db, cid, uid)
    title = form.get("title") or "other"
    scheme = form.get("scheme") or "none"
    if title not in TITLES or scheme not in SCHEMES:
        raise CpdError("Pick the title and how CPD is measured.")
    if scheme == "gdc" and title not in GDC_HOURS:
        raise CpdError("For the GDC scheme, pick one of the GDC's registered titles.")
    cycle = _day(form.get("cycle_start"), "Give the date this CPD cycle started.", scheme == "gdc")
    target = None
    if scheme == "yearly":
        target = _tenths(form.get("yearly_target"), "Give the yearly CPD target in hours or points, for example 20.")
        cycle = cycle or datetime.date.today().replace(month=1, day=1).isoformat()
    reg_exp = _day(form.get("reg_expires_on"), "Use a real date for when the registration runs out.", False)
    ind_exp = _day(form.get("indemnity_expires_on"), "Use a real date for when the indemnity cover runs out.", False)
    db.execute("""INSERT INTO staff_registrations(clinic_id, user_id, body, reg_number, title, reg_expires_on, indemnity_by, indemnity_ref,
                  indemnity_expires_on, scheme, cycle_start, yearly_target_tenths, created_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
               (cid, uid, (form.get("body") or "").strip()[:120] or None, (form.get("reg_number") or "").strip()[:60] or None, title, reg_exp,
                (form.get("indemnity_by") or "").strip()[:120] or None, (form.get("indemnity_ref") or "").strip()[:60] or None, ind_exp,
                scheme, cycle, target, by_user))
    audit.log(db, cid, by_user, "cpd.registration", "user", uid)


def registration(db, cid, uid):
    return db.execute("SELECT * FROM staff_registrations WHERE clinic_id = ? AND user_id = ? ORDER BY id DESC LIMIT 1", (cid, uid)).fetchone()


# ------------------------------------------------------------------------------------------------------------------ CPD
def add_cpd(db, clinic, by_user, uid, form, today=None):
    cid = clinic["id"]
    _user(db, cid, uid)
    title = (form.get("title") or "").strip()[:200]
    if not title:
        raise CpdError("Give the course or activity a title.")
    done = _day(form.get("done_on"), "Use a real date for when it was done.")
    if done > (today or datetime.date.today()).isoformat():
        raise CpdError("CPD can't be in the future.")
    hours = _tenths(form.get("hours"), "Give the hours as a number, for example 1.5.")
    outcomes = "".join(o for o in "ABCD" if o in (form.getlist("outcomes") if hasattr(form, "getlist") else form.get("outcomes") or []))
    reg = registration(db, cid, uid)
    verifiable = 1 if form.get("verifiable") == "1" else 0
    if reg and reg["scheme"] == "gdc" and verifiable and not outcomes:
        raise CpdError("For GDC CPD, link the activity to at least one development outcome, A to D.")
    cid_ = db.execute("""INSERT INTO cpd_records(clinic_id, user_id, done_on, title, provider, hours_tenths, verifiable, outcomes, certificate_ref, notes,
                         created_by) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                      (cid, uid, done, title, (form.get("provider") or "").strip()[:120] or None, hours, verifiable, outcomes or None,
                       (form.get("certificate_ref") or "").strip()[:120] or None, (form.get("notes") or "").strip()[:1000] or None, by_user)).lastrowid
    audit.log(db, cid, by_user, "cpd.add", "user", uid, title)
    return cid_


def strike(db, clinic, by_user, record_id, reason, own_uid=None):
    r = db.execute("SELECT * FROM cpd_records WHERE id = ? AND clinic_id = ?", (record_id, clinic["id"])).fetchone()
    if not r or (own_uid is not None and r["user_id"] != own_uid):
        raise CpdError("That CPD entry was not found.")
    if r["struck"]:
        raise CpdError("That entry is already struck out.")
    reason = (reason or "").strip()[:300]
    if not reason:
        raise CpdError("Say why the entry is wrong.")
    db.execute("UPDATE cpd_records SET struck = 1, struck_reason = ?, struck_by = ? WHERE id = ?", (reason, by_user, record_id))
    audit.log(db, clinic["id"], by_user, "cpd.strike", "user", r["user_id"], reason)


def records(db, cid, uid):
    return db.execute("SELECT * FROM cpd_records WHERE clinic_id = ? AND user_id = ? ORDER BY done_on DESC, id DESC", (cid, uid)).fetchall()


def _sum(db, cid, uid, start, end, verifiable_only):
    return db.execute("SELECT COALESCE(SUM(hours_tenths), 0) FROM cpd_records WHERE clinic_id = ? AND user_id = ? AND struck = 0 AND done_on BETWEEN ? AND ?"
                      + (" AND verifiable = 1" if verifiable_only else ""), (cid, uid, start, end)).fetchone()[0]


def _add_years(day, n):
    d = datetime.date.fromisoformat(day)
    try:
        return d.replace(year=d.year + n)
    except ValueError:
        return d.replace(year=d.year + n, day=28)


def progress(db, cid, uid, today=None):
    """Where the person stands against their scheme. None when nothing is tracked."""
    today = today or datetime.date.today()
    reg = registration(db, cid, uid)
    if not reg or reg["scheme"] == "none":
        return None
    if reg["scheme"] == "gdc":
        start = datetime.date.fromisoformat(reg["cycle_start"])
        while _add_years(start.isoformat(), 5) <= today:          # roll on to the cycle that holds today
            start = _add_years(start.isoformat(), 5)
        end = _add_years(start.isoformat(), 5) - datetime.timedelta(days=1)
        target = GDC_HOURS[reg["title"]] * 10
        done = _sum(db, cid, uid, start.isoformat(), end.isoformat(), True)
        two = _sum(db, cid, uid, _add_years(today.isoformat(), -2).isoformat(), today.isoformat(), True)
        elapsed = max(0, (today - start).days) / max(1, (end - start).days)
        return {"scheme": "gdc", "start": start.isoformat(), "end": end.isoformat(), "target": target, "done": done,
                "left": max(0, target - done), "two_years": two, "two_years_low": two < 100 and (today - start).days >= 730,
                "behind": done < target * elapsed * 0.8, "met": done >= target}
    year_start = datetime.date(today.year, 1, 1)
    target = reg["yearly_target_tenths"]
    done = _sum(db, cid, uid, year_start.isoformat(), datetime.date(today.year, 12, 31).isoformat(), False)
    elapsed = (today - year_start).days / 365
    return {"scheme": "yearly", "start": year_start.isoformat(), "end": datetime.date(today.year, 12, 31).isoformat(), "target": target, "done": done,
            "left": max(0, target - done), "behind": done < target * elapsed * 0.8, "met": done >= target, "two_years": None, "two_years_low": False}


def team(db, cid, today=None):
    """Everyone, with their registration, what runs out soon and their CPD progress."""
    today = today or datetime.date.today()
    soon = (today + datetime.timedelta(days=WARN_DAYS)).isoformat()
    out = []
    for u in db.execute("SELECT id, display_name, role FROM users WHERE clinic_id = ? AND active = 1 ORDER BY display_name", (cid,)):
        reg = registration(db, cid, u["id"])
        warn = []
        if reg:
            for field, label in (("reg_expires_on", "Registration"), ("indemnity_expires_on", "Indemnity")):
                if reg[field] and reg[field] < today.isoformat():
                    warn.append((label, "ran out", reg[field]))
                elif reg[field] and reg[field] <= soon:
                    warn.append((label, "runs out", reg[field]))
        out.append({"u": u, "reg": reg, "warn": warn, "progress": progress(db, cid, u["id"], today)})
    return out


def warnings(db, cid, today=None):
    return [x for x in team(db, cid, today) if x["warn"]]
