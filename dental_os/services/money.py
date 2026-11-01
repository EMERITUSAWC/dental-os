"""Money in minor units (whole numbers): pesewas, kobo, cents, pence. For a currency with no minor unit
(CFA franc, Ugandan shilling) one unit is one whole shilling or franc. Every function takes the currency
code, because each clinic has its own."""
import re
from decimal import Decimal, InvalidOperation

from .regions import CURRENCIES, currency_digits

MAX_MINOR = 10 ** 12          # far above any real bill, so anything bigger is a typo


def _symbol(currency):
    return CURRENCIES[currency][0] if currency in CURRENCIES else f"{currency} "


def parse_money(text, currency, allow_zero=False):
    """'1,250.50', 'GH₵ 80' or '80' -> minor units. Raises ValueError with a plain message."""
    digits = currency_digits(currency)
    raw = (text or "").strip()
    for token in (currency, _symbol(currency).strip()):
        if token:
            raw = re.sub(re.escape(token), "", raw, flags=re.I)
    raw = re.sub(r"\s", "", raw)
    if not raw:
        raise ValueError("Enter an amount.")
    # "12,50" is read as 12.50. "1,250" and "1,250.50" use the comma as a thousands mark.
    if digits and re.fullmatch(r"\d+,\d{1,%d}" % digits, raw):
        raw = raw.replace(",", ".")
    raw = raw.replace(",", "")
    try:
        value = Decimal(raw)
    except InvalidOperation:
        raise ValueError("Use numbers only, for example 150 or 150.50." if digits else "Use whole numbers only.")
    if not value.is_finite():
        raise ValueError("Use numbers only, for example 150 or 150.50.")
    if value != value.quantize(Decimal(1).scaleb(-digits)):
        raise ValueError(f"Use at most {digits} decimal places." if digits else "Use whole numbers only.")
    minor = int(value * 10 ** digits)
    if minor < 0 or (minor == 0 and not allow_zero):
        raise ValueError("The amount must be more than zero.")
    if minor > MAX_MINOR:
        raise ValueError("That amount looks too large. Check for a typing slip.")
    return minor


def fmt(p, currency):
    p = int(p or 0)
    digits = currency_digits(currency)
    sign = "-" if p < 0 else ""
    whole, frac = divmod(abs(p), 10 ** digits)
    body = f"{whole:,}.{frac:0{digits}d}" if digits else f"{whole:,}"
    return f"{sign}{_symbol(currency)}{body}"


def plain(p, currency):
    """For form fields: 15050 -> '150.50'."""
    p = int(p or 0)
    digits = currency_digits(currency)
    if not digits:
        return str(p)
    return f"{p // 10 ** digits}.{p % 10 ** digits:0{digits}d}"
