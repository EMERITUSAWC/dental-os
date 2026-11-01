"""v0.68: the review from version 0.1 onwards. Money that cannot be taken twice or made from nothing, messages that cannot carry
money, records the database itself keeps, operators with two-step login, off-site copies to Amazon S3, and every form in the app
sent hostile values without a crash."""
import datetime
import re
import time
from html.parser import HTMLParser

from werkzeug.routing.converters import IntegerConverter

from tests.test_v04 import Base
from dental_os import db as dbmod
from dental_os.services import billing, insurance as INS, money, msgtemplates as MT, offsite as OS, refunds as RF, twofactor


class MoneyParsingTests(Base):
    def test_only_plain_numbers_are_amounts(self):
        self.assertEqual(money.parse_money("1,250.50", "GHS"), 125050)
        self.assertEqual(money.parse_money("GH₵ 80", "GHS"), 8000)
        self.assertEqual(money.parse_money("12,50", "GHS"), 1250)
        for bad in ("1e5", "-1e309", "1" * 40, "NaN", "inf", "0x10", "1_000"):
            with self.assertRaises(ValueError, msg=bad):
                money.parse_money(bad, "GHS")
        with self.assertRaisesRegex(ValueError, "more than zero"):
            money.parse_money("-5", "GHS")


class DeskPaymentTests(Base):
    def _pay(self, c, **kw):
        data = {"amount": "100", "method": "cash"}
        data.update(kw)
        return self.post(c, "/patients/1/payments", data, follow_redirects=True).get_data(as_text=True)

    def _count(self):
        conn = self.db()
        n = conn.execute("SELECT COUNT(*) FROM payments WHERE patient_id = 1").fetchone()[0]
        conn.close()
        return n

    def test_the_same_form_sent_twice_takes_money_once(self):
        c = self.login("desk")
        self.assertIn("Payment recorded", self._pay(c))
        self.assertIn("recorded a moment ago", self._pay(c))
        self.assertEqual(self._count(), 1)
        self.assertIn("Payment recorded", self._pay(c, amount="120"))           # a different amount is a different payment
        self.assertEqual(self._count(), 2)

    def test_one_mobile_money_reference_is_one_payment(self):
        conn = self.db()
        conn.execute("UPDATE clinics SET payment_methods = 'cash,mobile_money' WHERE id = 1")
        conn.commit()
        conn.close()
        c = self.login("desk")
        self.assertIn("Payment recorded", self._pay(c, method="mobile_money", reference="MM-778899", amount="50"))
        billing.REPEAT_SECONDS, old = 0, billing.REPEAT_SECONDS                   # past the one-minute window: only the reference stops it
        try:
            self.assertIn("already on a payment", self._pay(c, method="mobile_money", reference="MM-778899", amount="70"))
        finally:
            billing.REPEAT_SECONDS = old
        self.assertEqual(self._count(), 1)

    def test_other_screens_may_take_equal_amounts(self):
        conn = self.db()
        clinic = conn.execute("SELECT * FROM clinics WHERE id = 1").fetchone()
        billing.record_payment(conn, clinic, 1, 1, 5000, "cash")
        billing.record_payment(conn, clinic, 1, 1, 5000, "cash")                  # two instalments at once: allowed
        conn.commit()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM payments WHERE patient_id = 1").fetchone()[0], 2)
        conn.close()


