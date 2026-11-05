"""Version 0.74: an optional fee on credit card payments at the desk. Off unless a manager turns it on.

The rules differ by country, and the clinic is responsible for following them, so the setting is strict:
  * The UK and the European Economic Area ban surcharges on consumer cards (UK: the Consumer Rights (Payment Surcharges)
    Regulations 2012, amended from 13 January 2018; EU: the second Payment Services Directive). The setting is refused there.
  * In the US, card network rules cap a credit card surcharge (Visa: 3%) and surcharging debit cards is not allowed; some states
    ban surcharges altogether. So the cap here is 3%, and the desk must confirm it is a credit card each time.
  * Elsewhere, including Ghana, check with the clinic's bank or card company first. The fee must be told to the patient before
    they pay.
The fee is recorded as its own payment (purpose 'card_fee'), so it never reduces the treatment balance and shows separately
in takings and on the receipt. No web framework here."""
from . import audit, billing

MAX_BP = 300                     # 3.00%
BANNED = {"GB", "AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR", "HU", "IE", "IT", "LV", "LT", "LU", "MT",
          "NL", "PL", "PT", "RO", "SK", "SI", "ES", "SE", "IS", "LI", "NO"}
PURPOSE = "card_fee"


class CardFeeError(ValueError):
    pass


def allowed(clinic):
    return (clinic["country"] or "").upper() not in BANNED


def parse_pct(raw):
    """'2.5' -> 250. Empty or 0 turns the fee off."""
    text = (raw or "").strip().replace("%", "")
    if not text:
        return 0
    try:
        bp = round(float(text) * 100)
    except ValueError as exc:
        raise CardFeeError("Type the fee as a percentage, for example 2.5, up to 3.") from exc
    if not 0 <= bp <= MAX_BP:
        raise CardFeeError("Type the fee as a percentage, for example 2.5, up to 3.")
    return bp


def set_fee(db, clinic, user_id, bp):
    if bp and not allowed(clinic):
        raise CardFeeError("Card fees on patients' cards are not allowed in this clinic's country.")
    db.execute("UPDATE clinics SET card_fee_bp = ? WHERE id = ?", (bp, clinic["id"]))
    audit.log(db, clinic["id"], user_id, "settings.card_fee", None, None, f"{bp / 100:.2f}%")


def fee_for(clinic, amount_p):
    bp = clinic["card_fee_bp"] or 0
    if not bp or not allowed(clinic) or amount_p <= 0:
        return 0
    return (amount_p * bp + 5000) // 10000                     # rounded to the nearest minor unit


def add_fee(db, clinic, user_id, patient_id, amount_p, receipt_no, reference):
    """Record the fee for a credit card payment as its own payment, with the same card reference. Returns its id, or None."""
    fee = fee_for(clinic, amount_p)
    if not fee:
        return None
    return billing.record_payment(db, clinic, user_id, patient_id, fee, "card", f"{reference} (fee)", f"Card fee for receipt {receipt_no}",
                                  purpose=PURPOSE)


def fee_with(db, cid, receipt_no):
    return db.execute("SELECT * FROM payments WHERE clinic_id = ? AND purpose = ? AND note = ? AND voided = 0",
                      (cid, PURPOSE, f"Card fee for receipt {receipt_no}")).fetchone()
