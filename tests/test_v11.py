"""v0.11: the official WhatsApp Business API. No test touches the internet: the network call is replaced by a fake."""
import hashlib
import hmac
import json
import unittest
from unittest import mock

from tests import dbmeta
from dental_os.services import outbox, whatsapp as WA
from tests.test_v07 import Comms

KEY = "test"                                         # the SECRET_KEY the test app is made with
TOKEN = "EAAB-this-is-a-long-fake-access-token-1234567890"
SECRET = "app-secret-0123456789abcdef"


class Fake:
    """A stand-in for Meta. Remembers every request and answers like the Cloud API."""

    def __init__(self, status=200, body=None):
        self.calls = []
        self.status = status
        self.body = body
        self.n = 0

    def __call__(self, url, headers, body, timeout=10):
        self.calls.append({"url": url, "headers": headers, "body": json.loads(body) if body else None})
        if self.body is not None:
            return self.status, json.dumps(self.body)
        self.n += 1
        return self.status, json.dumps({"messaging_product": "whatsapp", "contacts": [{"input": "x", "wa_id": "x"}],
                                        "messages": [{"id": f"wamid.TEST{self.n}"}]})


class Wa(Comms):
    def connect(self, clinic=1, pid="1234567890", enabled=True, auto=False):
        conn = self.db()
        WA.save_account(conn, self.clinic(clinic), None, KEY, enabled, pid, TOKEN, SECRET, "v22.0", auto)
        conn.commit()
        conn.close()

    def mapping(self, kind="reminder", lang="en", name="visit_reminder", params="first_name,clinic,dentist,day,time", clinic=1):
        conn = self.db()
        WA.save_mapping(conn, self.clinic(clinic), None, kind, lang, name, "", params)
        conn.commit()
        conn.close()

    def many(self, q, *a):
        conn = self.db()
        rows = conn.execute(q, a).fetchall()
        conn.close()
        return rows

    def refuse(self, fn, exc=Exception):
        """Run fn(conn), expect it to raise, and let go of the connection the way a failed request does. Returns the error."""
        conn = self.db()
        try:
            with self.assertRaises(exc) as ctx:
                fn(conn)
            return ctx.exception
        finally:
            conn.rollback()
            conn.close()

    def clinic(self, cid=1):
        return dict(self.db().execute("SELECT * FROM clinics WHERE id = ?", (cid,)).fetchone())

    def sign(self, body, secret=SECRET):
        return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

    def hook(self, clinic=1):
        return self.one("SELECT hook_key, verify_token FROM wa_account WHERE clinic_id = ?", clinic)

    def post_hook(self, payload, key=None, secret=SECRET, clinic=1, sig=True):
        raw = json.dumps(payload).encode()
        headers = {"Content-Type": "application/json"}
        if sig:
            headers["X-Hub-Signature-256"] = self.sign(raw, secret)
        return self.app.test_client().post(f"/hooks/whatsapp/{key or self.hook(clinic)[0]}", data=raw, headers=headers)

    def status_payload(self, wamid, status, pid="1234567890", errors=None):
        s = {"id": wamid, "status": status, "timestamp": "1700000000", "recipient_id": "233201112223"}
        if errors:
            s["errors"] = errors
        return {"object": "whatsapp_business_account", "entry": [{"id": "1", "changes": [{"field": "messages", "value": {
            "messaging_product": "whatsapp", "metadata": {"display_phone_number": "233", "phone_number_id": pid}, "statuses": [s]}}]}]}

    def inbound_payload(self, text, sender="233201112223", wamid="wamid.IN1", pid="1234567890", kind="text"):
        m = {"from": sender, "id": wamid, "timestamp": "1700000000", "type": kind}
        if kind == "text":
            m["text"] = {"body": text}
        return {"object": "whatsapp_business_account", "entry": [{"id": "1", "changes": [{"field": "messages", "value": {
            "metadata": {"display_phone_number": "233", "phone_number_id": pid}, "contacts": [{"wa_id": sender, "profile": {"name": "Ama"}}],
            "messages": [m]}}]}]}


