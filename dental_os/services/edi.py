"""US electronic dental claims in the HIPAA X12 5010 formats (version 0.23).

- 837D (005010X224A2): claims and predeterminations, built from claims already made in the system.
- 835 (005010X221A1): the payer's remittance. Read, matched to our claims, shown for checking, then posted as one payer payment.
- 270 / 271 (005010X279A1): an eligibility question for one cover, and the payer's answer, saved as the cover check.
- 999: the clearinghouse's receipt for a file we sent: accepted or rejected.

How files travel: the clinic's clearinghouse gives a portal, or a direct link. This module makes and reads the files; it does not
care how they move. The screens offer download and upload, which works with any clearinghouse today. A direct link is a
`transport` added for the clearinghouse the clinic signs with.

What the clinic must bring: an NPI for the practice and for each dentist, the practice's tax ID, its taxonomy code, the payer IDs its
clearinghouse publishes, and the CDT code for each of its procedures. CDT codes are the American Dental Association's licensed code
set, so none are shipped here: the clinic types in the codes from its own CDT licence.

Nothing here touches the network. No web framework here."""
import datetime
import json
import re

from . import audit, insurance as INS, payerpay as PP, teeth
from .timeutil import now_local

CDT = re.compile(r"^D\d{4}$")
TAXONOMY = re.compile(r"^[0-9A-Z]{9}X$")
STATE = re.compile(r"^[A-Z]{2}$")
RELATION = {"self": "The patient", "spouse": "Spouse", "child": "Child", "other": "Someone else"}
REL_SBR = {"self": "18", "spouse": "01", "child": "19", "other": "G8"}      # X12 individual relationship codes
FILE_KINDS = {"837D": "Claims sent", "270": "Cover check sent", "835": "Remittance received", "271": "Cover check answer",
              "999": "Receipt from the clearinghouse"}
CAS_GROUPS = {"CO": "Contract: the clinic writes it off", "PR": "The patient owes it", "OA": "Other adjustment",
              "PI": "Payer adjustment", "CR": "Correction"}
CLP_STATUS = {"1": "Paid as main payer", "2": "Paid as second payer", "3": "Paid as third payer", "4": "Denied",
              "19": "Paid as main payer, sent on to the next payer", "20": "Paid as second payer, sent on", "22": "Reversal of an earlier payment",
              "23": "Not our claim"}


class EdiError(Exception):
    pass


# ------------------------------------------------------------------------------------------------------------- checks
def npi_ok(npi):
    """The NPI check digit (Luhn, with the 80840 prefix the US uses for health identifiers)."""
    s = str(npi or "")
    if not re.fullmatch(r"\d{10}", s):
        return False
    digits = [int(c) for c in "80840" + s[:9]]
    total = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2 == 0:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return (10 - total % 10) % 10 == int(s[9])


def clean(text, n=60):
    """X12 values: plain letters, digits and simple punctuation only, in capitals. Separators are taken out."""
    t = re.sub(r"[~*:^|\\\r\n\t]", " ", str(text or ""))
    t = t.encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", t).strip().upper()[:n]


def amount(p):
    """Minor units to an X12 amount: 15000 -> 150, 15050 -> 150.5, 15055 -> 150.55."""
    neg = p < 0
    p = abs(int(p))
    whole, cents = divmod(p, 100)
    s = str(whole) if not cents else f"{whole}.{cents:02d}".rstrip("0")
    return ("-" if neg else "") + s


def to_minor(text):
    t = (text or "0").strip()
    if not re.fullmatch(r"-?\d+(\.\d{1,2})?", t):
        raise EdiError("An amount in the file could not be read.")
    neg = t.startswith("-")
    whole, _, frac = t.lstrip("-").partition(".")
    v = int(whole) * 100 + int((frac + "00")[:2])
    return -v if neg else v


# ----------------------------------------------------------------------------------------------------------- settings
FIELDS = ("submitter_id", "submitter_name", "contact_name", "contact_phone", "receiver_id", "receiver_name", "billing_npi", "tax_id",
          "taxonomy", "billing_address", "billing_city", "billing_state", "billing_zip", "usage")


def settings(db, cid):
    row = db.execute("SELECT * FROM edi_settings WHERE clinic_id = ?", (cid,)).fetchone()
    return dict(row) if row else {k: "" for k in FIELDS} | {"usage": "T", "next_control": 1}


