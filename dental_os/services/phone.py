import re

from .regions import COUNTRIES, country


def normalise_phone(raw, country_code=None):
    """Return an E.164 number such as +233504771418.

    A number that starts with + or 00 is taken as international and only checked for length.
    A number without one is read as a local number of `country_code` (the clinic's country).
    With no country given, local numbers are refused rather than guessed.
    """
    s = re.sub(r"[\s\-().]", "", raw or "")
    if not s:
        raise ValueError("Enter a phone number.")
    if s.startswith("00"):
        s = "+" + s[2:]
    if s.startswith("+"):
        digits = s[1:]
        if not digits.isdigit():
            raise ValueError("Phone numbers can only have digits, spaces and a leading +.")
        if not 8 <= len(digits) <= 15:
            raise ValueError("That phone number looks the wrong length.")
        # Calling codes never overlap, so at most one country in the table can match.
        # A known country with the wrong length is an error. An unknown country passes on length alone.
        for name, cc, _trunk, lens, _tz, _cur in COUNTRIES.values():
            if digits.startswith(cc):
                if any(len(digits) == len(cc) + n for n in lens):
                    return "+" + digits
                raise ValueError(f"That does not look like a valid {name} number.")
        return "+" + digits
    if not s.isdigit():
        raise ValueError("Phone numbers can only have digits and spaces.")
    row = country(country_code)
    if not row:
        raise ValueError("Start the number with the country code, for example +233 or +44.")
    name, cc, trunk, lens, _tz, _cur = row
    if s.startswith(cc) and any(len(s) == len(cc) + n for n in lens):
        return "+" + s
    if trunk and s.startswith(trunk) and any(len(s) - len(trunk) == n for n in lens):
        return "+" + cc + s[len(trunk):]
    if not (trunk and s.startswith(trunk)) and any(len(s) == n for n in lens):
        return "+" + cc + s          # leading zero missed off, or a country with no trunk prefix
    raise ValueError(f"That does not look like a valid {name} number. "
                     f"Check the digits, or start with the country code, for example +{cc}.")


def local_display(e164, country_code=None):
    """+233201234567 -> 020 123 4567 for a clinic in Ghana. Numbers from elsewhere are shown as stored."""
    row = country(country_code)
    if not e164 or not row:
        return e164 or ""
    _name, cc, trunk, lens, _tz, _cur = row
    if not e164.startswith("+" + cc):
        return e164
    national = e164[1 + len(cc):]
    if len(national) not in lens:
        return e164
    if country_code.upper() in ("US", "CA") and len(national) == 10:
        return f"({national[:3]}) {national[3:6]}-{national[6:]}"
    n = trunk + national
    return f"{n[:3]} {n[3:6]} {n[6:]}"


def wa_digits(e164):
    return (e164 or "").lstrip("+")
