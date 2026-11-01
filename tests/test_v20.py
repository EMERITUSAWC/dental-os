"""v0.20: online payments with Paystack and Stripe. The payment companies are played by a fake in these tests, which checks what
we send and answers the way their published APIs do. Nothing here touches the internet."""
import hashlib
import hmac
import json
import time
import urllib.parse

from tests.test_v05 import Money
from dental_os.services import billing, onlinepay as OP

SECRET = "test"          # the app's SECRET_KEY in the test app


class FakeCompany:
    """Answers like Paystack or Stripe. Records every call."""
    def __init__(self, provider):
        self.provider, self.calls, self.paid, self.amount_override, self.channel = provider, [], set(), None, "mobile_money"
        self.sessions = {}

    def __call__(self, method, url, headers, body=None, timeout=15):
        self.calls.append((method, url, headers, body))
        assert headers["Authorization"].startswith("Bearer sk_test_")
        if self.provider == "paystack":
            if url.endswith("/transaction/initialize"):
                data = json.loads(body)
                assert data["currency"] in ("GHS", "NGN") and isinstance(data["amount"], int) and data["email"]
                return 200, json.dumps({"status": True, "data": {"authorization_url": "https://checkout.paystack.com/abc", "access_code": "abc",
                                                                   "reference": data["reference"]}})
            if "/transaction/verify/" in url:
                ref = urllib.parse.unquote(url.rsplit("/", 1)[1])
                if ref in self.paid:
                    return 200, json.dumps({"status": True, "data": {"status": "success", "reference": ref, "amount": self.amount_override or 15000,
                                                                       "currency": "GHS", "channel": self.channel}})
                return 200, json.dumps({"status": True, "data": {"status": "abandoned", "reference": ref}})
            if "/transaction?" in url:
                return 200, json.dumps({"status": True, "data": []})
        else:
            if url.endswith("/checkout/sessions"):
                form = dict(urllib.parse.parse_qsl(body.decode()))
                sid = "cs_test_%d" % len(self.calls)
                self.sessions[sid] = form
                return 200, json.dumps({"id": sid, "url": "https://checkout.stripe.com/c/pay/" + sid})
            if "/checkout/sessions/" in url:
                sid = url.rsplit("/", 1)[1]
                form = self.sessions[sid]
                paid = form["client_reference_id"] in self.paid
                return 200, json.dumps({"id": sid, "payment_status": "paid" if paid else "unpaid", "client_reference_id": form["client_reference_id"],
                                        "amount_total": int(form["line_items[0][price_data][unit_amount]"]), "currency": form["line_items[0][price_data][currency]"],
                                        "payment_intent": "pi_1"})
            if url.endswith("/balance"):
                return 200, json.dumps({"object": "balance"})
        return 404, json.dumps({"message": "not found"})


class Pay(Money):
    def setUp(self):
        super().setUp()
        self.conn = self.db()
        self.conn.execute("UPDATE clinics SET email = 'desk@clinic.example' WHERE id = 1")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def c(self, n=1):
        return dict(self.conn.execute("SELECT * FROM clinics WHERE id = ?", (n,)).fetchone())

    def setup_paystack(self):
        OP.save_account(self.conn, self.c(), 1, SECRET, "paystack", True, "sk_test_" + "a" * 30)
        self.conn.commit()

    def setup_stripe(self):
        self.conn.execute("UPDATE clinics SET currency = 'GBP', country = 'GB' WHERE id = 1")
        OP.save_account(self.conn, self.c(), 1, SECRET, "stripe", True, "sk_test_" + "b" * 30, "whsec_" + "c" * 30)
        self.conn.commit()


class AccountTests(Pay):
    def test_keys_are_checked_sealed_and_never_logged(self):
        for provider, key, words in (("paystack", "pk_test_xxxxxxxxxxxxxxxxxxxxxxxx", "sk_live_"), ("stripe", "abc", "sk_live_"),
                                     ("visa", "sk_test_" + "a" * 30, "company")):
            with self.assertRaises(OP.PayError) as ctx:
                OP.save_account(self.conn, self.c(), 1, SECRET, provider, True, key)
            self.assertIn(words, str(ctx.exception))
        with self.assertRaises(OP.PayError):
            OP.save_account(self.conn, self.c(), 1, SECRET, "stripe", True, "sk_test_" + "a" * 30, "bad")
        self.setup_paystack()
        a = OP.account(self.conn, 1)
        self.assertNotIn("aaaaaaaa", a["secret_key"])
        self.assertTrue(OP.test_mode(a))
        self.assertEqual(a["key_hint"][:8], "sk_test_")
        logged = " ".join(r[0] or "" for r in self.conn.execute("SELECT detail FROM audit_log"))
        self.assertNotIn("aaaaaaaa", logged)
        self.conn.execute("UPDATE clinics SET currency = 'GBP' WHERE id = 1")
        with self.assertRaises(OP.PayError) as ctx:
            OP.save_account(self.conn, self.c(), 1, SECRET, "paystack", True)
        self.assertIn("currency", str(ctx.exception))

    def test_switching_on_without_a_key_is_refused(self):
        with self.assertRaises(OP.PayError):
            OP.save_account(self.conn, self.c(), 1, SECRET, "paystack", True)
        self.assertFalse(OP.is_on(self.conn, self.c()))

    def test_check_the_key(self):
        self.setup_paystack()
        self.assertIn("test mode", OP.check(self.conn, self.c(), SECRET, FakeCompany("paystack")))


