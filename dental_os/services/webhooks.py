"""Webhooks: when something happens, we tell an address the clinic chose. Built to be safe by default:
- Only https addresses, and never to private or internal networks (checked again when sending, with the address pinned, so a
  name that later points inside the network cannot be used to reach it).
- Each message is signed, so the receiver can tell it came from us and was not changed.
- Failures retry on a schedule, then stop. Nothing blocks the clinic's own work: messages are queued and sent by a separate
  command (deliver-webhooks) run every minute or so.
- Messages carry the same limited fields as the API. No clinical notes, medical history or message text.
"""
import hashlib
import hmac
import http.client
import ipaddress
import json
import secrets
import socket
import ssl
import time
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from . import audit

EVENTS = {
    "patient.created": "A patient was added",
    "patient.updated": "A patient's details changed",
    "appointment.booked": "An appointment was booked or moved",
    "appointment.updated": "An appointment was confirmed, completed, missed or cancelled",
    "payment.received": "A payment was recorded",
    "invoice.issued": "An invoice was issued",
    "plan.accepted": "A patient accepted a treatment plan",
    "booking.requested": "Someone asked for a visit on the online booking page",
}
RETRY_MINUTES = [1, 5, 30, 120, 720]          # after attempt 1, 2, 3, 4, 5. A sixth failure ends it.
MAX_BACKLOG = 5000
MAX_ENDPOINTS = 10
TIMEOUT = 6


class WebhookError(Exception):
    pass


class DeliveryError(Exception):
    pass


# ---------------------------------------------------------------- addresses
def _is_public(ip):
    try:
        return ipaddress.ip_address(ip).is_global
    except ValueError:
        return False


def check_url(url, allow_private=False):
    url = (url or "").strip()
    if len(url) > 300:
        raise WebhookError("That address is too long.")
    parts = urlsplit(url)
    if parts.scheme not in (("https", "http") if allow_private else ("https",)):
        raise WebhookError("The address must start with https://")
    if not parts.hostname or parts.username or parts.password:
        raise WebhookError("The address needs a host name and no user name or password in it.")
    try:
        parts.port
    except ValueError:
        raise WebhookError("That port number is not valid.")
    host = parts.hostname.lower()
    if not allow_private:
        try:
            ipaddress.ip_address(host)
            literal = True
        except ValueError:
            literal = False
        if literal and not _is_public(host):
            raise WebhookError("That address points to a private network, which is not allowed.")
        if host == "localhost" or host.endswith((".local", ".internal", ".localhost")):
            raise WebhookError("That address points to a private network, which is not allowed.")
    return url


def _events(selected):
    chosen = [e for e in (selected or []) if e in EVENTS]
    return "*" if not chosen or len(chosen) == len(EVENTS) else ",".join(sorted(set(chosen)))


def add_endpoint(db, clinic, user_id, url, selected=None, note=None, allow_private=False):
    cid = clinic["id"]
    url = check_url(url, allow_private)
    if db.execute("SELECT COUNT(*) FROM webhook_endpoints WHERE clinic_id = ? AND active = 1", (cid,)).fetchone()[0] >= MAX_ENDPOINTS:
        raise WebhookError(f"You can have up to {MAX_ENDPOINTS} active addresses.")
    secret = "whsec_" + secrets.token_urlsafe(30)
    cur = db.execute("INSERT INTO webhook_endpoints(clinic_id, url, secret, events, note, created_by) VALUES (?,?,?,?,?,?)",
                     (cid, url, secret, _events(selected), (note or "").strip()[:100] or None, user_id))
    audit.log(db, cid, user_id, "webhook.add", "webhook", cur.lastrowid)
    return cur.lastrowid, secret


def get_endpoint(db, cid, endpoint_id):
    return db.execute("SELECT * FROM webhook_endpoints WHERE id = ? AND clinic_id = ?", (endpoint_id, cid)).fetchone()


def endpoints(db, cid):
    return db.execute("SELECT * FROM webhook_endpoints WHERE clinic_id = ? ORDER BY active DESC, id", (cid,)).fetchall()


def set_active(db, clinic, user_id, endpoint_id, active):
    cur = db.execute("UPDATE webhook_endpoints SET active = ? WHERE id = ? AND clinic_id = ?", (1 if active else 0, endpoint_id, clinic["id"]))
    if not cur.rowcount:
        raise LookupError("webhook")
    audit.log(db, clinic["id"], user_id, "webhook.on" if active else "webhook.off", "webhook", endpoint_id)


