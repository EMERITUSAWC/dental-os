"""Taking payments online: a pay link a patient opens on their phone, paid by card or mobile money through the clinic's own account
with a payment company. Two companies are built in:

- Paystack, for cards and mobile money in Ghana, Nigeria, Kenya and South Africa (and US dollars).
- Stripe, for cards in the UK, Europe, the US and many other countries.

The clinic opens the account with the company and pastes its secret key here. The key is stored encrypted, never shown again, and
never written to the audit log. A test key (it starts sk_test_) works against the company's test system, so the whole flow can be
tried with test cards before any real money moves.

When a patient pays, the payment is checked with the company before it counts: either the company's signed message to our webhook
address, or our own look-up of the payment when the patient comes back to the clinic's page. The amount and currency must match the
link exactly. A paid link records one normal payment on the patient's account with a receipt; doing the check twice never records it
twice. This module has no web framework in it. The network call is passed in (`transport`) so tests never touch the internet."""
import base64
import datetime
import hashlib
import hmac
import json
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request

from . import audit, billing
from .timeutil import now_local

PROVIDERS = {"paystack": "Paystack (cards and mobile money in Ghana, Nigeria, Kenya, South Africa)",
             "stripe": "Stripe (cards in the UK, Europe, the US and many other countries)"}
PAYSTACK_CURRENCIES = ("GHS", "NGN", "KES", "ZAR", "USD")
PAYSTACK_API = "https://api.paystack.co"
STRIPE_API = "https://api.stripe.com/v1"
STRIPE_TOLERANCE = 300                     # seconds a signed Stripe message stays valid
LINK_DAYS = 7
MAX_BODY = 512 * 1024
STATUSES = {"open": "Waiting for payment", "paid": "Paid", "cancelled": "Cancelled", "expired": "Expired", "failed": "Failed"}


class PayError(Exception):
    pass


# ----------------------------------------------------------------------------------------------------------- secrets
def _fernet(secret_key):
    from cryptography.fernet import Fernet
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(b"dental-os/onlinepay/" + str(secret_key).encode()).digest()))


def _seal(secret_key, value):
    return _fernet(secret_key).encrypt(value.encode()).decode()


def _open(secret_key, value):
    from cryptography.fernet import InvalidToken
    try:
        return _fernet(secret_key).decrypt(value.encode()).decode()
    except (InvalidToken, ValueError, AttributeError):
        return None


# ----------------------------------------------------------------------------------------------------------- the account
def account(db, cid):
    return db.execute("SELECT * FROM pay_accounts WHERE clinic_id = ?", (cid,)).fetchone()


def is_on(db, clinic):
    a = account(db, clinic["id"])
    return bool(a and a["enabled"] and a["secret_key"])


def test_mode(a):
    return bool(a and a["key_hint"] and "test" in a["key_hint"])


def set_allow_save(db, clinic, user_id, on):
    """Offer patients the choice to keep their card on file. Off by default."""
    if not account(db, clinic["id"]):
        raise PayError("Set up online payments first.")
    db.execute("UPDATE pay_accounts SET allow_save = ? WHERE clinic_id = ?", (1 if on else 0, clinic["id"]))
    audit.log(db, clinic["id"], user_id, "pay.allow_save", None, None, "on" if on else "off")


def save_account(db, clinic, user_id, secret_key, provider, enabled, api_key=None, webhook_secret=None, new_hook=False):
    cid = clinic["id"]
    if provider not in PROVIDERS:
        raise PayError("Pick a payment company from the list.")
    api_key = (api_key or "").strip()
    webhook_secret = (webhook_secret or "").strip()
    if api_key:
        if provider == "paystack" and not (api_key.startswith("sk_live_") or api_key.startswith("sk_test_")):
            raise PayError("A Paystack secret key starts sk_live_ or sk_test_.")
        if provider == "stripe" and not (api_key.startswith("sk_live_") or api_key.startswith("sk_test_") or api_key.startswith("rk_")):
            raise PayError("A Stripe secret key starts sk_live_, sk_test_ or rk_.")
        if len(api_key) < 20 or " " in api_key:
            raise PayError("That key does not look right.")
    if webhook_secret and not webhook_secret.startswith("whsec_"):
        raise PayError("A Stripe signing secret starts whsec_.")
    if provider == "paystack" and clinic["currency"] not in PAYSTACK_CURRENCIES:
        raise PayError("Paystack does not take this clinic's currency. Use Stripe, or change the currency.")
    a = account(db, cid)
    if not a:
        db.execute("INSERT INTO pay_accounts(clinic_id, provider, hook_key) VALUES (?,?,?)", (cid, provider, secrets.token_urlsafe(18)))
        a = account(db, cid)
    same = a["provider"] == provider
    if api_key:
        key_val, hint = _seal(secret_key, api_key), api_key[:8] + "…" + api_key[-4:]
    elif same:
        key_val, hint = a["secret_key"], a["key_hint"]
    else:
        key_val, hint = None, None                                 # a key for one company is no use for the other
    wh = _seal(secret_key, webhook_secret) if webhook_secret else (a["webhook_secret"] if same else None)
    on = 1 if (enabled and key_val) else 0
    db.execute("""UPDATE pay_accounts SET provider = ?, secret_key = ?, key_hint = ?, webhook_secret = ?, enabled = ?,
                  hook_key = ?, updated_at = datetime('now') WHERE clinic_id = ?""",
               (provider, key_val, hint, wh, on, secrets.token_urlsafe(18) if new_hook else a["hook_key"], cid))
    audit.log(db, cid, user_id, "pay.account", None, None, f"{provider} {'on' if on else 'off'}")   # the key itself is never logged
    if enabled and not key_val:
        raise PayError("Paste the secret key before switching online payments on.")


