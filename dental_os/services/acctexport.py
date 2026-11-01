"""Exports for the clinic's accountant. A plain journal of money in and out (payments, voids, adjustments), and the issued invoices
laid out like Xero's sales invoice import, which most accounting packages can read. Amounts are in the clinic's currency with two
decimal places. Check the invoice file against your own accounting package's template before the first import: account codes and
tax types are set by the clinic, not guessed, and the unit amounts are the invoice's own line prices, so tell the package whether
those prices include tax (they do when the clinic's tax setting says prices include tax)."""
from . import billing


def _amt(minor):
    return "%.2f" % (minor / 100)


def _date(iso, country):
    y, m, d = iso[:10].split("-")
    return f"{m}/{d}/{y}" if country == "US" else f"{d}/{m}/{y}"


def journal(db, clinic, start, end):
    cid, country = clinic["id"], clinic["country"]
    out = [["Date", "Type", "Number", "Patient", "Method", "Amount", "Reference", "Note"]]
    for r in db.execute("""SELECT p.*, pa.first_name, pa.last_name FROM payments p JOIN patients pa ON pa.id = p.patient_id
                           WHERE p.clinic_id = ? AND p.received_on BETWEEN ? AND ? ORDER BY p.received_on, p.receipt_no""", (cid, start, end)):
        name = f"{r['first_name']} {r['last_name']}"
        out.append([_date(r["received_on"], country), "Payment", "R-%06d" % r["receipt_no"], name, billing.METHODS.get(r["method"], r["method"]),
                    _amt(r["amount_p"]), r["reference"] or "", r["purpose"] if r["purpose"] != "treatment" else ""])
        if r["voided"]:
            out.append([_date(r["received_on"], country), "Payment voided", "R-%06d" % r["receipt_no"], name,
                        billing.METHODS.get(r["method"], r["method"]), _amt(-r["amount_p"]), r["reference"] or "", r["void_reason"] or ""])
    for r in db.execute("""SELECT a.*, pa.first_name, pa.last_name FROM adjustments a JOIN patients pa ON pa.id = a.patient_id
                           WHERE a.clinic_id = ? AND a.adjusted_on BETWEEN ? AND ? ORDER BY a.adjusted_on, a.id""", (cid, start, end)):
        out.append([_date(r["adjusted_on"], country), "Adjustment", "A-%d" % r["id"], f"{r['first_name']} {r['last_name']}", "",
                    _amt(r["amount_p"]), r["reason"], r["note"] or ""])
    for r in db.execute("""SELECT f.*, pa.first_name, pa.last_name FROM refunds f JOIN patients pa ON pa.id = f.patient_id
                           WHERE f.clinic_id = ? AND f.refunded_on BETWEEN ? AND ? ORDER BY f.refunded_on, f.refund_no""", (cid, start, end)):
        name = f"{r['first_name']} {r['last_name']}"
        out.append([_date(r["refunded_on"], country), "Refund", "F-%06d" % r["refund_no"], name, billing.METHODS.get(r["method"], r["method"]),
                    _amt(-r["amount_p"]), r["reference"] or "", r["reason"]])
        if r["voided"]:
            out.append([_date(r["refunded_on"], country), "Refund voided", "F-%06d" % r["refund_no"], name, billing.METHODS.get(r["method"], r["method"]),
                        _amt(r["amount_p"]), r["reference"] or "", r["void_reason"] or ""])
    return out


def xero_invoices(db, clinic, start, end, account_code, tax_type):
    cid, country = clinic["id"], clinic["country"]
    out = [["ContactName", "EmailAddress", "InvoiceNumber", "Reference", "InvoiceDate", "DueDate", "Description", "Quantity",
            "UnitAmount", "AccountCode", "TaxType", "Currency"]]
    for inv in db.execute("""SELECT i.*, pa.first_name, pa.last_name, pa.email FROM invoices i JOIN patients pa ON pa.id = i.patient_id
                             WHERE i.clinic_id = ? AND i.status = 'issued' AND i.issued_on BETWEEN ? AND ? ORDER BY i.invoice_no""",
                          (cid, start, end)):
        for ln in db.execute("SELECT * FROM invoice_lines WHERE invoice_id = ? ORDER BY id", (inv["id"],)):
            out.append([f"{inv['first_name']} {inv['last_name']}", inv["email"] or "", str(inv["invoice_no"]), "",
                        _date(inv["issued_on"], country), _date(inv["due_on"], country), ln["description"], "1",
                        _amt(ln["amount_p"]), account_code or "", tax_type or "", clinic["currency"]])
    return out
