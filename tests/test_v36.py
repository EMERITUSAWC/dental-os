"""v0.36: associate pay. A share of production or collections, less part of the lab bills, worked out per period."""
import re

from tests.test_v04 import Base
from dental_os.services import providerpay as PP


class Pay(Base):
    def setUp(self):
        super().setUp()
        self.conn = c = self.db()
        code = c.execute("SELECT id FROM procedure_codes WHERE clinic_id = 1 LIMIT 1").fetchone()[0]

        def proc(pid, fee, day, prov):
            c.execute("INSERT INTO procedures(clinic_id, patient_id, code_id, status, fee_p, provider_id, planned_on, done_on) VALUES (1,?,?,'done',?,?,?,?)",
                      (pid, code, fee, prov, day, day))

        def pay(pid, amount, day, n):
            c.execute("INSERT INTO payments(clinic_id, patient_id, amount_p, method, receipt_no, received_on) VALUES (1,?,?,'cash',?,?)", (pid, amount, n, day))
        proc(1, 100000, "2026-09-10", 1)
        proc(1, 50000, "2026-10-05", 1)
        proc(1, 20000, "2026-10-06", None)                # a late fee: stays with the clinic
        proc(2, 30000, "2026-10-02", 1)
        pay(1, 60000, "2026-09-01", 1)                    # a deposit before the work
        pay(1, 70000, "2026-10-07", 2)
        pay(2, 10000, "2026-10-03", 3)
        c.execute("INSERT INTO payments(clinic_id, patient_id, amount_p, method, receipt_no, received_on, voided) VALUES (1,2,99900,'cash',4,'2026-10-03',1)")
        c.execute("INSERT INTO adjustments(clinic_id, patient_id, amount_p, reason, adjusted_on) VALUES (1,1,-5000,'discount','2026-10-08')")
        lab = c.execute("INSERT INTO labs(clinic_id, name) VALUES (1, 'City Lab')").lastrowid
        c.execute("INSERT INTO lab_cases(clinic_id, patient_id, lab_id, provider_id, kind, status, sent_on, cost_p) VALUES (1,1,?,1,'crown','sent','2026-10-04',20000)", (lab,))
        c.commit()

    def tearDown(self):
        self.conn.close()

    def c(self):
        return dict(self.conn.execute("SELECT * FROM clinics WHERE id = 1").fetchone())


class FigureTests(Pay):
    def test_money_is_matched_oldest_first_and_counted_when_the_work_is_done(self):
        sep = PP.figures(self.conn, 1, 1, "2026-09-01", "2026-09-30")
        self.assertEqual((sep["gross_production"], sep["collections"]), (100000, 60000))
        octo = PP.figures(self.conn, 1, 1, "2026-10-01", "2026-10-31")
        self.assertEqual(octo["gross_production"], 80000)
        self.assertEqual(octo["discounts"], 5000)
        self.assertEqual(octo["net_production"], 75000)
        self.assertEqual(octo["collections"], 40000 + 30000 + 10000)  # finishes the September crown first, then October work
        self.assertEqual((octo["lab_fees"], octo["lab_cases"], octo["days"]), (20000, 1, 2))
        self.assertEqual(PP.figures(self.conn, 1, 2, "2026-10-01", "2026-10-31")["gross_production"], 0)

    def test_terms_by_date_and_a_full_run(self):
        with self.assertRaises(PP.PayError):
            PP.new_run(self.conn, self.c(), 1, 1, "2026-10-01", "2026-10-31")
        PP.set_terms(self.conn, self.c(), 1, 1, "gross_production", "50", "0", 0, "2026-01-01")
        PP.set_terms(self.conn, self.c(), 1, 1, "collections", "40", "50", 0, "2026-10-01")
        for bad in (("collections", "140", "0", 0), ("collections", "40.555", "0", 0), ("bogus", "40", "0", 0), ("collections", "0", "0", 0)):
            with self.assertRaises(PP.PayError):
                PP.set_terms(self.conn, self.c(), 1, 1, *bad, "2026-10-01")
        with self.assertRaises(PP.PayError):
            PP.set_terms(self.conn, self.c(), 1, 3, "collections", "40", "0", 0, "2026-10-01")       # not this clinic's provider
        self.assertEqual(PP.work_out(self.conn, 1, 1, "2026-09-01", "2026-09-30")["total"], 50000)    # September: the old terms
        rid = PP.new_run(self.conn, self.c(), 1, 1, "2026-10-01", "2026-10-31")
        r = PP.run(self.conn, 1, rid)
        self.assertEqual((r["base_p"], r["share_p"], r["lab_deduction_p"], r["total_p"]), (80000, 32000, 10000, 22000))
        PP.add_extra(self.conn, self.c(), 1, rid, "Advance paid 15 October", -5000)
        self.assertEqual(PP.run(self.conn, 1, rid)["total_p"], 17000)
        PP.approve(self.conn, self.c(), 1, rid)
        with self.assertRaises(PP.PayError):
            PP.add_extra(self.conn, self.c(), 1, rid, "Late change", 100)
        with self.assertRaises(PP.PayError):
            PP.new_run(self.conn, self.c(), 1, 1, "2026-10-15", "2026-11-15")                      # overlaps an approved run
        PP.mark_paid(self.conn, self.c(), 1, rid, "2026-11-02", "Bank transfer 8812")
        self.assertEqual(PP.run(self.conn, 1, rid)["status"], "paid")
        self.assertIsNone(PP.run(self.conn, 2, rid))

    def test_daily_amount_and_cancel(self):
        PP.set_terms(self.conn, self.c(), 1, 1, "gross_production", "0", "0", 30000, "2026-01-01")
        rid = PP.new_run(self.conn, self.c(), 1, 1, "2026-10-01", "2026-10-31")
        self.assertEqual(PP.run(self.conn, 1, rid)["total_p"], 60000)
        with self.assertRaises(PP.PayError):
            PP.cancel(self.conn, self.c(), 1, rid, "")
        PP.cancel(self.conn, self.c(), 1, rid, "Wrong period")
        with self.assertRaises(PP.PayError):
            PP.approve(self.conn, self.c(), 1, rid)


class ScreenTests(Pay):
    def test_manager_sets_terms_runs_and_pays(self):
        mgr = self.login("mgr")
        self.assertIn("Dr 1", self.page(mgr, "/pay"))
        page = self.post(mgr, "/pay/providers/1", {"basis": "collections", "percent": "40", "lab_percent": "50", "daily": "",
                                                  "effective_from": "2026-01-01"}, follow_redirects=True).get_data(as_text=True)
        self.assertIn("Pay terms saved.", page)
        r = self.post(mgr, "/pay/providers/1", {"what": "run", "start": "2026-10-01", "end": "2026-10-31"})
        url = r.headers["Location"]
        page = self.page(mgr, url)
        self.assertIn("Pay statement", page)
        self.assertIn("750.00", page)
        page = self.post(mgr, url, {"what": "extra", "label": "Bonus", "sign": "plus", "amount": "50"}, follow_redirects=True).get_data(as_text=True)
        self.assertIn("270.00", page)
        self.assertIn("40% of collections", page)
        self.post(mgr, url, {"what": "approve"})
        page = self.post(mgr, url, {"what": "paid", "paid_on": "2026-11-01"}, follow_redirects=True).get_data(as_text=True)
        self.assertIn("Marked as paid.", page)
        self.assertEqual(self.login("dent").get("/pay").status_code, 403)
        self.assertEqual(self.login("mgr2").get(url).status_code, 404)
        self.assertEqual(self.login("mgr2").get("/pay/providers/1").status_code, 404)
