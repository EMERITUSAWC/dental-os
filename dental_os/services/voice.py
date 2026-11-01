"""Phone calls through the clinic's own Twilio account (version 0.21).

- A call to the clinic's Twilio number rings the desk phone. While it rings, every signed-in screen in the clinic shows who is calling,
  with a link to their record when the number is on a patient's record.
- A call nobody answers is logged as missed, and a call-back task is made for the front desk.
- Click to call: the desk phone rings first; when it is picked up, Twilio calls the patient and joins the two. The patient sees the
  clinic's number, not anyone's own phone.
- Every call is in the call log and on the patient's contact log.

Calls are not recorded: recording calls needs consent rules that differ by country. Twilio's messages to us carry Twilio's signature,
made with the clinic's auth token, and anything without a good signature is refused. The auth token is stored encrypted, never shown
again and never logged. This module has no web framework in it, and the network call is passed in (`transport`)."""
import base64
import datetime
import hashlib
import json
import hmac
import re
import secrets
import urllib.error
import urllib.parse
import urllib.request
from xml.sax.saxutils import escape

from . import audit, contactlog, tasks
from .timeutil import now_local

TWILIO_API = "https://api.twilio.com/2010-04-01"
RING_SECONDS = 25
STATUSES = {"ringing": "Ringing", "in-progress": "Answered", "completed": "Finished", "missed": "Missed", "busy": "Busy",
            "failed": "Failed", "no-answer": "No answer", "canceled": "Cancelled", "queued": "Calling"}
E164 = re.compile(r"^\+[1-9]\d{6,14}$")


class CallError(Exception):
    pass


def _fernet(secret_key):
    from cryptography.fernet import Fernet
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(b"dental-os/voice/" + str(secret_key).encode()).digest()))


def _open(secret_key, value):
    from cryptography.fernet import InvalidToken
    try:
        return _fernet(secret_key).decrypt(value.encode()).decode()
    except (InvalidToken, ValueError, AttributeError):
        return None


def account(db, cid):
    return db.execute("SELECT * FROM voice_accounts WHERE clinic_id = ?", (cid,)).fetchone()


def is_on(db, cid):
    a = account(db, cid)
    return bool(a and a["enabled"] and a["auth_token"] and a["account_sid"] and a["twilio_number"] and a["desk_number"])


def save_account(db, clinic, user_id, secret_key, enabled, account_sid, auth_token, twilio_number, desk_number, missed_task_to=None):
    cid = clinic["id"]
    sid = (account_sid or "").strip()
    if sid and not re.fullmatch(r"AC[0-9a-fA-F]{32}", sid):
        raise CallError("The Account SID starts AC and has 32 letters and digits after it.")
    token = (auth_token or "").strip()
    if token and not re.fullmatch(r"[0-9a-fA-F]{32}", token):
        raise CallError("The auth token is 32 letters and digits.")
    for n in (twilio_number, desk_number):
        if (n or "").strip() and not E164.fullmatch((n or "").strip()):
            raise CallError("Write phone numbers in full international form, for example +233302123456.")
    if missed_task_to and not db.execute("SELECT 1 FROM users WHERE id = ? AND clinic_id = ?", (missed_task_to, cid)).fetchone():
        raise CallError("Pick someone from this clinic.")
    a = account(db, cid)
    if not a:
        db.execute("INSERT INTO voice_accounts(clinic_id, hook_key) VALUES (?,?)", (cid, secrets.token_urlsafe(18)))
        a = account(db, cid)
    sealed = _fernet(secret_key).encrypt(token.encode()).decode() if token else a["auth_token"]
    values = (sid or a["account_sid"], sealed, (twilio_number or "").strip() or a["twilio_number"], (desk_number or "").strip() or a["desk_number"])
    if enabled and not all(values):
        raise CallError("Fill in the Account SID, auth token, the clinic's Twilio number and the desk phone first.")
    db.execute("""UPDATE voice_accounts SET account_sid = ?, auth_token = ?, twilio_number = ?, desk_number = ?, missed_task_to = ?, enabled = ?,
                  updated_at = datetime('now') WHERE clinic_id = ?""", values + (missed_task_to or None, 1 if enabled else 0, cid))
    audit.log(db, cid, user_id, "voice.account", None, None, "on" if enabled else "off")     # the token itself is never logged


# ----------------------------------------------------------------------------------------------------- Twilio signatures
def signature(auth_token, url, params):
    """Twilio's request signature: the full address, then each POST field name and value in name order, HMAC-SHA1, Base64."""
    data = url + "".join(k + v for k, v in sorted(params.items()))
    return base64.b64encode(hmac.new(auth_token.encode(), data.encode(), hashlib.sha1).digest()).decode()


