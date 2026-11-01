"""Languages. The menus, the sign-in pages, the online booking page, a patient's private page and the printed documents a patient is
handed (invoice, receipt, statement, quote, consent) can be shown in French as well as English. Since 0.66 the patient side is also in
Spanish, Portuguese and Arabic (see STAFF_LANGS below). The daily staff screens (dashboard, diary, patients,
appointments, treatment, account, messages, recalls) are French too. The rest of the staff screens are in English for now.

How it works: the English wording in the templates is the key. If the language is French and the catalogue (i18n_fr.py) has the wording,
the French is shown. If it has not, the English is shown, so a missing line never breaks a page. A test makes sure every wording marked
for translation has a French line, that none of them has gone missing from the templates, and that the {fields} and %(names)s in the English
and French match.

Which language is used:
  - a signed-in member of staff: their own choice, else the clinic's language
  - a patient's private page and the documents made for that patient: the patient's language, else the clinic's language
  - the booking page and the sign-in page: the visitor's choice (?lang=fr), else the browser's language, else the clinic's language
"""
import re

from . import timeutil as T

LANGS = {"en": "English", "fr": "Français", "es": "Español", "pt": "Português", "ar": "العربية"}
DEFAULT = "en"
# 0.66: every screen is in English and French. Spanish, Portuguese and Arabic cover everything a patient or a visitor sees (booking,
# the private page, forms, paying, check-in, the pharmacy check, printed documents and messages); staff screens fall back to English.
STAFF_LANGS = ("en", "fr")
RTL = ("ar",)


def _catalogue(lang):
    if lang == "fr":
        from . import i18n_fr
        return i18n_fr.FR
    if lang in ("es", "pt", "ar"):
        return __import__(f"dental_os.services.i18n_{lang}", fromlist=["WORDS"]).WORDS
    return {}


def _dates(lang):
    if lang in ("es", "pt", "ar"):
        m = __import__(f"dental_os.services.i18n_{lang}", fromlist=["MONTHS"])
        return m.MONTHS, m.DAYS, m.DATE, m.DAY_DATE
    return None


def starters(lang):
    """The message wording in this language, by kind, or None for English."""
    if lang == "fr":
        from .i18n_fr import STARTERS_FR
        return STARTERS_FR
    if lang in ("es", "pt", "ar"):
        return __import__(f"dental_os.services.i18n_{lang}", fromlist=["STARTERS"]).STARTERS
    return None


def direction(lang):
    return "rtl" if lang in RTL else "ltr"


def staff_pick(code):
    """A language the staff screens are fully written in, or None."""
    code = pick(code)
    return code if code in STAFF_LANGS else None

MONTHS_FR = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre", "octobre", "novembre", "décembre"]
DAYS_FR = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]       # Monday first, as date.weekday() counts


def pick(code):
    """The code if we support it, otherwise None."""
    code = (code or "").strip().lower()
    return code if code in LANGS else None


def from_header(header):
    """The best supported language in an Accept-Language header such as 'fr-CH, fr;q=0.9, en;q=0.8'. None if none is supported."""
    found = []
    for i, part in enumerate((header or "").split(",")[:12]):
        bits = part.strip().split(";")
        tag = bits[0].strip().lower().split("-")[0]
        q = 1.0
        for extra in bits[1:]:
            m = re.fullmatch(r"\s*q\s*=\s*([0-9.]+)\s*", extra)
            if m:
                try:
                    q = float(m.group(1))
                except ValueError:
                    q = 0.0
        if pick(tag) and q > 0:
            found.append((-q, i, tag))
    return sorted(found)[0][2] if found else None


def _col(row, name):
    try:
        return row[name]
    except (KeyError, IndexError, TypeError):
        return None


def clinic_lang(clinic):
    return pick(_col(clinic, "language")) or DEFAULT if clinic else DEFAULT


def user_lang(user, clinic):
    """Staff see their own choice, else the clinic's language when the staff screens are written in it, else English."""
    return (staff_pick(_col(user, "language")) if user else None) or staff_pick(_col(clinic, "language") if clinic else None) or DEFAULT


def patient_lang(patient, clinic):
    return (pick(_col(patient, "language")) if patient else None) or clinic_lang(clinic)


def t(lang, text, **kw):
    """The wording in the given language, with %(name)s fields filled in. English if there is no French."""
    if lang and lang != DEFAULT:
        text = _catalogue(lang).get(text, text)
    return text % kw if kw else text


# ---------------------------------------------------------------------------------------------------------- dates and times
def _day(d):
    return "1er" if d.day == 1 else str(d.day)


def fmt_date(d, lang="en"):
    """24 October 2026 / 24 octobre 2026"""
    if lang == "fr":
        return f"{_day(d)} {MONTHS_FR[d.month - 1]} {d.year}"
    names = _dates(lang)
    if names:
        return names[2].format(d=d.day, m=names[0][d.month - 1], y=d.year)
    return T.fmt_date(d)


def fmt_day_date(d, lang="en"):
    """Tuesday 3 November / mardi 3 novembre"""
    if lang == "fr":
        return f"{DAYS_FR[d.weekday()]} {_day(d)} {MONTHS_FR[d.month - 1]}"
    names = _dates(lang)
    if names:
        return names[3].format(wd=names[1][d.weekday()], d=d.day, m=names[0][d.month - 1])
    return T.fmt_day_date(d)


def fmt_time(dt, lang="en"):
    """4 pm, 4:30 pm / 16 h, 16 h 30"""
    if lang == "fr":
        return f"{dt.hour} h" if dt.minute == 0 else f"{dt.hour} h {dt.minute:02d}"
    if lang in ("es", "pt", "ar"):
        return f"{dt.hour}:{dt.minute:02d}"                       # the 24-hour clock, as most of these countries write it
    return T.fmt_time(dt)
