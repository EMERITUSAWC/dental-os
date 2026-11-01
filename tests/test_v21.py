"""v0.21: phone calls through the clinic's own Twilio account. Twilio is played by a fake: nothing here touches the internet."""
import json
import urllib.parse

from tests.test_v04 import Base
from dental_os.services import voice as V

SECRET = "test"
SID = "AC" + "1" * 32
TOKEN = "f" * 32


class FakeTwilio:
    def __init__(self, status=201):
        self.calls, self.status = [], status

    def __call__(self, method, url, headers, body=None, timeout=15):
        self.calls.append((method, url, headers, urllib.parse.parse_qs(body.decode())))
        if self.status >= 300:
            return self.status, json.dumps({"message": "The number is unverified."})
        return self.status, json.dumps({"sid": "CA" + "9" * 32, "status": "queued"})


class Phone(Base):
    def setUp(self):
        super().setUp()
        self.conn = self.db()

    def tearDown(self):
        self.conn.close()

    def c(self, n=1):
        return dict(self.conn.execute("SELECT * FROM clinics WHERE id = ?", (n,)).fetchone())

    def switch_on(self):
        V.save_account(self.conn, self.c(), 1, SECRET, True, SID, TOKEN, "+233302000111", "+233244000222")
        self.conn.commit()
        return V.account(self.conn, 1)

    def hook(self, client, path, form):
        """Post to a Twilio address with Twilio's signature, as Twilio would."""
        url = "http://localhost" + path
        sig = V.signature(TOKEN, url, form)
        return client.post(path, data=form, headers={"X-Twilio-Signature": sig})


class SignatureTests(Phone):
    def test_matches_twilios_published_example(self):
        # The worked example on Twilio's security page: token 12345, this address and these fields.
        params = {"CallSid": "CA1234567890ABCDE", "Caller": "+14158675310", "Digits": "1234", "From": "+14158675310", "To": "+18005551212"}
        self.assertEqual(V.signature("12345", "https://example.com/myapp.php?foo=1&bar=2", params), "L/OH5YylLD5NRKLltdqwSvS0BnU=")

    def test_unsigned_or_wrongly_signed_calls_are_refused(self):
        a = self.switch_on()
        c = self.app.test_client()
        path = f"/hooks/voice/{a['hook_key']}"
        self.assertEqual(c.post(path, data={"CallSid": "CA1", "From": "+233201112223"}).status_code, 403)
        self.assertEqual(c.post(path, data={"CallSid": "CA1"}, headers={"X-Twilio-Signature": "bad"}).status_code, 403)
        self.assertEqual(c.post("/hooks/voice/nope", data={}).status_code, 404)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM calls").fetchone()[0], 0)


class AccountTests(Phone):
    def test_details_are_checked_and_the_token_is_sealed(self):
        bad = [(("ACxyz", TOKEN, "+233302000111", "+233244000222"), "Account SID"),
               ((SID, "short", "+233302000111", "+233244000222"), "auth token"),
               ((SID, TOKEN, "0302000111", "+233244000222"), "international")]
        for args, word in bad:
            with self.assertRaises(V.CallError) as e:
                V.save_account(self.conn, self.c(), 1, SECRET, True, *args)
            self.assertIn(word, str(e.exception))
        with self.assertRaises(V.CallError):
            V.save_account(self.conn, self.c(), 1, SECRET, True, SID, "", "+233302000111", "+233244000222")   # no token yet
        a = self.switch_on()
        self.assertNotIn(TOKEN, a["auth_token"])
        self.assertTrue(V.is_on(self.conn, 1))
        self.assertFalse(V.is_on(self.conn, 2))
        V.save_account(self.conn, self.c(), 1, SECRET, True, "", "", "", "")          # empty boxes keep what is saved
        self.assertEqual(V.account(self.conn, 1)["auth_token"], a["auth_token"])
        logs = " ".join(str(tuple(r)) for r in self.conn.execute("SELECT * FROM audit_log").fetchall())
        self.assertNotIn(TOKEN, logs)

    def test_setup_screen_is_for_managers_and_shows_the_address(self):
        self.switch_on()
        self.assertIn("/hooks/voice/", self.page(self.login("mgr"), "/settings/phone"))
        self.assertNotEqual(self.login("desk").get("/settings/phone").status_code, 200)


