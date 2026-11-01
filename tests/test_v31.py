"""v0.31: email through the clinic's own mail account. The mail server is played by a fake; nothing is really sent."""
from tests.test_v04 import Base
from dental_os.services import mailer as M, outbox, statements as S, treatment as TR

SECRET = "test"
FORM = {"host": "smtp.example.com", "port": "587", "security": "starttls", "username": "desk@clinic.example", "password": "s3cret-pass",
        "from_addr": "desk@clinic.example", "from_name": "Clinic One", "reply_to": "", "enabled": "1"}


class FakeServer:
    def __init__(self, fail=None):
        self.sent, self.fail = [], fail

    def __call__(self, a, password, msg):
        if self.fail:
            raise M.MailError(self.fail)
        self.sent.append((a["host"], password, msg))


class Mail(Base):
    def setUp(self):
        super().setUp()
        self.conn = self.db()
        self.conn.execute("UPDATE patients SET email = 'ama@example.com', preferred_contact = 'email' WHERE id = 1")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def c(self):
        return dict(self.conn.execute("SELECT * FROM clinics WHERE id = 1").fetchone())

    def on(self, **extra):
        M.save_account(self.conn, self.c(), 1, SECRET, FORM | extra)
        self.conn.commit()

    def queued(self, kind="birthday", pid=1):
        self.conn.execute("INSERT INTO outbox(clinic_id, patient_id, kind, purpose, dedupe_key, due_on) VALUES (1, ?, ?, 'care', ?, date('now'))",
                          (pid, kind, f"t{kind}{pid}"))
        self.conn.commit()
        return self.conn.execute("SELECT MAX(id) FROM outbox").fetchone()[0]


class AccountTests(Mail):
    def test_details_are_checked_and_the_password_is_sealed(self):
        for bad in ({"host": "smtp example"}, {"port": "x"}, {"security": "magic"}, {"from_addr": "not-an-address"}, {"reply_to": "a@b"},
                    {"host": "", "enabled": "1"}):
            with self.assertRaises(M.MailError):
                M.save_account(self.conn, self.c(), 1, SECRET, FORM | bad)
        self.on()
        a = M.account(self.conn, 1)
        self.assertNotIn("s3cret-pass", a["password"])
        M.save_account(self.conn, self.c(), 1, SECRET, FORM | {"password": ""})
        self.assertEqual(M.account(self.conn, 1)["password"], a["password"])      # an empty box keeps the saved one
        logs = " ".join(str(tuple(r)) for r in self.conn.execute("SELECT * FROM audit_log").fetchall())
        self.assertNotIn("s3cret-pass", logs)
        fake = FakeServer()
        M.send_test(self.conn, self.c(), 1, SECRET, "manager@clinic.example", fake)
        host, password, msg = fake.sent[0]
        self.assertEqual((host, password), ("smtp.example.com", "s3cret-pass"))
        self.assertEqual(msg["From"], "Clinic One <desk@clinic.example>")

    def test_no_extra_headers_can_be_slipped_in(self):
        self.on()
        for bad in ("a@example.com\nBcc: x@evil.example", "a@example.com, b@example.com", ""):
            with self.assertRaises(M.MailError):
                M.send(self.conn, self.c(), SECRET, bad, "Hi", "Body", FakeServer())
        msg = M.send(self.conn, self.c(), SECRET, "a@example.com", "Hello\r\nBcc: x@evil.example", "Body", FakeServer())
        self.assertNotIn("\n", msg["Subject"])
        self.assertIsNone(msg["Bcc"])


class OutboxTests(Mail):
    def test_a_queued_message_goes_by_email_and_is_logged(self):
        self.on()
        oid = self.queued()
        fake = FakeServer()
        M.deliver_outbox(self.conn, self.c(), 1, SECRET, oid, fake)
        msg = fake.sent[0][2]
        self.assertEqual(msg["To"], "ama@example.com")
        self.assertIn("Happy birthday", msg.get_content())
        self.assertIn("To stop these emails", msg.get_content())
        row = self.conn.execute("SELECT * FROM messages").fetchone()
        self.assertEqual((row["channel"], row["kind"], row["status"]), ("email", "birthday", "sent"))
        self.assertEqual(outbox.get(self.conn, 1, oid)["status"], "sent")
        with self.assertRaises(outbox.MessageError):
            M.deliver_outbox(self.conn, self.c(), 1, SECRET, oid, fake)            # only once

    def test_consent_address_and_server_failures(self):
        self.on()
        self.conn.execute("UPDATE patients SET reminders_ok = 0 WHERE id = 1")
        oid = self.queued()
        with self.assertRaises(outbox.MessageError):
            M.deliver_outbox(self.conn, self.c(), 1, SECRET, oid, FakeServer())
        self.conn.execute("UPDATE patients SET reminders_ok = 1, email = NULL WHERE id = 1")
        with self.assertRaises(outbox.MessageError):
            M.deliver_outbox(self.conn, self.c(), 1, SECRET, oid, FakeServer())
        self.conn.execute("UPDATE patients SET email = 'ama@example.com' WHERE id = 1")
        self.queued(pid=1, kind="recall")
        res = M.send_ready(self.conn, self.c(), 1, SECRET, transport=FakeServer("Could not reach the mail server: timeout"))
        self.assertEqual((res["sent"], res["left"]), (0, 2))
        self.assertIn("Could not reach", res["stopped"])
        res = M.send_ready(self.conn, self.c(), 1, SECRET, transport=FakeServer())
        self.assertEqual(res["sent"], 2)

    def test_screens_and_the_scheduled_command(self):
        mgr = self.login("mgr")
        r = self.post(mgr, "/settings/email", FORM, follow_redirects=True)
        self.assertIn("Saved.", r.get_data(as_text=True))
        self.assertIn("Password saved.", self.page(mgr, "/settings/email"))
        self.assertNotIn("s3cret-pass", self.page(mgr, "/settings/email"))
        self.queued()
        self.assertIn("Email everyone who prefers email", self.page(mgr, "/messages"))
        self.assertEqual(self.login("desk").get("/settings/email").status_code, 403)
        out = self.app.test_cli_runner().invoke(args=["send-email"]).output
        self.assertIn("0 emailed", out)                                          # automatic email is off


class StatementTests(Mail):
    def test_a_statement_by_email_carries_the_link_not_the_amount(self):
        self.on()
        TR.ensure_catalogue(self.conn, 1)
        self.conn.execute("UPDATE procedure_codes SET fee_p = 30000 WHERE clinic_id = 1 AND code = 'FILL'")
        pr = TR.add_planned(self.conn, self.c(), 1, 1, self.conn.execute("SELECT id FROM procedure_codes WHERE clinic_id = 1 AND code = 'FILL'").fetchone()[0])
        TR.mark_done(self.conn, self.c(), 1, pr)
        rid = S.make_run(self.conn, self.c(), 1, "1", "0", "0", False)
        fake = FakeServer()
        sent, skipped = M.email_run(self.conn, self.c(), 1, SECRET, rid, lambda t: "https://clinic.example/p/" + t, fake)
        self.assertEqual((sent, skipped), (1, 0))
        body = fake.sent[0][2].get_content()
        self.assertIn("https://clinic.example/p/", body)
        self.assertNotIn("300", body)
        self.assertEqual(self.conn.execute("SELECT sent_how FROM statement_items").fetchone()[0], "email")
        logged = self.conn.execute("SELECT body FROM messages WHERE kind = 'statement'").fetchone()[0]
        self.assertIn("[private link]", logged)
