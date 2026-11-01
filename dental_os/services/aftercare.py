"""Aftercare sheets (version 0.44): what to do after treatment, printed for the patient.

The clinic writes a sheet for each kind of treatment (after an extraction, after a filling, after whitening, a new denture), in
English and, if it wants, French, and links it to the treatments on its list. A dentist must approve a sheet before it can be given
out, and approves it again after any change, because it is clinical advice. When a patient has that treatment, staff print the
sheets for it in the patient's language, and the system notes that they were given.

One starter sheet comes with the system, for tooth extractions, drawn from a published NHS hospital leaflet (University College
London Hospitals). It is not approved: the clinic's dentist reads it, changes what their practice does differently, and approves it.

Sheets are printed, not sent by WhatsApp or email: those messages never carry treatment details. Nothing is deleted. No web framework."""
import datetime
import json

from . import audit

STARTER_TITLE = "After a tooth extraction"
STARTER = """For the first few hours
- The numbness wears off in about 2 to 6 hours. Until then, avoid hot food and drinks so you do not burn or bite yourself.
- Eat soft food for the first few days: soup, mashed yam or potato, rice, scrambled egg.

The first 24 hours
- Do not rinse, spit or touch the area with your tongue or fingers. This lets a clot form in the socket.
- Avoid hard exercise.
- Do not smoke for at least 48 hours.

After 24 hours
- Brush your teeth as normal, keeping away from the socket.
- Rinse gently with warm salt water at least 4 times a day: two teaspoons of salt in a large cup of hot water, left to cool. Keep this up for a week or until the socket has closed.

Bleeding
- A little blood in your saliva for a few days is normal.
- If it bleeds more, roll up clean gauze or a clean handkerchief, put it on the socket and bite hard for 30 minutes. If it is still bleeding, bite for another 30 minutes.
- If it is still bleeding after an hour of biting hard, call us.

Pain and swelling
- Pain and swelling usually get worse for about 3 days, then settle. Some discomfort can last a couple of weeks.
- Take ordinary painkillers as the packet says, and never more than it says. Avoid painkillers that contain aspirin, because they can make it bleed.
- If you were given antibiotics, finish the course. If you get a rash, stop and call us.

Call us if
- the bleeding will not stop, the pain gets much worse after 3 days, or the swelling spreads or makes it hard to swallow or breathe. If you can't reach us and it is serious, go to the nearest emergency department."""


class AftercareError(ValueError):
    pass


def ensure_starter(db, cid):
    if not db.execute("SELECT 1 FROM aftercare_sheets WHERE clinic_id = ?", (cid,)).fetchone():
        db.execute("INSERT INTO aftercare_sheets(clinic_id, title, body, source, codes) VALUES (?,?,?,?, '[]')",
                   (cid, STARTER_TITLE, STARTER, "Starter text from UCLH NHS Foundation Trust, 'Dental extractions: post-operative instructions'."))


def sheets(db, cid, active_only=False):
    ensure_starter(db, cid)
    return db.execute("SELECT * FROM aftercare_sheets WHERE clinic_id = ?" + (" AND active = 1" if active_only else "") + " ORDER BY title", (cid,)).fetchall()


def get(db, cid, sid):
    return db.execute("SELECT * FROM aftercare_sheets WHERE id = ? AND clinic_id = ?", (sid, cid)).fetchone()