class AccountTests(Wa):
    def test_the_token_and_secret_are_stored_encrypted_and_never_logged(self):
        self.connect()
        raw = self.one("SELECT * FROM wa_account WHERE clinic_id = 1")
        self.assertNotIn(TOKEN, str(tuple(raw)))
        self.assertNotIn(SECRET, str(tuple(raw)))
        log = " ".join(str(tuple(r)) for r in self.many("SELECT * FROM audit_log"))
        self.assertNotIn(TOKEN, log)
        self.assertNotIn(SECRET, log)
        creds = WA.credentials(self.db(), 1, KEY)
        self.assertEqual(creds["token"], TOKEN)
        self.assertEqual(creds["phone_number_id"], "1234567890")

    def test_a_changed_secret_key_switches_it_off_safely(self):
        self.connect()
        self.assertIsNone(WA.credentials(self.db(), 1, "another-key"))
        self.assertFalse(WA.is_on(self.db(), 1, "another-key"))

    def test_it_cannot_be_switched_on_without_everything(self):
        conn = self.db()
        with self.assertRaises(WA.WhatsAppError):
            WA.save_account(conn, self.clinic(), None, KEY, True, "1234567890", TOKEN, "", "v22.0", False)
        with self.assertRaises(WA.WhatsAppError):
            WA.save_account(conn, self.clinic(), None, KEY, True, "", TOKEN, SECRET, "v22.0", False)
        WA.save_account(conn, self.clinic(), None, KEY, False, "", "", "", "v22.0", False)          # off needs nothing

    def test_bad_values_are_refused(self):
        conn = self.db()
        for pid, tok, sec, ver in (("12ab", TOKEN, SECRET, "v22.0"), ("1234567890", "short", SECRET, "v22.0"),
                                   ("1234567890", TOKEN, "tiny", "v22.0"), ("1234567890", TOKEN, SECRET, "22")):
            with self.assertRaises(WA.WhatsAppError):
                WA.save_account(conn, self.clinic(), None, KEY, False, pid, tok, sec, ver, False)

    def test_saving_again_without_a_token_keeps_the_old_one(self):
        self.connect()
        conn = self.db()
        WA.save_account(conn, self.clinic(), None, KEY, True, "1234567890", "", "", "v22.0", False)
        conn.commit()
        self.assertEqual(WA.credentials(self.db(), 1, KEY)["token"], TOKEN)

    def test_one_number_belongs_to_one_clinic(self):
        self.connect(clinic=1)
        conn = self.db()
        with self.assertRaises(WA.WhatsAppError):
            WA.save_account(conn, self.clinic(2), None, KEY, True, "1234567890", TOKEN, SECRET, "v22.0", False)

    def test_auto_send_needs_the_api_on(self):
        self.connect(enabled=False, auto=True)
        self.assertEqual(self.one("SELECT auto_send FROM wa_account WHERE clinic_id = 1")[0], 0)


class TemplateTests(Wa):
    def test_the_clinics_wording_becomes_a_template_to_paste_at_meta(self):
        text, order = WA.suggestion(self.db(), 1, "reminder", "en")
        self.assertIn("{{1}}", text)
        self.assertNotIn("{first_name}", text)
        self.assertEqual(order[:2], ["first_name", "clinic"])
        self.assertEqual(len(order), len(set(order)))

    def test_mapping_rules(self):
        self.mapping()
        m = WA.get_mapping(self.db(), 1, "reminder", "en")
        self.assertEqual((m["template_name"], m["language_code"]), ("visit_reminder", "en"))
        conn = self.db()
        for bad in (dict(name="Visit Reminder"), dict(name="ok_name", params="first_name,salary"), dict(name="ok_name", code="english")):
            with self.assertRaises(WA.WhatsAppError):
                WA.save_mapping(conn, self.clinic(), None, "reminder", "en", bad["name"], bad.get("code", ""), bad.get("params", ""))
        with self.assertRaises(LookupError):
            WA.save_mapping(conn, self.clinic(), None, "nonsense", "en", "x", "", "")
        WA.save_mapping(conn, self.clinic(), None, "reminder", "en", "", "", "")                 # empty name removes it
        conn.commit()
        self.assertIsNone(WA.get_mapping(self.db(), 1, "reminder", "en"))

    def test_a_parameter_is_cleaned_for_meta(self):
        body = WA.template_payload("233201112223", "t", "en", ["Ama", "", "line one\nline two", "a     b"])
        values = [p["text"] for p in body["template"]["components"][0]["parameters"]]
        self.assertEqual(values, ["Ama", "-", "line one line two", "a b"])
        self.assertNotIn("components", WA.template_payload("1", "t", "en", [])["template"])