def _key(db, cid, secret_key):
    a = account(db, cid)
    if not a or not a["enabled"] or not a["secret_key"]:
        raise PayError("Online payments are not switched on.")
    k = _open(secret_key, a["secret_key"])
    if not k:
        raise PayError("The payment key could not be read. A manager should paste it again under Settings.")
    return a, k


# ------------------------------------------------------------------------------------------------------------ the network
def default_transport(method, url, headers, body=None, timeout=15):
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:            # nosec: https to the payment company only, built here
            return resp.status, resp.read(200000).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(200000).decode("utf-8", "replace")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise PayError("Could not reach the payment company: %s" % (getattr(exc, "reason", None) or exc.__class__.__name__))


def _json(status, text, who):
    try:
        data = json.loads(text) if text else {}
    except ValueError:
        data = {}
    if status >= 300:
        msg = (data.get("message") if who == "paystack" else (data.get("error") or {}).get("message")) or f"HTTP {status}"
        raise PayError(f"{who.title()} refused it: {str(msg)[:200]}")
    return data


def _paystack(key, method, path, payload=None, transport=None):
    headers = {"Authorization": "Bearer " + key, "Content-Type": "application/json"}
    status, text = (transport or default_transport)(method, PAYSTACK_API + path, headers, json.dumps(payload).encode() if payload is not None else None)
    data = _json(status, text, "paystack")
    if data.get("status") is False:
        raise PayError("Paystack refused it: %s" % str(data.get("message"))[:200])
    return data.get("data") or {}


def _stripe(key, method, path, form=None, transport=None):
    headers = {"Authorization": "Bearer " + key, "Content-Type": "application/x-www-form-urlencoded"}
    body = urllib.parse.urlencode(form).encode() if form is not None else None
    status, text = (transport or default_transport)(method, STRIPE_API + path, headers, body)
    return _json(status, text, "stripe")


def check(db, clinic, secret_key, transport=None):
    """Ask the company whether the key works. Returns a short sentence."""
    a, k = _key(db, clinic["id"], secret_key)
    if a["provider"] == "paystack":
        _paystack(k, "GET", "/transaction?perPage=1", None, transport)
    else:
        _stripe(k, "GET", "/balance", None, transport)
    return "The key works" + (" (test mode: no real money moves)." if test_mode(a) else ".")


# ------------------------------------------------------------------------------------------------------------- pay links
def get_link(db, ref):
    return db.execute("""SELECT l.*, p.first_name, p.last_name, p.email, p.language AS patient_language FROM pay_links l
                         JOIN patients p ON p.id = l.patient_id WHERE l.ref = ?""", (ref,)).fetchone()


def links_for(db, cid, pid=None, status=None, limit=100):
    q = ("SELECT l.*, p.first_name, p.last_name FROM pay_links l JOIN patients p ON p.id = l.patient_id WHERE l.clinic_id = ?")
    args = [cid]
    if pid:
        q += " AND l.patient_id = ?"
        args.append(pid)
    if status:
        q += " AND l.status = ?"
        args.append(status)
    return db.execute(q + " ORDER BY l.id DESC LIMIT ?", args + [limit]).fetchall()


def _expire(db, clinic):
    now = now_local(clinic["timezone"]).strftime("%Y-%m-%d %H:%M")
    db.execute("UPDATE pay_links SET status = 'expired' WHERE clinic_id = ? AND status = 'open' AND expires_at < ?", (clinic["id"], now))