def save_settings(db, clinic, user_id, form):
    v = {k: (form.get(k) or "").strip() for k in FIELDS}
    v["contact_phone"] = re.sub(r"\D", "", v["contact_phone"])
    v["tax_id"] = re.sub(r"\D", "", v["tax_id"])
    v["billing_zip"] = re.sub(r"\D", "", v["billing_zip"])
    v["billing_state"] = v["billing_state"].upper()
    v["taxonomy"] = v["taxonomy"].upper()
    if v["usage"] not in ("T", "P"):
        raise EdiError("Pick test or live files.")
    for k in ("submitter_id", "receiver_id"):
        if v[k] and not re.fullmatch(r"[A-Za-z0-9]{2,15}", v[k]):
            raise EdiError("The submitter and receiver IDs are 2 to 15 letters and digits, as the clearinghouse gives them.")
    if v["billing_npi"] and not npi_ok(v["billing_npi"]):
        raise EdiError("That NPI is not right: it is 10 digits and the last one is a check digit.")
    if v["tax_id"] and len(v["tax_id"]) != 9:
        raise EdiError("The tax ID (EIN) is 9 digits.")
    if v["taxonomy"] and not TAXONOMY.match(v["taxonomy"]):
        raise EdiError("A taxonomy code is 10 characters ending in X, for example 1223G0001X.")
    if v["billing_state"] and not STATE.match(v["billing_state"]):
        raise EdiError("Use the two-letter state code, for example TX.")
    if v["billing_zip"] and len(v["billing_zip"]) != 9:
        raise EdiError("The practice's ZIP code needs all 9 digits (ZIP+4) on claims.")
    db.execute("INSERT INTO edi_settings(clinic_id) VALUES (?) ON CONFLICT(clinic_id) DO NOTHING", (clinic["id"],))
    db.execute("UPDATE edi_settings SET " + ", ".join(f"{k} = ?" for k in FIELDS) + " WHERE clinic_id = ?",
               [v[k] or None for k in FIELDS[:-1]] + [v["usage"], clinic["id"]])
    audit.log(db, clinic["id"], user_id, "edi.settings")


def save_codes(db, clinic, user_id, cdt_by_code_id, npi_by_provider, payer_ids):
    cid = clinic["id"]
    for code_id, cdt in cdt_by_code_id.items():
        cdt = (cdt or "").strip().upper()
        if cdt and not CDT.match(cdt):
            raise EdiError("A CDT code is D and four digits, for example D0120.")
        db.execute("UPDATE procedure_codes SET cdt_code = ? WHERE id = ? AND clinic_id = ?", (cdt or None, code_id, cid))
    for pv, npi in npi_by_provider.items():
        npi = (npi or "").strip()
        if npi and not npi_ok(npi):
            raise EdiError("One of the dentists' NPIs is not right: it is 10 digits and the last one is a check digit.")
        db.execute("UPDATE providers SET npi = ? WHERE id = ? AND clinic_id = ?", (npi or None, pv, cid))
    for payer, eid in payer_ids.items():
        eid = (eid or "").strip().upper()
        if eid and not re.fullmatch(r"[A-Z0-9]{2,15}", eid):
            raise EdiError("A payer ID is 2 to 15 letters and digits, as your clearinghouse lists it.")
        db.execute("UPDATE payers SET edi_payer_id = ? WHERE id = ? AND clinic_id = ?", (eid or None, payer, cid))
    audit.log(db, cid, user_id, "edi.codes")


COVER_FIELDS = ("relationship", "holder_first", "holder_last", "holder_dob", "holder_gender", "group_no", "holder_address",
                "holder_city", "holder_state", "holder_zip")


