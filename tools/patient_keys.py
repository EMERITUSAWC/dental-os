"""Lists every wording a patient or a member of the public can see: the public and printed pages, and the messages raised by
the code behind them. Each language beyond English and French translates at least these. Used by tests.test_v66."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "dental_os"
PAGES = ("aftercare_print", "cforms_done", "cforms_fill", "consent_show", "implants_card", "invoice", "letter_print", "pay_public",
         "plan_quote", "receipt", "referral", "rx_print", "statement", "statements_print", "login", "login_code", "portal",
         "portal_health", "public_book", "public_book_done", "rx_check", "base", "error", "error_500", "_letterhead", "kiosk")
CODE = ("booking.py", "kiosk.py", "formbuilder.py", "onlinepay.py", "rxlink.py", "links.py")
# Wording put into what patients see by the page and message code itself (public.py, outbox.py, mailer.py).
EXTRA = ("Thank you. The clinic has your agreement and will contact you to book.", "Thank you. The clinic will check your answers.",
         "Thank you. We have your answer.", "our team", "Sent by %(clinic)s. To stop these emails, reply and tell us.")


def page_keys():
    keys = set()
    for name in PAGES:
        s = (ROOT / "templates" / f"{name}.html").read_text()
        keys |= set(re.findall(r'\b_d?\(\s*"((?:[^"\\]|\\.)*)"', s)) | set(re.findall(r"\b_d?\(\s*'((?:[^'\\]|\\.)*)'", s))
    return keys


def code_keys(known):
    """Whole sentences in the code behind the public pages that already have French, so they are shown to people."""
    keys = set()
    for name in CODE:
        s = (ROOT / "services" / name).read_text()
        keys |= {k for k in re.findall(r'"((?:[^"\\]|\\.){6,})"', s) if k in known}
    return keys


def all_keys():
    from dental_os.services.i18n_fr import FR
    return sorted((page_keys() | code_keys(FR) | set(EXTRA)) & set(FR))


if __name__ == "__main__":
    import json
    print(json.dumps(all_keys(), ensure_ascii=False, indent=0))