class SendTests(Wa):
    def setUp(self):
        super().setUp()
        self.connect()
        self.fake = Fake()

    def queue_reminder(self):
        self.appt(days=1, hour=10)
        conn = self.db()
        outbox.build(conn, self.clinic())
        conn.commit()
        return self.one("SELECT id FROM outbox WHERE clinic_id = 1 AND kind = 'reminder'")[0]

    def test_a_template_message_has_the_shape_meta_expects(self):
        self.mapping()
        oid = self.queue_reminder()
        conn = self.db()
        mid = WA.deliver_outbox(conn, self.clinic(), 1, KEY, oid, self.fake)
        conn.commit()
        call = self.fake.calls[0]
        self.assertEqual(call["url"], "https://graph.facebook.com/v22.0/1234567890/messages")
        self.assertEqual(call["headers"]["Authorization"], "Bearer " + TOKEN)
        b = call["body"]
        self.assertEqual((b["messaging_product"], b["type"], b["to"]), ("whatsapp", "template", "233201112223"))
        self.assertEqual(b["template"]["name"], "visit_reminder")
        self.assertEqual(b["template"]["language"], {"code": "en"})
        params = b["template"]["components"][0]["parameters"]
        self.assertEqual(len(params), 5)
        self.assertEqual(params[0], {"type": "text", "text": "Ama"})
        m = self.one("SELECT * FROM messages WHERE id = ?", mid)
        self.assertEqual((m["status"], m["provider_id"], m["kind"]), ("sent", "wamid.TEST1", "reminder"))
        self.assertEqual(self.one("SELECT status FROM outbox WHERE id = ?", oid)[0], "sent")
        self.assertEqual(self.one("SELECT confirmation FROM appointments WHERE clinic_id = 1")[0], "sent")

    def test_no_template_and_no_recent_reply_means_a_person_sends_it(self):
        oid = self.queue_reminder()
        err = self.refuse(lambda conn: WA.deliver_outbox(conn, self.clinic(), 1, KEY, oid, self.fake), outbox.MessageError)
        self.assertIn("template", str(err))
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(self.one("SELECT status FROM outbox WHERE id = ?", oid)[0], "queued")

    def test_a_recent_reply_allows_plain_text(self):
        oid = self.queue_reminder()
        self.sql("INSERT INTO messages(clinic_id, patient_id, kind, direction, body, status) VALUES (1,1,'reply','in','hello','received')")
        conn = self.db()
        WA.deliver_outbox(conn, self.clinic(), 1, KEY, oid, self.fake)
        b = self.fake.calls[0]["body"]
        self.assertEqual(b["type"], "text")
        self.assertIn("Ama", b["text"]["body"])

    def test_an_old_reply_does_not_open_the_window(self):
        oid = self.queue_reminder()
        self.sql("INSERT INTO messages(clinic_id, patient_id, kind, direction, body, status, created_at) "
                 "VALUES (1,1,'reply','in','hello','received', datetime('now','-30 hours'))")
        self.refuse(lambda conn: WA.deliver_outbox(conn, self.clinic(), 1, KEY, oid, self.fake), outbox.MessageError)

    def test_consent_is_checked_again_at_the_moment_of_sending(self):
        self.mapping()
        oid = self.queue_reminder()
        self.sql("UPDATE patients SET reminders_ok = 0 WHERE id = 1")
        self.refuse(lambda conn: WA.deliver_outbox(conn, self.clinic(), 1, KEY, oid, self.fake), outbox.MessageError)
        self.assertEqual(self.fake.calls, [])
        self.sql("UPDATE patients SET reminders_ok = 1, status = 'archived' WHERE id = 1")
        self.refuse(lambda conn: WA.deliver_outbox(conn, self.clinic(), 1, KEY, oid, self.fake), outbox.MessageError)

    def test_offers_need_the_offers_consent(self):
        self.mapping("birthday", params="first_name,clinic")
        p = dict(self.db().execute("SELECT * FROM patients WHERE id = 1").fetchone())
        self.refuse(lambda conn: WA.deliver(conn, self.clinic(), 1, KEY, p, "birthday", "offer", {"first_name": "Ama", "clinic": "C"}, "hi", None, self.fake),
                    WA.WhatsAppError)
        self.sql("UPDATE patients SET offers_ok = 1 WHERE id = 1")
        p = dict(self.db().execute("SELECT * FROM patients WHERE id = 1").fetchone())
        conn = self.db()
        WA.deliver(conn, self.clinic(), 1, KEY, p, "birthday", "offer", {"first_name": "Ama", "clinic": "C"}, "hi", None, self.fake)
        conn.commit()
        conn.close()
        self.assertEqual(len(self.fake.calls), 1)

    def test_meta_refusing_logs_nothing_and_keeps_the_message_queued(self):
        self.mapping()
        oid = self.queue_reminder()
        bad = Fake(400, {"error": {"message": "Param error", "type": "OAuthException", "code": 132000,
                                   "error_data": {"details": "number of localizable_params does not match"}}})
        err = self.refuse(lambda conn: WA.deliver_outbox(conn, self.clinic(), 1, KEY, oid, bad), outbox.MessageError)
        self.assertIn("132000", str(err))
        self.assertNotIn(TOKEN, str(err))
        self.assertEqual(self.one("SELECT COUNT(*) FROM messages")[0], 0)
        self.assertEqual(self.one("SELECT status FROM outbox WHERE id = ?", oid)[0], "queued")

    def test_a_reply_without_a_message_number_is_not_treated_as_sent(self):
        self.mapping()
        oid = self.queue_reminder()
        self.refuse(lambda conn: WA.deliver_outbox(conn, self.clinic(), 1, KEY, oid, Fake(200, {"messages": []})), outbox.MessageError)
        self.assertEqual(self.one("SELECT COUNT(*) FROM messages")[0], 0)

    def test_send_ready_sends_what_it_can_and_leaves_the_rest(self):
        self.mapping()
        self.queue_reminder()
        self.appt(days=1, hour=11, pid=2)
        self.sql("UPDATE patients SET reminders_ok = 1 WHERE id = 2")
        conn = self.db()
        outbox.build(conn, self.clinic())
        conn.commit()
        self.sql("UPDATE patients SET language = 'fr' WHERE id = 2")                  # no French template: stays for a person
        conn = self.db()
        res = WA.send_ready(conn, self.clinic(), 1, KEY, transport=self.fake)
        conn.commit()
        self.assertEqual((res["sent"], res["left"], res["stopped"]), (1, 1, None))

    def test_a_network_failure_stops_the_run(self):
        self.mapping()
        self.queue_reminder()
        self.appt(days=1, hour=11, pid=2)
        self.sql("UPDATE patients SET reminders_ok = 1 WHERE id = 2")
        conn = self.db()
        outbox.build(conn, self.clinic())

        def down(url, headers, body, timeout=10):
            raise WA.WhatsAppError("Could not reach WhatsApp: timeout")
        res = WA.send_ready(conn, self.clinic(), 1, KEY, transport=down)
        self.assertEqual(res["sent"], 0)
        self.assertIn("Could not reach", res["stopped"])
        self.assertEqual(res["left"], 2)

    def test_the_visit_page_buttons_send_a_confirmation(self):
        self.mapping("confirmation", params="first_name,clinic,dentist,day,time")
        aid = self.appt(days=2)
        conn = self.db()
        p = dict(conn.execute("SELECT * FROM patients WHERE id = 1").fetchone())
        a = conn.execute("SELECT a.*, pr.name AS provider_name FROM appointments a LEFT JOIN providers pr ON pr.id = a.provider_id WHERE a.id = ?", (aid,)).fetchone()
        WA.send_appointment(conn, self.clinic(), 1, KEY, p, a, "confirmation", self.fake)
        conn.commit()
        self.assertEqual(self.fake.calls[0]["body"]["template"]["name"], "visit_reminder")
        self.assertEqual(self.one("SELECT confirmation FROM appointments WHERE id = ?", aid)[0], "sent")

    def test_check_connection(self):
        ok = Fake(200, {"display_phone_number": "+233 20 111 2223", "verified_name": "Demo Clinic"})
        text = WA.check_connection(self.db(), self.clinic(), KEY, ok)
        self.assertIn("+233 20 111 2223", text)
        self.assertEqual(ok.calls[0]["url"], "https://graph.facebook.com/v22.0/1234567890?fields=display_phone_number,verified_name")
        with self.assertRaises(WA.WhatsAppError):
            WA.check_connection(self.db(), self.clinic(), KEY, Fake(401, {"error": {"message": "Invalid OAuth access token", "code": 190}}))


