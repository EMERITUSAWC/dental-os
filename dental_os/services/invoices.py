"""Numbered invoices for finished work, with a tax line set by the clinic. Prices, tax rate and tax number are copied onto the
invoice when it is issued, so changing settings later never changes an invoice that already exists. Invoices are never
deleted: a manager voids one with a reason, and the number stays used. The patient's balance still comes from the account
(work done, adjustments, payments), so an invoice is a document, not a second set of money."""
from datetime import timedelta

from . import audit, billing, serialize, taxes, teeth, webhooks
from .timeutil import now_local

TERMS_DAYS = 14


class InvoiceError(Exception):
    pass


def _today(clinic):
    return now_local(clinic["timezone"]).date()


def tax_split(total_or_subtotal_p, rate_bp, inclusive):
    """(subtotal, tax, total) in minor units, rounded half up on the whole invoice. Inclusive: the amount already has tax in it."""
    if rate_bp <= 0:
        return total_or_subtotal_p, 0, total_or_subtotal_p
    if inclusive:
        tax = (total_or_subtotal_p * rate_bp + (10000 + rate_bp) // 2) // (10000 + rate_bp)
        return total_or_subtotal_p - tax, tax, total_or_subtotal_p
    tax = (total_or_subtotal_p * rate_bp + 5000) // 10000
    return total_or_subtotal_p, tax, total_or_subtotal_p + tax


def save_tax_settings(db, clinic, user_id, name, rate_pct, inclusive, number, footer):
    """rate_pct as text, for example '15' or '12.5'."""
    name = (name or "").strip()[:20]
    try:
        rate = round(float((rate_pct or "0").strip().replace(",", ".")) * 100)
    except ValueError:
        raise InvoiceError("Enter the tax rate as a number, for example 15 or 12.5. Use 0 for no tax.")
    if not 0 <= rate <= 10000:
        raise InvoiceError("The tax rate must be from 0 to 100.")
    if rate and not name:
        raise InvoiceError("Name the tax, for example VAT.")
    db.execute("UPDATE clinics SET tax_name = ?, tax_rate_bp = ?, tax_inclusive = ?, tax_number = ?, invoice_footer = ? WHERE id = ?",
               (name or None, rate, 1 if inclusive else 0, (number or "").strip()[:40] or None, (footer or "").strip()[:300] or None,
                clinic["id"]))
    audit.log(db, clinic["id"], user_id, "settings.tax")


def uninvoiced(db, cid, pid):
    return db.execute(
        """SELECT pr.*, c.name AS code_name, c.tax_rate_id FROM procedures pr JOIN procedure_codes c ON c.id = pr.code_id
           WHERE pr.clinic_id = ? AND pr.patient_id = ? AND pr.status = 'done' AND pr.fee_p > 0 AND NOT EXISTS (
             SELECT 1 FROM invoice_lines l JOIN invoices i ON i.id = l.invoice_id
             WHERE l.procedure_id = pr.id AND i.status = 'issued') ORDER BY pr.done_on, pr.id""", (cid, pid)).fetchall()


def create(db, clinic, user_id, pid, procedure_ids, note=None):
    cid = clinic["id"]
    wanted = {int(x) for x in procedure_ids}
    if not wanted:
        raise InvoiceError("Tick the work to put on the invoice.")
    rows = [r for r in uninvoiced(db, cid, pid) if r["id"] in wanted]
    if len(rows) != len(wanted):
        raise InvoiceError("Some of that work is not available to invoice. It may already be on an invoice.")
    billing._begin(db)
    no = db.execute("SELECT COALESCE(MAX(invoice_no), 0) + 1 FROM invoices WHERE clinic_id = ?", (cid,)).fetchone()[0]
    inclusive = bool(clinic["tax_inclusive"])
    book = taxes.rate_book(db, clinic)
    groups = {}                                   # (name, kind, rate) -> the work taxed that way. Tax is worked out once per group.
    for r in rows:
        t = taxes.for_code(book, clinic, r["tax_rate_id"])
        groups.setdefault((t["name"], t["kind"], t["rate_bp"]), []).append(r)
    sub = tax = total = 0
    breakdown = []
    for (tname, tkind, trate), members in groups.items():
        g_sub, g_tax, g_total = tax_split(sum(m["fee_p"] for m in members), trate, inclusive)
        sub, tax, total = sub + g_sub, tax + g_tax, total + g_total
        if tkind != "none":
            breakdown.append((tname, tkind, trate, g_sub, g_tax, g_total))
    taxed = [b for b in breakdown if b[1] == "rate"]
    if len(breakdown) == 1 and taxed:
        head_name, head_rate = taxed[0][0], taxed[0][2]
    elif len(breakdown) > 1:
        head_name, head_rate = "Several rates", 0
    else:
        head_name, head_rate = None, 0
    today = _today(clinic)
    cur = db.execute(
        """INSERT INTO invoices(clinic_id, invoice_no, patient_id, issued_on, due_on, subtotal_p, tax_p, total_p, tax_name, tax_rate_bp,
                                tax_inclusive, tax_number, note, created_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (cid, no, pid, today.isoformat(), (today + timedelta(days=TERMS_DAYS)).isoformat(), sub, tax, total,
         head_name, head_rate, 1 if inclusive else 0, clinic["tax_number"], (note or "").strip()[:200] or None, user_id))
    for name, kind, rate, g_sub, g_tax, g_total in breakdown:
        db.execute("INSERT INTO invoice_tax(clinic_id, invoice_id, name, kind, rate_bp, net_p, tax_p, gross_p) VALUES (?,?,?,?,?,?,?,?)",
                   (cid, cur.lastrowid, name, kind, rate, g_sub, g_tax, g_total))
    for r in rows:
        label = r["code_name"] + (f", tooth {teeth.show(r['tooth'])}" if r["tooth"] else "")
        t = taxes.for_code(book, clinic, r["tax_rate_id"])
        db.execute("""INSERT INTO invoice_lines(clinic_id, invoice_id, procedure_id, description, service_date, amount_p, tax_name, tax_rate_bp)
                      VALUES (?,?,?,?,?,?,?,?)""",
                   (cid, cur.lastrowid, r["id"], label, r["done_on"], r["fee_p"], t["name"], t["rate_bp"]))
    audit.log(db, cid, user_id, "invoice.issue", "invoice", cur.lastrowid)
    webhooks.emit(db, cid, "invoice.issued", serialize.invoice(db.execute("SELECT * FROM invoices WHERE id = ?", (cur.lastrowid,)).fetchone()))
    return cur.lastrowid


def get(db, cid, invoice_id):
    return db.execute("""SELECT i.*, p.first_name, p.last_name, p.phone, p.address FROM invoices i JOIN patients p ON p.id = i.patient_id
                         WHERE i.id = ? AND i.clinic_id = ?""", (invoice_id, cid)).fetchone()


def lines(db, cid, invoice_id):
    return db.execute("SELECT * FROM invoice_lines WHERE invoice_id = ? AND clinic_id = ? ORDER BY service_date, id", (invoice_id, cid)).fetchall()


def tax_lines(db, cid, invoice_id):
    """The tax breakdown. Empty for invoices made before version 0.9, which show their single tax line instead."""
    return db.execute("SELECT * FROM invoice_tax WHERE invoice_id = ? AND clinic_id = ? ORDER BY id", (invoice_id, cid)).fetchall()


def for_patient(db, cid, pid):
    return db.execute("SELECT * FROM invoices WHERE clinic_id = ? AND patient_id = ? ORDER BY id DESC", (cid, pid)).fetchall()


def void(db, clinic, user_id, invoice_id, reason):
    cid = clinic["id"]
    inv = get(db, cid, invoice_id)
    if not inv:
        raise LookupError("invoice")
    if inv["status"] == "void":
        raise InvoiceError("That invoice is already void.")
    reason = (reason or "").strip()
    if len(reason) < 3:
        raise InvoiceError("Say why the invoice is being voided.")
    db.execute("UPDATE invoices SET status = 'void', void_reason = ? WHERE id = ?", (reason[:200], invoice_id))
    audit.log(db, cid, user_id, "invoice.void", "invoice", invoice_id, reason[:60])