def save(db, clinic, user_id, form, sid=None):
    cid = clinic["id"]
    title = (form.get("title") or "").strip()[:120]
    body = (form.get("body") or "").strip()[:8000]
    if not title or not body:
        raise AftercareError("Give the sheet a title and write what the patient should do.")
    body_fr = (form.get("body_fr") or "").strip()[:8000] or None
    title_fr = (form.get("title_fr") or "").strip()[:120] or None
    valid = {r["id"] for r in db.execute("SELECT id FROM procedure_codes WHERE clinic_id = ?", (cid,))}
    raw = form.getlist("codes") if hasattr(form, "getlist") else (form.get("codes") or [])
    codes = sorted({int(c) for c in raw if str(c).isdigit() and int(c) in valid})
    active = 1 if form.get("active", "1") == "1" else 0
    if sid:
        s = get(db, cid, sid)
        if not s:
            raise AftercareError("That sheet was not found.")
        changed = (s["title"], s["body"], s["title_fr"], s["body_fr"]) != (title, body, title_fr, body_fr)
        db.execute("""UPDATE aftercare_sheets SET title = ?, body = ?, title_fr = ?, body_fr = ?, codes = ?, active = ?, updated_at = datetime('now')"""
                   + (", approved_by = NULL, approved_at = NULL" if changed else "") + " WHERE id = ?",
                   (title, body, title_fr, body_fr, json.dumps(codes), active, sid))
    else:
        sid = db.execute("INSERT INTO aftercare_sheets(clinic_id, title, body, title_fr, body_fr, codes, active, created_by) VALUES (?,?,?,?,?,?,?,?)",
                         (cid, title, body, title_fr, body_fr, json.dumps(codes), active, user_id)).lastrowid
    audit.log(db, cid, user_id, "aftercare.save", None, None, title)
    return sid


def approve(db, clinic, user, sid):
    if user["role"] != "dentist" and not (user["role"] == "manager" and _is_dentist(db, user)):
        raise AftercareError("A dentist approves aftercare sheets, because they are clinical advice.")
    s = get(db, clinic["id"], sid)
    if not s:
        raise AftercareError("That sheet was not found.")
    db.execute("UPDATE aftercare_sheets SET approved_by = ?, approved_at = datetime('now') WHERE id = ?", (user["id"], sid))
    audit.log(db, clinic["id"], user["id"], "aftercare.approve", None, None, s["title"])


def _is_dentist(db, user):
    """A manager who is also a dentist: their registration (version 0.38) says so."""
    row = db.execute("SELECT title FROM staff_registrations WHERE user_id = ? ORDER BY id DESC LIMIT 1", (user["id"],)).fetchone()
    return bool(row and row["title"] == "dentist")


def for_codes(db, cid, code_ids):
    out = []
    for s in sheets(db, cid, True):
        if s["approved_at"] and set(json.loads(s["codes"] or "[]")) & set(code_ids):
            out.append(s)
    return out


def suggested(db, cid, pid):
    """Approved sheets for treatment this patient had in the last 7 days, or has planned."""
    since = (datetime.date.today() - datetime.timedelta(days=7)).isoformat()
    codes = [r["code_id"] for r in db.execute("""SELECT DISTINCT code_id FROM procedures WHERE clinic_id = ? AND patient_id = ?
                                                 AND (status = 'planned' OR (status = 'done' AND done_on >= ?))""", (cid, pid, since))]
    return for_codes(db, cid, codes)


def give(db, clinic, user_id, pid, sheet_ids):
    cid = clinic["id"]
    if not db.execute("SELECT 1 FROM patients WHERE id = ? AND clinic_id = ?", (pid, cid)).fetchone():
        raise AftercareError("That patient was not found.")
    picked = [get(db, cid, i) for i in sheet_ids]
    picked = [s for s in picked if s]
    if not picked:
        raise AftercareError("Pick at least one sheet.")
    for s in picked:
        if not s["approved_at"]:
            raise AftercareError("A dentist has not approved that sheet yet, so it can't be given out.")
    for s in picked:
        db.execute("INSERT INTO aftercare_given(clinic_id, patient_id, sheet_id, given_by) VALUES (?,?,?,?)", (cid, pid, s["id"], user_id))
    audit.log(db, cid, user_id, "aftercare.give", "patient", pid, ", ".join(s["title"] for s in picked))
    return picked


def given(db, cid, pid):
    return db.execute("""SELECT g.*, s.title, u.display_name FROM aftercare_given g JOIN aftercare_sheets s ON s.id = g.sheet_id
                         LEFT JOIN users u ON u.id = g.given_by WHERE g.clinic_id = ? AND g.patient_id = ? ORDER BY g.id DESC""", (cid, pid)).fetchall()


def text_in(s, lang):
    """(title, body, in the language asked) for printing; English when the sheet has no French."""
    if lang == "fr" and s["body_fr"]:
        return s["title_fr"] or s["title"], s["body_fr"], True
    return s["title"], s["body"], lang == "en" or not lang