def rotate_secret(db, clinic, user_id, endpoint_id):
    secret = "whsec_" + secrets.token_urlsafe(30)
    cur = db.execute("UPDATE webhook_endpoints SET secret = ? WHERE id = ? AND clinic_id = ?", (secret, endpoint_id, clinic["id"]))
    if not cur.rowcount:
        raise LookupError("webhook")
    audit.log(db, clinic["id"], user_id, "webhook.secret", "webhook", endpoint_id)
    return secret


# ---------------------------------------------------------------- queueing
def _wants(endpoint, event):
    return endpoint["events"] == "*" or event in endpoint["events"].split(",")


def _envelope(clinic_id, event, data):
    return json.dumps({"id": "evt_" + uuid.uuid4().hex, "event": event, "clinic_id": clinic_id,
                       "created": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "data": data}, separators=(",", ":"))


def emit(db, clinic_id, event, data):
    """Queue a message for every endpoint that wants this event. Cheap: no network here."""
    rows = db.execute("SELECT id, events FROM webhook_endpoints WHERE clinic_id = ? AND active = 1", (clinic_id,)).fetchall()
    for ep in rows:
        if not _wants(ep, event):
            continue
        if db.execute("SELECT COUNT(*) FROM webhook_deliveries WHERE endpoint_id = ? AND status = 'pending'", (ep["id"],)).fetchone()[0] >= MAX_BACKLOG:
            continue
        db.execute("INSERT INTO webhook_deliveries(clinic_id, endpoint_id, event, payload) VALUES (?,?,?,?)",
                   (clinic_id, ep["id"], event, _envelope(clinic_id, event, data)))


def send_test(db, clinic, user_id, endpoint_id):
    ep = get_endpoint(db, clinic["id"], endpoint_id)
    if not ep:
        raise LookupError("webhook")
    db.execute("INSERT INTO webhook_deliveries(clinic_id, endpoint_id, event, payload) VALUES (?,?,?,?)",
               (clinic["id"], endpoint_id, "ping", _envelope(clinic["id"], "ping", {"message": "This is a test from Dental OS."})))
    audit.log(db, clinic["id"], user_id, "webhook.test", "webhook", endpoint_id)


def deliveries(db, cid, endpoint_id=None, limit=40):
    sql = "SELECT d.*, e.url FROM webhook_deliveries d JOIN webhook_endpoints e ON e.id = d.endpoint_id WHERE d.clinic_id = ?"
    args = [cid]
    if endpoint_id:
        sql += " AND d.endpoint_id = ?"
        args.append(endpoint_id)
    return db.execute(sql + " ORDER BY d.id DESC LIMIT ?", (*args, limit)).fetchall()


def retry(db, clinic, user_id, delivery_id):
    cur = db.execute("""UPDATE webhook_deliveries SET status = 'pending', next_try_at = datetime('now'), attempts = 0, error = NULL
                        WHERE id = ? AND clinic_id = ? AND status = 'dead'""", (delivery_id, clinic["id"]))
    if not cur.rowcount:
        raise WebhookError("Only a message that has stopped trying can be retried.")
    audit.log(db, clinic["id"], user_id, "webhook.retry", "webhook_delivery", delivery_id)


# ---------------------------------------------------------------- sending
def sign(secret, timestamp, body):
    return hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()


def verify(secret, header, body, tolerance=300, now=None):
    """For receivers (and our tests): check an 'X-DentalOS-Signature' header of the form 't=...,v1=...'."""
    try:
        bits = dict(p.split("=", 1) for p in header.split(","))
        ts = int(bits["t"])
    except (ValueError, KeyError, AttributeError):
        return False
    if abs((now or time.time()) - ts) > tolerance:
        return False
    return hmac.compare_digest(sign(secret, ts, body), bits.get("v1", ""))


