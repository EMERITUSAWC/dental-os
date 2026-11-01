"""SMS as a third channel (version 0.33), through the clinic's own SMS account, for patients who have no WhatsApp or prefer text.

Providers, as their public documents and published examples describe them (try a test message from set-up before relying on it):
- Hubtel (Ghana): GET https://smsc.hubtel.com/v1/messages/send with clientid, clientsecret, from, to and content in the query.
- Africa's Talking (Kenya, Nigeria, Uganda, Tanzania, Rwanda, Malawi, Zambia and more): POST https://api.africastalking.com/version1/messaging
  (sandbox: api.sandbox.africastalking.com) with an apiKey header and username, to, message, from as a form. The answer lists each
  recipient with a statusCode; 100, 101 and 102 mean processed, sent and queued.
- Twilio (worldwide): POST https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json with Basic auth and To, From, Body.

The same rules as WhatsApp and email: the patient must have agreed to that kind of message, an archived patient is never written to,
and only the same fields go in (no treatment details, no amounts). The secret is stored encrypted, never shown again, never logged.
The network call is passed in (`transport`), so tests never send a real message. No web framework here."""
import base64
import hashlib
import json
import re
import urllib.error
import urllib.parse
import urllib.request

from . import audit, i18n

PROVIDERS = {"hubtel": "Hubtel (Ghana)", "africastalking": "Africa's Talking (East, West and Southern Africa)", "twilio": "Twilio (worldwide)"}
HUBTEL_URL = "https://smsc.hubtel.com/v1/messages/send"
AT_LIVE = "https://api.africastalking.com/version1/messaging"
AT_SANDBOX = "https://api.sandbox.africastalking.com/version1/messaging"
TWILIO_API = "https://api.twilio.com/2010-04-01"
SENDER = re.compile(r"^(?:[A-Za-z0-9][A-Za-z0-9 .-]{0,10}|\+[1-9]\d{6,14}|\d{3,8})$")    # 11-character name, a full number, or a short code
E164 = re.compile(r"^\+[1-9]\d{6,14}$")
GSM7 = set("@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà")
GSM7_EXT = set("^{}\\[~]|€")
STOP = {"en": "Reply STOP to stop these messages.", "fr": "Répondez STOP pour ne plus recevoir ces messages."}


class SmsError(Exception):
    pass


def _fernet(secret_key):
    from cryptography.fernet import Fernet
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(b"dental-os/sms/" + str(secret_key).encode()).digest()))


def _open(secret_key, value):
    from cryptography.fernet import InvalidToken
    try:
        return _fernet(secret_key).decrypt(value.encode()).decode()
    except (InvalidToken, ValueError, AttributeError):
        return None