class WebhookTests(Wa):
    def setUp(self):
        super().setUp()
        self.connect()

    def sent(self, wamid="wamid.OUT1", appt=None):
        self.sql("INSERT INTO messages(clinic_id, patient_id, appointment_id, kind, body, status, provider_id) VALUES (1,1,?, 'reminder','x','sent',?)", appt, wamid)

    def status(self, wamid="wamid.OUT1"):
        return self.one("SELECT status, error FROM messages WHERE provider_id = ?", wamid)

    def test_meta_proves_the_address_with_the_verify_token(self):
        key, token = self.hook()
        c = self.app.test_client()
        r = c.get(f"/hooks/whatsapp/{key}?hub.mode=subscribe&hub.verify_token={token}&hub.challenge=12345")
        self.assertEqual((r.status_code, r.get_data(as_text=True)), (200, "12345"))
        self.assertEqual(c.get(f"/hooks/whatsapp/{key}?hub.mode=subscribe&hub.verify_token=wrong&hub.challenge=1").status_code, 403)
        self.assertEqual(c.get(f"/hooks/whatsapp/{key}?hub.mode=unsubscribe&hub.verify_token={token}&hub.challenge=1").status_code, 403)
        self.assertEqual(c.get(f"/hooks/whatsapp/{key}").status_code, 403)
        self.assertEqual(c.get(f"/hooks/whatsapp/no-such-key?hub.mode=subscribe&hub.verify_token={token}&hub.challenge=1").status_code, 404)

    def test_an_unsigned_or_wrongly_signed_message_is_refused(self):
        self.sent()
        p = self.status_payload("wamid.OUT1", "delivered")
        self.assertEqual(self.post_hook(p, sig=False).status_code, 403)
        self.assertEqual(self.post_hook(p, secret="some-other-secret-value").status_code, 403)
        self.assertEqual(self.status()[0], "sent")
        self.assertEqual(self.post_hook(p).status_code, 200)
        self.assertEqual(self.status()[0], "delivered")

    def test_no_secret_saved_means_nothing_is_accepted(self):
        self.sql("UPDATE wa_account SET app_secret = NULL")
        self.assertEqual(self.post_hook(self.status_payload("wamid.OUT1", "delivered")).status_code, 403)

    def test_a_signature_over_a_different_body_fails(self):
        raw = json.dumps(self.status_payload("wamid.OUT1", "read")).encode()
        r = self.app.test_client().post(f"/hooks/whatsapp/{self.hook()[0]}", data=raw + b" ", headers={"X-Hub-Signature-256": self.sign(raw)})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.app.test_client().post(f"/hooks/whatsapp/{self.hook()[0]}", data=raw, headers={"X-Hub-Signature-256": "sha256=zz"}).status_code, 403)

    def test_bad_json_and_huge_bodies(self):
        raw = b"{not json"
        r = self.app.test_client().post(f"/hooks/whatsapp/{self.hook()[0]}", data=raw, headers={"X-Hub-Signature-256": self.sign(raw)})
        self.assertEqual(r.status_code, 400)
        big = b" " * (WA.MAX_BODY + 10)
        r = self.app.test_client().post(f"/hooks/whatsapp/{self.hook()[0]}", data=big, headers={"X-Hub-Signature-256": self.sign(big)})
        self.assertEqual(r.status_code, 413)

    def test_delivery_only_moves_forward_and_a_repeat_changes_nothing(self):
        self.sent()
        for st in ("delivered", "read", "delivered", "sent"):
            self.assertEqual(self.post_hook(self.status_payload("wamid.OUT1", st)).status_code, 200)
        self.assertEqual(self.status()[0], "read")
        before = self.one("SELECT COUNT(*) FROM wa_events")[0]
        self.post_hook(self.status_payload("wamid.OUT1", "read"))
        self.assertEqual(self.one("SELECT COUNT(*) FROM wa_events")[0], before)

    def test_a_failed_message_keeps_the_reason_and_marks_the_visit(self):
        aid = self.appt(days=2, confirmation="sent")
        self.sent(appt=aid)
        self.post_hook(self.status_payload("wamid.OUT1", "failed", errors=[{"code": 131026, "title": "Message undeliverable"}]))
        st = self.status()
        self.assertEqual(st[0], "failed")
        self.assertIn("131026", st[1])
        self.assertEqual(self.one("SELECT confirmation FROM appointments WHERE id = ?", aid)[0], "failed")

    def test_a_read_message_is_not_turned_into_failed(self):
        self.sent()
        self.post_hook(self.status_payload("wamid.OUT1", "read"))
        self.post_hook(self.status_payload("wamid.OUT1", "failed", errors=[{"code": 1, "title": "x"}]))
        self.assertEqual(self.status()[0], "read")

    def test_a_reply_is_filed_under_the_patient_and_counts_as_unread(self):
        r = self.post_hook(self.inbound_payload("Yes I will come"))
        self.assertEqual(r.status_code, 200)
        m = self.one("SELECT * FROM messages WHERE direction = 'in'")
        self.assertEqual((m["patient_id"], m["body"], m["kind"], m["read_at"]), (1, "Yes I will come", "reply", None))
        self.assertEqual(outbox.attention(self.db(), self.clinic(), {"role": "manager"})["replies"], 1)
        self.post_hook(self.inbound_payload("Yes I will come"))                                   # Meta retried
        self.assertEqual(self.one("SELECT COUNT(*) FROM messages WHERE direction = 'in'")[0], 1)

    def test_other_kinds_of_message_are_noted_without_content(self):
        self.post_hook(self.inbound_payload("", kind="image", wamid="wamid.IMG"))
        self.assertEqual(self.one("SELECT body FROM messages WHERE direction = 'in'")[0], "[image received]")

    def test_stop_withdraws_consent(self):
        self.sql("UPDATE patients SET offers_ok = 1 WHERE id = 1")
        self.post_hook(self.inbound_payload("STOP"))
        p = self.one("SELECT reminders_ok, offers_ok FROM patients WHERE id = 1")
        self.assertEqual((p[0], p[1]), (0, 0))
        self.assertTrue(self.one("SELECT 1 FROM audit_log WHERE action = 'consent.withdrawn'"))
        self.sql("UPDATE patients SET reminders_ok = 1 WHERE id = 1")
        self.post_hook(self.inbound_payload("please stop sending", wamid="wamid.IN2"))              # only the exact word counts
        self.assertEqual(self.one("SELECT reminders_ok FROM patients WHERE id = 1")[0], 1)

    def test_a_number_we_do_not_know_is_kept_aside_and_not_filed(self):
        self.post_hook(self.inbound_payload("Hello", sender="233555000111", wamid="wamid.U1"))
        self.assertEqual(self.one("SELECT COUNT(*) FROM messages WHERE direction = 'in'")[0], 0)
        u = self.one("SELECT * FROM wa_events WHERE kind = 'unmatched'")
        self.assertIn("+233555000111", u["detail"])
        self.sql("UPDATE wa_events SET created_at = datetime('now','-40 days')")
        conn = self.db()
        self.assertEqual(WA.clean_events(conn, 30), 1)
        conn.commit()
        conn.close()

    def test_a_shared_phone_goes_to_the_person_we_last_wrote_to(self):
        self.sql("INSERT INTO patients(clinic_id, first_name, last_name, phone, reminders_ok) VALUES (1,'Kojo','Mensah','+233201112223',1)")     # patient 4
        self.sql("INSERT INTO messages(clinic_id, patient_id, kind, body, status) VALUES (1,4,'reminder','x','sent')")
        self.post_hook(self.inbound_payload("Yes"))
        self.assertEqual(self.one("SELECT patient_id FROM messages WHERE direction = 'in'")[0], 4)

    def test_a_message_for_another_number_is_ignored(self):
        self.post_hook(self.inbound_payload("Hi", pid="999999"))
        self.assertEqual(self.one("SELECT COUNT(*) FROM messages WHERE direction = 'in'")[0], 0)
        self.assertEqual(self.one("SELECT COUNT(*) FROM wa_events")[0], 0)

    def test_one_clinics_address_cannot_touch_another_clinics_messages(self):
        self.connect(clinic=2, pid="55555555")
        self.sent()
        # clinic 2's secret and address, clinic 1's message id
        r = self.post_hook(self.status_payload("wamid.OUT1", "read", pid="55555555"), clinic=2)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.status()[0], "sent")
        # clinic 2's address with clinic 1's number id is ignored too
        self.post_hook(self.status_payload("wamid.OUT1", "read"), clinic=2)
        self.assertEqual(self.status()[0], "sent")

    def test_old_bookkeeping_is_cleared_when_meta_calls(self):
        self.post_hook(self.inbound_payload("Hello", sender="233555000111", wamid="wamid.U1"))
        self.sql("UPDATE wa_events SET created_at = datetime('now','-40 days')")
        self.post_hook(self.status_payload("wamid.NONE", "read"))
        self.assertEqual(self.one("SELECT COUNT(*) FROM wa_events WHERE kind = 'unmatched'")[0], 0)

    def test_the_address_needs_no_sign_in_and_no_form_token(self):
        self.assertEqual(self.post_hook(self.status_payload("wamid.NONE", "read")).status_code, 200)          # an unknown message id is simply ignored