class CreditFromNothingTests(Base):
    def test_a_discount_cannot_be_paid_out_as_cash(self):
        conn = self.db()
        clinic = conn.execute("SELECT * FROM clinics WHERE id = 1").fetchone()
        billing.add_adjustment(conn, clinic, 1, 1, -50000, "discount", "test")
        conn.commit()
        self.assertEqual(billing.totals(conn, 1, 1)["balance"], -50000)       # the account shows a credit
        with self.assertRaisesRegex(RF.RefundError, "actually paid in"):
            RF.refund(conn, clinic, 1, 1, 50000, "cash", next(iter(RF.REASONS)))
        conn.rollback()
        billing.record_payment(conn, clinic, 1, 1, 20000, "cash")
        conn.commit()
        RF.refund(conn, clinic, 1, 1, 20000, "cash", next(iter(RF.REASONS)))     # money that came in can go back
        conn.commit()
        with self.assertRaisesRegex(RF.RefundError, "actually paid in"):
            RF.refund(conn, clinic, 1, 1, 100, "cash", next(iter(RF.REASONS)))
        conn.close()

    def test_a_write_off_cannot_be_more_than_is_left_on_a_claim(self):
        conn = self.db()
        clinic = conn.execute("SELECT * FROM clinics WHERE id = 1").fetchone()
        payer = conn.execute("INSERT INTO payers(clinic_id, name, kind) VALUES (1, 'Acacia', 'insurer')").lastrowid
        claim = conn.execute("""INSERT INTO claims(clinic_id, patient_id, payer_id, claim_no, kind, status, estimate_p, paid_p, writeoff_p)
                                VALUES (1, 1, ?, 1, 'claim', 'submitted', 10000, 0, 0)""", (payer,)).lastrowid
        conn.execute("UPDATE clinics SET payment_methods = 'cash,insurance' WHERE id = 1")
        conn.commit()
        clinic = conn.execute("SELECT * FROM clinics WHERE id = 1").fetchone()
        with self.assertRaisesRegex(INS.InsuranceError, "more than is left"):
            INS.record_payer_payment(conn, clinic, 1, claim, 6000, "TRF-1", writeoff_p=5000)
        conn.rollback()
        INS.record_payer_payment(conn, clinic, 1, claim, 6000, "TRF-1", writeoff_p=4000)
        conn.commit()
        self.assertEqual(conn.execute("SELECT status FROM claims WHERE id = ?", (claim,)).fetchone()[0], "paid")
        conn.close()


class NoMoneyInMessagesTests(Base):
    def test_wording_and_replies_cannot_mention_money(self):
        for bad in ("Hi {first_name}, your balance is GH₵500", "You still owe us", "Pay $20 today", "Votre facture", "200 cedis please"):
            with self.assertRaisesRegex(MT.TemplateError, "cannot mention money"):
                MT.validate(bad)
        for _kind, (_label, body, need) in MT.KINDS.items():
            MT.validate(body, required=need)                                   # the starters are fine
        conn = self.db()
        with self.assertRaisesRegex(MT.TemplateError, "cannot mention money"):
            MT.save(conn, 1, 1, "reminder", "Hello {first_name}, you owe GH₵200.")        # what the settings screen calls
        conn.close()
        from dental_os.services import inbox
        conn = self.db()
        clinic = conn.execute("SELECT * FROM clinics WHERE id = 1").fetchone()
        with self.assertRaisesRegex(inbox.InboxError, "cannot mention money"):
            inbox.reply(conn, clinic, 1, 1, "Your outstanding balance is 300")
        conn.close()


