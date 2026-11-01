"""v0.32: saved cards and automatic charges. Paystack and Stripe are played by a fake that answers the way their published APIs do."""
import datetime
import json
import urllib.parse

from tests.test_v05 import Money
from dental_os.services import billing, onlinepay as OP, ortho as O, savedcards as SC
from dental_os.services.timeutil import now_local

SECRET = "test"


class Company:
    def __init__(self, provider):
        self.provider, self.calls, self.paid, self.decline, self.sca = provider, [], set(), False, False
        self.sessions = {}

    def __call__(self, method, url, headers, body=None, timeout=15):
        self.calls.append((method, url, body))
        if self.provider == "paystack":
            if url.endswith("/transaction/initialize"):
                d = json.loads(body)
                return 200, json.dumps({"status": True, "data": {"authorization_url": "https://checkout.paystack.com/x", "reference": d["reference"]}})
            if "/transaction/verify/" in url:
                ref = urllib.parse.unquote(url.rsplit("/", 1)[1])
                if ref not in self.paid:
                    return 200, json.dumps({"status": True, "data": {"status": "abandoned"}})
                return 200, json.dumps({"status": True, "data": {"status": "success", "reference": ref, "amount": 15000, "currency": "GHS",
                                                                   "channel": "card", "customer": {"email": "ama@example.com"},
                                                                   "authorization": {"authorization_code": "AUTH_secret123", "reusable": True,
                                                                                     "last4": "4081", "exp_month": "12", "exp_year": "2030",
                                                                                     "card_type": "visa"}}})
            if url.endswith("/transaction/charge_authorization"):
                d = json.loads(body)
                assert d["authorization_code"] == "AUTH_secret123" and d["email"] == "ama@example.com"
                if self.decline:
                    return 200, json.dumps({"status": True, "data": {"status": "failed", "gateway_response": "Insufficient Funds"}})
                return 200, json.dumps({"status": True, "data": {"status": "success", "reference": d["reference"], "amount": d["amount"]}})
        else:
            if url.endswith("/checkout/sessions"):
                form = dict(urllib.parse.parse_qsl(body.decode()))
                sid = "cs_test_%d" % len(self.calls)
                self.sessions[sid] = form
                return 200, json.dumps({"id": sid, "url": "https://checkout.stripe.com/c/pay/" + sid})
            if "/checkout/sessions/" in url:
                form = self.sessions[url.rsplit("/", 1)[1]]
                paid = form["client_reference_id"] in self.paid
                return 200, json.dumps({"payment_status": "paid" if paid else "unpaid", "client_reference_id": form["client_reference_id"],
                                        "amount_total": int(form["line_items[0][price_data][unit_amount]"]), "currency": form["line_items[0][price_data][currency]"],
                                        "payment_intent": "pi_1", "customer": "cus_1" if form.get("customer_creation") == "always" else None})
            if url.endswith("/payment_intents/pi_1"):
                return 200, json.dumps({"id": "pi_1", "payment_method": "pm_1"})
            if url.endswith("/payment_methods/pm_1"):
                return 200, json.dumps({"id": "pm_1", "card": {"brand": "mastercard", "last4": "4444", "exp_month": 3, "exp_year": 2031}})
            if url.endswith("/payment_intents"):
                form = dict(urllib.parse.parse_qsl(body.decode()))
                assert form["off_session"] == "true" and form["customer"] == "cus_1" and form["payment_method"] == "pm_1"
                if self.sca:
                    return 402, json.dumps({"error": {"code": "authentication_required"}})
                return 200, json.dumps({"id": "pi_2", "status": "succeeded"})
        return 404, json.dumps({"message": "not found"})


class Cards(Money):
    def setUp(self):
        super().setUp()
        self.conn = self.db()
        self.conn.execute("UPDATE clinics SET email = 'desk@clinic.example' WHERE id = 1")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def c(self):
        return dict(self.conn.execute("SELECT * FROM clinics WHERE id = 1").fetchone())

    def paystack(self, allow=True):
        OP.save_account(self.conn, self.c(), 1, SECRET, "paystack", True, "sk_test_" + "a" * 30)
        OP.set_allow_save(self.conn, self.c(), 1, allow)
        self.conn.commit()
        return Company("paystack")

    def stripe(self):
        self.conn.execute("UPDATE clinics SET currency = 'GBP', country = 'GB' WHERE id = 1")
        OP.save_account(self.conn, self.c(), 1, SECRET, "stripe", True, "sk_test_" + "b" * 30, "whsec_" + "c" * 30)
        OP.set_allow_save(self.conn, self.c(), 1, True)
        self.conn.commit()
        return Company("stripe")

    def saved(self, fake, auto=True):
        ref = OP.create_link(self.conn, self.c(), 1, SECRET, 1, 15000, "Deposit", "https://clinic.example", transport=fake)
        SC.prepare(self.conn, self.c(), SECRET, ref, auto, "https://clinic.example", transport=fake)
        fake.paid.add(ref)
        self.assertEqual(OP.confirm(self.conn, self.c(), SECRET, ref, fake), "paid")
        self.conn.commit()
        return SC.cards_for(self.conn, 1, 1)[0]


