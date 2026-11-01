"""Email as a second channel (version 0.31), through the clinic's own mail account (SMTP).

Used for: visit reminders and the other messages on the send list, for patients whose preferred contact is email; statements (the
private link only, never the amount); a test message from set-up. The same rules as WhatsApp: the patient must have agreed to
that kind of message, an archived patient is never written to, and only the same fields go in. The mail password is stored
encrypted, never shown again and never logged. Addresses and subjects are checked so nothing can add extra mail headers.

This module has no web framework in it. The mail server call is passed in (`transport`), so tests never send real mail."""
import base64
import hashlib
import re
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid

from . import audit, i18n

ADDRESS = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$")
SECURITY = {"starttls": "STARTTLS (usually port 587)", "ssl": "SSL/TLS (usually port 465)", "none": "None (only on a private network)"}
SUBJECTS = {"reminder": "Your appointment", "recall": "Time for your check-up", "review_request": "How was your visit?",
            "birthday": "Happy birthday", "plan_followup": "The treatment we talked about", "campaign": "A message from us",
            "statement": "Your statement is ready", "test": "Test message"}


class MailError(Exception):
    pass


def _fernet(secret_key):
    from cryptography.fernet import Fernet
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(b"dental-os/mail/" + str(secret_key).encode()).digest()))


def _open(secret_key, value):
    from cryptography.fernet import InvalidToken
    try:
        return _fernet(secret_key).decrypt(value.encode()).decode()
    except (InvalidToken, ValueError, AttributeError):
        return None


def valid_address(a):
    return bool(a) and len(a) <= 254 and bool(ADDRESS.match(a))


def account(db, cid):
    return db.execute("SELECT * FROM mail_accounts WHERE clinic_id = ?", (cid,)).fetchone()


def is_on(db, cid):
    a = account(db, cid)
    return bool(a and a["enabled"] and a["host"] and a["from_addr"])


def save_account(db, clinic, user_id, secret_key, form):
    cid = clinic["id"]
    host = (form.get("host") or "").strip()
    if host and not re.fullmatch(r"[A-Za-z0-9.-]{1,253}", host):
        raise MailError("The mail server name looks wrong, for example smtp.example.com.")
    try:
        port = int(str(form.get("port") or "587").strip())
    except ValueError:
        raise MailError("The port is a number, usually 587 or 465.")
    if not 1 <= port <= 65535:
        raise MailError("The port is a number, usually 587 or 465.")
    security = form.get("security") or "starttls"
    if security not in SECURITY:
        raise MailError("Pick how the connection is secured.")
    from_addr = (form.get("from_addr") or "").strip()
    if from_addr and not valid_address(from_addr):
        raise MailError("The sender address does not look like an email address.")
    reply_to = (form.get("reply_to") or "").strip()
    if reply_to and not valid_address(reply_to):
        raise MailError("The reply address does not look like an email address.")
    from_name = re.sub(r"[\r\n<>\"]", "", (form.get("from_name") or "").strip())[:80]
    username = (form.get("username") or "").strip()[:200]
    password = form.get("password") or ""
    enabled = form.get("enabled") == "1"
    a = account(db, cid)
    sealed = _fernet(secret_key).encrypt(password.encode()).decode() if password else (a["password"] if a else None)
    if enabled and not (host and from_addr):
        raise MailError("Fill in the mail server and the sender address first.")
    db.execute("""INSERT INTO mail_accounts(clinic_id, host, port, security, username, password, from_addr, from_name, reply_to, enabled, auto_send)
                  VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(clinic_id) DO UPDATE SET host = excluded.host, port = excluded.port,
                  security = excluded.security, username = excluded.username, password = excluded.password, from_addr = excluded.from_addr,
                  from_name = excluded.from_name, reply_to = excluded.reply_to, enabled = excluded.enabled, auto_send = excluded.auto_send""",
               (cid, host or None, port, security, username or None, sealed, from_addr or None, from_name or None, reply_to or None,
                1 if enabled else 0, 1 if form.get("auto_send") == "1" else 0))
    audit.log(db, cid, user_id, "mail.account", None, None, "on" if enabled else "off")       # the password is never logged


