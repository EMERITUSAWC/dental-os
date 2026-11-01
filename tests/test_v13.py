"""v0.13: optional AI help for clinical notes. No test touches the internet: the network call is replaced by a fake."""
import json
import unittest
from unittest import mock

from dental_os.services import ainotes as AI, notes
from tests.test_v07 import Comms

KEY = "test"
API_KEY = "sk-ant-this-is-a-fake-key-0123456789abcdef"


class Fake:
    """A stand-in for the AI service. Remembers each request and answers like the Messages API."""

    def __init__(self, reply=None, status=200, raw=None):
        self.calls = []
        self.status = status
        self.reply = reply if reply is not None else {"Reason for visit": "", "Teeth and restorations": "Caries on 46.", "Diagnosis": "",
                                                      "Treatment plan agreed": "Composite filling.", "Advice given": "", "Next visit": "Review in 6 months."}
        self.raw = raw

    def __call__(self, url, headers, body, timeout=40):
        self.calls.append({"url": url, "headers": headers, "body": json.loads(body)})
        if self.raw is not None:
            return self.status, self.raw
        if self.status != 200:
            return self.status, json.dumps({"type": "error", "error": {"type": "x", "message": "secret detail"}})
        return 200, json.dumps({"type": "message", "content": [{"type": "text", "text": json.dumps(self.reply)}], "stop_reason": "end_turn"})


class Ai(Comms):
    def turn_on(self, clinic=1, enabled=True, key=API_KEY, model=""):
        conn = self.db()
        AI.save_account(conn, self.clinic(clinic), None, KEY, enabled, key, model)
        conn.commit()
        conn.close()

    def clinic(self, cid=1):
        return self.one("SELECT * FROM clinics WHERE id = ?", cid)

    def many(self, q, *a):
        conn = self.db()
        rows = conn.execute(q, a).fetchall()
        conn.close()
        return rows

    def patient(self, pid=1):
        return self.one("SELECT * FROM patients WHERE id = ?", pid)

    def ask(self, fake, text="46 mod caries, comp, rv 6/12", template="exam", pid=1):
        conn = self.db()
        try:
            return AI.draft(conn, self.clinic(), {"id": 2}, KEY, self.patient(pid), template, text, transport=fake)
        finally:
            conn.commit()
            conn.close()

    def refuse(self, fn):
        conn = self.db()
        try:
            with self.assertRaises(AI.AIError) as ctx:
                fn(conn)
            return str(ctx.exception)
        finally:
            conn.rollback()
            conn.close()


class AccountTests(Ai):
    def test_it_is_off_until_a_manager_switches_it_on(self):
        conn = self.db()
        self.assertFalse(AI.is_on(conn, 1, KEY))
        conn.close()
        msg = self.refuse(lambda c: AI.draft(c, self.clinic(), {"id": 2}, KEY, self.patient(), "exam", "x", transport=Fake()))
        self.assertIn("not switched on", msg)

    def test_the_key_is_stored_encrypted_and_not_in_the_audit_log(self):
        self.turn_on()
        stored = self.one("SELECT api_key FROM ai_account WHERE clinic_id = 1")[0]
        self.assertNotIn(API_KEY, stored)
        self.assertNotIn(API_KEY, " ".join(str(tuple(r)) for r in self.many("SELECT * FROM audit_log")))
        conn = self.db()
        self.assertTrue(AI.is_on(conn, 1, KEY))
        self.assertFalse(AI.is_on(conn, 1, "another secret"))        # a changed secret means the key cannot be read: treated as off
        self.assertFalse(AI.is_on(conn, 2, KEY))
        conn.close()

    def test_switching_on_needs_a_key_and_a_sensible_key(self):
        conn = self.db()
        with self.assertRaises(AI.AIError):
            AI.save_account(conn, self.clinic(), None, KEY, True, "", "")
        conn.rollback()
        with self.assertRaises(AI.AIError):
            AI.save_account(conn, self.clinic(), None, KEY, True, "short key", "")
        conn.rollback()
        with self.assertRaises(AI.AIError):
            AI.save_account(conn, self.clinic(), None, KEY, True, API_KEY, "bad model!!")
        conn.rollback()
        conn.close()

    def test_leaving_the_key_empty_keeps_the_old_one_and_off_keeps_it_too(self):
        self.turn_on()
        self.turn_on(enabled=False, key="")
        self.assertEqual(self.one("SELECT enabled FROM ai_account")[0], 0)
        self.turn_on(enabled=True, key="", model="claude-haiku-5-5")
        row = self.one("SELECT enabled, model FROM ai_account")
        self.assertEqual((row[0], row[1]), (1, "claude-haiku-5-5"))
        self.assertEqual(self.ask(Fake())["f3"], "Caries on 46.")


