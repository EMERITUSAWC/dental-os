"""v0.65: the assistant. A fake AI service plays the model's part and records everything Dental OS sends it."""
import datetime
import json
import re

from tests.test_v04 import Base
from dental_os.services import ainotes as AI, assistant as A
from dental_os.services.timeutil import now_local

AIKEY = "sk-ant-test-0123456789abcdefghij"


class FakeClaude:
    """script: a list of functions. Each gets the request body and returns the reply's content blocks."""
    def __init__(self, script, status=200):
        self.script, self.sent, self.status = list(script), [], status

    def __call__(self, url, headers, body, timeout=40):
        req = json.loads(body)
        self.sent.append(req)
        if self.status != 200:
            return self.status, json.dumps({"type": "error"})
        step = self.script.pop(0)
        blocks = step(req)
        stop = "tool_use" if any(b["type"] == "tool_use" for b in blocks) else "end_turn"
        return 200, json.dumps({"content": blocks, "stop_reason": stop})

    def raw(self):
        return json.dumps(self.sent)


def call(tool, **inp):
    return lambda req: [{"type": "tool_use", "id": f"toolu_{tool}", "name": tool, "input": inp}]


def last_result(req):
    return json.loads(req["messages"][-1]["content"][0]["content"])


def say(fn):
    return lambda req: [{"type": "text", "text": fn(req)}]


class Asst(Base):
    def setUp(self):
        super().setUp()
        self.conn = self.db()
        clinic = self.conn.execute("SELECT * FROM clinics WHERE id = 1").fetchone()
        AI.save_account(self.conn, clinic, 1, "test", False, AIKEY, None)
        self.conn.execute("INSERT INTO chairs(clinic_id, name) VALUES (1, 'Chair 1')")
        self.tomorrow = (now_local(clinic["timezone"]).date() + datetime.timedelta(days=1)).isoformat()
        self.conn.execute("""INSERT INTO appointments(clinic_id, patient_id, provider_id, chair_id, start_at, duration_min, status, category, confirmation)
                             VALUES (1, 1, 1, 1, ?, 30, 'scheduled', 'Consultation', 'sent')""", (f"{self.tomorrow} 09:00",))
        self.conn.execute("""INSERT INTO appointments(clinic_id, patient_id, provider_id, chair_id, start_at, duration_min, status, category, confirmation)
                             VALUES (1, 2, 1, 1, ?, 30, 'scheduled', 'Cleaning', 'confirmed')""", (f"{self.tomorrow} 10:00",))
        self.conn.execute("INSERT INTO patient_alerts(clinic_id, patient_id, kind, severity, text) VALUES (1, 1, 'allergy', 'critical', 'Penicillin')")
        self.conn.commit()
        self.app.config["SECRET_KEY"] = "test"

    def tearDown(self):
        self.conn.close()

    def switch_on(self):
        self.post(self.login("mgr"), "/settings/assistant", {"on": "1"})

    def ask(self, who, question, fake):
        self.app.config["ASSISTANT_TRANSPORT"] = fake
        return self.post(self.login(who), "/assistant", {"q": question}).get_data(as_text=True)


class SwitchTests(Asst):
    def test_off_until_a_manager_switches_it_on(self):
        dent = self.login("dent")
        self.assertNotIn('href="/assistant"', self.page(dent, "/"))
        self.assertIn("not switched on", self.page(dent, "/assistant"))
        self.assertEqual(self.post(dent, "/settings/assistant", {"on": "1"}).status_code, 403)
        self.switch_on()
        self.assertTrue(A.is_on(self.conn, 1, "test"))
        self.assertFalse(AI.is_on(self.conn, 1, "test"))                       # note drafting stays off: a separate switch
        self.assertIn('href="/assistant"', self.page(dent, "/"))
        self.assertFalse(A.is_on(self.conn, 2, "test"))


