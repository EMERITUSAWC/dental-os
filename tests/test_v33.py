"""v0.33: SMS through the clinic's own SMS account. The SMS companies are played by a fake; nothing is really sent."""
import json
import urllib.parse

from tests.test_v04 import Base
from dental_os.services import outbox, sms as S, statements as ST, treatment as TR

SECRET = "test"


class Company:
    def __init__(self, ok=True):
        self.calls, self.ok = [], ok

    def __call__(self, method, url, headers, body=None, timeout=20):
        self.calls.append((method, url, headers, body))
        if "hubtel" in url:
            q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
            assert q["clientid"] == "hubid" and q["clientsecret"] == "hubsecret"
            if not self.ok:
                return 400, json.dumps({"status": 1, "message": "Invalid sender"})
            return 200, json.dumps({"rate": 0.03, "messageId": "hub-1", "status": 0, "networkId": "62001"})
        if "africastalking" in url:
            assert headers["apiKey"] == "atkey" and headers["Accept"] == "application/json"
            f = dict(urllib.parse.parse_qsl(body.decode()))
            code = 101 if self.ok else 405
            return 201, json.dumps({"SMSMessageData": {"Message": "Sent to 1/1", "Recipients": [
                {"statusCode": code, "number": f["to"], "status": "Success" if self.ok else "InsufficientBalance", "cost": "KES 0.80", "messageId": "ATXid_1"}]}})
        if "twilio" in url:
            assert headers["Authorization"].startswith("Basic ")
            return (201, json.dumps({"sid": "SM1", "status": "queued"})) if self.ok else (400, json.dumps({"message": "The 'To' number is not valid."}))
        raise AssertionError(url)


class Sms(Base):
    def setUp(self):
        super().setUp()
        self.conn = self.db()
        self.conn.execute("UPDATE patients SET prefers_sms = 1 WHERE id = 1")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def c(self):
        return dict(self.conn.execute("SELECT * FROM clinics WHERE id = 1").fetchone())

    def on(self, provider="hubtel", **extra):
        creds = {"hubtel": ("hubid", "hubsecret"), "africastalking": ("sandbox", "atkey"), "twilio": ("AC" + "1" * 32, "f" * 32)}[provider]
        S.save_account(self.conn, self.c(), 1, SECRET, {"provider": provider, "sender": "SmileDent", "api_user": creds[0], "api_secret": creds[1],
                                                       "enabled": "1"} | extra)
        self.conn.commit()


class SendTests(Sms):
    def test_each_company_is_called_the_documented_way(self):
        for provider, want in (("hubtel", "hub-1"), ("africastalking", "ATXid_1"), ("twilio", "SM1")):
            self.on(provider, sandbox="1" if provider == "africastalking" else "")
            fake = Company()
            self.assertEqual(S.send(self.conn, self.c(), SECRET, "+233201112223", "Hello", fake), want)
            method, url, headers, body = fake.calls[0]
            if provider == "hubtel":
                q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
                self.assertEqual((method, q["to"], q["from"], q["content"]), ("GET", "233201112223", "SmileDent", "Hello"))
            elif provider == "africastalking":
                self.assertEqual(url, S.AT_SANDBOX)
                self.assertEqual(dict(urllib.parse.parse_qsl(body.decode()))["username"], "sandbox")
            else:
                self.assertTrue(url.endswith("/Accounts/AC" + "1" * 32 + "/Messages.json"))
            with self.assertRaises(S.SmsError):
                S.send(self.conn, self.c(), SECRET, "+233201112223", "Hello", Company(ok=False))

    def test_details_are_checked_and_the_key_is_sealed(self):
        for bad in ({"sender": "A name far too long"}, {"provider": "pigeon"}, {"provider": "twilio", "api_user": "nope"}, {"api_secret": ""}):
            with self.assertRaises(S.SmsError):
                S.save_account(self.conn, self.c(), 1, SECRET, {"provider": "hubtel", "sender": "Clinic", "api_user": "hubid", "api_secret": "x",
                                                               "enabled": "1"} | bad)
        self.on()
        self.assertNotIn("hubsecret", S.account(self.conn, 1)["api_secret"])
        logs = " ".join(str(tuple(r)) for r in self.conn.execute("SELECT * FROM audit_log"))
        self.assertNotIn("hubsecret", logs)
        with self.assertRaises(S.SmsError):
            S.send(self.conn, self.c(), SECRET, "0201112223", "Hello", Company())        # not in international form

    def test_segments_and_plain_text(self):
        self.assertEqual(S.segments("a" * 160), 1)
        self.assertEqual(S.segments("a" * 161), 2)
        self.assertEqual(S.segments("é" * 160), 1)                 # é is in the plain SMS alphabet
        self.assertEqual(S.segments("ŵ" * 71), 2)                  # a character outside it: 70 per part
        self.assertEqual(S.plain("l’heure – 9 h"), "l'heure - 9 h")
        self.on()
        with self.assertRaises(S.SmsError):
            S.send(self.conn, self.c(), SECRET, "+233201112223", "x" * 700, Company())


