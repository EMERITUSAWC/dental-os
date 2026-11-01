"""Rules for choosing a password. The aim is to stop the passwords people really guess, not to make long rules to remember.
A long phrase of ordinary words is fine: length counts for more than symbols."""
import re

MIN_LENGTH = 10
MAX_LENGTH = 128

# Letters only, lower case. A password is turned into letters only before it is compared, so "Password123!" is caught as "password".
_COMMON = set("""
password passw0rd passwort letmein welcome qwerty qwertyuiop qwertyuiopasdf asdfghjkl zxcvbnm iloveyou monkey dragon master
shadow sunshine princess football baseball basketball superman batman trustno login admin administrator administrador root
user guest test testing changeme change secret default hello hellohello freedom whatever starwars pokemon
dental dentist dentistry clinic clinics smile teeth tooth doctor nurse reception frontdesk manager staff patient patients
ghana accra osu kumasi nigeria lagos kenya nairobi london america africa
dentalos dentalclinic dentalcentre dentalcenter mypassword newpassword temppassword temporary
abcdef abcdefg abcdefgh abcdefghi abcdefghij
""".split())

_RUNS = ("01234567890123456789", "98765432109876543210", "abcdefghijklmnopqrstuvwxyz", "zyxwvutsrqponmlkjihgfedcba",
         "qwertyuiopasdfghjklzxcvbnm", "1q2w3e4r5t6y7u8i9o0p")


def check(password, username="", display_name=""):
    """Returns None if the password is acceptable, otherwise a short sentence saying what to change."""
    if len(password) < MIN_LENGTH:
        return f"The password needs at least {MIN_LENGTH} characters."
    if len(password) > MAX_LENGTH:
        return f"Use {MAX_LENGTH} characters or fewer."
    low = password.lower()
    letters = re.sub(r"[^a-z]", "", low)
    if letters in _COMMON:
        return "That password is one of the first ones anybody tries. Use a few ordinary words together, for example three or four unrelated words."
    if len(set(low)) <= 2:
        return "That password repeats the same few characters. Use a few ordinary words together."
    if len(low) <= 16 and any(low in run for run in _RUNS):
        return "That password is a run of keys or numbers in order. Use a few ordinary words together."
    user = (username or "").strip().lower()
    if len(user) >= 3 and user in low:
        return "The password must not contain the username."
    for part in re.split(r"\W+", (display_name or "").lower()):
        if len(part) >= 4 and part in low:
            return "The password must not contain the person's name."
    return None
