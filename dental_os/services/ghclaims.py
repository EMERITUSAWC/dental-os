"""Claims the way Ghana's insurers take them (version 0.52), useful anywhere a payer works the same way.

Diagnoses. Many payers, Ghana's private health insurance schemes and the NHIS among them, want a diagnosis on every line. Each
finished treatment can carry an ICD-10 code. The starter list is the dental block of the WHO's ICD-10 (5th edition), K00 to K14,
as published by NHS England's classification browser; a clinic can add other codes. A payer can be set to need a diagnosis on every
line, and then a claim without one is refused before it goes.

What a payer covers. A payer can keep a list of the treatments it pays for. Treatment not on the list is estimated at nothing, with
a note, everywhere estimates appear. The NHIS starter only creates the payer; the clinic ticks its own treatments against the NHIS
benefits list. The NHIA's site says the scheme covers, for oral health, pain relief (incision and drainage, tooth extraction and
temporary relief) and dental restoration (simple amalgam fillings and temporary dressing), and excludes dentures.

Monthly batches. Most of these payers take a month's claims at once, with a covering summary. A batch gathers one payer's claims
for one month, prints the summary and a claim form for each, gives a CSV for the payer's own spreadsheet, and marks them sent.

The NHIA's own electronic claims file (its e-claims XML standard) is not built: its published standard could not be read from here,
and guessing a government file format is how claims get rejected. It is listed under Waiting on an outside party. No web framework."""
import csv
import datetime
import io
import re

from . import audit

ICD10_DENTAL = {
    "K00": "Disorders of tooth development and eruption", "K01.0": "Embedded teeth", "K01.1": "Impacted teeth",
    "K02.0": "Caries limited to enamel", "K02.1": "Caries of dentine", "K02.2": "Caries of cementum", "K02.3": "Arrested dental caries",
    "K02.4": "Odontoclasia", "K02.5": "Caries with pulp exposure", "K02.8": "Other dental caries", "K02.9": "Dental caries, unspecified",
    "K03.0": "Excessive attrition of teeth", "K03.1": "Abrasion of teeth", "K03.2": "Erosion of teeth", "K03.3": "Pathological resorption of teeth",
    "K03.4": "Hypercementosis", "K03.5": "Ankylosis of teeth", "K03.6": "Deposits [accretions] on teeth",
    "K03.7": "Posteruptive colour changes of dental hard tissues", "K03.8": "Other specified diseases of hard tissues of teeth",
    "K03.9": "Disease of hard tissues of teeth, unspecified",
    "K04.0": "Pulpitis", "K04.1": "Necrosis of pulp", "K04.2": "Pulp degeneration", "K04.3": "Abnormal hard tissue formation in pulp",
    "K04.4": "Acute apical periodontitis of pulpal origin", "K04.5": "Chronic apical periodontitis", "K04.6": "Periapical abscess with sinus",
    "K04.7": "Periapical abscess without sinus", "K04.8": "Radicular cyst", "K04.9": "Other and unspecified diseases of pulp and periapical tissues",
    "K05.0": "Acute gingivitis", "K05.1": "Chronic gingivitis", "K05.2": "Acute periodontitis", "K05.3": "Chronic periodontitis",
    "K05.4": "Periodontosis", "K05.5": "Other periodontal diseases", "K05.6": "Periodontal disease, unspecified",
    "K06.0": "Gingival recession", "K06.1": "Gingival enlargement", "K06.2": "Gingival and edentulous alveolar ridge lesions associated with trauma",
    "K06.8": "Other specified disorders of gingiva and edentulous alveolar ridge", "K06.9": "Disorder of gingiva and edentulous alveolar ridge, unspecified",
    "K07.0": "Major anomalies of jaw size", "K07.1": "Anomalies of jaw-cranial base relationship", "K07.2": "Anomalies of dental arch relationship",
    "K07.3": "Anomalies of tooth position", "K07.4": "Malocclusion, unspecified", "K07.5": "Dentofacial functional abnormalities",
    "K07.6": "Temporomandibular joint disorders", "K07.8": "Other dentofacial anomalies", "K07.9": "Dentofacial anomaly, unspecified",
    "K08.0": "Exfoliation of teeth due to systemic causes", "K08.1": "Loss of teeth due to accident, extraction or local periodontal disease",
    "K08.2": "Atrophy of edentulous alveolar ridge", "K08.3": "Retained dental root", "K08.8": "Other specified disorders of teeth and supporting structures",
    "K08.9": "Disorder of teeth and supporting structures, unspecified",
    "K09": "Cysts of oral region, not elsewhere classified", "K10": "Other diseases of jaws", "K11": "Diseases of salivary glands",
    "K12.0": "Recurrent oral aphthae", "K12.1": "Other forms of stomatitis", "K12.2": "Cellulitis and abscess of mouth", "K12.3": "Oral mucositis (ulcerative)",
    "K13.0": "Diseases of lips", "K13.1": "Cheek and lip biting", "K13.2": "Leukoplakia and other disturbances of oral epithelium, including tongue",
    "K13.3": "Hairy leukoplakia", "K13.4": "Granuloma and granuloma-like lesions of oral mucosa", "K13.5": "Oral submucous fibrosis",
    "K13.6": "Irritative hyperplasia of oral mucosa", "K13.7": "Other and unspecified lesions of oral mucosa", "K14": "Diseases of tongue",
}
CODE = re.compile(r"^[A-Z]\d{2}(\.\d{1,2})?$")
BATCH_STATUS = {"draft": "Being put together", "sent": "Sent"}