def save_cover_details(db, clinic, user_id, cover_id, form):
    c = INS.get_cover(db, clinic["id"], cover_id)
    if not c:
        raise EdiError("That cover was not found.")
    v = {k: (form.get(k) or "").strip() for k in COVER_FIELDS}
    if v["relationship"] not in RELATION:
        raise EdiError("Say who holds the policy.")
    v["holder_state"] = v["holder_state"].upper()
    v["holder_zip"] = re.sub(r"\D", "", v["holder_zip"])
    if v["holder_gender"] not in ("", "F", "M", "U"):
        raise EdiError("Pick the policy holder's sex as the payer records it.")
    if v["holder_dob"]:
        try:
            datetime.date.fromisoformat(v["holder_dob"])
        except ValueError:
            raise EdiError("Use a date like 1980-04-23.")
    if v["holder_state"] and not STATE.match(v["holder_state"]):
        raise EdiError("Use the two-letter state code, for example TX.")
    if v["holder_zip"] and len(v["holder_zip"]) not in (5, 9):
        raise EdiError("A ZIP code is 5 or 9 digits.")
    db.execute("UPDATE patient_cover SET " + ", ".join(f"{k} = ?" for k in COVER_FIELDS) + " WHERE id = ?",
               [v[k] or None for k in COVER_FIELDS] + [cover_id])
    audit.log(db, clinic["id"], user_id, "edi.cover", "patient", c["patient_id"])


# ----------------------------------------------------------------------------------------------------- building files
class Writer:
    def __init__(self):
        self.segs = []

    def add(self, *els):
        while len(els) > 1 and els[-1] in ("", None):
            els = els[:-1]                                  # no empty elements at the end of a segment
        self.segs.append("*".join("" if e is None else str(e) for e in els))


def _names(full):
    parts = clean(full, 80).split(" ")
    if len(parts) == 1:
        return parts[0], ""
    return parts[-1], " ".join(parts[:-1])


def _envelope(s, ctl, now, kind, version):
    isa = ["ISA", "00", " " * 10, "00", " " * 10, "ZZ", clean(s["submitter_id"]).ljust(15)[:15], "ZZ",
           clean(s["receiver_id"]).ljust(15)[:15], now.strftime("%y%m%d"), now.strftime("%H%M"), "^", "00501", f"{ctl:09d}", "1",
           s.get("usage") or "T", ":"]
    gs = ["GS", kind, clean(s["submitter_id"]), clean(s["receiver_id"]), now.strftime("%Y%m%d"), now.strftime("%H%M"), str(ctl), "X", version]
    return "*".join(isa), "*".join(gs)


def _wrap(s, ctl, now, kind, version, st_code, body):
    isa, gs = _envelope(s, ctl, now, kind, version)
    segs = [f"ST*{st_code}*0001*{version}"] + body.segs
    segs.append(f"SE*{len(segs) + 1}*0001")
    return "~\n".join([isa, gs] + segs + [f"GE*1*{ctl}", f"IEA*1*{ctl:09d}"]) + "~\n"


def _next_control(db, cid):
    s = settings(db, cid)
    ctl = int(s.get("next_control") or 1)
    db.execute("UPDATE edi_settings SET next_control = ? WHERE clinic_id = ?", (ctl + 1 if ctl < 999999999 else 1, cid))
    return ctl


def _setup_problems(s):
    out = []
    for k, label in (("submitter_id", "your submitter ID"), ("receiver_id", "the clearinghouse's receiver ID"),
                     ("billing_npi", "the practice NPI"), ("tax_id", "the tax ID"), ("billing_address", "the practice address"),
                     ("billing_city", "the city"), ("billing_state", "the state"), ("billing_zip", "the ZIP+4 code")):
        if not s.get(k):
            out.append("Electronic claims set-up is missing " + label + ".")
    return out


def claim_problems(db, clinic, claim_id):
    """Everything that would stop this claim going out, in plain words. Empty: ready."""
    cid = clinic["id"]
    c = INS.get_claim(db, cid, claim_id)
    if not c:
        return ["That claim was not found."]
    out = []
    if c["status"] not in ("draft", "submitted"):
        out.append("Only a draft or a sent claim can go in a claim file.")
    payer = db.execute("SELECT * FROM payers WHERE id = ?", (c["payer_id"],)).fetchone()
    if not payer["edi_payer_id"]:
        out.append("The payer has no payer ID for electronic claims.")
    cover = INS.get_cover(db, cid, c["cover_id"]) if c["cover_id"] else None
    if not cover or not cover["member_no"]:
        out.append("The cover has no member ID.")
    if cover and not cover["relationship"]:
        out.append("The cover's US claim details are not filled in (who holds the policy, their address).")
    if cover and cover["relationship"] and not (cover["holder_address"] and cover["holder_city"] and cover["holder_state"] and cover["holder_zip"]):
        out.append("The policy holder's address is incomplete.")
    if cover and cover["relationship"] and cover["relationship"] != "self" and not (cover["holder_last"] and cover["holder_first"]):
        out.append("The policy holder's name is missing.")
    p = db.execute("SELECT * FROM patients WHERE id = ?", (c["patient_id"],)).fetchone()
    if not p["dob"]:
        out.append("The patient's date of birth is missing.")
    for it in _items(db, cid, claim_id):
        if not it["cdt_code"]:
            out.append("Procedure %s has no CDT code." % it["code"])
        if c["kind"] == "claim" and not it["done_on"]:
            out.append("Procedure %s has no date it was done." % it["code"])
    return sorted(set(out))