def smtp_transport(a, password, msg):
    """Send one message through the clinic's mail server."""
    ctx = ssl.create_default_context()
    try:
        if a["security"] == "ssl":
            server = smtplib.SMTP_SSL(a["host"], a["port"], timeout=20, context=ctx)
        else:
            server = smtplib.SMTP(a["host"], a["port"], timeout=20)
        with server:
            if a["security"] == "starttls":
                server.starttls(context=ctx)
            if a["username"]:
                server.login(a["username"], password or "")
            server.send_message(msg)
    except smtplib.SMTPAuthenticationError:
        raise MailError("The mail server refused the user name or password.")
    except (smtplib.SMTPException, OSError) as exc:
        raise MailError("Could not reach the mail server: %s" % exc.__class__.__name__)


def build(a, clinic, to, subject, body):
    to = (to or "").strip()
    if not valid_address(to):
        raise MailError("That email address does not look right.")
    subject = re.sub(r"[\r\n]+", " ", subject or "").strip()[:150]
    msg = EmailMessage()
    msg["From"] = formataddr((a["from_name"] or clinic["name"], a["from_addr"]))
    msg["To"] = to
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=False)
    msg["Message-ID"] = make_msgid(domain=a["from_addr"].split("@")[1])
    if a["reply_to"]:
        msg["Reply-To"] = a["reply_to"]
    msg.set_content(body)
    return msg


def send(db, clinic, secret_key, to, subject, body, transport=None):
    a = account(db, clinic["id"])
    if not a or not a["host"] or not a["from_addr"]:
        raise MailError("Email is not set up. A manager sets it up under Settings, Email.")
    msg = build(a, clinic, to, subject, body)
    password = _open(secret_key, a["password"]) if a["password"] else None
    if a["password"] and password is None:
        raise MailError("The mail password could not be read. A manager should type it again under Settings, Email.")
    (transport or smtp_transport)(a, password, msg)
    return msg


def subject_for(kind, clinic, lang):
    return f"{i18n.t(lang, SUBJECTS.get(kind, 'A message from us'))}: {clinic['name']}"


def _footer(clinic, lang):
    return "\n\n" + i18n.t(lang, "Sent by %(clinic)s. To stop these emails, reply and tell us.", clinic=clinic["name"])


def send_test(db, clinic, user_id, secret_key, to, transport=None):
    lang = i18n.clinic_lang(clinic)
    send(db, clinic, secret_key, to, subject_for("test", clinic, lang),
         i18n.t(lang, "This is a test message from Dental OS. Email from the clinic is working."), transport)
    audit.log(db, clinic["id"], user_id, "mail.test")


# ----------------------------------------------------------------------------------------------------- the send list
def deliver_outbox(db, clinic, user_id, secret_key, outbox_id, transport=None):
    """Send one item from the send list by email and finish it the same way the WhatsApp button does."""
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
    if not valid_address(p["email"] or ""):
        raise OB.MessageError("This patient has no email address.")
    lang = i18n.patient_lang(p, clinic)
    try:
        send(db, clinic, secret_key, p["email"], subject_for(o["kind"], clinic, lang), text + _footer(clinic, lang), transport)
    except MailError as exc:
        raise OB.MessageError(str(exc))
    cur = db.execute("""INSERT INTO messages(clinic_id, patient_id, appointment_id, channel, kind, body, status, created_by)
                        VALUES (?,?,?, 'email', ?,?, 'sent', ?)""", (clinic["id"], p["id"], o["appointment_id"], o["kind"], text, user_id))
    db.execute("UPDATE outbox SET status = 'sent', message_id = ?, done_at = datetime('now'), done_by = ? WHERE id = ?", (cur.lastrowid, user_id, outbox_id))
    if o["kind"] == "reminder" and o["appointment_id"]:
        db.execute("UPDATE appointments SET confirmation = 'sent', updated_at = datetime('now') WHERE id = ? AND clinic_id = ? AND confirmation = 'none'",
                   (o["appointment_id"], clinic["id"]))
    elif o["kind"] == "recall" and o["recall_id"]:
        recalls.mark_contacted(db, clinic, o["recall_id"])
    elif o["kind"] == "plan_followup" and o["plan_id"]:
        PL.log_followup(db, clinic, {"id": user_id}, o["plan_id"], "Message sent by email", 7)
    audit.log(db, clinic["id"], user_id, f"email.{o['kind']}", "message", cur.lastrowid)
    return cur.lastrowid