def signature_ok(db, acct, secret_key, url, params, header):
    token = _open(secret_key, acct["auth_token"]) if acct["auth_token"] else None
    return bool(token and header) and hmac.compare_digest(signature(token, url, params), header)


def account_for_hook(db, key):
    return db.execute("SELECT * FROM voice_accounts WHERE hook_key = ?", (key,)).fetchone()


# ----------------------------------------------------------------------------------------------------------- the call log
def _patient_for(db, cid, number):
    if not number:
        return None
    return db.execute("SELECT * FROM patients WHERE clinic_id = ? AND phone = ? AND status = 'active' ORDER BY id LIMIT 1",
                      (cid, number)).fetchone()


def _now(clinic):
    return now_local(clinic["timezone"]).strftime("%Y-%m-%d %H:%M:%S")


def incoming(db, clinic, call_sid, from_number, to_number):
    """Twilio says a call is coming in. Returns the TwiML that rings the desk."""
    a = account(db, clinic["id"])
    p = _patient_for(db, clinic["id"], from_number)
    if not db.execute("SELECT 1 FROM calls WHERE call_sid = ?", (call_sid,)).fetchone():
        db.execute("""INSERT INTO calls(clinic_id, call_sid, direction, from_number, to_number, patient_id, status, started_at)
                      VALUES (?,?, 'in', ?,?,?, 'ringing', ?)""", (clinic["id"], call_sid, from_number, to_number, p["id"] if p else None, _now(clinic)))
    return a


def twiml_ring(desk_number, action_url, caller_id=None):
    cid = f' callerId="{escape(caller_id)}"' if caller_id else ""
    return ('<?xml version="1.0" encoding="UTF-8"?><Response>'
            f'<Dial timeout="{RING_SECONDS}" action="{escape(action_url)}"{cid}><Number>{escape(desk_number)}</Number></Dial></Response>')


SAY_LANG = {"en": "en-GB", "fr": "fr-FR"}


def twiml_missed(message, lang="en"):
    """A short spoken message, in the clinic's language, then hang up."""
    return (f'<?xml version="1.0" encoding="UTF-8"?><Response><Say language="{SAY_LANG.get(lang, "en-GB")}">{escape(message)}</Say>'
            '<Hangup/></Response>')


def twiml_bridge(patient_number, caller_id):
    return ('<?xml version="1.0" encoding="UTF-8"?><Response>'
            f'<Dial callerId="{escape(caller_id)}"><Number>{escape(patient_number)}</Number></Dial></Response>')


def after_dial(db, clinic, call_sid, dial_status, duration=None):
    """After the desk phone rang: answered, or missed. A missed call from a known number makes a call-back task."""
    c = db.execute("SELECT * FROM calls WHERE call_sid = ? AND clinic_id = ?", (call_sid, clinic["id"])).fetchone()
    if not c:
        return None
    answered = dial_status == "completed"
    status = "completed" if answered else "missed"
    db.execute("UPDATE calls SET status = ?, duration_s = ?, ended_at = ? WHERE id = ?",
               (status, int(duration) if str(duration or "").isdigit() else None, _now(clinic), c["id"]))
    if c["patient_id"]:
        contactlog.add(db, clinic, None, c["patient_id"], "call_in", "Phone call answered at the desk." if answered else "Missed phone call.")
    if not answered and not c["task_id"]:
        a = account(db, clinic["id"])
        who = None
        if c["patient_id"]:
            p = db.execute("SELECT first_name, last_name FROM patients WHERE id = ?", (c["patient_id"],)).fetchone()
            who = f"{p['first_name']} {p['last_name']}"
        title = f"Call back {who or c['from_number']}"
        tid = tasks.add(db, clinic, None, title, a["missed_task_to"] if a else None, now_local(clinic["timezone"]).date().isoformat(), "high",
                        c["patient_id"], f"Missed call from {c['from_number']}.")
        db.execute("UPDATE calls SET task_id = ? WHERE id = ?", (tid, c["id"]))
    return status


def status_update(db, clinic, call_sid, status, duration=None):
    c = db.execute("SELECT * FROM calls WHERE call_sid = ? AND clinic_id = ?", (call_sid, clinic["id"])).fetchone()
    if not c or status not in STATUSES:
        return
    if c["status"] in ("missed",) and status == "completed":
        return
    db.execute("UPDATE calls SET status = ?, duration_s = COALESCE(?, duration_s), ended_at = CASE WHEN ? IN ('completed','busy','failed','no-answer','canceled') THEN ? ELSE ended_at END WHERE id = ?",
               (status, int(duration) if str(duration or "").isdigit() else None, status, _now(clinic), c["id"]))