class RecordLockTests(Base):
    def test_a_signed_note_keeps_its_words_but_can_move_in_a_merge(self):
        conn = self.db()
        conn.execute("""INSERT INTO clinical_notes(clinic_id, patient_id, title, body, status, author_id, signed_by, signed_at)
                        VALUES (1, 1, 'Exam', 'Caries 36 MO', 'signed', 2, 2, '2026-10-01 10:00')""")
        conn.execute("INSERT INTO clinical_notes(clinic_id, patient_id, title, body) VALUES (1, 1, 'Draft', 'x')")
        conn.commit()
        for sql in ("UPDATE clinical_notes SET body = 'changed' WHERE status = 'signed'",
                    "UPDATE clinical_notes SET status = 'draft' WHERE status = 'signed'",
                    "DELETE FROM clinical_notes WHERE status = 'signed'"):
            with self.assertRaises(Exception) as ctx:
                conn.execute(sql)
            self.assertIn("signed clinical note", str(ctx.exception))
            conn.rollback()
        conn.execute("UPDATE clinical_notes SET patient_id = 2 WHERE status = 'signed'")
        conn.execute("UPDATE clinical_notes SET body = 'y' WHERE status = 'draft'")
        conn.commit()
        self.assertTrue(dbmod.records_locked(conn))
        conn.close()

    def test_a_payment_keeps_its_amount_and_date_and_is_voided_once(self):
        conn = self.db()
        clinic = conn.execute("SELECT * FROM clinics WHERE id = 1").fetchone()
        pid = billing.record_payment(conn, clinic, 1, 1, 5000, "cash")
        conn.commit()
        for sql in ("UPDATE payments SET amount_p = 1 WHERE id = ?", "UPDATE payments SET received_on = '2020-01-01' WHERE id = ?",
                    "UPDATE payments SET receipt_no = 999 WHERE id = ?", "DELETE FROM payments WHERE id = ?"):
            with self.assertRaises(Exception) as ctx:
                conn.execute(sql, (pid,))
            self.assertIn("recorded payment", str(ctx.exception))
            conn.rollback()
        billing.void_payment(conn, 1, 1, pid, "Wrong patient")
        conn.commit()
        with self.assertRaises(Exception):
            conn.execute("UPDATE payments SET voided = 0 WHERE id = ?", (pid,))
        conn.rollback()
        conn.close()
        rows = {k: s for k, s, _d, _v in __import__("dental_os.services.compliance", fromlist=["x"]).controls(
            self.db(), self.db().execute("SELECT * FROM clinics WHERE id = 1").fetchone(), self.app.config)}
        self.assertEqual(rows["records"], "ok")


