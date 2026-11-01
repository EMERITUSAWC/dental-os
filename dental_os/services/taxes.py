"""Tax rates beyond the clinic's one default rate.

A clinic can add named rates (for example "Reduced 5%", "Zero rated", "Exempt") and give each procedure code one of them. A code
with no rate of its own uses the clinic's default (clinics.tax_*). Nothing here says what tax applies in any country: that is for
the clinic's accountant. Rates are never deleted, only switched off, because old codes and invoices point at them."""
from . import audit

KINDS = {"rate": "Taxed at this rate", "zero": "Zero rated", "exempt": "Exempt"}


class TaxError(Exception):
    pass


def parse_rate(text):
    try:
        bp = round(float((text or "0").strip().replace(",", ".")) * 100)
    except ValueError:
        raise TaxError("Enter the rate as a number, for example 5 or 12.5.")
    if not 0 <= bp <= 10000:
        raise TaxError("The rate must be from 0 to 100.")
    return bp


def list_rates(db, cid, active_only=False):
    sql = "SELECT * FROM tax_rates WHERE clinic_id = ?" + (" AND active = 1" if active_only else "") + " ORDER BY name"
    return db.execute(sql, (cid,)).fetchall()


def add_rate(db, clinic, user_id, name, kind, rate_text):
    name = (name or "").strip()[:30]
    if not name:
        raise TaxError("Name the rate, for example Reduced or Exempt.")
    if kind not in KINDS:
        raise TaxError("Pick what kind of rate this is.")
    bp = parse_rate(rate_text) if kind == "rate" else 0
    if kind == "rate" and bp == 0:
        raise TaxError("A taxed rate needs a rate above 0. For no tax, pick Zero rated or Exempt.")
    try:
        cur = db.execute("INSERT INTO tax_rates(clinic_id, name, kind, rate_bp) VALUES (?,?,?,?)", (clinic["id"], name, kind, bp))
    except Exception as exc:
        if "UNIQUE" in str(exc):
            raise TaxError("You already have a rate with that name.")
        raise
    audit.log(db, clinic["id"], user_id, "tax.rate.add", "tax_rate", cur.lastrowid, name)
    return cur.lastrowid


def toggle_rate(db, cid, user_id, rate_id):
    row = db.execute("SELECT * FROM tax_rates WHERE id = ? AND clinic_id = ?", (rate_id, cid)).fetchone()
    if not row:
        raise LookupError("tax rate")
    db.execute("UPDATE tax_rates SET active = 1 - active WHERE id = ?", (rate_id,))
    audit.log(db, cid, user_id, "tax.rate.toggle", "tax_rate", rate_id)


def set_code_rate(db, cid, user_id, code_id, rate_id):
    """rate_id None or 0 puts the code back on the clinic default."""
    code = db.execute("SELECT id, tax_rate_id FROM procedure_codes WHERE id = ? AND clinic_id = ?", (code_id, cid)).fetchone()
    if not code:
        raise LookupError("procedure code")
    if (rate_id or None) == code["tax_rate_id"]:
        return                                    # unchanged, even if that rate has since been switched off
    if rate_id:
        if not db.execute("SELECT 1 FROM tax_rates WHERE id = ? AND clinic_id = ? AND active = 1", (rate_id, cid)).fetchone():
            raise TaxError("That tax rate is not available.")
    db.execute("UPDATE procedure_codes SET tax_rate_id = ? WHERE id = ?", (rate_id or None, code_id))
    audit.log(db, cid, user_id, "tax.code.set", "procedure_code", code_id, str(rate_id or "default"))


def rate_book(db, clinic):
    """{rate id: dict(name, kind, rate_bp)} for every rate the clinic has ever made, switched off or not."""
    return {r["id"]: {"name": r["name"], "kind": r["kind"], "rate_bp": r["rate_bp"]} for r in list_rates(db, clinic["id"])}


def for_code(book, clinic, tax_rate_id):
    """The tax that applies to a procedure code: its own rate, or the clinic's default."""
    if tax_rate_id and tax_rate_id in book:
        return book[tax_rate_id]
    rate = clinic["tax_rate_bp"] or 0
    if rate > 0:
        return {"name": clinic["tax_name"] or "Tax", "kind": "rate", "rate_bp": rate}
    return {"name": None, "kind": "none", "rate_bp": 0}