def ringing_now(db, clinic):
    """Calls ringing in the last minute, for the screen pop-up."""
    since = (now_local(clinic["timezone"]) - datetime.timedelta(seconds=60)).strftime("%Y-%m-%d %H:%M:%S")
    return db.execute("""SELECT c.*, p.first_name, p.last_name FROM calls c LEFT JOIN patients p ON p.id = c.patient_id
                         WHERE c.clinic_id = ? AND c.direction = 'in' AND c.status = 'ringing' AND c.started_at >= ? ORDER BY c.id DESC LIMIT 3""",
                      (clinic["id"], since)).fetchall()


def log(db, cid, limit=200, patient_id=None):
    q = """SELECT c.*, p.first_name, p.last_name, u.display_name AS by_name FROM calls c LEFT JOIN patients p ON p.id = c.patient_id
           LEFT JOIN users u ON u.id = c.user_id WHERE c.clinic_id = ?"""
    args = [cid]
    if patient_id:
        q += " AND c.patient_id = ?"
        args.append(patient_id)
    return db.execute(q + " ORDER BY c.id DESC LIMIT ?", args + [limit]).fetchall()


# ----------------------------------------------------------------------------------------------------------- click to call
def default_transport(method, url, headers, body=None, timeout=15):
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:          # nosec: https to api.twilio.com only, built here
            return resp.status, resp.read(100000).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(100000).decode("utf-8", "replace")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise CallError("Could not reach Twilio: %s" % (getattr(exc, "reason", None) or exc.__class__.__name__))


def call_patient(db, clinic, user, secret_key, pid, base_url, transport=None):
    """Ring the desk phone; when it is answered Twilio calls the patient. Returns our call id."""
    cid = clinic["id"]
    if not is_on(db, cid):
        raise CallError("Phone calls are not switched on.")
    a = account(db, cid)
    p = db.execute("SELECT * FROM patients WHERE id = ? AND clinic_id = ? AND status = 'active'", (pid, cid)).fetchone()
    if not p or not p["phone"] or not E164.fullmatch(p["phone"]):
        raise CallError("This patient has no phone number to call.")
    token = _open(secret_key, a["auth_token"])
    if not token:
        raise CallError("The auth token could not be read. A manager should paste it again under Settings.")
    ref = secrets.token_urlsafe(12)
    base = base_url.rstrip("/") + f"/hooks/voice/{a['hook_key']}"
    form = {"To": a["desk_number"], "From": a["twilio_number"], "Url": f"{base}/bridge?ref={ref}", "Method": "POST",
            "StatusCallback": f"{base}/status", "StatusCallbackMethod": "POST"}
    auth = base64.b64encode(f"{a['account_sid']}:{token}".encode()).decode()
    status, text = (transport or default_transport)("POST", f"{TWILIO_API}/Accounts/{a['account_sid']}/Calls.json",
                                                     {"Authorization": "Basic " + auth, "Content-Type": "application/x-www-form-urlencoded"},
                                                     urllib.parse.urlencode(form).encode())
    try:
        data = json.loads(text) if text else {}
    except ValueError:
        data = {}
    if status >= 300:
        raise CallError("Twilio refused it: %s" % str(data.get("message") or f"HTTP {status}")[:200])
    call_id = db.execute("""INSERT INTO calls(clinic_id, call_sid, direction, from_number, to_number, patient_id, status, started_at, user_id, ref)
                            VALUES (?,?, 'out', ?,?,?, 'queued', ?,?,?)""",
                         (cid, data.get("sid") or ("pending-" + ref), a["twilio_number"], p["phone"], pid, _now(clinic), user["id"], ref)).lastrowid
    contactlog.add(db, clinic, user["id"], pid, "call_out", "Phone call to the patient through the clinic's line.")
    audit.log(db, cid, user["id"], "voice.call", "patient", pid)
    return call_id


def bridge_for(db, acct, ref):
    c = db.execute("SELECT * FROM calls WHERE clinic_id = ? AND ref = ? AND direction = 'out'", (acct["clinic_id"], ref)).fetchone()
    if not c:
        return None
    db.execute("UPDATE calls SET status = 'in-progress' WHERE id = ?", (c["id"],))
    return twiml_bridge(c["to_number"], acct["twilio_number"])
