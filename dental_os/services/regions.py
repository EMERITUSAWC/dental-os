"""Country and currency facts. Nothing in the product is tied to one country: a clinic picks its own.

Each clinic stores its own country, currency and timezone. The table below only gives sensible
starting values when a clinic is created. All of it can be changed in Settings.

Phone rules are deliberately simple (calling code, trunk prefix, length of the national number).
Before launch in a market, replace them with a full phone-number library.
"""

# code: (name, calling code, trunk prefix, allowed national-number lengths, default timezone, currency)
COUNTRIES = {
    "GH": ("Ghana", "233", "0", (9,), "Africa/Accra", "GHS"),
    "NG": ("Nigeria", "234", "0", (10,), "Africa/Lagos", "NGN"),
    "KE": ("Kenya", "254", "0", (9,), "Africa/Nairobi", "KES"),
    "UG": ("Uganda", "256", "0", (9,), "Africa/Kampala", "UGX"),
    "TZ": ("Tanzania", "255", "0", (9,), "Africa/Dar_es_Salaam", "TZS"),
    "ZA": ("South Africa", "27", "0", (9,), "Africa/Johannesburg", "ZAR"),
    "SN": ("Senegal", "221", "", (9,), "Africa/Dakar", "XOF"),
    "CI": ("Côte d'Ivoire", "225", "", (10,), "Africa/Abidjan", "XOF"),
    "GB": ("United Kingdom", "44", "0", (9, 10), "Europe/London", "GBP"),
    "IE": ("Ireland", "353", "0", (8, 9), "Europe/Dublin", "EUR"),
    "DE": ("Germany", "49", "0", (10, 11), "Europe/Berlin", "EUR"),
    "FR": ("France", "33", "0", (9,), "Europe/Paris", "EUR"),
    "US": ("United States", "1", "", (10,), "America/New_York", "USD"),
    "CA": ("Canada", "1", "", (10,), "America/Toronto", "CAD"),
}

# code: (symbol put before the amount, digits after the decimal point)
CURRENCIES = {
    "GHS": ("GH₵ ", 2), "NGN": ("₦", 2), "KES": ("KSh ", 2), "UGX": ("USh ", 0), "TZS": ("TSh ", 2),
    "ZAR": ("R ", 2), "XOF": ("CFA ", 0), "GBP": ("£", 2), "EUR": ("€", 2), "USD": ("$", 2), "CAD": ("CA$", 2),
}

# Ways of paying a clinic might switch on. Mobile money is common in Africa and rare elsewhere.
DEFAULT_METHODS = {
    "GH": "cash,mobile_money,card,bank_transfer,insurance",
    "NG": "cash,card,bank_transfer,insurance",
    "KE": "cash,mobile_money,card,bank_transfer,insurance",
    "UG": "cash,mobile_money,card,bank_transfer,insurance",
    "TZ": "cash,mobile_money,card,bank_transfer,insurance",
    "SN": "cash,mobile_money,card,bank_transfer,insurance",
    "CI": "cash,mobile_money,card,bank_transfer,insurance",
}
FALLBACK_METHODS = "cash,card,bank_transfer,insurance"


def country(code):
    return COUNTRIES.get((code or "").upper())


def defaults_for(code):
    """Starting settings for a new clinic in this country. Raises ValueError for an unknown code."""
    row = country(code)
    if not row:
        raise ValueError(f"Unknown country code {code!r}. Known: {', '.join(sorted(COUNTRIES))}.")
    name, _cc, _trunk, _lens, tz, cur = row
    code = code.upper()
    return {"country": code, "country_name": name, "timezone": tz, "currency": cur,
            "payment_methods": DEFAULT_METHODS.get(code, FALLBACK_METHODS)}


def currency_digits(code):
    return CURRENCIES[code][1] if code in CURRENCIES else 2