def _items(db, cid, claim_id):
    return db.execute("""SELECT ci.*, pr.tooth, pr.surfaces, pr.done_on, pr.planned_on, pr.provider_id, pc.code, pc.cdt_code,
                         pv.name AS provider_name, pv.npi AS provider_npi
                         FROM claim_items ci JOIN procedures pr ON pr.id = ci.procedure_id JOIN procedure_codes pc ON pc.id = pr.code_id
                         LEFT JOIN providers pv ON pv.id = pr.provider_id WHERE ci.clinic_id = ? AND ci.claim_id = ? ORDER BY ci.id""",
                      (cid, claim_id)).fetchall()


def control_number(claim_no):
    return f"C{claim_no}"


def build_837(db, clinic, user_id, claim_ids, now=None):
    """Make one 837D file for these claims. Refuses if any is not ready, and says which and why. Returns the file id."""
    cid = clinic["id"]
    s = settings(db, cid)
    problems = _setup_problems(s)
    if not claim_ids:
        raise EdiError("Tick at least one claim.")
    for x in claim_ids:
        for msg in claim_problems(db, clinic, x):
            c = INS.get_claim(db, cid, x)
            problems.append(f"Claim {c['claim_no'] if c else x}: {msg}")
    if problems:
        raise EdiError(" ".join(problems))
    now = now or now_local(clinic["timezone"])
    ctl = _next_control(db, cid)
    w = Writer()
    w.add("BHT", "0019", "00", f"DOS{ctl}", now.strftime("%Y%m%d"), now.strftime("%H%M"), "CH")
    w.add("NM1", "41", "2", clean(s["submitter_name"] or clinic["name"]), "", "", "", "", "46", clean(s["submitter_id"]))
    w.add("PER", "IC", clean(s["contact_name"] or clinic["name"]), "TE", s["contact_phone"] or re.sub(r"\D", "", clinic["phone"] or "")[-10:])
    w.add("NM1", "40", "2", clean(s["receiver_name"] or s["receiver_id"]), "", "", "", "", "46", clean(s["receiver_id"]))
    w.add("HL", "1", "", "20", "1")
    if s.get("taxonomy"):
        w.add("PRV", "BI", "PXC", s["taxonomy"])
    w.add("NM1", "85", "2", clean(clinic["name"]), "", "", "", "", "XX", s["billing_npi"])
    w.add("N3", clean(s["billing_address"], 55))
    w.add("N4", clean(s["billing_city"], 30), s["billing_state"], s["billing_zip"])
    w.add("REF", "EI", s["tax_id"])
    hl = 1
    for x in claim_ids:
        c = INS.get_claim(db, cid, x)
        cover = INS.get_cover(db, cid, c["cover_id"])
        payer = db.execute("SELECT * FROM payers WHERE id = ?", (c["payer_id"],)).fetchone()
        p = db.execute("SELECT * FROM patients WHERE id = ?", (c["patient_id"],)).fetchone()
        self_ = cover["relationship"] == "self"
        sub_hl = hl + 1
        w.add("HL", str(sub_hl), "1", "22", "0" if self_ else "1")
        w.add("SBR", "P" if cover["cover_rank"] == 1 else "S", "18" if self_ else "", clean(cover["group_no"], 50), "", "", "", "", "", "CI")
        if self_:
            w.add("NM1", "IL", "1", clean(p["last_name"]), clean(p["first_name"]), "", "", "", "MI", clean(cover["member_no"], 80))
            w.add("N3", clean(cover["holder_address"], 55))
            w.add("N4", clean(cover["holder_city"], 30), cover["holder_state"], cover["holder_zip"])
            w.add("DMG", "D8", p["dob"].replace("-", ""), {"Female": "F", "Male": "M"}.get(p["gender"], "U"))
        else:
            w.add("NM1", "IL", "1", clean(cover["holder_last"]), clean(cover["holder_first"]), "", "", "", "MI", clean(cover["member_no"], 80))
        w.add("NM1", "PR", "2", clean(payer["name"]), "", "", "", "", "PI", payer["edi_payer_id"])
        hl = sub_hl
        if not self_:
            hl += 1
            w.add("HL", str(hl), str(sub_hl), "23", "0")
            w.add("PAT", REL_SBR[cover["relationship"]])
            w.add("NM1", "QC", "1", clean(p["last_name"]), clean(p["first_name"]))
            w.add("N3", clean(cover["holder_address"], 55))
            w.add("N4", clean(cover["holder_city"], 30), cover["holder_state"], cover["holder_zip"])
            w.add("DMG", "D8", p["dob"].replace("-", ""), {"Female": "F", "Male": "M"}.get(p["gender"], "U"))
        items = _items(db, cid, x)
        clm = ["CLM", control_number(c["claim_no"]), amount(sum(i["fee_p"] for i in items)), "", "", "11:B:1", "Y", "A", "Y", "Y"]
        if c["kind"] == "preauth":
            clm += [""] * 9 + ["PB"]                         # CLM19: a predetermination of benefits
        w.add(*clm)
        rendering = next((i for i in items if i["provider_npi"]), None)
        if rendering:
            last, first = _names(rendering["provider_name"])
            w.add("NM1", "82", "1", last, first, "", "", "", "XX", rendering["provider_npi"])
            if s.get("taxonomy"):
                w.add("PRV", "PE", "PXC", s["taxonomy"])
        for n, it in enumerate(items, 1):
            w.add("LX", str(n))
            w.add("SV3", "AD:" + it["cdt_code"], amount(it["fee_p"]), "", "", "", "1")
            if it["tooth"]:
                uni = teeth.FDI_TO_UNI.get(it["tooth"])
                if uni:
                    surf = ":".join(ch for ch in (it["surfaces"] or "") if ch in "MODBLI")
                    w.add("TOO", "JP", uni, surf)
            day = it["done_on"] if c["kind"] == "claim" else None
            if day:
                w.add("DTP", "472", "D8", day.replace("-", ""))
    body = _wrap(s, ctl, now, "HC", "005010X224A2", "837", w)
    fid = _save_file(db, clinic, user_id, "837D", "out", ctl, body, claim_ids)
    for x in claim_ids:
        c = INS.get_claim(db, cid, x)
        if c["status"] == "draft":
            INS.submit(db, clinic, user_id, x, now.date().isoformat(), None)
    audit.log(db, cid, user_id, "edi.837", "edi_file", fid, f"{len(claim_ids)} claims")
    return fid


