"""Billing clinics for the service (version 0.54).

Plans. The operator sets plans: a name, a monthly price in each currency the service sells in, and how many dentists it allows
(empty means no limit). A clinic is put on a plan when it leaves its trial.

Bills. `flask bill-clinics`, once a day, makes each clinic's bill when its paid-up period runs out: one month at the plan's price
in the clinic's currency, due in 7 days. A clinic with an open bill past its due date is overdue; after the grace period it can look
but not change anything until it pays (see platform.py). Paying, by card or mobile money through the service's own Paystack or Stripe
account, or by bank transfer the operator marks as received, moves the paid-up date on a month and the clinic is active again.

Prices are as the operator sets them; tax on the service (VAT, NHIL and the like) depends on where the service company is registered,
and is for its accountant. Only the payment company's reference is kept, never card details. No web framework here."""
import datetime
import json
import secrets
import urllib.error
import urllib.parse
import urllib.request

from .money import currency_digits

DUE_DAYS = 7
PAYSTACK = "https://api.paystack.co"
STRIPE = "https://api.stripe.com/v1"


class BillError(ValueError):
    pass


def _add_month(d):
    y, m = divmod(d.month, 12)
    nxt = datetime.date(d.year + y, m + 1, 1)
    import calendar
    return nxt.replace(day=min(d.day, calendar.monthrange(nxt.year, nxt.month)[1]))


# ------------------------------------------------------------------------------------------------------------------ plans
def save_plan(conn, name, prices, max_dentists=None, plan_id=None, active=True):
    """prices: {currency: minor units a month}."""
    name = (name or "").strip()[:60]
    if not name:
        raise BillError("Give the plan a name.")
    clean = {}
    for cur, p in prices.items():
        if p is None:
            continue
        if not isinstance(p, int) or p <= 0:
            raise BillError("A price must be more than nothing.")
        clean[cur.upper()] = p
    if not clean:
        raise BillError("Give the plan a price in at least one currency.")
    if max_dentists is not None and (not isinstance(max_dentists, int) or max_dentists < 1):
        raise BillError("The number of dentists is a whole number from 1, or empty for no limit.")
    if plan_id:
        conn.execute("UPDATE plans SET name = ?, prices = ?, max_dentists = ?, active = ? WHERE id = ?",
                     (name, json.dumps(clean), max_dentists, 1 if active else 0, plan_id))
        return plan_id
    return conn.execute("INSERT INTO plans(name, prices, max_dentists, active) VALUES (?,?,?,?)",
                        (name, json.dumps(clean), max_dentists, 1 if active else 0)).lastrowid


def plans(conn, active_only=False):
    return [dict(r) | {"price_map": json.loads(r["prices"])} for r in
            conn.execute("SELECT * FROM plans" + (" WHERE active = 1" if active_only else "") + " ORDER BY name").fetchall()]


def put_on_plan(conn, op_id, cid, plan_id, start=None):
    c = conn.execute("SELECT * FROM clinics WHERE id = ?", (cid,)).fetchone()
    p = conn.execute("SELECT * FROM plans WHERE id = ? AND active = 1", (plan_id,)).fetchone()
    if not c or not p:
        raise BillError("Pick a clinic and a plan in use.")
    if c["currency"] not in json.loads(p["prices"]):
        raise BillError("That plan has no price in the clinic's currency. Add one first.")
    if p["max_dentists"]:
        n = conn.execute("SELECT COUNT(*) FROM providers WHERE clinic_id = ? AND active = 1", (cid,)).fetchone()[0]
        if n > p["max_dentists"]:
            raise BillError("The clinic has more dentists than that plan allows.")
    start = start or (c["trial_ends_on"] if c["plan_state"] == "trial" and c["trial_ends_on"] else datetime.date.today().isoformat())
    conn.execute("UPDATE clinics SET plan_id = ?, paid_until = COALESCE(paid_until, ?) WHERE id = ?", (plan_id, start, cid))
    conn.execute("INSERT INTO operator_log(operator_id, clinic_id, action, detail) VALUES (?,?, 'plan', ?)", (op_id, cid, p["name"]))


