import re
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

from werkzeug.security import generate_password_hash

from dental_os import create_app, db as dbmod
from dental_os.services import billing as B, messaging, money, recalls as R, reports, scheduling as S
from dental_os.services import treatment as TR
from tests.test_all import clinic, make_conn, next_weekday, patient


def with_catalogue(conn):
    for cid in (1, 2):
        TR.ensure_catalogue(conn, cid)
    conn.commit()


def code_id(conn, cid, code="CONS"):
    return conn.execute("SELECT id FROM procedure_codes WHERE clinic_id=? AND code=?", (cid, code)).fetchone()["id"]


class MoneyTests(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(money.parse_money("150", "GHS"), 15000)
        self.assertEqual(money.parse_money("GH₵ 1,250.50", "GHS"), 125050)
        self.assertEqual(money.parse_money("ghs 80", "GHS"), 8000)
        self.assertEqual(money.parse_money("0", "GHS", allow_zero=True), 0)

    def test_bad_amounts(self):
        for raw in ("", "abc", "-5", "0", "10.555", "99999999999"):
            with self.assertRaises(ValueError, msg=raw):
                money.parse_money(raw, "GHS")

    def test_format(self):
        self.assertEqual(money.fmt(125050, "GHS"), "GH₵ 1,250.50")
        self.assertEqual(money.fmt(-5000, "GHS"), "-GH₵ 50.00")
        self.assertEqual(money.fmt(0, "GHS"), "GH₵ 0.00")
        self.assertEqual(money.plain(15005, "GHS"), "150.05")


class TreatmentTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_conn()
        with_catalogue(self.conn)
        self.c = clinic(self.conn)
        self.pid = patient(self.conn, 1)
        self.crown = code_id(self.conn, 1, "CROWN")

    def plan(self, **kw):
        return TR.add_planned(self.conn, self.c, None, self.pid, kw.pop("code", self.crown), **kw)

    def test_catalogue_added_once(self):
        before = self.conn.execute("SELECT COUNT(*) FROM procedure_codes WHERE clinic_id=1").fetchone()[0]
        self.assertEqual(TR.ensure_catalogue(self.conn, 1), 0)
        self.assertGreater(before, 10)

    def test_tooth_and_surfaces(self):
        self.assertEqual(TR.clean_tooth("36"), 36)
        self.assertEqual(TR.clean_tooth("54"), 54)
        self.assertIsNone(TR.clean_tooth(""))
        for bad in ("19", "0", "abc", "99"):
            with self.assertRaises(TR.TreatmentError):
                TR.clean_tooth(bad)
        self.assertEqual(TR.clean_surfaces(["d", "O", "m"]), "MOD")
        with self.assertRaises(TR.TreatmentError):
            TR.clean_surfaces(["X"])

    def test_fee_defaults_to_list_price_and_can_differ(self):
        self.conn.execute("UPDATE procedure_codes SET fee_p = 50000 WHERE id = ?", (self.crown,))
        a = self.plan()
        b = self.plan(fee_p=45000)
        self.assertEqual(TR.get(self.conn, 1, a)["fee_p"], 50000)
        self.assertEqual(TR.get(self.conn, 1, b)["fee_p"], 45000)

    def test_done_locks_fee_and_cancel(self):
        pid = self.plan(fee_p=10000)
        TR.set_fee(self.conn, 1, None, pid, 12000)
        TR.mark_done(self.conn, self.c, None, pid)
        row = TR.get(self.conn, 1, pid)
        self.assertEqual((row["status"], row["fee_p"]), ("done", 12000))
        for action in (lambda: TR.set_fee(self.conn, 1, None, pid, 1), lambda: TR.cancel(self.conn, 1, None, pid),
                       lambda: TR.mark_done(self.conn, self.c, None, pid)):
            with self.assertRaises(TR.TreatmentError):
                action()

    def test_void_needs_reason_and_done_status(self):
        pid = self.plan(fee_p=10000)
        with self.assertRaises(TR.TreatmentError):
            TR.void(self.conn, 1, None, pid, "typo")          # not done yet
        TR.mark_done(self.conn, self.c, None, pid)
        with self.assertRaises(TR.TreatmentError):
            TR.void(self.conn, 1, None, pid, " ")
        TR.void(self.conn, 1, None, pid, "entered on wrong patient")
        self.assertEqual(TR.get(self.conn, 1, pid)["status"], "voided")
        self.assertEqual(B.totals(self.conn, 1, self.pid)["charges"], 0)

    def test_done_can_link_only_to_own_visit(self):
        pid = self.plan(fee_p=10000)
        other = patient(self.conn, 1, "Kofi", "Asare", "0244000111")
        start = next_weekday(1, 9)
        theirs = S.book(self.conn, self.c, None, other, 1, 1, start, 30)
        with self.assertRaises(TR.TreatmentError):
            TR.mark_done(self.conn, self.c, None, pid, appointment_id=theirs)
        mine = S.book(self.conn, self.c, None, self.pid, 1, 1, start + timedelta(hours=1), 30)
        TR.mark_done(self.conn, self.c, None, pid, appointment_id=mine)
        self.assertEqual(TR.get(self.conn, 1, pid)["done_on"], start.date().isoformat())

    def test_other_clinic_cannot_use_or_touch(self):
        other_code = code_id(self.conn, 2, "CROWN")
        with self.assertRaises(TR.TreatmentError):
            self.plan(code=other_code)
        pid = self.plan()
        self.assertIsNone(TR.get(self.conn, 2, pid))
        with self.assertRaises(LookupError):
            TR.cancel(self.conn, 2, None, pid)

    def test_archived_patient_cannot_be_planned(self):
        from dental_os.services import patients as P
        P.set_archived(self.conn, 1, None, self.pid, True)
        with self.assertRaises(TR.TreatmentError):
            self.plan()


class BillingTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_conn()
        with_catalogue(self.conn)
        self.c = clinic(self.conn)
        self.c2 = clinic(self.conn, 2)
        self.pid = patient(self.conn, 1)

    def charge(self, amount_p, pid=None):
        proc = TR.add_planned(self.conn, self.c, None, pid or self.pid, code_id(self.conn, 1), fee_p=amount_p)
        TR.mark_done(self.conn, self.c, None, proc)

    def test_balance_maths(self):
        self.charge(50000)
        B.add_adjustment(self.conn, self.c, None, self.pid, -5000, "birthday_offer")
        B.record_payment(self.conn, self.c, None, self.pid, 20000, "cash")
        t = B.totals(self.conn, 1, self.pid)
        self.assertEqual((t["charges"], t["adjustments"], t["paid"], t["balance"]), (50000, -5000, 20000, 25000))

    def test_overpayment_is_credit(self):
        self.charge(10000)
        B.record_payment(self.conn, self.c, None, self.pid, 15000, "cash")
        self.assertEqual(B.totals(self.conn, 1, self.pid)["balance"], -5000)

    def test_payment_rules(self):
        with self.assertRaises(B.BillingError):
            B.record_payment(self.conn, self.c, None, self.pid, 1000, "mobile_money")        # needs reference
        with self.assertRaises(B.BillingError):
            B.record_payment(self.conn, self.c, None, self.pid, 0, "cash")
        with self.assertRaises(B.BillingError):
            B.record_payment(self.conn, self.c, None, self.pid, 1000, "crypto")
        B.record_payment(self.conn, self.c, None, self.pid, 1000, "mobile_money", "MP2610270001")
        foreign = patient(self.conn, 2)
        with self.assertRaises(B.BillingError):
            B.record_payment(self.conn, self.c, None, foreign, 1000, "cash")

    def test_receipt_numbers_run_per_clinic(self):
        other = patient(self.conn, 2)
        a = B.record_payment(self.conn, self.c, None, self.pid, 1000, "cash")
        b = B.record_payment(self.conn, self.c, None, self.pid, 1000, "cash")
        z = B.record_payment(self.conn, self.c2, None, other, 1000, "cash")
        nums = [B.get_receipt(self.conn, cid, i)["receipt_no"] for cid, i in ((1, a), (1, b), (2, z))]
        self.assertEqual(nums, [1, 2, 1])
        self.assertIsNone(B.get_receipt(self.conn, 2, a))

    def test_void_payment_restores_balance_but_keeps_record(self):
        self.charge(10000)
        pay = B.record_payment(self.conn, self.c, None, self.pid, 10000, "cash")
        with self.assertRaises(B.BillingError):
            B.void_payment(self.conn, 1, None, pay, "")
        B.void_payment(self.conn, 1, None, pay, "wrong patient")
        self.assertEqual(B.totals(self.conn, 1, self.pid)["balance"], 10000)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM payments").fetchone()[0], 1)
        with self.assertRaises(B.BillingError):
            B.void_payment(self.conn, 1, None, pay, "again")
        with self.assertRaises(LookupError):
            B.void_payment(self.conn, 2, None, pay, "other clinic")

    def test_adjustment_rules(self):
        with self.assertRaises(B.BillingError):
            B.add_adjustment(self.conn, self.c, None, self.pid, -1000, "write_off")           # note needed
        with self.assertRaises(B.BillingError):
            B.add_adjustment(self.conn, self.c, None, self.pid, 0, "discount", "x")
        B.add_adjustment(self.conn, self.c, None, self.pid, -1000, "write_off", "patient hardship")

    def test_statement_running_balance_skips_voided(self):
        self.charge(30000)
        p1 = B.record_payment(self.conn, self.c, None, self.pid, 10000, "cash")
        B.record_payment(self.conn, self.c, None, self.pid, 5000, "cash")
        B.void_payment(self.conn, 1, None, p1, "mistake")
        events = B.statement(self.conn, 1, self.pid)
        self.assertEqual(events[-1]["balance"], 25000)
        self.assertTrue(any(e.get("voided") for e in events))

    def test_family_balance(self):
        child = self.conn.execute(
            "INSERT INTO patients(clinic_id, first_name, last_name, phone, guarantor_id) VALUES (1,'Kid','Mensah','+233201112223',?)",
            (self.pid,)).lastrowid
        self.charge(20000, self.pid)
        self.charge(30000, child)
        self.assertEqual(B.family_balance(self.conn, 1, child), {"members": 2, "balance": 50000})
        self.assertIsNone(B.family_balance(self.conn, 1, patient(self.conn, 1, "Solo", "Person", "0244000111")))

    def test_daily_takings_split_by_method(self):
        B.record_payment(self.conn, self.c, None, self.pid, 10000, "cash")
        B.record_payment(self.conn, self.c, None, self.pid, 5000, "mobile_money", "ref1")
        gone = B.record_payment(self.conn, self.c, None, self.pid, 7000, "cash")
        B.void_payment(self.conn, 1, None, gone, "oops")
        today = datetime.utcnow().date().isoformat()
        day = B.daily_takings(self.conn, 1, today)
        self.assertEqual((day["by_method"]["cash"], day["by_method"]["mobile_money"], day["total"]), (10000, 5000, 15000))

    def test_outstanding_lists_only_debtors(self):
        self.charge(10000)
        other = patient(self.conn, 1, "Paid", "Up", "0244000111")
        self.charge(5000, other)
        B.record_payment(self.conn, self.c, None, other, 5000, "cash")
        owing = B.outstanding(self.conn, 1)
        self.assertEqual([(b, r["id"]) for b, r in owing], [(10000, self.pid)])


class RecallTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_conn()
        self.c = clinic(self.conn)
        self.pid = patient(self.conn, 1)

    def test_add_months_clamps(self):
        self.assertEqual(R.add_months(date(2026, 8, 31), 6), date(2027, 2, 28))
        self.assertEqual(R.add_months(date(2026, 10, 27), 6), date(2027, 4, 27))
        self.assertEqual(R.add_months(date(2026, 12, 15), 1), date(2027, 1, 15))

    def test_completing_a_checkup_sets_recall_and_rebooking_moves_it(self):
        start = next_weekday(1, 9)
        aid = S.book(self.conn, self.c, None, self.pid, 1, 1, start, 30, "Consultation")
        S.set_status(self.conn, 1, None, aid, "complete")
        row = self.conn.execute("SELECT * FROM recalls WHERE patient_id=?", (self.pid,)).fetchone()
        self.assertEqual(row["due_on"], R.add_months(start.date(), 6).isoformat())
        R.mark_contacted(self.conn, self.c, row["id"])
        R.set_due_after_visit(self.conn, 1, self.pid, start.date() + timedelta(days=200))
        again = self.conn.execute("SELECT * FROM recalls WHERE patient_id=?", (self.pid,)).fetchone()
        self.assertEqual((again["status"], again["contacted_on"]), ("due", None))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM recalls").fetchone()[0], 1)

    def test_other_categories_do_not_create_recalls(self):
        aid = S.book(self.conn, self.c, None, self.pid, 1, 1, next_weekday(1, 9), 30, "Tooth extractions")
        S.set_status(self.conn, 1, None, aid, "complete")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM recalls").fetchone()[0], 0)

    def test_upcoming_skips_booked_snoozed_and_archived(self):
        today = date(2026, 10, 27)
        near = patient(self.conn, 1, "Near", "Due", "0244000111")
        booked = patient(self.conn, 1, "Is", "Booked", "0244000222")
        snoozed = patient(self.conn, 1, "Snoozed", "Away", "0244000333")
        far = patient(self.conn, 1, "Far", "Off", "0244000444")
        for pid, due in ((near, "2026-10-20"), (booked, "2026-10-25"), (snoozed, "2026-10-26"), (far, "2027-03-01")):
            R.set_due_after_visit(self.conn, 1, pid, today)
            self.conn.execute("UPDATE recalls SET due_on=? WHERE patient_id=?", (due, pid))
        self.conn.execute("""INSERT INTO appointments(clinic_id, patient_id, provider_id, chair_id, start_at, status)
                             VALUES (1, ?, 1, 1, '2026-11-02 10:00', 'scheduled')""", (booked,))
        rid = self.conn.execute("SELECT id FROM recalls WHERE patient_id=?", (snoozed,)).fetchone()["id"]
        self.conn.execute("UPDATE recalls SET snoozed_until='2026-11-20' WHERE id=?", (rid,))
        names = [r["first_name"] for r in R.upcoming(self.conn, 1, today)]
        self.assertEqual(names, ["Near"])

    def test_recall_message_needs_consent_and_logs(self):
        R.set_due_after_visit(self.conn, 1, self.pid, date(2026, 1, 1))
        rec = R.get(self.conn, 1, self.conn.execute("SELECT id FROM recalls").fetchone()["id"])
        from dental_os.services import patients as P
        link = messaging.prepare_recall(self.conn, self.c, None, P.get(self.conn, 1, self.pid), rec)
        self.assertTrue(link.startswith("https://wa.me/233201112223?text="))
        self.assertEqual(R.get(self.conn, 1, rec["id"])["status"], "contacted")
        quiet = patient(self.conn, 1, "No", "Consent", "0244000111", reminders=0)
        R.set_due_after_visit(self.conn, 1, quiet, date(2026, 1, 1))
        rec2 = R.get(self.conn, 1, self.conn.execute("SELECT id FROM recalls WHERE patient_id=?", (quiet,)).fetchone()["id"])
        with self.assertRaises(PermissionError):
            messaging.prepare_recall(self.conn, self.c, None, P.get(self.conn, 1, quiet), rec2)


class ReportTests(unittest.TestCase):
    def test_pilot_rates_split_by_confirmation(self):
        conn = make_conn()
        pid = patient(conn, 1)
        rows = [("complete", None, "confirmed")] * 8 + [("broken", "missed", "confirmed")] * 2 + \
               [("complete", None, "none")] * 3 + [("broken", "missed", "none")] * 3 + [("broken", "cancelled", "none")]
        for i, (status, reason, conf) in enumerate(rows):
            conn.execute("""INSERT INTO appointments(clinic_id, patient_id, provider_id, chair_id, start_at, status, break_reason, confirmation)
                            VALUES (1, ?, 1, 1, ?, ?, ?, ?)""", (pid, f"2026-10-{10 + i % 5:02d} {9 + i % 6:02d}:00", status, reason, conf))
        r = reports.pilot(conn, 1, date(2026, 10, 1), date(2026, 10, 31))
        self.assertEqual(r["confirmed"]["rate"], 20.0)          # 2 of 10
        self.assertEqual(r["not_confirmed"]["rate"], 50.0)      # 3 of 6 (cancelled not counted as no-show)
        self.assertEqual(r["cancelled"], 1)
        self.assertEqual(r["closed"], 17)

    def test_pilot_with_no_data(self):
        r = reports.pilot(make_conn(), 1, date(2026, 10, 1), date(2026, 10, 31))
        self.assertIsNone(r["all"]["rate"])


class WebTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = create_app({"TESTING": True, "DATABASE": str(Path(self.tmp.name) / "t.sqlite"), "SECRET_KEY": "test"})
        conn = dbmod.connect(self.app.config["DATABASE"])
        for name in ("Clinic A", "Clinic B"):
            conn.execute("INSERT INTO clinics(name, whatsapp, country, currency, timezone, payment_methods) "
                         "VALUES (?, '+233504771418', 'GH', 'GHS', 'Africa/Accra', 'cash,mobile_money,card,bank_transfer,insurance')", (name,))
        for cid in (1, 2):
            conn.execute("INSERT INTO providers(clinic_id, name) VALUES (?, ?)", (cid, f"Dr {cid}"))
            conn.execute("INSERT INTO chairs(clinic_id, name) VALUES (?, 'Chair 1')", (cid,))
            TR.ensure_catalogue(conn, cid)
        conn.execute("UPDATE procedure_codes SET fee_p = 20000 WHERE code = 'CONS'")
        for uname, cid, role in (("manager", 1, "manager"), ("desk", 1, "front_desk"), ("other", 2, "manager")):
            conn.execute("INSERT INTO users(clinic_id, username, display_name, role, password_hash) VALUES (?,?,?,?,?)",
                         (cid, uname, uname.title(), role, generate_password_hash("correct-horse-battery")))
        conn.commit()
        conn.close()
        self.client = self.app.test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def token(self, path="/login"):
        return re.search(r'name="_csrf" value="([^"]+)"', self.client.get(path).get_data(as_text=True)).group(1)

    def login(self, user="manager"):
        self.client.post("/login", data={"username": user, "password": "correct-horse-battery", "_csrf": self.token()})

    def post(self, path, **data):
        data["_csrf"] = self.token("/")
        return self.client.post(path, data=data)

    def new_patient(self):
        self.post("/patients/new", first_name="Ama", last_name="Mensah", phone="0201112223", reminders_ok="1")

    def test_plan_do_charge_pay_receipt(self):
        self.login()
        self.new_patient()
        cons = code_id(dbmod.connect(self.app.config["DATABASE"]), 1, "CONS")
        r = self.post("/patients/1/treatment", code_id=str(cons), tooth="36", surfaces=["O", "D"])
        self.assertEqual(r.status_code, 302)
        page = self.client.get("/patients/1/treatment").get_data(as_text=True)
        self.assertIn("GH₵ 200.00", page)
        self.assertIn("36 OD", page)
        self.post("/procedures/1/done")
        self.assertIn("GH₵ 200.00", self.client.get("/patients/1/account").get_data(as_text=True))
        r = self.post("/patients/1/payments", amount="150", method="cash")
        self.assertEqual(r.status_code, 302)
        receipt = self.client.get(r.headers["Location"]).get_data(as_text=True)
        self.assertIn("R-000001", receipt)
        self.assertIn("GH₵ 150.00", receipt)
        self.assertIn("GH₵ 50.00", receipt)              # balance now
        self.assertIn("Account: GH₵ 50.00", self.client.get("/patients/1").get_data(as_text=True))

    def test_momo_needs_reference_message_shown(self):
        self.login()
        self.new_patient()
        r = self.post("/patients/1/payments", amount="50", method="mobile_money")
        self.assertEqual(r.headers["Location"], "/patients/1/account")
        self.assertIn("reference number", self.client.get("/patients/1/account").get_data(as_text=True))

    def test_bad_amount_does_not_crash(self):
        self.login()
        self.new_patient()
        self.assertEqual(self.post("/patients/1/payments", amount="lots", method="cash").status_code, 302)
        self.assertEqual(self.post("/patients/1/treatment", code_id="9999", tooth="99").status_code, 302)

    def test_desk_cannot_discount_void_or_see_reports(self):
        self.login("manager")
        self.new_patient()
        self.client.post("/logout", data={"_csrf": self.token("/")})
        self.login("desk")
        self.assertEqual(self.post("/patients/1/adjustments", amount="10", reason="discount", note="x").status_code, 403)
        for path in ("/reports", "/reports/takings", "/catalogue"):
            self.assertEqual(self.client.get(path).status_code, 403)
        self.assertEqual(self.client.get("/recalls").status_code, 200)
        self.assertEqual(self.client.get("/patients/1/account").status_code, 200)

    def test_other_clinic_gets_404_everywhere(self):
        self.login("manager")
        self.new_patient()
        self.post("/patients/1/treatment", code_id=str(code_id(dbmod.connect(self.app.config["DATABASE"]), 1)))
        r = self.post("/patients/1/payments", amount="10", method="cash")
        self.client.post("/logout", data={"_csrf": self.token("/")})
        self.login("other")
        for path in ("/patients/1/treatment", "/patients/1/account", r.headers["Location"]):
            self.assertEqual(self.client.get(path).status_code, 404, path)
        self.assertEqual(self.post("/procedures/1/done").status_code, 404)
        self.assertEqual(self.post("/patients/1/payments", amount="10", method="cash").status_code, 404)
        self.assertEqual(self.post("/payments/1/void", reason="try it").status_code, 404)

    def test_manager_adjustment_and_void(self):
        self.login()
        self.new_patient()
        self.post("/patients/1/treatment", code_id=str(code_id(dbmod.connect(self.app.config["DATABASE"]), 1)))
        self.post("/procedures/1/done")
        self.post("/patients/1/adjustments", amount="20", reason="birthday_offer", direction="discount")
        self.assertIn("Account: GH₵ 180.00", self.client.get("/patients/1").get_data(as_text=True))
        r = self.post("/patients/1/payments", amount="180", method="cash")
        pay_url = r.headers["Location"]
        self.post(pay_url.replace("/receipt", "/void"), reason="entered twice")
        self.assertIn("VOIDED", self.client.get(pay_url).get_data(as_text=True))
        self.assertIn("Account: GH₵ 180.00", self.client.get("/patients/1").get_data(as_text=True))

    def test_catalogue_and_reports_render(self):
        self.login()
        self.assertEqual(self.client.get("/catalogue").status_code, 200)
        self.post("/catalogue", action="update", id="1", fee="75.50", active="1")
        self.assertIn("75.50", self.client.get("/catalogue").get_data(as_text=True))
        self.assertEqual(self.post("/catalogue", action="add", code="CONS", name="dup", fee="0").status_code, 302)
        page = self.client.get("/reports?start=2026-10-01&end=2026-10-31").get_data(as_text=True)
        self.assertIn("Appointment confirmations", page)
        self.assertEqual(self.client.get("/reports?start=garbage").status_code, 200)
        self.assertEqual(self.client.get("/reports/takings?date=nonsense").status_code, 200)

    def test_recalls_page_after_completed_visit(self):
        self.login()
        self.new_patient()
        conn = dbmod.connect(self.app.config["DATABASE"])
        R.set_due_after_visit(conn, 1, 1, date.today() - timedelta(days=200))
        conn.commit()
        html = self.client.get("/recalls").get_data(as_text=True)
        self.assertIn("Ama Mensah", html)
        self.assertIn("Overdue", html)
        r = self.post("/recalls/1/whatsapp")
        self.assertTrue(r.headers["Location"].startswith("https://wa.me/233201112223"))
        self.post("/recalls/1/snooze", days="30")
        self.assertNotIn("Ama Mensah", self.client.get("/recalls").get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
