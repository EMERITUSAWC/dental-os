"""The official WhatsApp Business API (Meta's Cloud API), as a second way to send. The first way stays: staff open WhatsApp with
the words ready and press send. Nothing here changes that, and a clinic that does not connect the API sees no difference.

What it does, in plain terms:
  - Sends a message from the clinic's own WhatsApp Business number, without a person pressing send.
  - Business-started messages must use a template that Meta has approved. The clinic writes its templates at Meta, then tells
    us here which approved template goes with which message (reminder, recall and so on) and in which order the fields go in.
    If the patient wrote to us in the last 24 hours, ordinary text can be sent instead, with no template.
  - Reports back: Meta tells us when a message was sent, delivered, read, or failed, and when a patient replies. A reply that says
    STOP withdraws the patient's agreement to messages, because Meta's rules and good practice both require that.

Safety rules kept from the manual flow: the patient must have agreed (appointment messages for 'care', offers for 'offer'),
an archived patient is never messaged, only the same fields as before go into a message (never treatment details or money),
and the access token and app secret are stored encrypted, never shown again, and never written to the audit log.

This module has no web framework in it. The network call is passed in (`transport`) so tests never touch the internet.
Facts taken from Meta's documentation (developers.facebook.com, Cloud API): POST https://graph.facebook.com/<version>/<phone number id>/messages
with a Bearer token; a template message has type "template" with components of type "body" and positional text parameters; the
reply holds messages[0].id (the wamid); webhooks are verified with hub.mode, hub.verify_token and hub.challenge, are signed with
HMAC-SHA256 in X-Hub-Signature-256 using the app secret, report statuses sent, delivered, read and failed, and are retried by Meta
for up to 7 days unless we answer 200, so every handler here is safe to run twice."""
import base64
import hashlib
import hmac
import json
import re
import secrets
import urllib.error
import urllib.request

from . import audit, i18n, msgtemplates as MT
from .phone import wa_digits
from .timeutil import parse_dt

DEFAULT_VERSION = "v22.0"
GRAPH = "https://graph.facebook.com"
WINDOW_HOURS = 24
KINDS = ("confirmation", "reminder", "recall", "plan_followup", "review_request", "birthday", "campaign", "reactivation")
PARAM_FIELDS = ("first_name", "clinic", "dentist", "day", "time", "review_link", "text")      # 'text' is the whole finished message
RANK = {"sent": 1, "delivered": 2, "read": 3}
STOP_WORDS = {"stop", "stop all", "unsubscribe", "arret", "arrêt", "stoppez", "desabonner", "désabonner"}
MAX_BODY = 1024 * 1024


class WhatsAppError(Exception):
    pass


# ----------------------------------------------------------------------------- secrets
def _fernet(secret_key):
    from cryptography.fernet import Fernet
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(b"dental-os/whatsapp/" + str(secret_key).encode()).digest()))


def _seal(secret_key, value):
    return _fernet(secret_key).encrypt(value.encode()).decode()


def _open(secret_key, value):
    from cryptography.fernet import InvalidToken
    try:
        return _fernet(secret_key).decrypt(value.encode()).decode()
    except (InvalidToken, ValueError, AttributeError):
        return None                                      # the key changed or the value is damaged: treat as not set


# ----------------------------------------------------------------------------- the account
def row(db, clinic_id):
    return db.execute("SELECT * FROM wa_account WHERE clinic_id = ?", (clinic_id,)).fetchone()


def ensure(db, clinic_id):
    if not row(db, clinic_id):
        db.execute("INSERT INTO wa_account(clinic_id, hook_key, verify_token) VALUES (?,?,?)",
                   (clinic_id, secrets.token_urlsafe(18), secrets.token_urlsafe(18)))
    return row(db, clinic_id)


def credentials(db, clinic_id, secret_key):
    """What is needed to send, or None if the API is off or not complete. The token is decrypted here and goes nowhere but the request."""
    r = row(db, clinic_id)
    if not r or not r["enabled"] or not r["phone_number_id"] or not r["access_token"]:
        return None
    token = _open(secret_key, r["access_token"])
    if not token:
        return None
    return {"phone_number_id": r["phone_number_id"], "token": token, "version": r["api_version"] or DEFAULT_VERSION}