class QueueTests(Sms):
    def test_a_queued_message_goes_by_sms_and_offers_carry_stop(self):
        self.on()
        self.conn.execute("INSERT INTO outbox(clinic_id, patient_id, kind, purpose, dedupe_key, due_on) VALUES (1, 1, 'birthday', 'care', 'b1', date('now'))")
        self.conn.commit()
        fake = Company()
        res = S.send_ready(self.conn, self.c(), 1, SECRET, transport=fake)
        self.assertEqual((res["sent"], res["left"]), (1, 0))
        m = self.conn.execute("SELECT * FROM messages WHERE channel = 'sms'").fetchone()
        self.assertEqual((m["kind"], m["provider_id"]), ("birthday", "hub-1"))
        self.assertNotIn("STOP", m["body"])
        self.conn.execute("UPDATE patients SET offers_ok = 1 WHERE id = 1")
        self.conn.execute("INSERT INTO campaigns(clinic_id, name, kind, purpose, body, created_by) VALUES (1, 'Offer', 'lapsed', 'offer', 'Hello {first_name}, 10% off whitening at {clinic}.', 1)")
        cid = self.conn.execute("SELECT id FROM campaigns").fetchone()[0]
        self.conn.execute("INSERT INTO outbox(clinic_id, patient_id, kind, purpose, campaign_id, dedupe_key, due_on) VALUES (1, 1, 'campaign', 'offer', ?, 'c1', date('now'))", (cid,))
        self.conn.commit()
        S.send_ready(self.conn, self.c(), 1, SECRET, transport=fake)
        body = self.conn.execute("SELECT body FROM messages WHERE kind = 'campaign'").fetchone()[0]
        self.assertIn("Reply STOP", body)
        self.conn.execute("UPDATE patients SET reminders_ok = 0 WHERE id = 1")
        self.conn.execute("INSERT INTO outbox(clinic_id, patient_id, kind, purpose, dedupe_key, due_on) VALUES (1, 1, 'recall', 'care', 'r1', date('now'))")
        self.conn.commit()
        self.assertEqual(S.send_ready(self.conn, self.c(), 1, SECRET, transport=fake)["sent"], 0)     # no consent: not sent

    def test_screens_and_statement_by_sms(self):
        mgr = self.login("mgr")
        self.page(mgr, "/settings/sms")
        r = self.post(mgr, "/settings/sms", {"provider": "hubtel", "sender": "SmileDent", "api_user": "hubid", "api_secret": "hubsecret", "enabled": "1"},
                      follow_redirects=True)
        self.assertIn("Saved.", r.get_data(as_text=True))
        self.conn.execute("INSERT INTO outbox(clinic_id, patient_id, kind, purpose, dedupe_key, due_on) VALUES (1, 1, 'birthday', 'care', 'b2', date('now'))")
        self.conn.commit()
        page = self.page(mgr, "/messages")
        self.assertIn("Text everyone who prefers SMS", page)
        self.assertIn("/sms\"", page)
        self.assertEqual(self.login("desk").get("/settings/sms").status_code, 403)
        c = self.c()
        TR.ensure_catalogue(self.conn, 1)
        pr = TR.add_planned(self.conn, c, 1, 1, self.conn.execute("SELECT id FROM procedure_codes WHERE clinic_id = 1 AND code = 'CONS'").fetchone()[0], fee_p=5000)
        TR.mark_done(self.conn, c, 1, pr)
        rid = ST.make_run(self.conn, c, 1, "1", "0", "0", False)
        item = self.conn.execute("SELECT id FROM statement_items WHERE run_id = ?", (rid,)).fetchone()[0]
        self.conn.commit()
        fake = Company()
        S.send_statement(self.conn, c, 1, SECRET, item, lambda t: "https://clinic.example/p/" + t, fake)
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(fake.calls[0][1]).query))
        self.assertIn("https://clinic.example/p/", q["content"])
        self.assertNotIn("50", q["content"].split("https://")[0])                         # never the amount
        self.assertEqual(self.conn.execute("SELECT sent_how FROM statement_items WHERE id = ?", (item,)).fetchone()[0], "sms")


class PreferenceTests(Sms):
    def test_the_patient_form_keeps_sms_as_the_preferred_way(self):
        mgr = self.login("mgr")
        self.post(mgr, "/patients/2/edit", {"first_name": "Kofi", "last_name": "Young", "phone": "0201112224", "dob": "2016-05-01", "preferred_contact": "sms"})
        row = self.db().execute("SELECT preferred_contact, prefers_sms FROM patients WHERE id = 2").fetchone()
        self.assertEqual(tuple(row), ("whatsapp", 1))
        self.assertIn('<option value="sms" selected', self.page(mgr, "/patients/2/edit"))
        self.post(mgr, "/patients/2/edit", {"first_name": "Kofi", "last_name": "Young", "phone": "0201112224", "dob": "2016-05-01", "preferred_contact": "email"})
        self.assertEqual(self.db().execute("SELECT prefers_sms FROM patients WHERE id = 2").fetchone()[0], 0)
