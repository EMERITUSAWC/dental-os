"""Saved cards and automatic charges (version 0.32), through the clinic's own Paystack or Stripe account.

The card itself never touches this system: the payment company keeps it and gives back a token (Paystack: an authorization code;
Stripe: a customer and a payment method). Only that token is kept, encrypted, with the card brand, last four digits and expiry so
staff can tell cards apart.

Consent comes first and is the patient's own: on the pay page the patient ticks "keep this card" (and, separately, "use it for my
monthly ortho or membership fees as they fall due"). The wording they agreed to and the time are kept with the card. Staff can then
charge the card for an amount the patient owes; the scheduled job charges only the monthly fees the patient chose. A patient can ask
for a card to be removed at any time, and removing it stops all use at once. The clinic must check its own card terms, and local law,
before switching this on.

Facts used (from Paystack's and Stripe's public API references; check them with test keys before live use):
Paystack returns data.authorization (authorization_code, reusable, last4, exp_month, exp_year, card_type, bank) on a successful
transaction, and POST /transaction/charge_authorization charges it with authorization_code, email, amount and currency.
Stripe saves a card from Checkout when the session has customer_creation=always and payment_intent_data[setup_future_usage]=off_session;
a later charge is POST /v1/payment_intents with customer, payment_method, off_session=true and confirm=true, and a card that needs
the holder to approve it comes back as an error with code authentication_required. No web framework here."""
import datetime
import json
import secrets
import urllib.parse

from . import audit, billing, onlinepay as OP
from .timeutil import now_local

CONSENT_SAVE = "I agree that {clinic} may keep this card with its payment company and charge it for amounts I owe, after telling me."
CONSENT_AUTO = "I also agree that {clinic} may charge it for my monthly orthodontic or membership fees on the day they fall due."


class CardError(Exception):
    pass


def _today(clinic):
    return now_local(clinic["timezone"]).date()


def allowed(db, clinic):
    a = OP.account(db, clinic["id"])
    return bool(a and a["enabled"] and a["secret_key"] and a["allow_save"])


def cards_for(db, cid, pid, active_only=True):
    return db.execute("SELECT * FROM saved_cards WHERE clinic_id = ? AND patient_id = ?" + (" AND status = 'active'" if active_only else "") +
                      " ORDER BY id DESC", (cid, pid)).fetchall()


def expired(card, today):
    if not card["exp_year"] or not card["exp_month"]:
        return False
    return (card["exp_year"], card["exp_month"]) < (today.year, today.month)


# ------------------------------------------------------------------------------------------------------- saving a card
def prepare(db, clinic, secret_key, ref, auto_ok, base_url, transport=None, lang="en"):
    """The patient chose to keep the card. Record their consent; for Stripe, make a checkout that saves the card."""
    link = OP.get_link(db, ref)
    if not link or link["clinic_id"] != clinic["id"] or link["status"] != "open":
        raise CardError("That pay link cannot be used.")
    if not allowed(db, clinic):
        raise CardError("This clinic does not keep cards.")
    from . import i18n
    text = i18n.t(lang, CONSENT_SAVE).replace("{clinic}", clinic["name"]) + (
        (" " + i18n.t(lang, CONSENT_AUTO).replace("{clinic}", clinic["name"])) if auto_ok else "")      # the words the patient saw
    url, provider_ref = link["checkout_url"], link["provider_ref"]
    if link["provider"] == "stripe":
        a, k = OP._key(db, clinic["id"], secret_key)
        p = db.execute("SELECT * FROM patients WHERE id = ?", (link["patient_id"],)).fetchone()
        back = base_url.rstrip("/") + f"/pay/{ref}/done"
        form = {"mode": "payment", "success_url": back, "cancel_url": back, "client_reference_id": ref, "customer_creation": "always",
                "payment_intent_data[setup_future_usage]": "off_session",
                "line_items[0][quantity]": "1", "line_items[0][price_data][currency]": link["currency"].lower(),
                "line_items[0][price_data][unit_amount]": str(link["amount_p"]),
                "line_items[0][price_data][product_data][name]": link["description"] or clinic["name"],
                "metadata[ref]": ref, "metadata[clinic_id]": str(clinic["id"])}
        if p["email"]:
            form["customer_email"] = p["email"]
        data = OP._stripe(k, "POST", "/checkout/sessions", form, transport)
        url, provider_ref = data.get("url"), data.get("id")
        if not url or not str(url).startswith("https://"):
            raise CardError("The payment company did not give back a payment page.")
    db.execute("UPDATE pay_links SET save_card = 1, auto_ok = ?, consent_text = ?, consent_at = datetime('now'), checkout_url = ?, provider_ref = ? WHERE id = ?",
               (1 if auto_ok else 0, text, url, provider_ref, link["id"]))
    audit.log(db, clinic["id"], None, "card.consent", "patient", link["patient_id"])
    return url