def is_on(db, clinic_id, secret_key):
    return credentials(db, clinic_id, secret_key) is not None


def save_account(db, clinic, user_id, secret_key, enabled, phone_number_id, access_token, app_secret, api_version, auto_send,
                 new_hook_key=False):
    cid = clinic["id"]
    r = ensure(db, cid)
    pid = (phone_number_id or "").strip()
    if pid and not re.fullmatch(r"\d{5,25}", pid):
        raise WhatsAppError("The phone number ID is only digits. You find it in Meta's WhatsApp set-up page, next to your number.")
    version = (api_version or "").strip() or DEFAULT_VERSION
    if not re.fullmatch(r"v\d{1,2}\.\d", version):
        raise WhatsAppError("The version looks like v22.0.")
    token = (access_token or "").strip()
    app_sec = (app_secret or "").strip()
    if token and (len(token) < 20 or " " in token):
        raise WhatsAppError("That access token does not look right.")
    if app_sec and (len(app_sec) < 16 or " " in app_sec):
        raise WhatsAppError("That app secret does not look right.")
    if pid and pid != r["phone_number_id"]:
        taken = db.execute("SELECT 1 FROM wa_account WHERE phone_number_id = ? AND clinic_id != ?", (pid, cid)).fetchone()
        if taken:
            raise WhatsAppError("That phone number ID is already connected to another clinic.")
    token_enc = _seal(secret_key, token) if token else r["access_token"]
    secret_enc = _seal(secret_key, app_sec) if app_sec else r["app_secret"]
    if enabled and not (pid and token_enc and secret_enc):
        raise WhatsAppError("To switch it on you need the phone number ID, the access token and the app secret. "
                            "The app secret is how we know a message really came from Meta.")
    hook = secrets.token_urlsafe(18) if new_hook_key else r["hook_key"]
    db.execute("""UPDATE wa_account SET enabled = ?, phone_number_id = ?, access_token = ?, app_secret = ?, api_version = ?,
                  auto_send = ?, hook_key = ?, updated_at = datetime('now') WHERE clinic_id = ?""",
               (1 if enabled else 0, pid or None, token_enc, secret_enc, version, 1 if (auto_send and enabled) else 0, hook, cid))
    audit.log(db, cid, user_id, "whatsapp.account", None, None, "on" if enabled else "off")       # never the token or the secret


def hook_for_key(db, key):
    return db.execute("SELECT * FROM wa_account WHERE hook_key = ?", (key or "",)).fetchone()


# ----------------------------------------------------------------------------- templates the clinic has at Meta
def default_language_code(lang):
    return {"en": "en", "fr": "fr"}.get(lang, lang)


def suggestion(db, clinic_id, kind, lang):
    """The clinic's own wording turned into a template to write at Meta: {first_name} becomes {{1}} and so on, in order of first use.
    Returns (text for Meta, list of field names in order)."""
    if kind == "campaign":
        return "(Write the campaign wording at Meta once. Use {{1}} for the first name and {{2}} for the clinic.)", ["first_name", "clinic"]
    body = MT.body_for(db, clinic_id, kind, lang) if kind in MT.KINDS else ""
    order, seen = [], {}

    def number(match):
        name = match.group(1)
        if name not in seen:
            seen[name] = len(order) + 1
            order.append(name)
        return "{{%d}}" % seen[name]
    return re.sub(r"\{(\w+)\}", number, body), order


def get_mapping(db, clinic_id, kind, lang):
    return db.execute("SELECT * FROM wa_templates WHERE clinic_id = ? AND kind = ? AND lang = ? AND active = 1", (clinic_id, kind, lang)).fetchone()


def mappings(db, clinic_id):
    return {(r["kind"], r["lang"]): r for r in db.execute("SELECT * FROM wa_templates WHERE clinic_id = ?", (clinic_id,))}