def build_270(db, clinic, user_id, cover_id, now=None):
    cid = clinic["id"]
    s = settings(db, cid)
    cover = INS.get_cover(db, cid, cover_id)
    if not cover:
        raise EdiError("That cover was not found.")
    payer = db.execute("SELECT * FROM payers WHERE id = ?", (cover["payer_id"],)).fetchone()
    p = db.execute("SELECT * FROM patients WHERE id = ?", (cover["patient_id"],)).fetchone()
    problems = [m for m in _setup_problems(s) if "address" not in m and "city" not in m and "state" not in m and "ZIP" not in m]
    if not payer["edi_payer_id"]:
        problems.append("The payer has no payer ID for electronic claims.")
    if not cover["member_no"]:
        problems.append("The cover has no member ID.")
    if not p["dob"]:
        problems.append("The patient's date of birth is missing.")
    if problems:
        raise EdiError(" ".join(problems))
    now = now or now_local(clinic["timezone"])
    ctl = _next_control(db, cid)
    w = Writer()
    w.add("BHT", "0022", "13", f"DOS{ctl}", now.strftime("%Y%m%d"), now.strftime("%H%M"))
    w.add("HL", "1", "", "20", "1")
    w.add("NM1", "PR", "2", clean(payer["name"]), "", "", "", "", "PI", payer["edi_payer_id"])
    w.add("HL", "2", "1", "21", "1")
    w.add("NM1", "1P", "2", clean(clinic["name"]), "", "", "", "", "XX", s["billing_npi"])
    w.add("HL", "3", "2", "22", "0")
    w.add("TRN", "1", f"E{cover_id}-{ctl}", "1" + (s["tax_id"] or "000000000"))
    w.add("NM1", "IL", "1", clean(p["last_name"]), clean(p["first_name"]), "", "", "", "MI", clean(cover["member_no"], 80))
    w.add("DMG", "D8", p["dob"].replace("-", ""))
    w.add("DTP", "291", "D8", now.strftime("%Y%m%d"))
    w.add("EQ", "35")                                         # 35: dental care
    body = _wrap(s, ctl, now, "HS", "005010X279A1", "270", w)
    fid = _save_file(db, clinic, user_id, "270", "out", ctl, body, [], cover_id=cover_id)
    audit.log(db, cid, user_id, "edi.270", "patient", cover["patient_id"])
    return fid