def segments(text):
    """How many SMS parts a text takes: 160 plain characters (153 each when split), or 70 (67 each) with other characters."""
    if all(ch in GSM7 or ch in GSM7_EXT for ch in text):
        n = sum(2 if ch in GSM7_EXT else 1 for ch in text)
        return 1 if n <= 160 else -(-n // 153)
    n = len(text.encode("utf-16-le")) // 2
    return 1 if n <= 70 else -(-n // 67)


def plain(text):
    """Swap curly quotes and dashes for plain ones, so a French or English message stays in the cheaper plain alphabet."""
    return (text or "").translate(str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"', "–": "-", "—": "-", " ": " ", " ": " ", "…": "..."}))


# ----------------------------------------------------------------------------------------------------------- the account
def account(db, cid):
    return db.execute("SELECT * FROM sms_accounts WHERE clinic_id = ?", (cid,)).fetchone()


def is_on(db, cid):
    a = account(db, cid)
    return bool(a and a["enabled"] and a["api_user"] and a["api_secret"] and a["sender"])


def save_account(db, clinic, user_id, secret_key, form):
    cid = clinic["id"]
    provider = form.get("provider") or "hubtel"
    if provider not in PROVIDERS:
        raise SmsError("Pick your SMS company.")
    sender = (form.get("sender") or "").strip()
    if sender and not SENDER.match(sender):
        raise SmsError("The sender is a name of up to 11 letters and digits, a full phone number like +233302123456, or a short code.")
    api_user = (form.get("api_user") or "").strip()[:120]
    if provider == "twilio" and api_user and not re.fullmatch(r"AC[0-9a-fA-F]{32}", api_user):
        raise SmsError("The Twilio Account SID starts AC and has 32 letters and digits after it.")
    if api_user and not re.fullmatch(r"[A-Za-z0-9_.@-]{2,120}", api_user):
        raise SmsError("The user name or client ID has letters and digits only.")
    secret = (form.get("api_secret") or "").strip()
    a = account(db, cid)
    sealed = _fernet(secret_key).encrypt(secret.encode()).decode() if secret else (a["api_secret"] if a and a["provider"] == provider else None)
    enabled = form.get("enabled") == "1"
    if enabled and not (sender and api_user and sealed):
        raise SmsError("Fill in the sender, the user name or ID and the key first.")
    db.execute("""INSERT INTO sms_accounts(clinic_id, provider, sender, api_user, api_secret, sandbox, enabled, auto_send)
                  VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(clinic_id) DO UPDATE SET provider = excluded.provider, sender = excluded.sender,
                  api_user = excluded.api_user, api_secret = excluded.api_secret, sandbox = excluded.sandbox, enabled = excluded.enabled,
                  auto_send = excluded.auto_send""",
               (cid, provider, sender or None, api_user or None, sealed, 1 if form.get("sandbox") == "1" else 0, 1 if enabled else 0,
                1 if form.get("auto_send") == "1" else 0))
    audit.log(db, cid, user_id, "sms.account", None, None, f"{provider} {'on' if enabled else 'off'}")      # the key is never logged


# ---------------------------------------------------------------------------------------------------------- sending
def default_transport(method, url, headers, body=None, timeout=20):
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:          # nosec: https to the SMS company only, built here
            return resp.status, resp.read(100000).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(100000).decode("utf-8", "replace")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise SmsError("Could not reach the SMS company: %s" % (getattr(exc, "reason", None) or exc.__class__.__name__))


def _json(text):
    try:
        return json.loads(text) if text else {}
    except ValueError:
        return {}


def send(db, clinic, secret_key, to, body, transport=None):
    """Send one text. Returns the SMS company's message id. Raises SmsError with a plain reason."""
    a = account(db, clinic["id"])
    if not a or not a["api_user"] or not a["api_secret"] or not a["sender"]:
        raise SmsError("SMS is not set up. A manager sets it up under Settings, SMS.")
    to = (to or "").strip()
    if not E164.match(to):
        raise SmsError("This patient has no mobile number in full international form.")
    body = plain(body).strip()
    if not body:
        raise SmsError("Write the message.")
    if segments(body) > 4:
        raise SmsError("That message is too long for SMS. Keep it shorter.")
    key = _open(secret_key, a["api_secret"])
    if not key:
        raise SmsError("The SMS key could not be read. A manager should type it again under Settings, SMS.")
    t = transport or default_transport
    if a["provider"] == "hubtel":
        q = urllib.parse.urlencode({"clientid": a["api_user"], "clientsecret": key, "from": a["sender"], "to": to.lstrip("+"), "content": body})
        status, text = t("GET", f"{HUBTEL_URL}?{q}", {"Accept": "application/json"})
        data = _json(text)
        if status >= 300 or str(data.get("status", data.get("Status", 0))) not in ("0", "Success", "success"):
            raise SmsError("Hubtel did not send it: %s" % str(data.get("message") or data.get("Message") or f"HTTP {status}")[:150])
        return str(data.get("messageId") or data.get("MessageId") or "")
    if a["provider"] == "africastalking":
        form = urllib.parse.urlencode({"username": a["api_user"], "to": to, "message": body, "from": a["sender"]}).encode()
        status, text = t("POST", AT_SANDBOX if a["sandbox"] else AT_LIVE,
                         {"apiKey": key, "Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"}, form)
        data = _json(text)
        rec = ((data.get("SMSMessageData") or {}).get("Recipients") or [{}])[0]
        if status >= 300 or int(rec.get("statusCode") or 0) not in (100, 101, 102):
            raise SmsError("Africa's Talking did not send it: %s" % str(rec.get("status") or (data.get("SMSMessageData") or {}).get("Message")
                                                                         or f"HTTP {status}")[:150])
        return str(rec.get("messageId") or "")
    auth = base64.b64encode(f"{a['api_user']}:{key}".encode()).decode()
    form = urllib.parse.urlencode({"To": to, "From": a["sender"], "Body": body}).encode()
    status, text = t("POST", f"{TWILIO_API}/Accounts/{a['api_user']}/Messages.json",
                     {"Authorization": "Basic " + auth, "Content-Type": "application/x-www-form-urlencoded"}, form)
    data = _json(text)
    if status >= 300:
        raise SmsError("Twilio did not send it: %s" % str(data.get("message") or f"HTTP {status}")[:150])
    return str(data.get("sid") or "")


def send_test(db, clinic, user_id, secret_key, to, transport=None):
    lang = i18n.clinic_lang(clinic)
    send(db, clinic, secret_key, to, i18n.t(lang, "This is a test message from Dental OS. SMS from the clinic is working."), transport)
    audit.log(db, clinic["id"], user_id, "sms.test")


def _with_stop(text, lang):
    return text + " " + STOP.get(lang, STOP["en"])


# ------------------------------------------------------------------------------------------------------ the send list
def deliver_outbox(db, clinic, user_id, secret_key, outbox_id, transport=None):
    from . import outbox as OB, plans as PL, recalls
    o = OB.get(db, clinic["id"], outbox_id)
    if not o:
        raise LookupError("outbox")
    if o["status"] != "queued":
        raise OB.MessageError("That message has already been dealt with.")
    p, text = OB.text_for(db, clinic, o)
    if p["status"] != "active":
        raise OB.MessageError("This patient is archived.")
    if not (p["reminders_ok"] if o["purpose"] == "care" else p["offers_ok"]):
        raise OB.MessageError("This patient has not agreed to these messages.")
    lang = i18n.patient_lang(p, clinic)
    body = _with_stop(text, lang) if o["purpose"] == "offer" else text
    try:
        ref = send(db, clinic, secret_key, p["phone"], body, transport)
    except SmsError as exc:
        raise OB.MessageError(str(exc))
    cur = db.execute("""INSERT INTO messages(clinic_id, patient_id, appointment_id, channel, kind, body, status, provider_id, created_by)
                        VALUES (?,?,?, 'sms', ?,?, 'sent', ?, ?)""", (clinic["id"], p["id"], o["appointment_id"], o["kind"], body, ref or None, user_id))
    db.execute("UPDATE outbox SET status = 'sent', message_id = ?, done_at = datetime('now'), done_by = ? WHERE id = ?", (cur.lastrowid, user_id, outbox_id))
    if o["kind"] == "reminder" and o["appointment_id"]:
        db.execute("UPDATE appointments SET confirmation = 'sent', updated_at = datetime('now') WHERE id = ? AND clinic_id = ? AND confirmation = 'none'",
                   (o["appointment_id"], clinic["id"]))
    elif o["kind"] == "recall" and o["recall_id"]:
        recalls.mark_contacted(db, clinic, o["recall_id"])
    elif o["kind"] == "plan_followup" and o["plan_id"]:
        PL.log_followup(db, clinic, {"id": user_id}, o["plan_id"], "Message sent by SMS", 7)
    audit.log(db, clinic["id"], user_id, f"sms.{o['kind']}", "message", cur.lastrowid)
    return cur.lastrowid


def send_ready(db, clinic, user_id, secret_key, limit=100, transport=None):
    """Text every queued message for patients who prefer SMS. Stops at the first failure to reach the company."""
    from . import outbox as OB
    sent = left = 0
    stopped = None
    ids = [r["id"] for r in db.execute("""SELECT o.id FROM outbox o JOIN patients p ON p.id = o.patient_id WHERE o.clinic_id = ? AND o.status = 'queued'
                                          AND p.prefers_sms = 1 ORDER BY o.due_on, o.id LIMIT ?""", (clinic["id"], limit))]
    for oid in ids:
        if stopped:
            left += 1
            continue
        try:
            deliver_outbox(db, clinic, user_id, secret_key, oid, transport)
            sent += 1
        except OB.MessageError as exc:
            left += 1
            if "Could not reach" in str(exc) or "could not be read" in str(exc) or "not set up" in str(exc):
                stopped = str(exc)
    return {"sent": sent, "left": left, "stopped": stopped}


def send_statement(db, clinic, user_id, secret_key, item_id, portal_url_for, transport=None):
    from . import links, statements as S
    cid = clinic["id"]
    it = db.execute("SELECT * FROM statement_items WHERE id = ? AND clinic_id = ?", (item_id, cid)).fetchone()
    if not it:
        raise SmsError("That statement was not found.")
    p = db.execute("SELECT * FROM patients WHERE id = ?", (it["patient_id"],)).fetchone()
    if not p["reminders_ok"]:
        raise SmsError("This patient has not agreed to messages. Print the statement or give it in person.")
    if not E164.match(p["phone"] or ""):
        raise SmsError("This patient has no mobile number in full international form.")
    token = links.create(db, clinic, user_id, p["id"])
    url = portal_url_for(token)
    lang = i18n.patient_lang(p, clinic)
    body = S.STATEMENT_TEXT.get(lang, S.STATEMENT_TEXT["en"]).format(first_name=p["first_name"], clinic=clinic["name"], link=url)
    ref = send(db, clinic, secret_key, p["phone"], body, transport)
    db.execute("INSERT INTO messages(clinic_id, patient_id, channel, kind, body, status, provider_id, created_by) VALUES (?,?, 'sms', 'statement', ?, 'sent', ?, ?)",
               (cid, p["id"], plain(body).replace(url, "[private link]"), ref or None, user_id))
    db.execute("UPDATE statement_items SET sent_at = datetime('now'), sent_by = ?, sent_how = 'sms' WHERE id = ?", (user_id, item_id))
    audit.log(db, cid, user_id, "statements.sms", "patient", p["id"])