def save_mapping(db, clinic, user_id, kind, lang, template_name, language_code, params):
    cid = clinic["id"]
    if kind not in KINDS:
        raise LookupError("kind")
    if i18n.pick(lang) is None:
        raise LookupError("lang")
    name = (template_name or "").strip()
    if not name:
        db.execute("DELETE FROM wa_templates WHERE clinic_id = ? AND kind = ? AND lang = ?", (cid, kind, lang))
        audit.log(db, cid, user_id, "whatsapp.template.remove", None, None, f"{kind}/{lang}")
        return
    if not re.fullmatch(r"[a-z0-9_]{1,512}", name):
        raise WhatsAppError("A template name has only lowercase letters, digits and underscores, exactly as it is at Meta.")
    code = (language_code or "").strip() or default_language_code(lang)
    if not re.fullmatch(r"[a-z]{2,3}(_[A-Z]{2})?", code):
        raise WhatsAppError("The language code looks like en, en_GB or fr.")
    fields = [p.strip() for p in (params or "").replace(";", ",").split(",") if p.strip()]
    if len(fields) > 10:
        raise WhatsAppError("Ten fields at most.")
    for f in fields:
        if f not in PARAM_FIELDS:
            raise WhatsAppError("{" + f + "} is not a field you can send. Use: " + ", ".join(PARAM_FIELDS) + ".")
    db.execute("""INSERT INTO wa_templates(clinic_id, kind, lang, template_name, language_code, params, active) VALUES (?,?,?,?,?,?,1)
                  ON CONFLICT(clinic_id, kind, lang) DO UPDATE SET template_name = excluded.template_name,
                  language_code = excluded.language_code, params = excluded.params, active = 1""",
               (cid, kind, lang, name, code, ",".join(fields)))
    audit.log(db, cid, user_id, "whatsapp.template.save", None, None, f"{kind}/{lang}")


# ----------------------------------------------------------------------------- building the request
def _clean_param(value):
    """Meta does not accept an empty parameter, a new line or a tab, or a run of more than 4 spaces."""
    text = re.sub(r"[\r\n\t]+", " ", str(value if value is not None else ""))
    text = re.sub(r" {2,}", " ", text).strip()
    return text[:1000] or "-"


def template_payload(to, name, language_code, values):
    body = {"messaging_product": "whatsapp", "recipient_type": "individual", "to": to, "type": "template",
            "template": {"name": name, "language": {"code": language_code}}}
    if values:
        body["template"]["components"] = [{"type": "body", "parameters": [{"type": "text", "text": _clean_param(v)} for v in values]}]
    return body


def text_payload(to, text):
    return {"messaging_product": "whatsapp", "recipient_type": "individual", "to": to, "type": "text",
            "text": {"preview_url": False, "body": text[:4000]}}