def send_ready(db, clinic, user_id, secret_key, limit=100, transport=None):
    """Email every queued message for patients who prefer email. Stops at the first failure to reach the server."""
    from . import outbox as OB
    sent = left = 0
    stopped = None
    ids = [r["id"] for r in db.execute("""SELECT o.id FROM outbox o JOIN patients p ON p.id = o.patient_id WHERE o.clinic_id = ? AND o.status = 'queued'
                                          AND p.preferred_contact = 'email' AND p.email IS NOT NULL AND p.email != '' ORDER BY o.due_on, o.id LIMIT ?""",
                                       (clinic["id"], limit))]
    for oid in ids:
        if stopped:
            left += 1
            continue
        try:
            deliver_outbox(db, clinic, user_id, secret_key, oid, transport)
            sent += 1
        except OB.MessageError as exc:
            left += 1
            if "Could not reach" in str(exc) or "refused the user name" in str(exc):
                stopped = str(exc)
    return {"sent": sent, "left": left, "stopped": stopped}


# --------------------------------------------------------------------------------------------------------- statements
def send_statement(db, clinic, user_id, secret_key, item_id, portal_url_for, transport=None):
    from . import links, statements as S
    cid = clinic["id"]
    it = db.execute("SELECT * FROM statement_items WHERE id = ? AND clinic_id = ?", (item_id, cid)).fetchone()
    if not it:
        raise MailError("That statement was not found.")
    p = db.execute("SELECT * FROM patients WHERE id = ?", (it["patient_id"],)).fetchone()
    if not p["reminders_ok"]:
        raise MailError("This patient has not agreed to messages. Print the statement or give it in person.")
    if not valid_address(p["email"] or ""):
        raise MailError("This patient has no email address.")
    token = links.create(db, clinic, user_id, p["id"])
    url = portal_url_for(token)
    lang = i18n.patient_lang(p, clinic)
    body = S.STATEMENT_TEXT.get(lang, S.STATEMENT_TEXT["en"]).format(first_name=p["first_name"], clinic=clinic["name"], link=url)
    send(db, clinic, secret_key, p["email"], subject_for("statement", clinic, lang), body + _footer(clinic, lang), transport)
    db.execute("INSERT INTO messages(clinic_id, patient_id, channel, kind, body, status, created_by) VALUES (?,?, 'email', 'statement', ?, 'sent', ?)",
               (cid, p["id"], body.replace(url, "[private link]"), user_id))
    db.execute("UPDATE statement_items SET sent_at = datetime('now'), sent_by = ?, sent_how = 'email' WHERE id = ?", (user_id, item_id))
    audit.log(db, cid, user_id, "statements.email", "patient", p["id"])


def email_run(db, clinic, user_id, secret_key, rid, portal_url_for, transport=None):
    """Email every statement in a run that is not sent yet and has an address. Returns (sent, skipped)."""
    sent = skipped = 0
    for it in db.execute("""SELECT i.id FROM statement_items i JOIN patients p ON p.id = i.patient_id WHERE i.run_id = ? AND i.clinic_id = ?
                            AND i.sent_at IS NULL""", (rid, clinic["id"])).fetchall():
        try:
            send_statement(db, clinic, user_id, secret_key, it["id"], portal_url_for, transport)
            sent += 1
        except MailError as exc:
            if "Could not reach" in str(exc) or "refused the user name" in str(exc):
                raise
            skipped += 1
    return sent, skipped