class ScreenTests(Wa):
    def test_set_up_page_saves_and_never_shows_the_secrets(self):
        c = self.login("mgr")
        form = {"action": "wa_account", "enabled": "1", "phone_number_id": "1234567890", "access_token": TOKEN, "app_secret": SECRET,
                "api_version": "v22.0"}
        r = self.post(c, "/messages/setup", form, follow_redirects=True)
        page = r.get_data(as_text=True)
        self.assertIn("Saved.", page)
        self.assertIn("WhatsApp Business API", page)
        self.assertNotIn(TOKEN, page)
        self.assertNotIn(SECRET, page)
        self.assertIn("/hooks/whatsapp/", page)
        self.assertTrue(WA.is_on(self.db(), 1, KEY))
        self.post(c, "/messages/setup", dict(form, phone_number_id="abc"))
        self.assertEqual(self.one("SELECT phone_number_id FROM wa_account WHERE clinic_id = 1")[0], "1234567890")

    def test_only_a_manager_can_open_it(self):
        self.assertEqual(self.login("desk").get("/messages/setup").status_code, 403)
        c = self.login("desk")
        self.assertEqual(self.post(c, "/messages/setup", {"action": "wa_account", "enabled": "1"}).status_code, 403)

    def test_templates_are_saved_from_the_page(self):
        c = self.login("mgr")
        self.post(c, "/messages/setup", {"action": "wa_template", "kind": "reminder", "lang": "fr", "template_name": "rappel_visite",
                                         "language_code": "fr", "params": "first_name,clinic"})
        self.assertEqual(WA.get_mapping(self.db(), 1, "reminder", "fr")["template_name"], "rappel_visite")
        self.assertEqual(self.post(c, "/messages/setup", {"action": "wa_template", "kind": "zzz", "lang": "fr"}).status_code, 404)

    def test_the_send_list_shows_the_api_buttons_only_when_it_is_on(self):
        self.appt(days=1)
        c = self.login("mgr")
        page = self.page(c, "/messages")
        self.assertNotIn("Send everything that can go", page)
        self.connect()
        page = self.page(c, "/messages")
        self.assertIn("Send everything that can go", page)
        self.assertIn("/send", page)
        self.assertNotIn("Automatic sending is on", page)
        self.sql("UPDATE wa_account SET auto_send = 1")
        self.assertIn("Automatic sending is on", self.page(c, "/messages"))

    def test_send_buttons_work_through_the_screens(self):
        self.connect()
        self.mapping()
        self.appt(days=1)
        c = self.login("mgr")
        self.page(c, "/messages")
        oid = self.one("SELECT id FROM outbox WHERE kind = 'reminder'")[0]
        fake = Fake()
        with mock.patch.object(WA, "default_transport", fake):
            r = self.post(c, f"/messages/queue/{oid}/send", follow_redirects=True)
        self.assertIn("Sent by WhatsApp", r.get_data(as_text=True))
        self.assertEqual(len(fake.calls), 1)
        page = self.page(c, "/patients/1/messages")
        self.assertIn("Sent", page)
        self.post_hook(self.status_payload("wamid.TEST1", "delivered"))
        self.assertIn("Delivered", self.page(c, "/patients/1/messages"))

    def test_send_all_and_the_visit_page_button(self):
        self.connect()
        self.mapping()
        self.mapping("confirmation", params="first_name,clinic,dentist,day,time", name="booking_confirm")
        aid = self.appt(days=1)
        c = self.login("mgr")
        self.page(c, "/messages")
        fake = Fake()
        with mock.patch.object(WA, "default_transport", fake):
            r = self.post(c, "/messages/queue/send-all", follow_redirects=True)
            self.assertIn("1 sent by WhatsApp", r.get_data(as_text=True))
            page = self.page(c, f"/appointments/{aid}")
            self.assertIn("Send confirmation through the API", page)
            r = self.post(c, f"/appointments/{aid}/whatsapp-api", {"kind": "confirmation"}, follow_redirects=True)
            self.assertIn("Sent by WhatsApp", r.get_data(as_text=True))
        self.assertEqual(fake.calls[1]["body"]["template"]["name"], "booking_confirm")

    def test_the_visit_page_hides_the_api_buttons_when_it_is_off(self):
        aid = self.appt(days=1)
        self.assertNotIn("through the API", self.page(self.login("mgr"), f"/appointments/{aid}"))

    def test_the_new_screens_are_in_french(self):
        self.connect()
        self.sql("UPDATE clinics SET language = 'fr' WHERE id = 1")
        self.appt(days=1)
        page = self.page(self.login("mgr"), "/messages")
        self.assertIn("Envoyer tout ce qui peut partir", page)
        self.assertIn("L&#39;API WhatsApp Business est activée.", page)

    def test_the_command_sends_only_for_clinics_that_asked(self):
        self.connect(auto=True)
        self.mapping()
        self.appt(days=1)
        runner = self.app.test_cli_runner()
        fake = Fake()
        with mock.patch.object(WA, "default_transport", fake):
            out = runner.invoke(args=["send-whatsapp"]).output
        self.assertIn("1 sent", out)
        self.assertEqual(len(fake.calls), 1)
        self.sql("UPDATE wa_account SET auto_send = 0")
        self.appt(days=1, hour=12, pid=1)
        with mock.patch.object(WA, "default_transport", fake):
            out = runner.invoke(args=["send-whatsapp"]).output
        self.assertIn("0 sent", out)


class SchemaTests(Wa):
    def test_upgrading_an_older_database_adds_the_new_columns(self):
        conn = self.db()
        cols = dbmeta.columns(conn, "messages")
        self.assertTrue({"provider_id", "error"} <= cols)
        for t in ("wa_account", "wa_templates", "wa_events"):
            self.assertTrue(dbmeta.has_table(conn, t), t)


if __name__ == "__main__":
    unittest.main()