class ClaimFormError(ValueError):
    pass


# ---------------------------------------------------------------------------------------------------------------- diagnoses
def diagnoses(db, cid):
    out = dict(ICD10_DENTAL)
    for r in db.execute("SELECT code, title FROM clinic_diagnoses WHERE clinic_id = ?", (cid,)):
        out[r["code"]] = r["title"]
    return dict(sorted(out.items()))


def add_diagnosis(db, clinic, user_id, code, title):
    code = (code or "").strip().upper()
    title = (title or "").strip()[:200]
    if not CODE.match(code):
        raise ClaimFormError("An ICD-10 code looks like K02.1: a letter, two digits, and maybe a point and one or two more.")
    if not title:
        raise ClaimFormError("Give the diagnosis its title.")
    if code in diagnoses(db, clinic["id"]):
        raise ClaimFormError("That code is already on the list.")
    db.execute("INSERT INTO clinic_diagnoses(clinic_id, code, title) VALUES (?,?,?)", (clinic["id"], code, title))
    audit.log(db, clinic["id"], user_id, "dx.add", None, None, code)


def set_dx(db, clinic, user_id, proc_id, code):
    cid = clinic["id"]
    proc = db.execute("SELECT * FROM procedures WHERE id = ? AND clinic_id = ?", (proc_id, cid)).fetchone()
    if not proc:
        raise ClaimFormError("That treatment was not found.")
    code = (code or "").strip().upper() or None
    if code and code not in diagnoses(db, cid):
        raise ClaimFormError("Pick a diagnosis from the list, or add the code to the list first.")
    db.execute("UPDATE procedures SET dx_code = ? WHERE id = ?", (code, proc_id))
    for item in db.execute("""SELECT ci.id FROM claim_items ci JOIN claims c ON c.id = ci.claim_id WHERE ci.procedure_id = ? AND c.status = 'draft'""", (proc_id,)):
        db.execute("UPDATE claim_items SET dx_code = ? WHERE id = ?", (code, item["id"]))
    audit.log(db, cid, user_id, "dx.set", "procedure", proc_id, code or "none")


# ------------------------------------------------------------------------------------------------------- what a payer covers
def covered(db, cid, payer_id):
    return {r["code_id"] for r in db.execute("SELECT code_id FROM payer_covered WHERE clinic_id = ? AND payer_id = ?", (cid, payer_id))}


def covers_code(db, cid, payer_id, code_id):
    """True unless the payer keeps a list of what it covers and this treatment is not on it."""
    if not db.execute("SELECT 1 FROM payer_covered WHERE clinic_id = ? AND payer_id = ? LIMIT 1", (cid, payer_id)).fetchone():
        return True
    return bool(db.execute("SELECT 1 FROM payer_covered WHERE clinic_id = ? AND payer_id = ? AND code_id = ?", (cid, payer_id, code_id)).fetchone())


