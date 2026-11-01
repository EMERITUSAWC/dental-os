"""Two-step login with a code from an authenticator app (the standard time-based code, RFC 6238), plus one-time recovery codes.

The shared secret is kept encrypted with a key made from the app's SECRET_KEY, so a copy of the database alone does not give
anyone the codes. (If SECRET_KEY is changed, every secret becomes unreadable and a manager must reset two-step login for the staff.)
A code can be used once: the time-step of the last accepted code is stored and an equal or older one is refused. Recovery codes are
kept only as hashes and each works once."""
import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote

from cryptography.fernet import Fernet, InvalidToken

from . import audit, throttle

STEP = 30
DIGITS = 6
WINDOW = 1                      # also accept the code just before and just after, for clocks a little out of line
MAX_FAILS = 5                   # wrong codes allowed in FAIL_MINUTES before the account waits
FAIL_MINUTES = 15
RECOVERY_COUNT = 10
_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"           # no 0/O or 1/I to confuse people reading codes from paper


class TwoFactorError(Exception):
    pass


def _fernet(secret_key):
    key = base64.urlsafe_b64encode(hashlib.sha256(b"dental-os/2fa/" + str(secret_key).encode()).digest())
    return Fernet(key)


def new_secret():
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def code_at(secret, step):
    key = base64.b32decode(secret + "=" * (-len(secret) % 8))
    digest = hmac.new(key, struct.pack(">Q", step), hashlib.sha1).digest()
    off = digest[-1] & 0x0F
    num = (struct.unpack(">I", digest[off:off + 4])[0] & 0x7FFFFFFF) % (10 ** DIGITS)
    return str(num).zfill(DIGITS)


def check(secret, code, last_step=0, now=None):
    """The time-step the code belongs to, or None. A step at or before last_step is refused (a code cannot be used twice)."""
    code = "".join(ch for ch in str(code or "") if ch.isdigit())
    if len(code) != DIGITS:
        return None
    here = int((now if now is not None else time.time()) // STEP)
    found = None
    for step in range(here - WINDOW, here + WINDOW + 1):
        if hmac.compare_digest(code_at(secret, step), code) and step > last_step:
            found = step
    return found


def uri(secret, username, issuer):
    label = quote(f"{issuer}:{username}")
    return f"otpauth://totp/{label}?secret={secret}&issuer={quote(issuer)}&algorithm=SHA1&digits={DIGITS}&period={STEP}"


def spaced(secret):
    return " ".join(secret[i:i + 4] for i in range(0, len(secret), 4))


# ---------------------------------------------------------------- setup
def begin_setup(db, secret_key, user):
    """Start (or carry on) setting up. A secret waits, not yet in force, until the person proves their app shows the right code."""
    row = db.execute("SELECT totp_secret, totp_enabled FROM users WHERE id = ?", (user["id"],)).fetchone()
    if row["totp_enabled"]:
        raise TwoFactorError("Two-step login is already on.")
    secret = None
    if row["totp_secret"]:
        try:
            secret = _fernet(secret_key).decrypt(row["totp_secret"].encode()).decode()
        except InvalidToken:
            secret = None
    if not secret:
        secret = new_secret()
        db.execute("UPDATE users SET totp_secret = ?, totp_last_step = 0 WHERE id = ?",
                   (_fernet(secret_key).encrypt(secret.encode()).decode(), user["id"]))
    return secret


def _secret_of(db, secret_key, user_id):
    row = db.execute("SELECT totp_secret, totp_last_step FROM users WHERE id = ?", (user_id,)).fetchone()
    if not row or not row["totp_secret"]:
        return None, 0
    try:
        return _fernet(secret_key).decrypt(row["totp_secret"].encode()).decode(), row["totp_last_step"]
    except InvalidToken:
        return None, 0


def confirm_setup(db, secret_key, user, code):
    """Turn two-step login on once the first code is right. Returns the recovery codes, which are shown only this once."""
    if _locked(db, user["id"]):
        raise TwoFactorError("Too many wrong codes. Wait a few minutes.")
    secret, last = _secret_of(db, secret_key, user["id"])
    if not secret:
        raise TwoFactorError("Start the setup again.")
    step = check(secret, code, last)
    if step is None:
        throttle.record(db, f"2fa:{user['id']}")
        raise TwoFactorError("That code is not right. Check the app and try the new code it shows.")
    db.execute("UPDATE users SET totp_enabled = 1, totp_last_step = ? WHERE id = ?", (step, user["id"]))
    codes = new_recovery_codes(db, user["id"])
    audit.log(db, user["clinic_id"], user["id"], "2fa.enable", "user", user["id"])
    return codes


def disable(db, user_id, by_user_id, clinic_id, reason="2fa.disable"):
    db.execute("UPDATE users SET totp_secret = NULL, totp_enabled = 0, totp_last_step = 0 WHERE id = ?", (user_id,))
    db.execute("DELETE FROM recovery_codes WHERE user_id = ?", (user_id,))
    audit.log(db, clinic_id, by_user_id, reason, "user", user_id)


# ---------------------------------------------------------------- recovery codes
def _norm(code):
    return "".join(ch for ch in str(code or "").upper() if ch.isalnum())


def _hash(code):
    return hashlib.sha256(("dental-os/recovery/" + _norm(code)).encode()).hexdigest()


def new_recovery_codes(db, user_id):
    """Replace any unused codes with a fresh set. Returns the plain codes for showing once."""
    db.execute("DELETE FROM recovery_codes WHERE user_id = ?", (user_id,))
    out = []
    for _ in range(RECOVERY_COUNT):
        raw = "".join(secrets.choice(_ALPHABET) for _ in range(10))
        out.append(f"{raw[:5]}-{raw[5:]}")
        db.execute("INSERT INTO recovery_codes(user_id, code_hash) VALUES (?,?)", (user_id, _hash(raw)))
    return out


def recovery_left(db, user_id):
    return db.execute("SELECT COUNT(*) FROM recovery_codes WHERE user_id = ? AND used_at IS NULL", (user_id,)).fetchone()[0]


# ---------------------------------------------------------------- signing in
def _locked(db, user_id):
    return throttle.count(db, f"2fa:{user_id}", FAIL_MINUTES) >= MAX_FAILS


def verify_login(db, secret_key, user, code, now=None):
    """'totp' or 'recovery' if the code is good, 'locked' if there have been too many wrong tries, or None."""
    if _locked(db, user["id"]):
        return "locked"
    secret, last = _secret_of(db, secret_key, user["id"])
    if secret:
        step = check(secret, code, last, now)
        if step is not None:
            db.execute("UPDATE users SET totp_last_step = ? WHERE id = ?", (step, user["id"]))
            return "totp"
    text = _norm(code)
    if len(text) == 10:
        cur = db.execute("UPDATE recovery_codes SET used_at = datetime('now') WHERE user_id = ? AND code_hash = ? AND used_at IS NULL",
                         (user["id"], _hash(text)))
        if cur.rowcount:
            return "recovery"
    throttle.record(db, f"2fa:{user['id']}")
    return None