class PaystackTests(Pay):
    def test_link_pay_and_record_once(self):
        self.setup_paystack()
        fake = FakeCompany("paystack")
        ref = OP.create_link(self.conn, self.c(), 1, SECRET, 1, 15000, "Crown deposit", "https://clinic.example", transport=fake)
        sent = json.loads(fake.calls[0][3])
        self.assertEqual((sent["amount"], sent["currency"], sent["email"]), (15000, "GHS", "desk@clinic.example"))
        self.assertEqual(sent["callback_url"], f"https://clinic.example/pay/{ref}/done")
        self.assertEqual(OP.confirm(self.conn, self.c(), SECRET, ref, fake), "open")
        fake.paid.add(ref)
        self.assertEqual(OP.confirm(self.conn, self.c(), SECRET, ref, fake), "paid")
        self.assertEqual(OP.confirm(self.conn, self.c(), SECRET, ref, fake), "paid")
        pays = self.conn.execute("SELECT * FROM payments WHERE patient_id = 1").fetchall()
        self.assertEqual(len(pays), 1)
        self.assertEqual((pays[0]["amount_p"], pays[0]["method"]), (15000, "mobile_money"))       # counted even though the desk has not switched mobile money on
        self.assertEqual(OP.get_link(self.conn, ref)["payment_id"], pays[0]["id"])

    def test_an_amount_that_does_not_match_is_not_counted(self):
        self.setup_paystack()
        fake = FakeCompany("paystack")
        ref = OP.create_link(self.conn, self.c(), 1, SECRET, 1, 15000, "x", "https://c.example", transport=fake)
        fake.paid.add(ref)
        fake.amount_override = 100
        with self.assertRaises(OP.PayError):
            OP.confirm(self.conn, self.c(), SECRET, ref, fake)
        self.assertEqual(OP.get_link(self.conn, ref)["status"], "failed")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM payments").fetchone()[0], 0)

    def test_webhook_needs_a_good_signature(self):
        self.setup_paystack()
        fake = FakeCompany("paystack")
        ref = OP.create_link(self.conn, self.c(), 1, SECRET, 1, 15000, "x", "https://c.example", transport=fake)
        self.conn.commit()
        body = json.dumps({"event": "charge.success", "data": {"reference": ref, "status": "success", "amount": 15000, "currency": "GHS",
                                                               "channel": "card"}}).encode()
        key = OP.account(self.conn, 1)["hook_key"]
        client = self.app.test_client()
        r = client.post(f"/hooks/pay/{key}", data=body, headers={"X-Paystack-Signature": "0" * 128})
        self.assertEqual(r.status_code, 401)
        sig = hmac.new(("sk_test_" + "a" * 30).encode(), body, hashlib.sha512).hexdigest()
        r = client.post(f"/hooks/pay/{key}", data=body, headers={"X-Paystack-Signature": sig})
        self.assertEqual((r.status_code, r.get_data(as_text=True)), (200, "paid"))
        r = client.post(f"/hooks/pay/{key}", data=body, headers={"X-Paystack-Signature": sig})
        self.assertEqual(r.status_code, 200)                                                    # sent twice: still one payment
        self.assertEqual(self.db().execute("SELECT COUNT(*) FROM payments").fetchone()[0], 1)
        self.assertEqual(client.post("/hooks/pay/nope", data=body).status_code, 404)

    def test_no_email_anywhere(self):
        self.conn.execute("UPDATE clinics SET email = NULL WHERE id = 1")
        self.setup_paystack()
        with self.assertRaises(OP.PayError) as ctx:
            OP.create_link(self.conn, self.c(), 1, SECRET, 1, 15000, "x", "https://c.example", transport=FakeCompany("paystack"))
        self.assertIn("email", str(ctx.exception))


