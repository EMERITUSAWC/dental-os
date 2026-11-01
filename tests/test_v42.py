"""v0.42 and v0.43: refunds of credit, and closing the books up to a date."""
import datetime

from tests.test_v04 import Base
from dental_os.services import ageing, billing, cashup, periodlock as PL, providerpay as PP, refunds as RF, treatment as TR

TODAY = datetime.date.today().isoformat()


class Money(Base):
    def setUp(self):
        super().setUp()
        self.conn = c = self.db()
        self.code = c.execute("SELECT id FROM procedure_codes WHERE clinic_id = 1 LIMIT 1").fetchone()[0]
        c.execute("INSERT INTO procedures(clinic_id, patient_id, code_id, status, fee_p, provider_id, planned_on, done_on) VALUES (1,1,?,'done',30000,1,'2026-09-01','2026-09-01')", (self.code,))
        c.execute("INSERT INTO payments(clinic_id, patient_id, amount_p, method, receipt_no, received_on) VALUES (1,1,50000,'cash',1,'2026-08-20')")
        c.commit()

    def tearDown(self):
        self.conn.close()

    def c(self):
        return dict(self.conn.execute("SELECT * FROM clinics WHERE id = 1").fetchone())


class RefundTests(Money):
    def test_only_credit_can_be_refunded_and_everything_follows(self):
        self.assertEqual(billing.totals(self.conn, 1, 1)["balance"], -20000)
        for amount, method, reason in ((25000, "cash", "overpaid"), (0, "cash", "overpaid"), (100, "insurance", "overpaid"), (100, "cash", "bogus")):
            with self.assertRaises(RF.RefundError):
                RF.refund(self.conn, self.c(), 1, 1, amount, method, reason)
        with self.assertRaises(RF.RefundError):
            RF.refund(self.conn, self.c(), 1, 2, 100, "cash", "overpaid")             # patient 2 holds no credit
        rid = RF.refund(self.conn, self.c(), 1, 1, 15000, "cash", "overpaid", refunded_on=TODAY)
        t = billing.totals(self.conn, 1, 1)
        self.assertEqual((t["refunded"], t["balance"]), (15000, -5000))
        self.assertEqual(billing.statement(self.conn, 1, 1)[-1]["kind"], "refund")
        self.assertEqual(billing.daily_takings(self.conn, 1, TODAY)["by_method"]["cash"], -15000)
        self.assertEqual(cashup.expected(self.conn, 1, TODAY)["cash"]["total"], -15000)
        self.assertEqual([x["credit"] for x in RF.credits(self.conn, 1)], [5000])
        with self.assertRaises(RF.RefundError):
            RF.refund(self.conn, self.c(), 1, 1, 6000, "cash", "overpaid")
        RF.void(self.conn, self.c(), 1, rid, "Typed twice")
        self.assertEqual(billing.totals(self.conn, 1, 1)["balance"], -20000)

    def test_ageing_and_associate_pay_see_the_refund(self):
        RF.refund(self.conn, self.c(), 1, 1, 5000, "cash", "overpaid", refunded_on="2026-08-25")
        self.conn.execute("INSERT INTO procedures(clinic_id, patient_id, code_id, status, fee_p, provider_id, planned_on, done_on) VALUES (1,1,?,'done',20000,1,?,?)",
                          (self.code, TODAY, TODAY))
        out = ageing.patient_ageing(self.conn, 1, 1, datetime.date.today())
        self.assertEqual(sum(out.values()), 5000)                                       # 300 + 200 work, 500 paid, 50 given back
        f = PP.figures(self.conn, 1, 1, "2000-01-01", "2099-12-31")
        self.assertEqual(f["collections"], 45000)


class LockTests(Money):
    def test_closed_days_refuse_changes(self):
        with self.assertRaises(PL.LockedError):
            PL.set_lock(self.conn, self.c(), 1, TODAY)                                   # only up to yesterday
        PL.set_lock(self.conn, self.c(), 1, "2026-09-30")
        with self.assertRaises(billing.BillingError):
            billing.void_payment(self.conn, 1, 1, 1, "Wrong patient")
        pr = self.conn.execute("SELECT id FROM procedures").fetchone()[0]
        with self.assertRaises(TR.TreatmentError):
            TR.void(self.conn, 1, 1, pr, "Wrong tooth")
        with self.assertRaises(PL.LockedError):
            RF.refund(self.conn, self.c(), 1, 1, 100, "cash", "overpaid", refunded_on="2026-09-15")
        RF.refund(self.conn, self.c(), 1, 1, 100, "cash", "overpaid", refunded_on=TODAY)  # after the lock is fine
        with self.assertRaises(PL.LockedError):
            PL.set_lock(self.conn, self.c(), 1, "2026-08-31")                            # going back needs a reason
        PL.set_lock(self.conn, self.c(), 1, "", "The accountant found an error in September")
        billing.void_payment(self.conn, 1, 1, 1, "Wrong patient")
        self.assertEqual(len(PL.history(self.conn, 1)), 2)


class ScreenTests(Money):
    def test_manager_refunds_and_closes_the_books(self):
        mgr = self.login("mgr")
        page = self.page(mgr, "/patients/1/account")
        self.assertIn("Refund the credit", page)
        page = self.post(mgr, "/patients/1/refunds", {"amount": "50", "method": "mobile_money", "reason": "overpaid", "reference": "MM123"},
                         follow_redirects=True).get_data(as_text=True)
        self.assertIn("Refund recorded.", page)
        self.assertIn("Refund 1", page)
        self.assertIn("Patients in credit", self.page(mgr, "/reports"))
        self.assertIn("150.00", self.page(mgr, "/credits"))
        page = self.post(mgr, "/books", {"locked_to": "2026-09-30"}, follow_redirects=True).get_data(as_text=True)
        self.assertIn("30 September 2026", page)
        self.assertNotIn("Refund the credit", self.page(self.login("desk"), "/patients/1/account"))
        self.assertEqual(self.login("desk").post("/patients/1/refunds", data={}).status_code in (400, 403), True)
        self.assertEqual(self.login("mgr2").get("/credits").status_code, 200)
        self.assertNotIn("Ama", self.page(self.login("mgr2"), "/credits"))