def create_link(db, clinic, user_id, secret_key, pid, amount_p, description, base_url, purpose="treatment", transport=None):
    """Make a link with the company and keep it. base_url is where the patient comes back, for example https://clinic.example.com"""
    cid = clinic["id"]
    a, k = _key(db, cid, secret_key)
    patient = db.execute("SELECT * FROM patients WHERE id = ? AND clinic_id = ? AND status = 'active'", (pid, cid)).fetchone()
    if not patient:
        raise PayError("That patient was not found, or is archived.")
    if not amount_p or amount_p <= 0:
        raise PayError("The amount must be more than zero.")
    if purpose not in ("treatment", "membership"):
        raise PayError("Unknown kind of payment.")
    description = (description or "").strip()[:120] or f"{clinic['name']}: payment"
    ref = "dos_" + secrets.token_urlsafe(16)
    back = base_url.rstrip("/") + f"/pay/{ref}/done"
    email = (patient["email"] or clinic["email"] or "").strip()
    if a["provider"] == "paystack":
        if not email:
            raise PayError("Paystack needs an email address. Add one to the patient's record, or the clinic's email under Settings.")
        data = _paystack(k, "POST", "/transaction/initialize", {
            "email": email, "amount": amount_p, "currency": clinic["currency"], "reference": ref, "callback_url": back,
            "metadata": {"clinic_id": cid, "patient_id": pid, "purpose": purpose}}, transport)
        url, provider_ref = data.get("authorization_url"), data.get("reference") or ref
    else:
        form = {"mode": "payment", "success_url": back, "cancel_url": back, "client_reference_id": ref,
                "line_items[0][quantity]": "1", "line_items[0][price_data][currency]": clinic["currency"].lower(),
                "line_items[0][price_data][unit_amount]": str(amount_p), "line_items[0][price_data][product_data][name]": description,
                "metadata[ref]": ref, "metadata[clinic_id]": str(cid)}
        if email:
            form["customer_email"] = email
        data = _stripe(k, "POST", "/checkout/sessions", form, transport)
        url, provider_ref = data.get("url"), data.get("id")
    if not url or not str(url).startswith("https://"):
        raise PayError("The payment company did not give back a payment page.")
    expires = (now_local(clinic["timezone"]) + datetime.timedelta(days=LINK_DAYS)).strftime("%Y-%m-%d %H:%M")
    lid = db.execute("""INSERT INTO pay_links(clinic_id, patient_id, ref, provider, amount_p, currency, description, purpose, checkout_url,
                        provider_ref, created_by, expires_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                     (cid, pid, ref, a["provider"], amount_p, clinic["currency"], description, purpose, url, provider_ref, user_id, expires)).lastrowid
    audit.log(db, cid, user_id, "pay.link", "patient", pid, f"{a['provider']} {amount_p}")
    return ref


def cancel_link(db, clinic, user_id, ref):
    link = get_link(db, ref)
    if not link or link["clinic_id"] != clinic["id"]:
        raise PayError("That pay link was not found.")
    if link["status"] != "open":
        raise PayError("Only a link that is still waiting can be cancelled.")
    db.execute("UPDATE pay_links SET status = 'cancelled' WHERE id = ?", (link["id"],))
    audit.log(db, clinic["id"], user_id, "pay.link.cancel", "patient", link["patient_id"])


def _record(db, clinic, link, amount_p, currency, channel, provider_ref):
    """Record the payment once. Returns the payment id."""
    if link["payment_id"]:
        return link["payment_id"]
    if amount_p != link["amount_p"] or (currency or "").upper() != link["currency"]:
        db.execute("UPDATE pay_links SET status = 'failed', error = ? WHERE id = ?",
                   (f"Paid {amount_p} {currency}, expected {link['amount_p']} {link['currency']}", link["id"]))
        audit.log(db, clinic["id"], None, "pay.mismatch", "patient", link["patient_id"])
        raise PayError("The amount paid does not match the link. Check it with the payment company.")
    method = "mobile_money" if channel == "mobile_money" else "card"
    pay_id = billing.record_payment(db, clinic, None, link["patient_id"], amount_p, method, provider_ref or link["ref"],
                                    f"Paid online ({link['provider'].title()})", purpose=link["purpose"], online=True)
    db.execute("UPDATE pay_links SET status = 'paid', paid_at = datetime('now'), payment_id = ?, channel = ? WHERE id = ?",
               (pay_id, channel, link["id"]))
    return pay_id


def confirm(db, clinic, secret_key, ref, transport=None):
    """Look the payment up with the company (used when the patient comes back). Returns the link's status."""
    link = get_link(db, ref)
    if not link or link["clinic_id"] != clinic["id"]:
        raise PayError("That pay link was not found.")
    if link["status"] != "open":
        return link["status"]
    a, k = _key(db, clinic["id"], secret_key)
    if link["provider"] == "paystack":
        data = _paystack(k, "GET", "/transaction/verify/" + urllib.parse.quote(link["ref"]), None, transport)
        if data.get("status") == "success":
            _record(db, clinic, link, int(data.get("amount") or 0), data.get("currency"), data.get("channel"), data.get("reference"))
            _save_card(db, clinic, link, secret_key, data, None, transport)
            return "paid"
        return "open"
    data = _stripe(k, "GET", "/checkout/sessions/" + urllib.parse.quote(link["provider_ref"] or ""), None, transport)
    if data.get("payment_status") == "paid" and data.get("client_reference_id") == link["ref"]:
        _record(db, clinic, link, int(data.get("amount_total") or 0), data.get("currency"), "card", data.get("payment_intent") or data.get("id"))
        _save_card(db, clinic, link, secret_key, None, data, transport)
        return "paid"
    return "open"


def _save_card(db, clinic, link, secret_key, paystack_data, stripe_session, transport=None):
    """Keep the card if the patient asked. A problem here never undoes the payment itself."""
    if not link["save_card"]:
        return
    from . import savedcards as SC
    try:
        if paystack_data is not None:
            SC.capture_paystack(db, clinic, link, secret_key, paystack_data)
        else:
            SC.capture_stripe(db, clinic, link, secret_key, stripe_session, transport)
    except (PayError, SC.CardError, KeyError, ValueError):
        audit.log(db, clinic["id"], None, "card.save_failed", "patient", link["patient_id"])


# ---------------------------------------------------------------------------------------------------------------- webhooks
def account_for_hook(db, hook_key):
    return db.execute("SELECT * FROM pay_accounts WHERE hook_key = ?", (hook_key,)).fetchone()


def paystack_signature_ok(key, raw, header):
    want = hmac.new(key.encode(), raw, hashlib.sha512).hexdigest()
    return bool(header) and hmac.compare_digest(want, header.strip())


def stripe_signature_ok(whsec, raw, header, now=None):
    parts = {}
    for item in (header or "").split(","):
        k, _, v = item.strip().partition("=")
        parts.setdefault(k, []).append(v)
    try:
        ts = int(parts.get("t", ["0"])[0])
    except ValueError:
        return False
    if abs((now or time.time()) - ts) > STRIPE_TOLERANCE:
        return False
    want = hmac.new(whsec.encode(), f"{ts}.".encode() + raw, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(want, v) for v in parts.get("v1", []))


def handle_hook(db, acct, raw, headers, secret_key, now=None):
    """A message from the company. Returns a short word for the log. Raises PayError if it is not genuine."""
    clinic = db.execute("SELECT * FROM clinics WHERE id = ?", (acct["clinic_id"],)).fetchone()
    if acct["provider"] == "paystack":
        k = _open(secret_key, acct["secret_key"]) if acct["secret_key"] else None
        if not k or not paystack_signature_ok(k, raw, headers.get("X-Paystack-Signature", "")):
            raise PayError("bad signature")
        event = json.loads(raw.decode("utf-8"))
        if event.get("event") != "charge.success":
            return "ignored"
        d = event.get("data") or {}
        link = get_link(db, d.get("reference") or "")
        if not link or link["clinic_id"] != clinic["id"]:
            return "unknown"
        if d.get("status") != "success":
            return "not paid"
        _record(db, clinic, link, int(d.get("amount") or 0), d.get("currency"), d.get("channel"), d.get("reference"))
        _save_card(db, clinic, get_link(db, link["ref"]), secret_key, d, None)
        return "paid"
    whsec = _open(secret_key, acct["webhook_secret"]) if acct["webhook_secret"] else None
    if not whsec or not stripe_signature_ok(whsec, raw, headers.get("Stripe-Signature", ""), now):
        raise PayError("bad signature")
    event = json.loads(raw.decode("utf-8"))
    if event.get("type") != "checkout.session.completed":
        return "ignored"
    s = (event.get("data") or {}).get("object") or {}
    link = get_link(db, s.get("client_reference_id") or "")
    if not link or link["clinic_id"] != clinic["id"]:
        return "unknown"
    if s.get("payment_status") != "paid":
        return "not paid"
    _record(db, clinic, link, int(s.get("amount_total") or 0), s.get("currency"), "card", s.get("payment_intent") or s.get("id"))
    _save_card(db, clinic, get_link(db, link["ref"]), secret_key, None, s)
    return "paid"