class StripeTests(Pay):
    def test_checkout_and_signed_webhook(self):
        self.setup_stripe()
        fake = FakeCompany("stripe")
        ref = OP.create_link(self.conn, self.c(), 1, SECRET, 1, 4500, "Check-up", "https://c.example", transport=fake)
        form = list(fake.sessions.values())[0]
        self.assertEqual((form["line_items[0][price_data][currency]"], form["line_items[0][price_data][unit_amount]"], form["client_reference_id"]),
                         ("gbp", "4500", ref))
        self.conn.commit()
        link = OP.get_link(self.conn, ref)
        event = {"type": "checkout.session.completed", "data": {"object": {"id": link["provider_ref"], "client_reference_id": ref,
                                                                            "payment_status": "paid", "amount_total": 4500, "currency": "gbp"}}}
        body = json.dumps(event).encode()
        ts = int(time.time())
        good = hmac.new(("whsec_" + "c" * 30).encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
        client = self.app.test_client()
        key = OP.account(self.conn, 1)["hook_key"]
        old = hmac.new(("whsec_" + "c" * 30).encode(), f"{ts - 1000}.".encode() + body, hashlib.sha256).hexdigest()
        self.assertEqual(client.post(f"/hooks/pay/{key}", data=body, headers={"Stripe-Signature": f"t={ts - 1000},v1={old}"}).status_code, 401)
        self.assertEqual(client.post(f"/hooks/pay/{key}", data=body, headers={"Stripe-Signature": f"t={ts},v1={'0' * 64}"}).status_code, 401)
        r = client.post(f"/hooks/pay/{key}", data=body, headers={"Stripe-Signature": f"t={ts},v1={good}"})
        self.assertEqual(r.get_data(as_text=True), "paid")
        pay = self.db().execute("SELECT * FROM payments").fetchone()
        self.assertEqual((pay["amount_p"], pay["method"]), (4500, "card"))

    def test_coming_back_looks_it_up(self):
        self.setup_stripe()
        fake = FakeCompany("stripe")
        ref = OP.create_link(self.conn, self.c(), 1, SECRET, 1, 4500, "Check-up", "https://c.example", transport=fake)
        fake.paid.add(ref)
        self.assertEqual(OP.confirm(self.conn, self.c(), SECRET, ref, fake), "paid")


class ScreenTests(Pay):
    def test_staff_make_a_link_and_the_patient_sees_the_pay_page(self):
        self.setup_paystack()
        fake = FakeCompany("paystack")
        OP.default_transport, real = fake, OP.default_transport
        try:
            m = self.login("mgr")
            self.assertIn("Key saved", self.page(m, "/settings/online-payments"))
            r = self.post(m, "/settings/online-payments", {"action": "check"}, follow_redirects=True)
            self.assertIn("The key works", r.get_data(as_text=True))
            c = self.login("desk")
            self.assertIn("Ask for a payment online", self.page(c, "/patients/1/pay-links"))
            r = self.post(c, "/patients/1/pay-links", {"amount": "150", "description": "Deposit"})
            self.assertEqual(r.status_code, 302)
            ref = r.headers["Location"].rsplit("/", 1)[1]
            page = self.page(c, f"/pay-links/{ref}")
            self.assertIn("wa.me", page)
            self.assertIn("<svg", page)
            anon = self.app.test_client()
            page = anon.get(f"/pay/{ref}").get_data(as_text=True)
            self.assertIn("Pay now", page)
            self.assertNotIn("Mensah", page)                                 # only the first name on the public page
            self.assertEqual(anon.get(f"/pay/{ref}/go").headers["Location"], "https://checkout.paystack.com/abc")
            self.assertIn("not received the payment yet", anon.get(f"/pay/{ref}/done").get_data(as_text=True))
            fake.paid.add(ref)
            self.assertIn("payment has been received", anon.get(f"/pay/{ref}/done").get_data(as_text=True))
            self.assertIn("Paid", self.page(c, "/pay-links"))
            self.assertEqual(anon.get("/pay/dos_nothinghere").status_code, 404)
            self.assertEqual(anon.get("/pay/whatever").status_code, 404)
        finally:
            OP.default_transport = real

    def test_cancel_and_other_clinic(self):
        self.setup_paystack()
        ref = OP.create_link(self.conn, self.c(), 1, SECRET, 1, 15000, "x", "https://c.example", transport=FakeCompany("paystack"))
        self.conn.commit()
        self.assertEqual(self.login("mgr2").get(f"/pay-links/{ref}").status_code, 404)
        c = self.login("desk")
        self.post(c, f"/pay-links/{ref}/cancel")
        self.assertIn("can no longer be used", self.app.test_client().get(f"/pay/{ref}").get_data(as_text=True))

    def test_settings_only_for_managers_and_french(self):
        self.assertEqual(self.login("desk").get("/settings/online-payments").status_code, 403)
        self.conn.execute("UPDATE clinics SET language = 'fr' WHERE id = 1")
        self.conn.commit()
        self.assertIn("Paiements en ligne", self.page(self.login("mgr"), "/settings/online-payments"))