# ------------------------------------------------------------------------------------------------------------------ bills
def make_bills(conn, today=None):
    """Make the next bill for each clinic whose paid-up period has run out and has no open bill. Returns how many."""
    today = today or datetime.date.today()
    made = 0
    for c in conn.execute("""SELECT c.*, p.prices, p.name AS plan_name FROM clinics c JOIN plans p ON p.id = c.plan_id
                             WHERE c.plan_state IN ('trial','active','past_due') AND c.paid_until IS NOT NULL AND c.paid_until <= ?""",
                          (today.isoformat(),)).fetchall():
        if conn.execute("SELECT 1 FROM platform_invoices WHERE clinic_id = ? AND status = 'open'", (c["id"],)).fetchone():
            continue
        price = json.loads(c["prices"]).get(c["currency"])
        if not price:
            continue
        start = datetime.date.fromisoformat(c["paid_until"])
        no = conn.execute("SELECT COALESCE(MAX(number), 0) + 1 FROM platform_invoices").fetchone()[0]
        conn.execute("""INSERT INTO platform_invoices(clinic_id, number, plan_name, period_from, period_to, amount_p, currency, due_on, status)
                        VALUES (?,?,?,?,?,?,?,?, 'open')""",
                     (c["id"], no, c["plan_name"], start.isoformat(), _add_month(start).isoformat(), price, c["currency"],
                      (today + datetime.timedelta(days=DUE_DAYS)).isoformat()))
        made += 1
    mark_overdue(conn, today)
    return made


def mark_overdue(conn, today=None):
    today = (today or datetime.date.today()).isoformat()
    for r in conn.execute("SELECT DISTINCT clinic_id FROM platform_invoices WHERE status = 'open' AND due_on < ?", (today,)).fetchall():
        conn.execute("UPDATE clinics SET plan_state = 'past_due' WHERE id = ? AND plan_state IN ('trial','active')", (r["clinic_id"],))


def bills(conn, cid):
    return conn.execute("SELECT * FROM platform_invoices WHERE clinic_id = ? ORDER BY id DESC", (cid,)).fetchall()


def get_bill(conn, cid, bill_id):
    return conn.execute("SELECT * FROM platform_invoices WHERE id = ? AND clinic_id = ?", (bill_id, cid)).fetchone()


def _paid(conn, bill, method, ref, op_id=None):
    conn.execute("UPDATE platform_invoices SET status = 'paid', paid_on = ?, paid_method = ?, paid_ref = ? WHERE id = ?",
                 (datetime.date.today().isoformat(), method, (ref or "")[:120] or None, bill["id"]))
    conn.execute("UPDATE clinics SET paid_until = ? WHERE id = ? AND (paid_until IS NULL OR paid_until < ?)",
                 (bill["period_to"], bill["clinic_id"], bill["period_to"]))
    if not conn.execute("SELECT 1 FROM platform_invoices WHERE clinic_id = ? AND status = 'open'", (bill["clinic_id"],)).fetchone():
        conn.execute("UPDATE clinics SET plan_state = 'active' WHERE id = ? AND plan_state IN ('trial','past_due')", (bill["clinic_id"],))
    if op_id:
        conn.execute("INSERT INTO operator_log(operator_id, clinic_id, action, detail) VALUES (?,?, 'bill.paid', ?)",
                     (op_id, bill["clinic_id"], f"{bill['number']} {method}"))


def mark_paid(conn, op_id, bill_id, method, ref):
    bill = conn.execute("SELECT * FROM platform_invoices WHERE id = ?", (bill_id,)).fetchone()
    if not bill or bill["status"] != "open":
        raise BillError("Only an open bill can be marked paid.")
    if method not in ("bank_transfer", "mobile_money", "cash", "card"):
        raise BillError("Pick how it was paid.")
    if not (ref or "").strip():
        raise BillError("Give the payment's reference, so it can be matched to the bank statement.")
    _paid(conn, bill, method, ref, op_id)


# ------------------------------------------------------------------------------------------------- paying online
def _http(method, url, headers, body=None, timeout=20):
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()
    except (urllib.error.URLError, OSError) as exc:
        raise BillError("Could not reach the payment company: %s" % (getattr(exc, "reason", None) or exc.__class__.__name__))


