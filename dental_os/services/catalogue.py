"""Procedure packs and the price list as a spreadsheet.

A pack is a ready-made group of procedure codes for one kind of practice (children's dentistry, orthodontics and so on). Adding a
pack never overwrites anything: codes the clinic already has are left alone. New codes from a pack come in switched off with a price
of zero, so nothing shows up on the chart or an invoice until the manager has set a price and ticked "In use".

The codes are our own. They are not the American CDT codes (licensed) or the British NHS codes, so no licence is needed.

The price list can be downloaded as a CSV, edited in a spreadsheet and uploaded again. A file with any mistake in it changes
nothing, and the screen says which rows to fix."""
import csv
import io
import re

from . import audit, money, taxes, treatment
from .dataio import _safe_cell

MAX_ROWS = 1500
CODE_RE = re.compile(r"[A-Z0-9][A-Z0-9._/\-]{0,15}")

PACKS = {
    "core": ("Everyday dentistry", "Check-ups, cleaning, fillings, extractions, root canals, crowns, dentures, implants and X-rays.",
             None),                                  # the starter list in treatment.py
    "hygiene": ("Gum care and prevention", "Deep cleaning, gum checks, sealants and stain removal.", [
        ("SCALE-DEEP", "Deep cleaning, per quarter of the mouth", "Periodontics"),
        ("PERIO-ASSESS", "Gum health assessment", "Periodontics"),
        ("PERIO-MAINT", "Gum maintenance visit", "Periodontics"),
        ("AIRPOLISH", "Air-polish stain removal", "Preventive"),
        ("SEALANT", "Fissure sealant, per tooth", "Preventive"),
        ("DESENS", "Sensitivity treatment", "Preventive"),
        ("OHI", "Oral hygiene instruction", "Preventive"),
    ]),
    "children": ("Children's dentistry", "Child check-ups, baby teeth fillings and extractions, space maintainers.", [
        ("PAED-EXAM", "Child examination", "Children"),
        ("PAED-FLUOR", "Fluoride varnish, child", "Children"),
        ("PAED-FILL", "Filling, baby tooth", "Children"),
        ("PAED-EXT", "Extraction, baby tooth", "Children"),
        ("PAED-CROWN", "Stainless steel crown, baby tooth", "Children"),
        ("PULPOTOMY", "Pulpotomy, baby tooth", "Children"),
        ("SPACE-MAIN", "Space maintainer", "Children"),
    ]),
    "endo": ("Root canal extras", "Root canal by tooth type, retreatment, posts and emergency visits.", [
        ("RCT-ANT", "Root canal, front tooth", "Endodontics"),
        ("RCT-PREM", "Root canal, premolar", "Endodontics"),
        ("RCT-MOLAR", "Root canal, molar", "Endodontics"),
        ("RCT-RETREAT", "Root canal retreatment", "Endodontics"),
        ("PULP-CAP", "Pulp cap", "Endodontics"),
        ("POST-CORE", "Post and core", "Endodontics"),
        ("EMERGENCY", "Emergency pain visit", "Examination"),
    ]),
    "surgery": ("Oral surgery", "Wisdom teeth, impacted teeth, biopsies and small procedures.", [
        ("WISDOM", "Wisdom tooth removal", "Surgery"),
        ("WISDOM-IMP", "Impacted wisdom tooth removal", "Surgery"),
        ("BIOPSY", "Biopsy", "Surgery"),
        ("ABSCESS", "Abscess drainage", "Surgery"),
        ("FRENECTOMY", "Frenectomy", "Surgery"),
        ("APICOECT", "Root-end surgery", "Surgery"),
        ("SUTURE-OUT", "Stitches removal", "Surgery"),
    ]),
    "prosth": ("Crowns, bridges and dentures", "Crowns by material, inlays, denture repairs and relines.", [
        ("CROWN-PFM", "Crown, porcelain on metal", "Prosthetics"),
        ("CROWN-CER", "Crown, all ceramic", "Prosthetics"),
        ("CROWN-ZIRC", "Crown, zirconia", "Prosthetics"),
        ("CROWN-RECEM", "Crown, re-cement", "Prosthetics"),
        ("INLAY", "Inlay or onlay", "Prosthetics"),
        ("DENT-REPAIR", "Denture repair", "Prosthetics"),
        ("DENT-RELINE", "Denture reline", "Prosthetics"),
        ("DENT-IMMED", "Immediate denture", "Prosthetics"),
    ]),
    "implants": ("Implants", "Implant consultation, placement, abutment, crown and bone work.", [
        ("IMP-CONS", "Implant consultation", "Implants"),
        ("IMP-PLACE", "Implant placement", "Implants"),
        ("IMP-ABUT", "Implant abutment", "Implants"),
        ("IMP-CROWN", "Implant crown", "Implants"),
        ("IMP-MAINT", "Implant maintenance visit", "Implants"),
        ("BONE-GRAFT", "Bone graft", "Implants"),
        ("SINUS-LIFT", "Sinus lift", "Implants"),
    ]),
    "ortho": ("Braces and aligners", "Consultation, braces, aligners, adjustment visits and retainers.", [
        ("ORTHO-CONS", "Orthodontic consultation", "Orthodontics"),
        ("BRACES-METAL", "Braces, metal, full course", "Orthodontics"),
        ("BRACES-CER", "Braces, ceramic, full course", "Orthodontics"),
        ("ALIGNER", "Clear aligners, full course", "Orthodontics"),
        ("ORTHO-ADJ", "Braces adjustment", "Orthodontics"),
        ("RETAINER", "Retainer", "Orthodontics"),
        ("ORTHO-DEBOND", "Braces removal", "Orthodontics"),
    ]),
    "cosmetic": ("Cosmetic dentistry", "Whitening, bonding, veneers and smile design.", [
        ("WHITEN-HOME", "Whitening, home kit", "Cosmetic"),
        ("WHITEN-CHAIR", "Whitening, in the chair", "Cosmetic"),
        ("BONDING", "Cosmetic bonding, per tooth", "Cosmetic"),
        ("VENEER", "Veneer, per tooth", "Cosmetic"),
        ("GUM-CONTOUR", "Gum contouring", "Cosmetic"),
        ("SMILE-DESIGN", "Smile design consultation", "Cosmetic"),
    ]),
    "extras": ("Guards, sedation and paperwork", "Night guards, sports guards, sedation, letters and missed-visit fees.", [
        ("NIGHTGUARD", "Night guard", "Other"),
        ("MOUTHGUARD", "Sports mouth guard", "Other"),
        ("TMJ-CONS", "Jaw joint consultation", "Other"),
        ("SEDATION", "Sedation", "Other"),
        ("CERTIFICATE", "Dental certificate or sick note", "Other"),
        ("REPORT", "Report or letter", "Other"),
        ("MISSED-FEE", "Missed visit fee", "Other"),
    ]),
}