class IncomingTests(Phone):
    def test_a_known_caller_pops_up_and_an_answered_call_is_logged(self):
        a = self.switch_on()
        tw = self.app.test_client()
        r = self.hook(tw, f"/hooks/voice/{a['hook_key']}", {"CallSid": "CA100", "From": "+233201112223", "To": "+233302000111"})
        xml = r.get_data(as_text=True)
        self.assertEqual(r.status_code, 200)
        self.assertIn("<Number>+233244000222</Number>", xml)
        self.assertIn('timeout="25"', xml)
        desk = self.login("desk")
        live = json.loads(desk.get("/calls/live").get_data(as_text=True))["calls"]
        self.assertEqual(len(live), 1)
        self.assertEqual(live[0]["name"], "Ama Mensah")
        self.assertIn("/patients/1", live[0]["url"])
        self.assertNotIn(b"Ama", self.login("mgr2").get("/calls/live").data)                 # clinic two sees nothing
        self.hook(tw, f"/hooks/voice/{a['hook_key']}/after", {"CallSid": "CA100", "DialCallStatus": "completed", "DialCallDuration": "75"})
        row = self.conn.execute("SELECT * FROM calls WHERE call_sid = 'CA100'").fetchone()
        self.assertEqual((row["status"], row["duration_s"], row["patient_id"]), ("completed", 75, 1))
        self.assertEqual(json.loads(desk.get("/calls/live").data)["calls"], [])
        self.assertEqual(self.conn.execute("SELECT kind FROM contact_log WHERE patient_id = 1").fetchone()[0], "call_in")
        page = self.page(desk, "/calls")
        self.assertIn("Ama Mensah", page)
        self.assertIn("1:15", page)

    def test_a_missed_call_makes_one_call_back_task(self):
        a = self.switch_on()
        tw = self.app.test_client()
        self.hook(tw, f"/hooks/voice/{a['hook_key']}", {"CallSid": "CA200", "From": "+233209999999", "To": "+233302000111"})
        r = self.hook(tw, f"/hooks/voice/{a['hook_key']}/after", {"CallSid": "CA200", "DialCallStatus": "no-answer"})
        self.assertIn("<Say language=", r.get_data(as_text=True))
        self.hook(tw, f"/hooks/voice/{a['hook_key']}/after", {"CallSid": "CA200", "DialCallStatus": "no-answer"})     # Twilio may retry
        tasks = self.conn.execute("SELECT * FROM tasks").fetchall()
        self.assertEqual(len(tasks), 1)
        self.assertIn("+233209999999", tasks[0]["title"])
        self.assertEqual(tasks[0]["priority"], "high")
        self.assertEqual(self.conn.execute("SELECT status FROM calls").fetchone()[0], "missed")
        self.hook(tw, f"/hooks/voice/{a['hook_key']}/status", {"CallSid": "CA200", "CallStatus": "completed"})
        self.assertEqual(self.conn.execute("SELECT status FROM calls").fetchone()[0], "missed")       # stays missed

    def test_a_switched_off_line_answers_politely(self):
        a = self.switch_on()
        V.save_account(self.conn, self.c(), 1, SECRET, False, "", "", "", "")
        self.conn.commit()
        r = self.hook(self.app.test_client(), f"/hooks/voice/{a['hook_key']}", {"CallSid": "CA300", "From": "+233201112223"})
        self.assertIn("<Say language=", r.get_data(as_text=True))


class OutgoingTests(Phone):
    def test_click_to_call_rings_the_desk_then_bridges_to_the_patient(self):
        a = self.switch_on()
        fake = FakeTwilio()
        user = self.conn.execute("SELECT * FROM users WHERE username = 'desk'").fetchone()
        call_id = V.call_patient(self.conn, self.c(), user, SECRET, 1, "https://clinic.example", transport=fake)
        self.conn.commit()
        method, url, headers, form = fake.calls[0]
        self.assertEqual(url, f"https://api.twilio.com/2010-04-01/Accounts/{SID}/Calls.json")
        self.assertTrue(headers["Authorization"].startswith("Basic "))
        self.assertEqual(form["To"], ["+233244000222"])           # the desk rings first
        self.assertEqual(form["From"], ["+233302000111"])
        row = self.conn.execute("SELECT * FROM calls WHERE id = ?", (call_id,)).fetchone()
        self.assertEqual((row["direction"], row["to_number"], row["status"]), ("out", "+233201112223", "queued"))
        bridge = urllib.parse.urlparse(form["Url"][0])
        self.assertEqual(bridge.netloc, "clinic.example")
        with self.app.app_context():
            pass
        xml = V.bridge_for(self.conn, a, urllib.parse.parse_qs(bridge.query)["ref"][0])
        self.assertIn("<Number>+233201112223</Number>", xml)
        self.assertIn('callerId="+233302000111"', xml)
        self.assertIsNone(V.bridge_for(self.conn, a, "wrong"))

    def test_refusals(self):
        user = self.conn.execute("SELECT * FROM users WHERE username = 'desk'").fetchone()
        with self.assertRaises(V.CallError):
            V.call_patient(self.conn, self.c(), user, SECRET, 1, "https://x", transport=FakeTwilio())     # not switched on
        self.switch_on()
        with self.assertRaises(V.CallError):
            V.call_patient(self.conn, self.c(), user, SECRET, 3, "https://x", transport=FakeTwilio())     # other clinic's patient
        with self.assertRaises(V.CallError) as e:
            V.call_patient(self.conn, self.c(), user, SECRET, 1, "https://x", transport=FakeTwilio(400))
        self.assertIn("unverified", str(e.exception))

    def test_the_call_button_shows_only_when_calls_are_on(self):
        desk = self.login("desk")
        self.assertNotIn("/patients/1/call", self.page(desk, "/patients/1"))
        self.switch_on()
        page = self.page(desk, "/patients/1")
        self.assertIn("/patients/1/call", page)
        self.assertIn('id="callpop"', page)