def _keep(db, clinic, link, secret_key, provider, token, brand, last4, exp_month, exp_year, email):
    if db.execute("SELECT 1 FROM saved_cards WHERE link_id = ?", (link["id"],)).fetchone():
        return None
    cid = db.execute("""INSERT INTO saved_cards(clinic_id, patient_id, provider, token, brand, last4, exp_month, exp_year, email, auto_ok,
                        consent_text, consent_at, link_id, status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?, 'active')""",
                     (clinic["id"], link["patient_id"], provider, OP._seal(secret_key, token), (brand or "card")[:20], (last4 or "")[-4:],
                      int(exp_month) if str(exp_month or "").isdigit() else None, int(exp_year) if str(exp_year or "").isdigit() else None,
                      email, link["auto_ok"] or 0, link["consent_text"], link["consent_at"], link["id"])).lastrowid
    audit.log(db, clinic["id"], None, "card.saved", "patient", link["patient_id"], f"{brand} {last4}")    # the token is never logged
    return cid


def capture_paystack(db, clinic, link, secret_key, data):
    """After a Paystack payment the patient agreed to save: keep the authorization if Paystack says it can be reused."""
    if not link["save_card"]:
        return None
    auth = data.get("authorization") or {}
    if not auth.get("authorization_code") or not auth.get("reusable"):
        return None
    email = ((data.get("customer") or {}).get("email") or "").strip() or None
    return _keep(db, clinic, link, secret_key, "paystack", auth["authorization_code"], auth.get("card_type") or auth.get("brand"),
                 auth.get("last4"), auth.get("exp_month"), auth.get("exp_year"), email)


def capture_stripe(db, clinic, link, secret_key, session, transport=None):
    """After a Stripe Checkout that saved the card: find the payment method and keep it with the customer."""
    if not link["save_card"]:
        return None
    customer, pi = session.get("customer"), session.get("payment_intent")
    if not customer or not pi:
        return None
    a, k = OP._key(db, clinic["id"], secret_key)
    intent = OP._stripe(k, "GET", "/payment_intents/" + urllib.parse.quote(pi), None, transport)
    pm = intent.get("payment_method")
    if not pm:
        return None
    method = OP._stripe(k, "GET", "/payment_methods/" + urllib.parse.quote(pm), None, transport)
    card = method.get("card") or {}
    return _keep(db, clinic, link, secret_key, "stripe", f"{customer}|{pm}", card.get("brand"), card.get("last4"),
                 card.get("exp_month"), card.get("exp_year"), None)


def remove(db, clinic, user_id, card_id, reason):
    c = db.execute("SELECT * FROM saved_cards WHERE id = ? AND clinic_id = ?", (card_id, clinic["id"])).fetchone()
    if not c or c["status"] != "active":
        raise CardError("That card is not on file.")
    reason = (reason or "").strip()[:200]
    if not reason:
        raise CardError("Say why, for example: the patient asked.")
    db.execute("UPDATE saved_cards SET status = 'removed', token = '', removed_at = datetime('now'), removed_reason = ? WHERE id = ?", (reason, card_id))
    audit.log(db, clinic["id"], user_id, "card.removed", "patient", c["patient_id"])