class ScrubTests(Ai):
    def test_name_phone_and_email_are_removed(self):
        p = {"first_name": "Ama", "last_name": "Mensah-Boateng"}
        out = AI.scrub("Ama Mensah-Boateng called 0201112223 or +44 20 7946 0958, mail ama@example.com. Boateng agreed.", p)
        for bad in ("Ama", "Mensah", "Boateng", "0201112223", "7946", "example.com"):
            self.assertNotIn(bad, out)
        self.assertIn("the patient", out)
        self.assertNotIn("the patient the patient", out)

    def test_clinical_numbers_survive(self):
        text = "46 47 48 caries, 500 mg, rv 6/12, 11 12 13 14 21 22 23, 120/80"
        self.assertEqual(AI.scrub(text, {"first_name": "Ama", "last_name": "Mensah"}), text)

    def test_a_name_inside_another_word_is_left_alone(self):
        self.assertEqual(AI.scrub("Amalgam on 36", {"first_name": "Ama", "last_name": "Mensah"}), "Amalgam on 36")


class DraftTests(Ai):
    def setUp(self):
        super().setUp()
        self.turn_on()

    def test_the_request_has_the_shape_the_service_expects(self):
        fake = Fake()
        self.ask(fake)
        call = fake.calls[0]
        self.assertEqual(call["url"], "https://api.anthropic.com/v1/messages")
        self.assertEqual(call["headers"]["x-api-key"], API_KEY)
        self.assertEqual(call["headers"]["anthropic-version"], "2023-06-01")
        body = call["body"]
        self.assertEqual(body["model"], "claude-sonnet-5-5")
        self.assertEqual(body["messages"][0]["role"], "user")
        self.assertIn("<shorthand>", body["messages"][0]["content"])
        for h in notes.TEMPLATES["exam"][1]:
            self.assertIn(h, body["messages"][0]["content"])

    def test_only_the_scrubbed_shorthand_leaves_the_server(self):
        fake = Fake()
        first, last = self.patient()["first_name"], self.patient()["last_name"]
        self.ask(fake, f"{first} {last} 46 caries, phone 0201112223")
        sent = json.dumps(fake.calls[0]["body"])
        for bad in (first, last, "0201112223", "+233201112223"):
            self.assertNotIn(bad, sent)
        self.assertNotIn("patient_id", sent)

    def test_boxes_come_back_for_the_note_form(self):
        values = self.ask(Fake())
        names = notes.TEMPLATES["exam"][1]
        self.assertEqual(values["f%d" % names.index("Teeth and restorations")], "Caries on 46.")
        self.assertEqual(values["f%d" % names.index("Treatment plan agreed")], "Composite filling.")
        self.assertNotIn("f%d" % names.index("Reason for visit"), values)           # an empty box stays empty

    def test_a_draft_saves_no_note_and_the_log_holds_no_text(self):
        before = self.one("SELECT COUNT(*) FROM clinical_notes")[0]
        self.ask(Fake(), "secret shorthand words")
        self.assertEqual(self.one("SELECT COUNT(*) FROM clinical_notes")[0], before)
        log = self.many("SELECT action, detail FROM audit_log WHERE action = 'ai.draft'")
        self.assertEqual(len(log), 1)
        self.assertNotIn("secret", " ".join(str(tuple(r)) for r in self.many("SELECT * FROM audit_log")))
        self.assertNotIn("Caries", " ".join(str(tuple(r)) for r in self.many("SELECT * FROM audit_log")))

    def test_a_reply_wrapped_in_text_is_still_read_but_rubbish_is_refused(self):
        wrapped = json.dumps({"content": [{"type": "text", "text": 'Here you go:\n```json\n{"Note": "Review done."}\n```'}]})
        self.assertEqual(self.ask(Fake(raw=wrapped), template="free"), {"f0": "Review done."})
        for raw in (json.dumps({"content": [{"type": "text", "text": "I cannot help"}]}), "not json at all",
                    json.dumps({"content": []}), json.dumps({"content": [{"type": "text", "text": '{"Note": ""}'}]})):
            self.refuse(lambda c, raw=raw: AI.draft(c, self.clinic(), {"id": 2}, KEY, self.patient(), "free", "x", transport=Fake(raw=raw)))

    def test_a_long_answer_is_cut_to_the_size_of_a_box(self):
        out = self.ask(Fake(reply={"Note": "x" * 9000}), template="free")
        self.assertEqual(len(out["f0"]), AI.FIELD_MAX)

    def test_errors_are_plain_and_never_pass_on_the_services_words(self):
        for status, word in ((401, "refused the key"), (403, "refused the key"), (429, "busy"), (529, "busy"), (500, "error 500")):
            msg = self.refuse(lambda c, s=status: AI.draft(c, self.clinic(), {"id": 2}, KEY, self.patient(), "exam", "x", transport=Fake(status=s)))
            self.assertIn(word, msg)
            self.assertNotIn("secret detail", msg)

    def test_empty_long_and_unknown_inputs_are_refused_before_anything_is_sent(self):
        fake = Fake()
        for text, template in (("   ", "exam"), ("x" * (AI.MAX_INPUT + 1), "exam"), ("fine", "nonsense")):
            self.refuse(lambda c, t=text, k=template: AI.draft(c, self.clinic(), {"id": 2}, KEY, self.patient(), k, t, transport=fake))
        self.assertEqual(fake.calls, [])

    def test_there_is_a_limit_per_hour(self):
        fake = Fake()
        for _ in range(AI.PER_HOUR):
            self.ask(fake)
        self.refuse(lambda c: AI.draft(c, self.clinic(), {"id": 2}, KEY, self.patient(), "exam", "x", transport=fake))
        self.assertEqual(len(fake.calls), AI.PER_HOUR)

    def test_another_clinic_cannot_use_this_clinics_key(self):
        conn = self.db()
        msg = None
        try:
            with self.assertRaises(AI.AIError) as ctx:
                AI.draft(conn, self.clinic(2), {"id": 5}, KEY, self.patient(3), "exam", "x", transport=Fake())
            msg = str(ctx.exception)
        finally:
            conn.rollback()
            conn.close()
        self.assertIn("not switched on", msg)