def set_covered(db, clinic, user_id, payer_id, code_ids, needs_dx):
    cid = clinic["id"]
    if not db.execute("SELECT 1 FROM payers WHERE id = ? AND clinic_id = ?", (payer_id, cid)).fetchone():
        raise ClaimFormError("That payer was not found.")
    valid = {r["id"] for r in db.execute("SELECT id FROM procedure_codes WHERE clinic_id = ?", (cid,))}
    db.execute("DELETE FROM payer_covered WHERE clinic_id = ? AND payer_id = ?", (cid, payer_id))
    for c in sorted(set(code_ids) & valid):
        db.execute("INSERT INTO payer_covered(clinic_id, payer_id, code_id) VALUES (?,?,?)", (cid, payer_id, c))
    db.execute("UPDATE payers SET needs_dx = ? WHERE id = ?", (1 if needs_dx else 0, payer_id))
    audit.log(db, cid, user_id, "payer.covered", "payer", payer_id, str(len(set(code_ids) & valid)))


def add_nhis(db, clinic, user_id):
    cid = clinic["id"]
    row = db.execute("SELECT id FROM payers WHERE clinic_id = ? AND name = 'NHIS'", (cid,)).fetchone()
    if row:
        raise ClaimFormError("The NHIS is already on the payer list.")
    pid = db.execute("""INSERT INTO payers(clinic_id, name, kind, contracted, notes, needs_dx) VALUES (?, 'NHIS', 'government', 1, ?, 1)""",
                     (cid, "National Health Insurance Scheme. Tick only the treatments on the NHIS benefits list, and check them against the NHIA's current list.")).lastrowid
    audit.log(db, cid, user_id, "payer.nhis", "payer", pid)
    return pid


def missing_dx(db, cid, claim_id):
    """Lines of a claim with no diagnosis, when the payer needs one."""
    c = db.execute("SELECT c.*, p.needs_dx FROM claims c JOIN payers p ON p.id = c.payer_id WHERE c.id = ? AND c.clinic_id = ?", (claim_id, cid)).fetchone()
    if not c or not c["needs_dx"]:
        return 0
    return db.execute("SELECT COUNT(*) FROM claim_items WHERE claim_id = ? AND (dx_code IS NULL OR dx_code = '')", (claim_id,)).fetchone()[0]


# -------------------------------------------------------------------------------------------------------------- batches
def _month(text):
    if not re.fullmatch(r"\d{4}-\d{2}", text or ""):
        raise ClaimFormError("Pick a month.")
    return text


def ready_for_batch(db, cid, payer_id, month):
    """Draft claims for this payer for work in this month, not yet in a batch."""
    return db.execute("""SELECT c.*, p.first_name, p.last_name, pc.member_no FROM claims c JOIN patients p ON p.id = c.patient_id
                         LEFT JOIN patient_cover pc ON pc.id = c.cover_id
                         WHERE c.clinic_id = ? AND c.payer_id = ? AND c.kind = 'claim' AND c.status = 'draft' AND c.batch_id IS NULL
                         AND EXISTS (SELECT 1 FROM claim_items ci JOIN procedures pr ON pr.id = ci.procedure_id
                                     WHERE ci.claim_id = c.id AND substr(pr.done_on, 1, 7) = ?)
                         ORDER BY p.last_name, p.first_name, c.claim_no""", (cid, payer_id, _month(month))).fetchall()


def make_batch(db, clinic, user_id, payer_id, month):
    cid = clinic["id"]
    rows = ready_for_batch(db, cid, payer_id, month)
    if not rows:
        raise ClaimFormError("There are no draft claims for that payer and month.")
    short = [r["claim_no"] for r in rows if missing_dx(db, cid, r["id"])]
    if short:
        raise ClaimFormError("This payer needs a diagnosis on every line. Add the missing ones first.")
    bid = db.execute("INSERT INTO claim_batches(clinic_id, payer_id, month, status, created_by) VALUES (?,?,?, 'draft', ?)",
                     (cid, payer_id, month, user_id)).lastrowid
    for r in rows:
        db.execute("UPDATE claims SET batch_id = ? WHERE id = ?", (bid, r["id"]))
    audit.log(db, cid, user_id, "claims.batch", "payer", payer_id, f"{month}: {len(rows)}")
    return bid