class CatalogueError(Exception):
    pass


def pack_codes(key):
    if key not in PACKS:
        raise LookupError("pack")
    codes = PACKS[key][2]
    return list(treatment.STARTER_CODES) if codes is None else list(codes)


def packs_status(db, clinic_id):
    have = {r["code"] for r in db.execute("SELECT code FROM procedure_codes WHERE clinic_id = ?", (clinic_id,))}
    out = []
    for key, (label, about, _codes) in PACKS.items():
        codes = pack_codes(key)
        present = sum(1 for c, _n, _g in codes if c in have)
        out.append({"key": key, "label": label, "about": about, "total": len(codes), "present": present,
                    "missing": len(codes) - present})
    return out


def add_pack(db, clinic_id, user_id, key):
    """Add the codes of a pack that the clinic does not have yet. Returns how many were added."""
    codes = pack_codes(key)
    added = 0
    for code, name, group in codes:
        cur = db.execute("INSERT OR IGNORE INTO procedure_codes(clinic_id, code, name, category, fee_p, active) VALUES (?,?,?,?,0,?)",
                         (clinic_id, code, name, group, 1 if key == "core" else 0))
        added += cur.rowcount
    audit.log(db, clinic_id, user_id, "catalogue.pack", detail=f"{key}: {added} added")
    return added


# ------------------------------------------------------------------------------------------------------------ price list
COLUMNS = ["code", "name", "group", "price", "in_use", "tax"]


def export_csv(db, clinic):
    rates = {r["id"]: r["name"] for r in taxes.list_rates(db, clinic["id"])}
    rows = db.execute("SELECT * FROM procedure_codes WHERE clinic_id = ? ORDER BY category, code", (clinic["id"],)).fetchall()
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(COLUMNS)
    for r in rows:
        writer.writerow([_safe_cell(r["code"]), _safe_cell(r["name"]), _safe_cell(r["category"]),
                         money.plain(r["fee_p"], clinic["currency"]), "yes" if r["active"] else "no",
                         _safe_cell(rates.get(r["tax_rate_id"], ""))])
    return out.getvalue()


def _unsafe(cell):
    """Undo the apostrophe that export puts in front of a cell a spreadsheet might run as a formula."""
    cell = (cell or "").strip()
    if len(cell) > 1 and cell[0] == "'" and cell[1] in "=@+-\t\r":
        return cell[1:]
    return cell


_HEADERS = {"code": "code", "name": "name", "procedure": "name", "group": "group", "category": "group",
            "price": "price", "fee": "price", "in_use": "in_use", "inuse": "in_use", "active": "in_use", "tax": "tax"}