class ScreenTests(Ai):
    def fake_on(self, fake):
        return mock.patch.object(AI, "default_transport", fake)

    def test_the_note_screen_shows_the_button_only_when_it_is_on(self):
        c = self.login("dent")
        self.assertNotIn("Draft with AI", self.page(c, "/patients/1/notes/new"))
        self.turn_on()
        self.assertIn("Draft with AI", self.page(c, "/patients/1/notes/new"))

    def test_a_draft_fills_the_boxes_and_saves_nothing(self):
        self.turn_on()
        c = self.login("dent")
        fake = Fake()
        with self.fake_on(fake):
            r = self.post(c, "/patients/1/notes/ai-draft", {"template": "exam", "shorthand": "46 mod caries", "tooth": "46"})
        page = r.get_data(as_text=True)
        self.assertEqual(r.status_code, 200)
        self.assertIn("Caries on 46.", page)
        self.assertIn("AI draft", page)
        self.assertIn("46 mod caries", page)                                  # the shorthand stays so it can be tried again
        self.assertEqual(self.one("SELECT COUNT(*) FROM clinical_notes")[0], 0)
        self.assertEqual(len(fake.calls), 1)

    def test_a_failure_shows_a_message_and_keeps_the_shorthand(self):
        self.turn_on()
        c = self.login("dent")
        with self.fake_on(Fake(status=429)):
            page = self.post(c, "/patients/1/notes/ai-draft", {"template": "exam", "shorthand": "keep me"}).get_data(as_text=True)
        self.assertIn("busy", page)
        self.assertIn("keep me", page)
        self.assertEqual(self.one("SELECT COUNT(*) FROM audit_log WHERE action = 'ai.draft'")[0], 1)        # the attempt is still counted

    def test_the_front_desk_cannot_ask_and_another_clinics_patient_is_not_found(self):
        self.turn_on()
        with self.fake_on(Fake()) as _:
            self.assertEqual(self.post(self.login("desk"), "/patients/1/notes/ai-draft", {"template": "exam", "shorthand": "x"}).status_code, 403)
            self.assertEqual(self.post(self.login("dent"), "/patients/3/notes/ai-draft", {"template": "exam", "shorthand": "x"}).status_code, 404)

    def test_a_draft_is_asked_for_through_a_post_with_the_token(self):
        self.turn_on()
        c = self.login("dent")
        self.assertEqual(c.post("/patients/1/notes/ai-draft", data={"template": "exam", "shorthand": "x"}).status_code, 400)

    def test_the_settings_panel_saves_and_never_shows_the_key(self):
        c = self.login("mgr")
        page = self.post(c, "/settings", {"action": "ai_account", "enabled": "1", "api_key": API_KEY, "model": "claude-sonnet-5-5"},
                         follow_redirects=True).get_data(as_text=True)
        self.assertIn("AI help for clinical notes", page)
        self.assertNotIn(API_KEY, page)
        self.assertIn("saved, not shown", page)
        conn = self.db()
        self.assertTrue(AI.is_on(conn, 1, KEY))
        conn.close()
        bad = self.post(c, "/settings", {"action": "ai_account", "enabled": "1", "api_key": "short"}, follow_redirects=True).get_data(as_text=True)
        self.assertIn("does not look right", bad)
        self.assertEqual(self.post(self.login("dent"), "/settings", {"action": "ai_account", "enabled": "1", "api_key": API_KEY}).status_code, 403)

    def test_a_signed_note_is_still_needed(self):
        """The draft goes through the ordinary save: a dentist saves it as a draft note, and signing is a separate step."""
        self.turn_on()
        c = self.login("dent")
        with self.fake_on(Fake()):
            self.post(c, "/patients/1/notes/ai-draft", {"template": "exam", "shorthand": "x"})
        self.post(c, "/patients/1/notes/new", {"template": "exam", "f3": "Caries on 46."})
        row = self.one("SELECT status FROM clinical_notes")
        self.assertEqual(row[0], "draft")


if __name__ == "__main__":
    unittest.main()