def default_transport(url, headers, body, timeout=10):
    """The real network call. Returns (status, text). Tests pass their own."""
    req = urllib.request.Request(url, data=body, headers=headers, method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:          # nosec: https to graph.facebook.com only, built here
            return resp.status, resp.read(200000).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(200000).decode("utf-8", "replace")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise WhatsAppError("Could not reach WhatsApp: %s" % (getattr(exc, "reason", None) or exc.__class__.__name__))


def _call(creds, path, payload, transport):
    url = f"{GRAPH}/{creds['version']}/{path}"
    headers = {"Authorization": "Bearer " + creds["token"], "Content-Type": "application/json"}
    status, text = (transport or default_transport)(url, headers, json.dumps(payload).encode() if payload is not None else None)
    try:
        data = json.loads(text) if text else {}
    except ValueError:
        data = {}
    if status >= 300 or "error" in data:
        err = data.get("error") or {}
        detail = (err.get("error_data") or {}).get("details") or err.get("message") or f"HTTP {status}"
        raise WhatsAppError(f"WhatsApp refused it: {str(detail)[:200]} (code {err.get('code', status)})")
    return data


def check_connection(db, clinic, secret_key, transport=None):
    """Ask Meta about the connected number. Returns a short sentence."""
    creds = credentials(db, clinic["id"], secret_key)
    if not creds:
        raise WhatsAppError("Fill in the number ID, the token and the secret, switch it on and save first.")
    data = _call(creds, f"{creds['phone_number_id']}?fields=display_phone_number,verified_name", None, transport)
    return "Connected to %s (%s)." % (data.get("display_phone_number", "the number"), data.get("verified_name", "name not shown"))


# ----------------------------------------------------------------------------- sending
def in_window(db, clinic_id, patient_id):
    """True if the patient wrote to us within the last 24 hours, so ordinary text is allowed."""
    return db.execute("""SELECT 1 FROM messages WHERE clinic_id = ? AND patient_id = ? AND direction = 'in'
                         AND created_at >= datetime('now', ?) LIMIT 1""", (clinic_id, patient_id, f"-{WINDOW_HOURS} hours")).fetchone() is not None


def plan(db, clinic, secret_key, patient, kind, purpose, ctx, text):
    """Decide how a message would go. Returns ('template', mapping, values) or ('text', None, None). Raises WhatsAppError if it cannot go by the API."""
    if not credentials(db, clinic["id"], secret_key):
        raise WhatsAppError("The WhatsApp API is not switched on.")
    if patient["status"] != "active":
        raise WhatsAppError("This patient is archived.")
    if not (patient["reminders_ok"] if purpose == "care" else patient["offers_ok"]):
        raise WhatsAppError("This patient has not agreed to these messages.")
    if not wa_digits(patient["phone"]):
        raise WhatsAppError("This patient has no phone number.")
    lang = i18n.patient_lang(patient, clinic)
    m = get_mapping(db, clinic["id"], kind, lang)
    if m:
        full = dict(ctx, text=text)
        names = [p for p in (m["params"] or "").split(",") if p]
        return "template", m, [full.get(n, "") for n in names]
    if in_window(db, clinic["id"], patient["id"]):
        return "text", None, None
    raise WhatsAppError("No approved WhatsApp template is set for this message in %s, and the patient has not written in the last 24 hours. "
                        "Add the template under Message set-up, or use the WhatsApp button to send it by hand." % i18n.LANGS[lang])


def deliver(db, clinic, user_id, secret_key, patient, kind, purpose, ctx, text, appointment_id=None, transport=None):
    """Send one message through the API and log it. Returns the new message id. Raises WhatsAppError and logs nothing if it cannot go."""
    how, m, values = plan(db, clinic, secret_key, patient, kind, purpose, ctx, text)
    creds = credentials(db, clinic["id"], secret_key)
    to = wa_digits(patient["phone"])
    payload = template_payload(to, m["template_name"], m["language_code"], values) if how == "template" else text_payload(to, text)
    data = _call(creds, f"{creds['phone_number_id']}/messages", payload, transport)
    wamid = ((data.get("messages") or [{}])[0]).get("id")
    if not wamid:
        raise WhatsAppError("WhatsApp did not give a message number back, so we cannot tell whether it was sent. Check before sending again.")
    cur = db.execute("""INSERT INTO messages(clinic_id, patient_id, appointment_id, kind, body, status, provider_id, created_by)
                        VALUES (?,?,?,?,?, 'sent', ?, ?)""", (clinic["id"], patient["id"], appointment_id, kind, text, wamid, user_id))
    audit.log(db, clinic["id"], user_id, f"message.{kind}", "message", cur.lastrowid, "api/" + how)
    return cur.lastrowid


def send_appointment(db, clinic, user_id, secret_key, patient, appt, kind, transport=None):
    """Confirm or remind about one booked visit through the API (the buttons on the visit page)."""
    from . import messaging
    if kind not in ("confirmation", "reminder"):
        raise WhatsAppError("Unknown message kind.")
    if appt["status"] != "scheduled" or not appt["start_at"]:
        raise WhatsAppError("Only booked appointments can be messaged.")
    start = parse_dt(appt["start_at"])
    lang = i18n.patient_lang(patient, clinic)
    ctx = {"first_name": patient["first_name"], "clinic": clinic["name"], "dentist": appt["provider_name"] or i18n.t(lang, "our team"),
           "day": i18n.fmt_day_date(start, lang), "time": i18n.fmt_time(start, lang), "review_link": ""}
    text = messaging.appointment_text(clinic, patient, appt, kind, db)
    mid = deliver(db, clinic, user_id, secret_key, patient, kind, "care", ctx, text, appt["id"], transport)
    if appt["confirmation"] == "none":
        db.execute("UPDATE appointments SET confirmation = 'sent', updated_at = datetime('now') WHERE id = ? AND clinic_id = ?", (appt["id"], clinic["id"]))
    return mid


def deliver_outbox(db, clinic, user_id, secret_key, outbox_id, transport=None):
    """Send one item from the send list through the API and finish it the same way the manual button does."""
    from . import outbox as OB, recalls, plans as PL
    o = OB.get(db, clinic["id"], outbox_id)
    if not o:
        raise LookupError("outbox")
    if o["status"] != "queued":
        raise OB.MessageError("That message has already been dealt with.")
    p, ctx = OB.context(db, clinic, o)
    p, text = OB.text_for(db, clinic, o)
    try:
        mid = deliver(db, clinic, user_id, secret_key, p, o["kind"], o["purpose"], ctx, text, o["appointment_id"], transport)
    except WhatsAppError as exc:
        raise OB.MessageError(str(exc))
    db.execute("UPDATE outbox SET status = 'sent', message_id = ?, done_at = datetime('now'), done_by = ? WHERE id = ?", (mid, user_id, outbox_id))
    if o["kind"] == "reminder" and o["appointment_id"]:
        db.execute("UPDATE appointments SET confirmation = 'sent', updated_at = datetime('now') WHERE id = ? AND clinic_id = ? AND confirmation = 'none'",
                   (o["appointment_id"], clinic["id"]))
    elif o["kind"] == "recall" and o["recall_id"]:
        recalls.mark_contacted(db, clinic, o["recall_id"])
    elif o["kind"] == "plan_followup" and o["plan_id"]:
        PL.log_followup(db, clinic, {"id": user_id}, o["plan_id"], "Message sent by WhatsApp", 7)
    return mid


def send_ready(db, clinic, user_id, secret_key, limit=50, transport=None):
    """Send every queued message that can go by the API. A message that cannot (no template, no consent) stays in the list for a person.
    Stops at the first network failure, so one outage does not burn through the list. Returns {'sent': n, 'left': n, 'stopped': text or None}."""
    from . import outbox as OB
    sent = left = 0
    stopped = None
    ids = [r["id"] for r in db.execute("SELECT id FROM outbox WHERE clinic_id = ? AND status = 'queued' ORDER BY due_on, id LIMIT ?", (clinic["id"], limit))]
    for oid in ids:
        if stopped:
            left += 1
            continue
        try:
            deliver_outbox(db, clinic, user_id, secret_key, oid, transport)
            sent += 1
        except OB.MessageError as exc:
            left += 1
            if "Could not reach" in str(exc):
                stopped = str(exc)
        except Exception:                                  # a bad row must not stop the rest
            left += 1
    return {"sent": sent, "left": left, "stopped": stopped}


# ----------------------------------------------------------------------------- what Meta sends us
def verify_signature(app_secret, raw, header):
    """True only if the header is sha256=<hex> and matches the HMAC-SHA256 of the raw body made with the app secret."""
    if not app_secret or not header or not header.startswith("sha256="):
        return False
    want = hmac.new(app_secret.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(want, header[7:].strip().lower())


def app_secret(db, clinic_id, secret_key):
    r = row(db, clinic_id)
    return _open(secret_key, r["app_secret"]) if r and r["app_secret"] else None


def _seen(db, clinic_id, key, kind, detail=None):
    """Record a webhook item. False if we have already handled it (Meta retries, so repeats are normal)."""
    cur = db.execute("INSERT OR IGNORE INTO wa_events(clinic_id, event_key, kind, detail) VALUES (?,?,?,?)", (clinic_id, key, kind, detail))
    return cur.rowcount == 1


def _patient_for(db, clinic_id, wa_id):
    digits = re.sub(r"\D", "", wa_id or "")
    if not digits:
        return None
    rows = db.execute("""SELECT p.id, p.status, (SELECT MAX(m.id) FROM messages m WHERE m.clinic_id = p.clinic_id AND m.patient_id = p.id AND m.direction = 'out') AS last_out
                         FROM patients p WHERE p.clinic_id = ? AND REPLACE(p.phone, '+', '') = ?
                         ORDER BY (p.status = 'active') DESC, last_out DESC, p.id""", (clinic_id, digits)).fetchall()
    return rows[0]["id"] if rows else None


def _status(db, clinic_id, item):
    wamid, status = item.get("id"), item.get("status")
    if not wamid or status not in ("sent", "delivered", "read", "failed") or not _seen(db, clinic_id, f"st:{wamid}:{status}", "status"):
        return 0
    m = db.execute("SELECT * FROM messages WHERE clinic_id = ? AND provider_id = ?", (clinic_id, wamid)).fetchone()
    if not m:
        return 0
    if status == "failed":
        if m["status"] == "read":
            return 0
        errs = item.get("errors") or [{}]
        code = str(errs[0].get("code", ""))[:20]
        title = str(errs[0].get("title") or errs[0].get("message") or "")[:150]
        db.execute("UPDATE messages SET status = 'failed', error = ? WHERE id = ?", ((code + " " + title).strip() or "failed", m["id"]))
        if m["appointment_id"]:
            db.execute("UPDATE appointments SET confirmation = 'failed', updated_at = datetime('now') WHERE id = ? AND clinic_id = ? AND confirmation = 'sent'",
                       (m["appointment_id"], clinic_id))
        return 1
    if RANK.get(status, 0) > RANK.get(m["status"], 0):
        db.execute("UPDATE messages SET status = ?, error = NULL WHERE id = ?", (status, m["id"]))
        return 1
    return 0


def _inbound(db, clinic, item):
    wamid = item.get("id")
    if not wamid:
        return 0
    kind = item.get("type") or "unknown"
    if kind == "text":
        text = ((item.get("text") or {}).get("body") or "")
    elif kind == "button":
        text = ((item.get("button") or {}).get("text") or "")
    elif kind == "interactive":
        inter = item.get("interactive") or {}
        text = ((inter.get("button_reply") or inter.get("list_reply") or {}).get("title") or "")
    else:
        text = ""
    text = text.strip()[:1000] or f"[{kind} received]"
    sender = item.get("from")
    pid = _patient_for(db, clinic["id"], sender)
    digits = re.sub(r"\D", "", sender or "")
    if not _seen(db, clinic["id"], f"in:{wamid}", "message" if pid else "unmatched", None if pid else f"+{digits}: {text[:200]}"):
        return 0
    if not pid:
        return 1
    db.execute("INSERT INTO messages(clinic_id, patient_id, kind, direction, body, status, provider_id) VALUES (?,?, 'reply', 'in', ?, 'received', ?)",
               (clinic["id"], pid, text, wamid))
    if text.lower().strip(" .!") in STOP_WORDS:
        db.execute("UPDATE patients SET reminders_ok = 0, offers_ok = 0 WHERE id = ? AND clinic_id = ?", (pid, clinic["id"]))
        audit.log(db, clinic["id"], None, "consent.withdrawn", "patient", pid, "asked to stop by WhatsApp")
    return 1


def handle(db, clinic, payload):
    """Process one webhook body for one clinic. Returns {'statuses': n, 'messages': n}. Safe to run twice on the same body."""
    out = {"statuses": 0, "messages": 0}
    if not isinstance(payload, dict):
        return out
    r = row(db, clinic["id"])
    for entry in payload.get("entry") or []:
        for change in (entry or {}).get("changes") or []:
            value = (change or {}).get("value") or {}
            if (change or {}).get("field") != "messages":
                continue
            if str((value.get("metadata") or {}).get("phone_number_id", r["phone_number_id"])) != str(r["phone_number_id"]):
                continue                                   # another number: not ours
            for s in value.get("statuses") or []:
                out["statuses"] += _status(db, clinic["id"], s or {})
            for msg in value.get("messages") or []:
                out["messages"] += _inbound(db, clinic, msg or {})
    return out


def unmatched(db, clinic_id, limit=20):
    return db.execute("SELECT * FROM wa_events WHERE clinic_id = ? AND kind = 'unmatched' ORDER BY id DESC LIMIT ?", (clinic_id, limit)).fetchall()


def clean_events(db, days=30):
    """Forget old webhook bookkeeping. Meta stops retrying after 7 days, so 30 is plenty."""
    return db.execute("DELETE FROM wa_events WHERE created_at < datetime('now', ?)", (f"-{int(days)} days",)).rowcount