def _save_file(db, clinic, user_id, kind, direction, ctl, body, claim_ids, cover_id=None, status="made", summary=None):
    return db.execute("""INSERT INTO edi_files(clinic_id, kind, direction, control_no, body, claim_ids, cover_id, status, summary, created_by, created_at)
                         VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                      (clinic["id"], kind, direction, ctl, body, json.dumps(list(claim_ids)), cover_id, status, summary, user_id,
                       now_local(clinic["timezone"]).strftime("%Y-%m-%d %H:%M"))).lastrowid


def get_file(db, cid, fid):
    return db.execute("SELECT * FROM edi_files WHERE id = ? AND clinic_id = ?", (fid, cid)).fetchone()


def files(db, cid, limit=100):
    return db.execute("""SELECT f.*, u.display_name AS by_name FROM edi_files f LEFT JOIN users u ON u.id = f.created_by
                         WHERE f.clinic_id = ? ORDER BY f.id DESC LIMIT ?""", (cid, limit)).fetchall()


def ready_claims(db, clinic):
    """Draft and sent claims whose payer takes electronic claims, each with what still stops it."""
    rows = db.execute("""SELECT c.*, pa.first_name, pa.last_name, py.name AS payer_name FROM claims c JOIN patients pa ON pa.id = c.patient_id
                         JOIN payers py ON py.id = c.payer_id WHERE c.clinic_id = ? AND c.status IN ('draft','submitted')
                         AND py.edi_payer_id IS NOT NULL ORDER BY c.claim_no""", (clinic["id"],)).fetchall()
    return [{"c": r, "problems": claim_problems(db, clinic, r["id"])} for r in rows]


# ------------------------------------------------------------------------------------------------------ reading files
def parse(text):
    """Split an X12 file into segments of elements. The separators are read from the ISA segment, as the standard says."""
    t = (text or "").lstrip("﻿ \r\n\t")
    if not t.startswith("ISA") or len(t) < 106:
        raise EdiError("That is not an X12 file. It should start with ISA.")
    el, comp, term = t[3], t[104], t[105]
    segs = []
    for raw in t.split(term):
        raw = raw.strip("\r\n ")
        if raw:
            segs.append(raw.split(el))
    return segs, comp


def kind_of(segs):
    st = next((s for s in segs if s[0] == "ST"), None)
    if not st:
        raise EdiError("The file has no ST segment.")
    return st[1]


def read_835(db, clinic, text):
    """What a remittance says, matched to our claims. Nothing is saved yet."""
    segs, comp = parse(text)
    if kind_of(segs) != "835":
        raise EdiError("That file is not a remittance (835).")
    out = {"total_p": 0, "method": "", "trace": "", "paid_on": None, "payer_name": "", "payer_id": None, "claims": [], "plb_p": 0}
    cur = None
    for s in segs:
        tag = s[0]
        g = lambda i: s[i] if len(s) > i else ""          # noqa: E731
        if tag == "BPR":
            out["total_p"], out["method"] = to_minor(g(2)), g(4)
            if g(16):
                out["paid_on"] = f"{g(16)[:4]}-{g(16)[4:6]}-{g(16)[6:8]}"
        elif tag == "TRN":
            out["trace"] = g(2)
        elif tag == "N1" and g(1) == "PR":
            out["payer_name"] = g(2)
        elif tag == "REF" and g(1) == "2U" and not cur:
            out["payer_edi"] = g(2)
        elif tag == "CLP":
            cur = {"ref": g(1), "status": g(2), "charge_p": to_minor(g(3)), "paid_p": to_minor(g(4)), "patient_p": to_minor(g(5) or "0"),
                   "payer_claim": g(7), "adjust": [], "claim": None, "problem": None}
            out["claims"].append(cur)
        elif tag == "CAS" and cur is not None:
            group = g(1)
            for i in range(2, len(s), 3):
                if s[i] and len(s) > i + 1:
                    cur["adjust"].append({"group": group, "reason": s[i], "amount_p": to_minor(s[i + 1])})
        elif tag == "PLB":
            for i in range(3, len(s), 2):
                if len(s) > i + 1 and s[i + 1]:
                    out["plb_p"] += to_minor(s[i + 1])
    payer = None
    if out.get("payer_edi"):
        payer = db.execute("SELECT * FROM payers WHERE clinic_id = ? AND edi_payer_id = ?", (clinic["id"], out["payer_edi"].upper())).fetchone()
    for x in out["claims"]:
        m = re.fullmatch(r"C(\d+)", x["ref"] or "")
        c = db.execute("SELECT * FROM claims WHERE clinic_id = ? AND claim_no = ?", (clinic["id"], int(m.group(1)))).fetchone() if m else None
        if not c:
            x["problem"] = "No claim in this clinic has this number."
            continue
        if payer and c["payer_id"] != payer["id"]:
            x["problem"] = "That claim is with a different payer."
            continue
        payer = payer or db.execute("SELECT * FROM payers WHERE id = ?", (c["payer_id"],)).fetchone()
        x["claim"] = c
        x["writeoff_p"] = sum(a["amount_p"] for a in x["adjust"] if a["group"] == "CO")
        x["patient_owes_p"] = sum(a["amount_p"] for a in x["adjust"] if a["group"] == "PR")
        if c["status"] not in ("submitted", "partly_paid"):
            x["problem"] = "That claim is not waiting for payment."
        elif x["status"] == "22":
            x["problem"] = "This is a reversal of an earlier payment. Deal with it by hand."
    out["payer_id"] = payer["id"] if payer else None
    out["payer"] = payer
    usable = [x for x in out["claims"] if x["claim"] and not x["problem"]]
    out["matched_p"] = sum(x["paid_p"] for x in usable)
    out["ok"] = bool(usable) and out["payer_id"] and out["matched_p"] == out["total_p"] and not out["plb_p"]
    return out


def post_835(db, clinic, user_id, text):
    """Post a remittance: one payer payment for the paid claims (contract amounts written off), and denied claims marked rejected."""
    r = read_835(db, clinic, text)
    if not r["payer_id"]:
        raise EdiError("None of the claims in the file could be matched, so the payer is not known.")
    if r["plb_p"]:
        raise EdiError("This remittance has adjustments at the practice level (PLB). Post it by hand on the payer payment screen.")
    usable = [x for x in r["claims"] if x["claim"] and not x["problem"]]
    if r["matched_p"] != r["total_p"]:
        raise EdiError("The claims that could be matched do not add up to the amount paid. Post it by hand on the payer payment screen.")
    if not usable:
        raise EdiError("Nothing in the file can be posted.")
    paid = [x for x in usable if x["status"] != "4" and (x["paid_p"] or x["writeoff_p"])]
    denied = [x for x in usable if x["status"] == "4" and not x["paid_p"]]
    pp_id = None
    if paid:
        try:
            today = now_local(clinic["timezone"]).date().isoformat()
            day = min(r["paid_on"] or today, today)              # an EFT can be dated a day or two ahead
            pp_id = PP.post(db, clinic, user_id, r["payer_id"], day, r["total_p"], r["trace"] or "835",
                            [{"claim_id": x["claim"]["id"], "amount_p": x["paid_p"], "writeoff_p": max(x["writeoff_p"], 0), "final": True}
                             for x in paid], note="From remittance file (835)")
        except PP.PayerPayError as exc:
            raise EdiError(str(exc))
    for x in denied:
        codes = ", ".join(f"{a['group']}-{a['reason']}" for a in x["adjust"]) or "no reason code"
        try:
            INS.reject(db, clinic, user_id, x["claim"]["id"], f"Denied by the payer ({codes}).")
        except INS.InsuranceError as exc:
            raise EdiError(str(exc))
    _save_file(db, clinic, user_id, "835", "in", None, text, [x["claim"]["id"] for x in usable], status="posted",
               summary=f"{len(paid)} paid, {len(denied)} denied")
    audit.log(db, clinic["id"], user_id, "edi.835", "payer_payment", pp_id)
    return {"payer_payment_id": pp_id, "paid": len(paid), "denied": len(denied)}


def read_271(text):
    """The payer's answer to a cover check, in plain words."""
    segs, comp = parse(text)
    if kind_of(segs) != "271":
        raise EdiError("That file is not a cover check answer (271).")
    lines, active, ref = [], None, None
    for s in segs:
        g = lambda i: s[i] if len(s) > i else ""          # noqa: E731
        if s[0] == "TRN":
            ref = g(2)
        elif s[0] == "AAA":
            lines.append("The payer could not answer (reason %s)." % g(3))
        elif s[0] == "EB":
            code = g(1)
            if code == "1":
                active = True
                lines.append("Cover is active.")
            elif code == "6":
                active = False
                lines.append("Cover is not active.")
            elif code == "C" and g(7):
                lines.append("Deductible: %s%s." % (g(7), " for this " + {"23": "year", "29": "remaining", "25": "contract"}.get(g(6), "period") if g(6) else ""))
            elif code == "A" and g(8):
                lines.append("The patient pays %s%% (co-insurance)." % round(float(g(8)) * 100))
            elif code == "F" and g(7):
                lines.append("Benefit limit: %s." % g(7))
            elif code == "G" and g(7):
                lines.append("Out of pocket limit: %s." % g(7))
    return {"active": active, "lines": lines, "ref": ref}


def post_271(db, clinic, user_id, text):
    r = read_271(text)
    m = re.fullmatch(r"E(\d+)-\d+", r["ref"] or "")
    cover = INS.get_cover(db, clinic["id"], int(m.group(1))) if m else None
    if not cover:
        raise EdiError("This answer does not match a cover check sent from this clinic.")
    note = "Electronic check: " + " ".join(r["lines"] or ["No details given."])
    try:
        INS.verify(db, clinic, user_id, cover["id"], note[:300])
    except INS.InsuranceError as exc:
        raise EdiError(str(exc))
    _save_file(db, clinic, user_id, "271", "in", None, text, [], cover_id=cover["id"], status="posted", summary=note[:300])
    return cover


def read_999(text):
    segs, comp = parse(text)
    if kind_of(segs) != "999":
        raise EdiError("That file is not a clearinghouse receipt (999).")
    ctl, status, errors = None, None, []
    for s in segs:
        if s[0] == "AK1" and len(s) > 2:
            ctl = int(s[2]) if s[2].isdigit() else None
        elif s[0] == "AK9" and len(s) > 1:
            status = s[1]
        elif s[0] in ("IK3", "IK4") and len(s) > 1:
            errors.append(" ".join(s[1:]))
    return {"control_no": ctl, "accepted": status in ("A", "E"), "status": status, "errors": errors}


def post_999(db, clinic, user_id, text):
    r = read_999(text)
    f = db.execute("SELECT * FROM edi_files WHERE clinic_id = ? AND direction = 'out' AND control_no = ? ORDER BY id DESC LIMIT 1",
                   (clinic["id"], r["control_no"])).fetchone() if r["control_no"] is not None else None
    if not f:
        raise EdiError("This receipt does not match a file sent from this clinic.")
    status = "accepted" if r["accepted"] else "rejected"
    db.execute("UPDATE edi_files SET status = ?, summary = ? WHERE id = ?",
               (status, ("Accepted by the clearinghouse." if r["accepted"] else "Rejected by the clearinghouse: " + "; ".join(r["errors"])[:250]), f["id"]))
    _save_file(db, clinic, user_id, "999", "in", None, text, [], status="posted", summary=status)
    audit.log(db, clinic["id"], user_id, "edi.999", "edi_file", f["id"], status)
    return f["id"], r["accepted"]