class PaystackTests(Cards):
    def test_a_card_is_kept_only_with_consent_and_only_as_a_sealed_token(self):
        fake = self.paystack()
        ref = OP.create_link(self.conn, self.c(), 1, SECRET, 1, 15000, "Deposit", "https://clinic.example", transport=fake)
        fake.paid.add(ref)
        OP.confirm(self.conn, self.c(), SECRET, ref, fake)
        self.assertEqual(SC.cards_for(self.conn, 1, 1), [])                       # paid without ticking the box: nothing kept
        card = self.saved(fake)
        self.assertEqual((card["brand"], card["last4"], card["exp_month"], card["exp_year"], card["auto_ok"]), ("visa", "4081", 12, 2030, 1))
        self.assertNotIn("AUTH_secret123", card["token"])
        self.assertIn("monthly", card["consent_text"])
        logs = " ".join(str(tuple(r)) for r in self.conn.execute("SELECT * FROM audit_log"))
        self.assertNotIn("AUTH_secret123", logs)

    def test_staff_charge_failures_are_kept_and_removal_stops_everything(self):
        fake = self.paystack()
        card = self.saved(fake, auto=False)
        before = billing.totals(self.conn, 1, 1)["balance"]
        SC.charge(self.conn, self.c(), 1, SECRET, card["id"], 5000, transport=fake)
        self.assertEqual(billing.totals(self.conn, 1, 1)["balance"], before - 5000)
        fake.decline = True
        with self.assertRaises(SC.CardError) as e:
            SC.charge(self.conn, self.c(), 1, SECRET, card["id"], 5000, transport=fake)
        self.assertIn("Insufficient Funds", str(e.exception))
        self.assertEqual([a["status"] for a in SC.attempts(self.conn, 1, 1)], ["failed", "paid"])
        with self.assertRaises(SC.CardError):
            SC.set_auto(self.conn, self.c(), 1, card["id"], True)                 # only the patient can agree to that
        SC.remove(self.conn, self.c(), 1, card["id"], "The patient asked")
        fake.decline = False
        with self.assertRaises(SC.CardError):
            SC.charge(self.conn, self.c(), 1, SECRET, card["id"], 5000, transport=fake)
        self.assertEqual(self.conn.execute("SELECT token FROM saved_cards").fetchone()[0], "")

    def test_the_daily_job_charges_only_what_the_patient_agreed_to(self):
        fake = self.paystack()
        card = self.saved(fake, auto=True)
        today = now_local(self.c()["timezone"]).date()
        case = O.open_case(self.conn, self.c(), 2, 1, 1, {"appliance": "fixed_metal", "est_months": "12"})
        O.start(self.conn, self.c(), 2, case)
        O.make_contract(self.conn, self.c(), 1, case, "1200", "0", "12", today.isoformat())     # the first month is posted today
        self.conn.commit()
        self.assertEqual(SC.run_automatic(self.conn, self.c(), SECRET, transport=fake), {"paid": 1, "failed": 0})
        self.assertEqual(SC.run_automatic(self.conn, self.c(), SECRET, transport=fake), {"paid": 0, "failed": 0})    # once per amount
        SC.set_auto(self.conn, self.c(), 1, card["id"], False)
        O.post_due(self.conn, self.c(), today=O.add_months(today, 1))
        self.assertEqual(SC.run_automatic(self.conn, self.c(), SECRET, transport=fake, today=O.add_months(today, 1)), {"paid": 0, "failed": 0})
        OP.set_allow_save(self.conn, self.c(), 1, False)
        self.assertFalse(SC.allowed(self.conn, self.c()))


class StripeTests(Cards):
    def test_stripe_saves_through_checkout_and_charges_off_session(self):
        fake = self.stripe()
        card = self.saved(fake)
        session_form = next(f for f in fake.sessions.values() if f.get("customer_creation") == "always")
        self.assertEqual(session_form["payment_intent_data[setup_future_usage]"], "off_session")
        self.assertEqual((card["brand"], card["last4"]), ("mastercard", "4444"))
        SC.charge(self.conn, self.c(), 1, SECRET, card["id"], 2500, transport=fake)
        fake.sca = True
        with self.assertRaises(SC.CardError) as e:
            SC.charge(self.conn, self.c(), 1, SECRET, card["id"], 2500, transport=fake)
        self.assertIn("approve this payment themselves", str(e.exception))


class ScreenTests(Cards):
    def test_the_pay_page_offers_to_keep_the_card_and_staff_can_charge_it(self):
        fake = self.paystack()
        ref = OP.create_link(self.conn, self.c(), 1, SECRET, 1, 15000, "Deposit", "https://clinic.example", transport=fake)
        self.conn.commit()
        visitor = self.app.test_client()
        page = visitor.get(f"/pay/{ref}").get_data(as_text=True)
        self.assertIn("Keep this card", page)
        import re
        tok = re.search(r'name="_csrf" value="([^"]+)"', page).group(1)
        r = visitor.post(f"/pay/{ref}/keep", data={"_csrf": tok, "save": "1", "auto": "1"})
        self.assertEqual(r.headers["Location"], "https://checkout.paystack.com/x")
        self.assertEqual(OP.get_link(self.conn, ref)["save_card"], 1)
        fake.paid.add(ref)
        OP.confirm(self.conn, self.c(), SECRET, ref, fake)
        self.conn.commit()
        mgr = self.login("mgr")
        page = self.page(mgr, "/patients/1/pay-links")
        self.assertIn("ending 4081", page)
        self.assertIn("Monthly fees charged automatically", page)
        card = SC.cards_for(self.conn, 1, 1)[0]
        r = self.post(mgr, f"/patients/1/cards/{card['id']}/charge", {"amount": "20"}, follow_redirects=True)
        self.assertIn("Tick that the patient was told", r.get_data(as_text=True))
        self.assertIn("Let patients keep a card on file", self.page(mgr, "/settings/online-payments"))
        self.assertEqual(self.login("mgr2").post(f"/patients/1/cards/{card['id']}/remove", data={}).status_code, 400)
        out = self.app.test_cli_runner().invoke(args=["run-autopay"]).output
        self.assertIn("charged", out)