def https_post(url, body, headers, allow_private=False):
    """POST once. Resolve the name, refuse private addresses, connect to the address we checked, do not follow redirects."""
    parts = urlsplit(url)
    host, port = parts.hostname, parts.port or (443 if parts.scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        raise DeliveryError("Could not find that address.")
    ips = [i[4][0] for i in infos]
    if not ips:
        raise DeliveryError("Could not find that address.")
    if not allow_private and not all(_is_public(ip) for ip in ips):
        raise DeliveryError("That address points to a private network.")
    ip = ips[0]
    path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")

    if parts.scheme == "https":
        class Pinned(http.client.HTTPSConnection):
            def connect(self):
                sock = socket.create_connection((ip, port), self.timeout)
                self.sock = self._context.wrap_socket(sock, server_hostname=host)
        conn = Pinned(host, port, timeout=TIMEOUT, context=ssl.create_default_context())
    else:
        conn = http.client.HTTPConnection(ip, port, timeout=TIMEOUT)
        headers = {**headers, "Host": parts.netloc}
    try:
        conn.request("POST", path, body=body, headers=headers)
        resp = conn.getresponse()
        resp.read(65536)
        return resp.status
    except (OSError, http.client.HTTPException, ssl.SSLError) as exc:
        raise DeliveryError(f"{type(exc).__name__}")
    finally:
        conn.close()


def _now():
    return datetime.utcnow()


def deliver_due(db, post=None, allow_private=False, limit=50, now=None):
    """Send what is due. Returns counts. `post` can be swapped for tests."""
    post = post or https_post
    now = now or _now()
    stamp = now.strftime("%Y-%m-%d %H:%M:%S")
    rows = db.execute("SELECT * FROM webhook_deliveries WHERE status = 'pending' AND next_try_at <= ? ORDER BY id LIMIT ?", (stamp, limit)).fetchall()
    done = {"sent": 0, "retry": 0, "dead": 0}
    for d in rows:
        ep = db.execute("SELECT * FROM webhook_endpoints WHERE id = ?", (d["endpoint_id"],)).fetchone()
        attempts = d["attempts"] + 1
        if not ep or not ep["active"]:
            db.execute("UPDATE webhook_deliveries SET status='dead', error='The address is switched off', attempts=? WHERE id=?", (attempts, d["id"]))
            done["dead"] += 1
            continue
        body = d["payload"].encode()
        ts = int(now.replace(tzinfo=timezone.utc).timestamp())
        headers = {"Content-Type": "application/json", "User-Agent": "DentalOS-Webhook/1", "X-DentalOS-Event": d["event"],
                   "X-DentalOS-Delivery": str(d["id"]), "X-DentalOS-Signature": f"t={ts},v1={sign(ep['secret'], ts, body)}"}
        code, error = None, None
        try:
            check_url(ep["url"], allow_private)
            code = post(ep["url"], body, headers, allow_private)
            if not 200 <= code < 300:
                error = f"The receiver answered {code}"
        except (WebhookError, DeliveryError) as exc:
            error = str(exc)
        if error is None:
            db.execute("UPDATE webhook_deliveries SET status='sent', attempts=?, response_code=?, error=NULL, delivered_at=datetime('now') WHERE id=?",
                       (attempts, code, d["id"]))
            done["sent"] += 1
        elif attempts > len(RETRY_MINUTES):
            db.execute("UPDATE webhook_deliveries SET status='dead', attempts=?, response_code=?, error=? WHERE id=?", (attempts, code, error, d["id"]))
            done["dead"] += 1
        else:
            nxt = (now + timedelta(minutes=RETRY_MINUTES[attempts - 1])).strftime("%Y-%m-%d %H:%M:%S")
            db.execute("UPDATE webhook_deliveries SET attempts=?, response_code=?, error=?, next_try_at=? WHERE id=?", (attempts, code, error, nxt, d["id"]))
            done["retry"] += 1
    db.execute("DELETE FROM webhook_deliveries WHERE status = 'sent' AND delivered_at < datetime('now', '-30 days')")
    db.execute("DELETE FROM webhook_deliveries WHERE status = 'dead' AND created_at < datetime('now', '-60 days')")
    return done


def redact_patient(db, clinic_id, patient_id):
    """Messages queued or kept for a person who has just been anonymised must not carry their details. Waiting ones are cancelled
    and every message about the person has its content removed. Returns how many were touched."""
    n = 0
    like_a, like_b, like_c = f'%"patient_id":{int(patient_id)},%', f'%"patient_id":{int(patient_id)}}}%', f'%"data":{{"id":{int(patient_id)},%'
    rows = db.execute("""SELECT id, event, payload FROM webhook_deliveries WHERE clinic_id = ? AND payload != '{}'
                         AND (payload LIKE ? OR payload LIKE ? OR (event LIKE 'patient.%' AND payload LIKE ?))""",
                      (clinic_id, like_a, like_b, like_c)).fetchall()
    for r in rows:
        try:
            data = json.loads(r["payload"]).get("data") or {}
        except ValueError:
            continue
        mine = data.get("patient_id") == patient_id or (r["event"].startswith("patient.") and data.get("id") == patient_id)
        if not mine:
            continue
        db.execute("""UPDATE webhook_deliveries SET payload = '{}', error = 'Content removed: the person was anonymised',
                      status = CASE WHEN status = 'pending' THEN 'dead' ELSE status END WHERE id = ?""", (r["id"],))
        n += 1
    return n