class PrivacyTests(Asst):
    def test_names_never_leave_and_come_back_on_screen(self):
        self.switch_on()
        holder = {}

        def answer(req):
            visits = last_result(req)["visits"]
            holder["code"] = next(v["patient"] for v in visits if v["confirmation"] != "confirmed")
            return f"[[{holder['code']}]] has not confirmed the 09:00 visit."
        fake = FakeClaude([call("unconfirmed", date="tomorrow"), say(answer)])
        page = self.ask("desk", "Who has not confirmed for tomorrow?", fake)
        self.assertIn('<a href="/patients/1">Ama Mensah</a> has not confirmed the 09:00 visit.', page)
        sent = fake.raw()
        for private in ("Ama", "Mensah", "Kofi", "Young", "201112223", "1990-05-01"):
            self.assertNotIn(private, sent)
        self.assertIn(holder["code"], sent)
        self.assertNotIn("Kofi Young", page)                                  # the confirmed one is not listed

    def test_a_name_or_number_typed_in_the_question_is_not_sent(self):
        self.switch_on()
        fake = FakeClaude([say(lambda req: "Noted.")])
        self.ask("desk", "When is ama mensah coming in? Her number is +233 20 111 2223 and email ama@example.com", fake)
        q = fake.sent[0]["messages"][0]["content"]
        self.assertNotIn("mensah", q.lower())
        self.assertNotIn("2223", q)
        self.assertNotIn("ama@example.com", q)
        self.assertRegex(q, r"\[\[P[A-Z2-7]{4}\]\]")

    def test_codes_change_every_question(self):
        self.switch_on()
        codes = []

        def keep(req):
            codes.append(last_result(req)["patients"][0]["patient"])
            return "ok"
        for _ in range(2):
            self.ask("desk", "Find Mensah", FakeClaude([call("find_patient", **{"name": "Mensah"}), say(keep)]))
        self.assertNotEqual(codes[0], codes[1])

    def test_the_ai_text_is_never_trusted_as_html(self):
        self.switch_on()
        page = self.ask("desk", "Hello", FakeClaude([say(lambda req: "<script>alert(1)</script> [[PZZZZ]]")]))
        self.assertNotIn("<script>alert(1)</script>", page)
        self.assertIn("&lt;script&gt;", page)
        self.assertIn("(unknown patient)", page)

    def test_what_is_kept(self):
        self.switch_on()
        self.ask("desk", "Who has not confirmed for tomorrow?", FakeClaude([call("unconfirmed", date="tomorrow"), say(lambda r: "One.")]))
        log = self.conn.execute("SELECT action, detail, object_id FROM audit_log WHERE action LIKE 'ai.assistant%' ORDER BY id").fetchall()
        self.assertIn(("ai.assistant", "unconfirmed", None), [tuple(r) for r in log])
        self.assertIn(("ai.assistant.read", None, 1), [tuple(r) for r in log])               # in Ama's "who looked" log
        everything = " ".join(str(v) for r in self.conn.execute("SELECT * FROM audit_log").fetchall() for v in tuple(r))
        self.assertNotIn("not confirmed for tomorrow", everything)                        # the question itself is not stored


class PermissionTests(Asst):
    def test_tools_follow_the_persons_permissions(self):
        self.switch_on()
        desk = {t["name"] for t in A.tools_for(self.conn, self.conn.execute("SELECT * FROM users WHERE username = 'desk'").fetchone())}
        mgr = {t["name"] for t in A.tools_for(self.conn, self.conn.execute("SELECT * FROM users WHERE username = 'mgr'").fetchone())}
        self.assertNotIn("takings", desk)
        self.assertIn("takings", mgr)
        fake = FakeClaude([call("takings", start="2026-01-01", end="2026-12-31"), say(lambda req: json.dumps(last_result(req)))])
        page = self.ask("desk", "How much did we take this year?", fake)
        self.assertIn("may not see that", page)
        self.assertNotIn("takings", json.dumps(fake.sent[0]["tools"]))

    def test_medical_alerts_only_for_clinical_staff(self):
        self.switch_on()

        def overview(name):
            def step(req):
                code = last_result(req)["patients"][0]["patient"]
                return [{"type": "tool_use", "id": "t2", "name": "patient_overview", "input": {"patient": code}}]
            return [call("find_patient", name="Mensah"), step, say(lambda req: json.dumps(last_result(req)))]
        desk_page = self.ask("desk", "Tell me about Mensah", FakeClaude(overview("desk")))
        self.assertNotIn("Penicillin", desk_page)
        dent_page = self.ask("dent", "Tell me about Mensah", FakeClaude(overview("dent")))
        self.assertIn("Penicillin", dent_page)

    def test_another_clinic_cannot_be_reached(self):
        self.switch_on()
        fake = FakeClaude([call("patient_overview", patient="P3333"), say(lambda req: json.dumps(last_result(req)))])
        self.assertIn("No patient with that code", self.ask("dent", "Patient 3?", fake))
        fake = FakeClaude([call("find_patient", name="Other"), say(lambda req: json.dumps(last_result(req)))])
        page = self.ask("dent", "Find Other", fake)
        self.assertIn('"patients": []', page.replace("&#34;", '"'))                # clinic two's patient is not found


class LimitTests(Asst):
    def test_errors_and_limits(self):
        self.switch_on()
        self.assertIn("refused the key", self.ask("desk", "Hi", FakeClaude([], status=401)))
        self.assertIn("Type a question first", self.ask("desk", "   ", FakeClaude([])))
        loop = FakeClaude([call("list_dentists")] * A.MAX_ROUNDS)
        self.assertIn("could not finish an answer", self.ask("desk", "Loop forever", loop))
        self.assertEqual(len(loop.sent), A.MAX_ROUNDS)
        self.conn.executemany("INSERT INTO audit_log(clinic_id, user_id, action) VALUES (1, 4, 'ai.assistant')", [()] * A.PER_HOUR)
        self.conn.commit()
        fake = FakeClaude([say(lambda req: "x")])
        self.assertIn("A lot of questions", self.ask("desk", "Hi", fake))
        self.assertEqual(fake.sent, [])

    def test_free_slots_and_dentists(self):
        self.switch_on()
        fake = FakeClaude([call("list_dentists"), call("free_slots", date="tomorrow", dentist_id=1, minutes=45),
                           say(lambda req: json.dumps(last_result(req)))])
        page = self.ask("desk", "Next free 45 minutes with Dr 1 tomorrow?", fake)
        self.assertIn('"minutes": 45', page.replace("&#34;", '"'))
        self.assertEqual(json.loads(fake.sent[1]["messages"][-1]["content"][0]["content"])["dentists"][0]["name"], "Dr 1")
