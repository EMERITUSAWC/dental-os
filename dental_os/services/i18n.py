"""Languages. The menus, the sign-in pages, the online booking page, a patient's private page and the printed documents a patient is
handed (invoice, receipt, statement, quote, consent) can be shown in French as well as English. The daily staff screens (dashboard, diary, patients,
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

LANGS = {"en": "English", "fr": "Français"}
DEFAULT = "en"

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
    return (pick(_col(user, "language")) if user else None) or clinic_lang(clinic)


def patient_lang(patient, clinic):
    return (pick(_col(patient, "language")) if patient else None) or clinic_lang(clinic)


def t(lang, text, **kw):
    """The wording in the given language, with %(name)s fields filled in. English if there is no French."""
    if lang == "fr":
        from .i18n_fr import FR
        text = FR.get(text, text)
    return text % kw if kw else text


# ---------------------------------------------------------------------------------------------------------- dates and times
def _day(d):
    return "1er" if d.day == 1 else str(d.day)


def fmt_date(d, lang="en"):
    """24 October 2026 / 24 octobre 2026"""
    if lang == "fr":
        return f"{_day(d)} {MONTHS_FR[d.month - 1]} {d.year}"
    return T.fmt_date(d)


def fmt_day_date(d, lang="en"):
    """Tuesday 3 November / mardi 3 novembre"""
    if lang == "fr":
        return f"{DAYS_FR[d.weekday()]} {_day(d)} {MONTHS_FR[d.month - 1]}"
    return T.fmt_day_date(d)


def fmt_time(dt, lang="en"):
    """4 pm, 4:30 pm / 16 h, 16 h 30"""
    if lang == "fr":
        return f"{dt.hour} h" if dt.minute == 0 else f"{dt.hour} h {dt.minute:02d}"
    return T.fmt_time(dt)