def set_auto(db, clinic, user_id, card_id, on):
    """Staff can only switch automatic charging off; switching it on needs the patient's own agreement on a pay page."""
    c = db.execute("SELECT * FROM saved_cards WHERE id = ? AND clinic_id = ?", (card_id, clinic["id"])).fetchone()
    if not c or c["status"] != "active":
        raise CardError("That card is not on file.")
    if on:
        raise CardError("Only the patient can agree to automatic charges, on a pay page.")
    db.execute("UPDATE saved_cards SET auto_ok = 0 WHERE id = ?", (card_id,))
    audit.log(db, clinic["id"], user_id, "card.auto_off", "patient", c["patient_id"])


# ------------------------------------------------------------------------------------------------------------- charging
def charge(db, clinic, user_id, secret_key, card_id, amount_p, purpose="treatment", what=None, transport=None):
    """Charge a saved card. Returns the payment id. Every attempt, good or bad, is kept."""
    cid = clinic["id"]
    c = db.execute("SELECT * FROM saved_cards WHERE id = ? AND clinic_id = ?", (card_id, cid)).fetchone()
    if not c or c["status"] != "active":
        raise CardError("That card is not on file.")
    if not amount_p or amount_p <= 0:
        raise CardError("The amount must be more than zero.")
    if expired(c, _today(clinic)):
        raise CardError("That card has expired. Send the patient a pay link instead.")
    a, k = OP._key(db, cid, secret_key)
    if a["provider"] != c["provider"]:
        raise CardError("The clinic now uses a different payment company. This card cannot be charged.")
    token = OP._open(secret_key, c["token"])
    if not token:
        raise CardError("The card token could not be read.")
    ref = "dosc_" + secrets.token_urlsafe(14)
    att = db.execute("""INSERT INTO card_charges(clinic_id, card_id, patient_id, ref, amount_p, currency, purpose, what, status, created_by)
                        VALUES (?,?,?,?,?,?,?,?, 'trying', ?)""",
                     (cid, card_id, c["patient_id"], ref, amount_p, clinic["currency"], purpose, (what or "")[:120] or None, user_id)).lastrowid
    try:
        if c["provider"] == "paystack":
            if not c["email"]:
                raise CardError("Paystack needs the email the card was saved with, and there is none.")
            data = OP._paystack(k, "POST", "/transaction/charge_authorization", {
                "authorization_code": token, "email": c["email"], "amount": amount_p, "currency": clinic["currency"], "reference": ref}, transport)
            if data.get("status") != "success":
                raise CardError("The card was declined (%s). Ask the patient to pay another way." % (data.get("gateway_response") or data.get("status") or "no reason given"))
            provider_ref = data.get("reference") or ref
            if int(data.get("amount") or 0) != amount_p:
                raise CardError("Paystack charged a different amount. Check it in the Paystack dashboard.")
        else:
            customer, pm = token.split("|", 1)
            status, text = (transport or OP.default_transport)("POST", OP.STRIPE_API + "/payment_intents",
                                                               {"Authorization": "Bearer " + k, "Content-Type": "application/x-www-form-urlencoded"},
                                                               urllib.parse.urlencode({"amount": str(amount_p), "currency": clinic["currency"].lower(),
                                                                                       "customer": customer, "payment_method": pm, "off_session": "true",
                                                                                       "confirm": "true", "metadata[ref]": ref}).encode())
            try:
                data = json.loads(text) if text else {}
            except ValueError:
                data = {}
            if status >= 300:
                err = data.get("error") or {}
                if err.get("code") == "authentication_required":
                    raise CardError("The bank wants the patient to approve this payment themselves. Send them a pay link instead.")
                raise CardError("The card was declined (%s). Ask the patient to pay another way." % (err.get("decline_code") or err.get("code") or f"HTTP {status}"))
            if data.get("status") != "succeeded":
                raise CardError("The payment did not go through (%s). Ask the patient to pay another way." % data.get("status"))
            provider_ref = data.get("id") or ref
    except (CardError, OP.PayError) as exc:
        db.execute("UPDATE card_charges SET status = 'failed', error = ? WHERE id = ?", (str(exc)[:300], att))
        audit.log(db, cid, user_id, "card.charge_failed", "patient", c["patient_id"])
        raise CardError(str(exc))
    pay_id = billing.record_payment(db, clinic, user_id, c["patient_id"], amount_p, "card", provider_ref,
                                    f"Saved card {c['brand']} {c['last4']}", purpose=purpose, online=True)
    db.execute("UPDATE card_charges SET status = 'paid', payment_id = ?, provider_ref = ? WHERE id = ?", (pay_id, provider_ref, att))
    db.execute("UPDATE saved_cards SET last_used_at = datetime('now') WHERE id = ?", (card_id,))
    audit.log(db, cid, user_id, "card.charged", "patient", c["patient_id"], str(amount_p))
    return pay_id