class OperatorTwoStepTests(Base):
    def test_operators_need_a_code(self):
        self.app.config["OPERATOR_REQUIRE_2FA"] = True
        conn = self.db()
        from dental_os.services import platform as PF
        PF.add_operator(conn, "owner", "Owner", "a-long-operator-password")
        conn.commit()
        with self.assertRaisesRegex(PF.PlatformError, "must use two-step login"):
            PF.operator_login(conn, "owner", "a-long-operator-password", "ip", None, "test", True)
        secret = twofactor.new_secret()
        op = conn.execute("SELECT id FROM operators WHERE username = 'owner'").fetchone()[0]
        PF.set_operator_2fa(conn, op, "test", secret, 0)
        conn.commit()
        with self.assertRaisesRegex(PF.PlatformError, "code do not match"):
            PF.operator_login(conn, "owner", "a-long-operator-password", "ip2", "000000", "test", True)
        code = twofactor.code_at(secret, int(time.time() // 30))
        self.assertEqual(PF.operator_login(conn, "owner", "a-long-operator-password", "ip3", code, "test", True)["username"], "owner")
        with self.assertRaises(PF.PlatformError):                                  # the same code twice: refused
            PF.operator_login(conn, "owner", "a-long-operator-password", "ip4", code, "test", True)
        conn.close()
        self.assertIn('name="code"', self.app.test_client().get("/operator/login").get_data(as_text=True))


class S3CopyTests(Base):
    def test_requests_are_signed_the_way_aws_documents(self):
        h = OS.s3_sign("GET", "https://examplebucket.s3.amazonaws.com/test.txt", "us-east-1", "AKIAIOSFODNN7EXAMPLE",
                       "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", b"", {"Range": "bytes=0-9"},
                       now=datetime.datetime(2013, 5, 24, tzinfo=datetime.timezone.utc))
        self.assertTrue(h["Authorization"].endswith("Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41"))

    def test_a_copy_goes_to_s3_and_comes_back(self):
        store = {}

        def fake(method, url, headers, body=None, timeout=120):
            self.assertIn("AWS4-HMAC-SHA256", headers["Authorization"])
            self.assertTrue(url.startswith("https://clinic-copies.s3.eu-west-2.amazonaws.com/dental-os/"))
            if method == "PUT":
                store[url] = body
                return 200, b""
            data = store.get(url)
            if data is None:
                return 404, b""
            return (206, data[:len(OS.MAGIC)]) if "Range" in headers else (200, data)
        cfg = {"OFFSITE_TARGET": "s3://clinic-copies/dental-os", "OFFSITE_S3_REGION": "eu-west-2", "OFFSITE_USER": "AKIA-TEST",
               "OFFSITE_PASSWORD": "secret", "OFFSITE_PASSPHRASE": "a long passphrase kept on paper"}
        blob = OS.lock(b"PK-test-data", cfg["OFFSITE_PASSPHRASE"])
        where = OS.send(cfg, blob, "dental-os-latest.copy", fake)
        self.assertEqual(where, "s3://clinic-copies/dental-os/dental-os-latest.copy")
        name, back = OS.latest(cfg, fake)
        self.assertEqual(back, blob)
        with self.assertRaisesRegex(OS.OffsiteError, "access key"):
            OS.send({**cfg, "OFFSITE_USER": ""}, blob, "x.copy", fake)


class _Forms(HTMLParser):
    def __init__(self):
        super().__init__()
        self.forms, self.cur = [], None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "form":
            self.cur = {"action": a.get("action"), "method": (a.get("method") or "get").lower(), "fields": {}}
        elif self.cur is not None and tag in ("input", "select", "textarea") and a.get("name"):
            self.cur["fields"].setdefault(a["name"], (a.get("type") or "", a.get("value") or ""))

    def handle_endtag(self, tag):
        if tag == "form" and self.cur is not None:
            self.forms.append(self.cur)
            self.cur = None


class HostileFormCrawl(Base):
    """Every form on every page, sent with impossible dates, huge and odd numbers and very long text. Nothing may crash."""

    def _value(self, name, typ, value, mode):
        n = name.lower()
        if typ == "hidden" and mode == 0:
            return value
        if mode == 2:
            return ""
        if typ == "date" or any(k in n for k in ("date", "_on", "day", "dob", "start", "end", "until")):
            return "2026-02-30" if mode == 0 else "9999-99-99"
        if typ == "number" or any(k in n for k in ("amount", "fee", "price", "qty", "pct", "minutes", "days", "count", "tooth", "id")):
            return "1" * 30 if mode == 0 else "-1"
        return "x" * 20000 if mode == 0 else "\u0000<x-probe>'\";--" * 40

    def test_no_form_crashes(self):
        self.app.test_cli_runner().invoke(args=["seed-demo", "--clinic-id", "1"])
        self.app.config["PROPAGATE_EXCEPTIONS"] = False
        self.app.testing = False
        c = self.login("mgr")
        with self.app.test_request_context():
            from flask import url_for
            pages = []
            for rule in self.app.url_map.iter_rules():
                if "GET" not in rule.methods or rule.endpoint.startswith(("static", "operator.")) or rule.endpoint == "auth.logout":
                    continue
                for v in ((1, 2) if rule.arguments else (1,)):
                    try:
                        pages.append(url_for(rule.endpoint, **{n: (v if isinstance(rule._converters.get(n), IntegerConverter) else "x")
                                                               for n in rule.arguments}))
                    except Exception:                                    # noqa: BLE001 - a converter that refuses
                        pass
        seen, crashes = set(), []
        for page in pages:
            if not re.search(r'name="_csrf"', c.get("/").get_data(as_text=True)):
                c = self.login("mgr")
            r = c.get(page)
            if r.status_code != 200 or "html" not in (r.mimetype or ""):
                continue
            parser = _Forms()
            parser.feed(r.get_data(as_text=True))
            for f in parser.forms:
                action = f["action"] or page
                key = (action.split("?")[0], tuple(sorted(f["fields"])))
                if f["method"] != "post" or key in seen or "logout" in action or "/login" in action:
                    continue
                seen.add(key)
                for mode in range(3):
                    data = {k: self._value(k, t, v, mode) for k, (t, v) in f["fields"].items() if k != "_csrf"}
                    if not re.search(r'name="_csrf"', c.get("/").get_data(as_text=True)):
                        c = self.login("mgr")
                    if self.post(c, action, data).status_code >= 500:
                        crashes.append(f"{action} ({mode})")
        self.assertGreater(len(seen), 150)
        self.assertEqual(crashes, [], "These forms crashed")