def _read(text):
    text = (text or "").lstrip("﻿")
    if not text.strip():
        raise CatalogueError("The file is empty.")
    first = text.splitlines()[0]
    delimiter = max(",;\t", key=first.count)
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    try:
        head = next(reader)
    except StopIteration:
        raise CatalogueError("The file is empty.")
    names = [_HEADERS.get(re.sub(r"[^a-z]+", "_", h.strip().lower()).strip("_")) for h in head]
    if "code" not in names or "name" not in names:
        raise CatalogueError("The first row must name the columns, and it needs at least code and name. "
                             "Download the price list to see the layout.")
    rows = []
    for number, cells in enumerate(reader, start=2):
        if not any((c or "").strip() for c in cells):
            continue
        rows.append((number, {n: _unsafe(cells[i]) if i < len(cells) else "" for i, n in enumerate(names) if n}))
        if len(rows) > MAX_ROWS:
            raise CatalogueError(f"That file has more than {MAX_ROWS} rows. Split it into smaller files.")
    if not rows:
        raise CatalogueError("The file has no rows under the column names.")
    return rows


def _yes_no(text):
    t = (text or "").strip().lower()
    if t in ("yes", "y", "1", "true", "on"):
        return 1
    if t in ("no", "n", "0", "false", "off"):
        return 0
    return None


def import_csv(db, clinic, user_id, text, update_existing=False, apply=True):
    """Read a price list. Nothing is changed unless every row is good. Returns
    {'added': n, 'updated': n, 'unchanged': n, 'errors': [(row number, sentence)], 'applied': bool}."""
    rows = _read(text)
    cid = clinic["id"]
    rates = {r["name"].strip().lower(): r for r in taxes.list_rates(db, cid)}
    existing = {r["code"]: r for r in db.execute("SELECT * FROM procedure_codes WHERE clinic_id = ?", (cid,))}
    errors, plan, seen = [], [], set()
    for number, cells in rows:
        code = cells.get("code", "").upper()
        name = cells.get("name", "")
        group = cells.get("group", "")
        if not CODE_RE.fullmatch(code):
            errors.append((number, "The code must be 1 to 16 letters, numbers, dashes, dots or slashes, with no spaces."))
            continue
        if code in seen:
            errors.append((number, f"The code {code} appears twice in this file."))
            continue
        seen.add(code)
        old = existing.get(code)
        if not name and old:
            name = old["name"]
        if not name or len(name) > 80:
            errors.append((number, f"{code}: the name is missing or longer than 80 characters."))
            continue
        if len(group) > 40:
            errors.append((number, f"{code}: the group is longer than 40 characters."))
            continue
        price = cells.get("price", "")
        try:
            fee = money.parse_money(price, clinic["currency"], allow_zero=True) if price else (old["fee_p"] if old else 0)
        except ValueError as exc:
            errors.append((number, f"{code}: {exc}"))
            continue
        flag = cells.get("in_use", "")
        in_use = _yes_no(flag) if flag else (old["active"] if old else 0)
        if in_use is None:
            errors.append((number, f"{code}: write yes or no for in use."))
            continue
        tax_text = cells.get("tax", "")
        rate_id = None
        if tax_text and tax_text.lower() not in ("default", "clinic default"):
            rate = rates.get(tax_text.lower())
            if not rate:
                errors.append((number, f"{code}: there is no tax rate called {tax_text}. Add it in Settings first."))
                continue
            if not rate["active"] and not (old and old["tax_rate_id"] == rate["id"]):
                errors.append((number, f"{code}: the tax rate {tax_text} is switched off."))
                continue
            rate_id = rate["id"]
        elif old and not tax_text and "tax" not in cells:
            rate_id = old["tax_rate_id"]
        plan.append((number, code, name, group or (old["category"] if old else "General"), fee, in_use, rate_id, old))

    result = {"added": 0, "updated": 0, "unchanged": 0, "errors": errors, "applied": False}
    if errors:
        return result
    for _number, code, name, group, fee, in_use, rate_id, old in plan:
        if old is None:
            result["added"] += 1
        elif update_existing and (old["name"], old["category"], old["fee_p"], old["active"], old["tax_rate_id"]) != (name, group, fee, in_use, rate_id):
            result["updated"] += 1
        else:
            result["unchanged"] += 1
    if not apply:
        return result
    for _number, code, name, group, fee, in_use, rate_id, old in plan:
        if old is None:
            db.execute("INSERT INTO procedure_codes(clinic_id, code, name, category, fee_p, active, tax_rate_id) VALUES (?,?,?,?,?,?,?)",
                       (cid, code, name, group, fee, in_use, rate_id))
        elif update_existing:
            db.execute("UPDATE procedure_codes SET name = ?, category = ?, fee_p = ?, active = ?, tax_rate_id = ? WHERE id = ?",
                       (name, group, fee, in_use, rate_id, old["id"]))
    result["applied"] = True
    audit.log(db, cid, user_id, "catalogue.import", detail=f"{result['added']} added, {result['updated']} updated")
    return result