def attempts(db, cid, pid):
    return db.execute("""SELECT x.*, s.brand, s.last4, u.display_name AS by_name FROM card_charges x JOIN saved_cards s ON s.id = x.card_id
                         LEFT JOIN users u ON u.id = x.created_by WHERE x.clinic_id = ? AND x.patient_id = ? ORDER BY x.id DESC LIMIT 50""", (cid, pid)).fetchall()


def run_automatic(db, clinic, secret_key, transport=None, today=None):
    """The daily job. Charges, on a card the patient agreed to use automatically: membership fees that are due, and ortho monthly
    amounts posted in the last 7 days that are not paid for yet. One try per amount: a failure is left for staff, never retried."""
    if not allowed(db, clinic):
        return {"paid": 0, "failed": 0}
    cid = clinic["id"]
    today = today or _today(clinic)
    paid = failed = 0
    from . import memberships as MS
    for ch in db.execute("""SELECT mc.*, m.patient_id FROM membership_charges mc JOIN memberships m ON m.id = mc.membership_id
                            WHERE mc.clinic_id = ? AND mc.status = 'due' AND mc.due_on <= ? AND mc.auto_tried = 0""", (cid, today.isoformat())).fetchall():
        card = _auto_card(db, cid, ch["patient_id"], today)
        if not card:
            continue
        db.execute("UPDATE membership_charges SET auto_tried = 1 WHERE id = ?", (ch["id"],))
        try:
            pay_id = charge(db, clinic, None, secret_key, card["id"], ch["amount_p"], "membership", "Membership fee", transport)
            db.execute("UPDATE membership_charges SET status = 'paid', payment_id = ?, paid_on = ? WHERE id = ?", (pay_id, today.isoformat(), ch["id"]))
            MS._refresh_status(db, clinic, ch["membership_id"])
            paid += 1
        except CardError as exc:
            db.execute("UPDATE membership_charges SET status = 'failed', note = ? WHERE id = ?", (("Saved card: " + str(exc))[:200], ch["id"]))
            failed += 1
    since = (today - datetime.timedelta(days=7)).isoformat()
    for ch in db.execute("""SELECT oc.*, o.patient_id FROM ortho_charges oc JOIN ortho_contracts k ON k.id = oc.contract_id JOIN ortho_cases o ON o.id = k.case_id
                            WHERE oc.clinic_id = ? AND oc.kind = 'monthly' AND oc.procedure_id IS NOT NULL AND oc.posted_on >= ? AND oc.auto_tried = 0""",
                         (cid, since)).fetchall():
        card = _auto_card(db, cid, ch["patient_id"], today)
        if not card:
            continue
        db.execute("UPDATE ortho_charges SET auto_tried = 1 WHERE id = ?", (ch["id"],))
        try:
            charge(db, clinic, None, secret_key, card["id"], ch["amount_p"], "treatment", "Ortho monthly fee", transport)
            paid += 1
        except CardError:
            failed += 1
    return {"paid": paid, "failed": failed}


def _auto_card(db, cid, pid, today):
    for c in db.execute("SELECT * FROM saved_cards WHERE clinic_id = ? AND patient_id = ? AND status = 'active' AND auto_ok = 1 ORDER BY id DESC",
                        (cid, pid)).fetchall():
        if not expired(c, today):
            return c
    return None