def batch(db, cid, bid):
    return db.execute("""SELECT b.*, p.name AS payer_name FROM claim_batches b JOIN payers p ON p.id = b.payer_id
                         WHERE b.id = ? AND b.clinic_id = ?""", (bid, cid)).fetchone()


def batch_claims(db, cid, bid):
    return db.execute("""SELECT c.*, p.first_name, p.last_name, p.dob, pc.member_no FROM claims c JOIN patients p ON p.id = c.patient_id
                         LEFT JOIN patient_cover pc ON pc.id = c.cover_id WHERE c.clinic_id = ? AND c.batch_id = ?
                         ORDER BY p.last_name, p.first_name, c.claim_no""", (cid, bid)).fetchall()


def batches(db, cid):
    return db.execute("""SELECT b.*, p.name AS payer_name, (SELECT COUNT(*) FROM claims c WHERE c.batch_id = b.id) AS n,
                         (SELECT COALESCE(SUM(c.total_fee_p), 0) FROM claims c WHERE c.batch_id = b.id) AS total_p
                         FROM claim_batches b JOIN payers p ON p.id = b.payer_id WHERE b.clinic_id = ? ORDER BY b.id DESC LIMIT 50""", (cid,)).fetchall()


def mark_sent(db, clinic, user_id, bid, sent_on=None, ref=None):
    from . import insurance as INS
    cid = clinic["id"]
    b = batch(db, cid, bid)
    if not b or b["status"] != "draft":
        raise ClaimFormError("Only a batch being put together can be marked sent.")
    day = sent_on or datetime.date.today().isoformat()
    for c in batch_claims(db, cid, bid):
        if c["status"] == "draft":
            INS.submit(db, clinic, user_id, c["id"], day, ref)
    db.execute("UPDATE claim_batches SET status = 'sent', sent_on = ?, payer_ref = ? WHERE id = ?", (day, (ref or "").strip()[:120] or None, bid))
    audit.log(db, cid, user_id, "claims.batch_sent", "payer", b["payer_id"], b["month"])


def remove_from_batch(db, clinic, user_id, bid, claim_id):
    b = batch(db, clinic["id"], bid)
    if not b or b["status"] != "draft":
        raise ClaimFormError("Only a batch being put together can be changed.")
    n = db.execute("UPDATE claims SET batch_id = NULL WHERE id = ? AND batch_id = ? AND clinic_id = ?", (claim_id, bid, clinic["id"])).rowcount
    if not n:
        raise ClaimFormError("That claim is not in this batch.")


def batch_csv(db, clinic, bid):
    """One line per treatment, for the payer's own spreadsheet. Money as plain numbers in the clinic's currency."""
    from .money import plain
    from .dataio import _safe_cell
    cid = clinic["id"]
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["Claim number", "Member number", "Patient", "Date of birth", "Date of treatment", "Diagnosis (ICD-10)", "Treatment code",
                "Treatment", "Tooth", "Fee", "Expected from payer"])
    for c in batch_claims(db, cid, bid):
        for it in db.execute("""SELECT ci.*, pr.done_on, pr.tooth, pc.code, pc.name FROM claim_items ci JOIN procedures pr ON pr.id = ci.procedure_id
                                JOIN procedure_codes pc ON pc.id = pr.code_id WHERE ci.claim_id = ? ORDER BY pr.done_on, ci.id""", (c["id"],)):
            w.writerow([_safe_cell(v) for v in (c["claim_no"], c["member_no"] or "", f"{c['first_name']} {c['last_name']}", c["dob"] or "",
                        (it["done_on"] or "")[:10], it["dx_code"] or "", it["code"], it["name"], it["tooth"] or "",
                        plain(it["fee_p"], clinic["currency"]), plain(it["estimate_p"], clinic["currency"]))])
    return out.getvalue()