def provider_for(cfg, currency):
    if currency == "GHS" and cfg.get("PLATFORM_PAYSTACK_SECRET"):
        return "paystack"
    if cfg.get("PLATFORM_STRIPE_SECRET"):
        return "stripe"
    if cfg.get("PLATFORM_PAYSTACK_SECRET") and currency in ("NGN", "KES", "ZAR", "USD"):
        return "paystack"
    return None


def start_payment(conn, cfg, clinic, bill_id, email, return_url, transport=None):
    """Returns the payment company's page to send the manager to."""
    bill = get_bill(conn, clinic["id"], bill_id)
    if not bill or bill["status"] != "open":
        raise BillError("That bill is not waiting to be paid.")
    provider = provider_for(cfg, bill["currency"])
    if not provider:
        raise BillError("Paying online is not set up for this currency. Pay by bank transfer, and we will mark it paid.")
    ref = "dosb_" + secrets.token_urlsafe(12)
    send = transport or _http
    if provider == "paystack":
        if not email or "@" not in email:
            raise BillError("Give the email the receipt should go to.")
        status, body = send("POST", PAYSTACK + "/transaction/initialize",
                            {"Authorization": "Bearer " + cfg["PLATFORM_PAYSTACK_SECRET"], "Content-Type": "application/json"},
                            json.dumps({"email": email, "amount": bill["amount_p"], "currency": bill["currency"], "reference": ref,
                                        "callback_url": return_url}).encode())
        data = json.loads(body or "{}")
        if status != 200 or not data.get("status"):
            raise BillError("The payment company refused: %s" % str(data.get("message") or status)[:150])
        url = data["data"]["authorization_url"]
    else:
        form = {"mode": "payment", "client_reference_id": ref, "success_url": return_url, "cancel_url": return_url,
                "line_items[0][quantity]": "1", "line_items[0][price_data][currency]": bill["currency"].lower(),
                "line_items[0][price_data][unit_amount]": str(bill["amount_p"]),
                "line_items[0][price_data][product_data][name]": f"Dental OS, {bill['plan_name']}, {bill['period_from']} to {bill['period_to']}"}
        if email:
            form["customer_email"] = email
        status, body = send("POST", STRIPE + "/checkout/sessions", {"Authorization": "Bearer " + cfg["PLATFORM_STRIPE_SECRET"],
                                                                  "Content-Type": "application/x-www-form-urlencoded"}, urllib.parse.urlencode(form).encode())
        data = json.loads(body or "{}")
        if status != 200 or "url" not in data:
            raise BillError("The payment company refused: %s" % str((data.get("error") or {}).get("message") or status)[:150])
        url, ref = data["url"], data["id"] + "|" + ref
    conn.execute("UPDATE platform_invoices SET pay_ref = ?, pay_provider = ? WHERE id = ?", (ref, provider, bill_id))
    return url


def confirm(conn, cfg, clinic, bill_id, transport=None):
    """Ask the payment company whether the bill was paid. Returns True when it was."""
    bill = get_bill(conn, clinic["id"], bill_id)
    if not bill or not bill["pay_ref"]:
        return False
    if bill["status"] == "paid":
        return True
    send = transport or _http
    if bill["pay_provider"] == "paystack":
        status, body = send("GET", PAYSTACK + "/transaction/verify/" + urllib.parse.quote(bill["pay_ref"]),
                            {"Authorization": "Bearer " + cfg["PLATFORM_PAYSTACK_SECRET"]})
        d = (json.loads(body or "{}").get("data") or {})
        ok = status == 200 and d.get("status") == "success" and d.get("amount") == bill["amount_p"] and (d.get("currency") or "").upper() == bill["currency"]
        ref = bill["pay_ref"]
    else:
        sid = bill["pay_ref"].split("|", 1)[0]
        status, body = send("GET", STRIPE + "/checkout/sessions/" + urllib.parse.quote(sid), {"Authorization": "Bearer " + cfg["PLATFORM_STRIPE_SECRET"]})
        d = json.loads(body or "{}")
        ok = status == 200 and d.get("payment_status") == "paid" and d.get("amount_total") == bill["amount_p"] and (d.get("currency") or "").upper() == bill["currency"]
        ref = sid
    if ok:
        _paid(conn, bill, "card" if bill["pay_provider"] == "stripe" else "online", ref)
    return ok
