"""v0.24: NHS dentistry in England: courses, bands, charges and exemptions, UDAs, FP17 tracking."""
import datetime

from tests.test_v04 import Base
from dental_os.services import billing, nhs as N, treatment as TR
from dental_os.services.timeutil import now_local


class Nhs(Base):
    def setUp(self):
        super().setUp()
        self.conn = self.db()
        self.conn.execute("UPDATE clinics SET country = 'GB', currency = 'GBP', timezone = 'Europe/London' WHERE id = 1")
        TR.ensure_catalogue(self.conn, 1)
        self.conn.execute("UPDATE patients SET gender = 'Female', address = '1 High St, Leeds' WHERE id = 1")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def c(self):
        return dict(self.conn.execute("SELECT * FROM clinics WHERE id = 1").fetchone())

    def today(self):
        return now_local("Europe/London").date()

    def code(self, code):
        return self.conn.execute("SELECT id FROM procedure_codes WHERE clinic_id = 1 AND code = ?", (code,)).fetchone()[0]

    def switch_on(self, year=True):
        N.save_settings(self.conn, self.c(), 1, True, "123456/0001")
        N.save_performer(self.conn, self.c(), 1, 1, "654321")
        N.save_class(self.conn, self.c(), 1, self.code("FILL"), "b2")
        N.save_class(self.conn, self.c(), 1, self.code("CONS"), "b1")
        N.save_class(self.conn, self.c(), 1, self.code("CROWN"), "b3")
        if year:
            N.save_year(self.conn, self.c(), 1, N.year_of(self.today()), "5000", "30")
        self.conn.commit()

    def do(self, code, pid=1):
        pr = TR.add_planned(self.conn, self.c(), 2, pid, self.code(code), tooth=36, fee_p=0, provider_id=1)
        TR.mark_done(self.conn, self.c(), 2, pr)
        return pr

    def balance(self, pid=1):
        return billing.totals(self.conn, 1, pid)["balance"]


class SetupTests(Nhs):
    def test_must_be_switched_on_with_a_contract_and_a_performer(self):
        with self.assertRaises(N.NhsError):
            N.open_course(self.conn, self.c(), 1, 1, 1)
        with self.assertRaises(N.NhsError):
            N.save_settings(self.conn, self.c(), 1, True, "")
        N.save_settings(self.conn, self.c(), 1, True, "123456/0001")
        with self.assertRaises(N.NhsError) as e:
            N.open_course(self.conn, self.c(), 1, 1, 1)
        self.assertIn("performer number", str(e.exception))
        with self.assertRaises(N.NhsError):
            N.save_performer(self.conn, self.c(), 1, 1, "AB12")
        self.assertEqual(N.settings(self.conn, 1)["charges"]["band2"], 7660)

    def test_the_year_runs_from_april_and_charges_can_be_updated(self):
        self.assertEqual(N.year_of(datetime.date(2027, 3, 31)), 2026)
        self.assertEqual(N.year_of(datetime.date(2027, 4, 1)), 2027)
        N.save_settings(self.conn, self.c(), 1, True, "1", {"band1": "28.50", "band2": "78", "band3": "340.00", "from": "2027-04-01", "source": "test"})
        self.assertEqual(N.settings(self.conn, 1)["charges"], {"band1": 2850, "band2": 7800, "band3": 34000, "from": "2027-04-01", "source": "test"})
        with self.assertRaises(N.NhsError):
            N.save_year(self.conn, self.c(), 1, 2026, "lots", "30")


class CourseTests(Nhs):
    def test_a_band_2_course_charges_the_patient_and_earns_3_udas(self):
        self.switch_on()
        before = self.balance()
        cid = N.open_course(self.conn, self.c(), 3, 1, 1)
        self.do("CONS")
        self.do("FILL")
        c = N.get(self.conn, 1, cid)
        self.assertEqual(N.suggest(N.items(self.conn, 1, c)), "band2")
        with self.assertRaises(N.NhsError):
            N.open_course(self.conn, self.c(), 3, 1, 1)                       # one open course at a time
        N.close_course(self.conn, self.c(), 3, cid, "band2a")
        c = N.get(self.conn, 1, cid)
        self.assertEqual((c["status"], c["uda"], c["charge_p"]), ("closed", 3.0, 7660))
        self.assertEqual(c["fp17_due"], (self.today() + datetime.timedelta(days=62)).isoformat())
        self.assertEqual(self.balance() - before, 7660)
        pr = N.progress(self.conn, self.c())
        self.assertEqual((pr["done"], pr["target"], pr["courses"]), (3.0, 5000, 1))
        self.assertEqual(pr["urgent_target"], 165)                           # £150,000 contract: 11 per £10,000
        N.withdraw(self.conn, self.c(), 1, cid, "Opened by mistake")
        self.assertEqual(self.balance(), before)                             # the charge is taken off again
        self.assertEqual(N.progress(self.conn, self.c())["done"], 0)

    def test_exemptions(self):
        self.switch_on()
        kid = N.open_course(self.conn, self.c(), 3, 2, 1, exemption="under18")
        self.do("CROWN", pid=2)
        N.close_course(self.conn, self.c(), 3, kid, "band3")
        self.assertEqual((N.get(self.conn, 1, kid)["charge_p"], N.get(self.conn, 1, kid)["uda"]), (0, 12.0))
        with self.assertRaises(N.NhsError) as e:
            N.open_course(self.conn, self.c(), 3, 1, 1, exemption="under18")    # Ama is an adult
        self.assertIn("18 or over", str(e.exception))
        hc3 = N.open_course(self.conn, self.c(), 3, 1, 1, exemption="hc3", evidence="seen", hc3_amount="40")
        N.close_course(self.conn, self.c(), 3, hc3, "band3")
        self.assertEqual(N.get(self.conn, 1, hc3)["charge_p"], 4000)          # the HC3 limit, below the Band 3 charge
        self.assertEqual(N.charge_for(self.conn, 1, "band1", "hc3", self.today(), 99999), 2790)
        self.assertEqual(N.charge_for(self.conn, 1, "urgent", "none", self.today()), 2790)
        self.assertEqual(N.charge_for(self.conn, 1, "band2c", "pregnant", self.today()), 0)

    def test_urgent_care_udas_come_from_the_uda_value(self):
        self.switch_on(year=False)
        cid = N.open_course(self.conn, self.c(), 3, 1, 1)
        with self.assertRaises(N.NhsError) as e:
            N.close_course(self.conn, self.c(), 3, cid, "urgent")
        self.assertIn("UDA value", str(e.exception))
        N.save_year(self.conn, self.c(), 1, N.year_of(self.today()), "5000", "30")
        N.close_course(self.conn, self.c(), 3, cid, "urgent")
        self.assertEqual(N.get(self.conn, 1, cid)["uda"], 2.5)                 # £75 at £30 a UDA
        self.assertEqual(N.progress(self.conn, self.c())["urgent"], 1)
        k = N.open_course(self.conn, self.c(), 3, 1, 1)
        with self.assertRaises(N.NhsError):
            N.close_course(self.conn, self.c(), 3, k, "band1_fv")             # fluoride varnish course is for children

    def test_the_fp17_must_be_complete_before_it_is_marked_sent(self):
        self.switch_on()
        self.conn.execute("UPDATE patients SET address = NULL WHERE id = 1")
        cid = N.open_course(self.conn, self.c(), 3, 1, 1, exemption="universal_credit", evidence="na")
        self.do("FILL")
        N.close_course(self.conn, self.c(), 3, cid, "band2b")
        c = N.get(self.conn, 1, cid)
        msgs = " ".join(N.fp17_problems(self.conn, self.c(), c))
        for word in ("address", "declaration", "evidence"):
            self.assertIn(word, msgs)
        with self.assertRaises(N.NhsError):
            N.mark_sent(self.conn, self.c(), 1, cid, "REF1")
        self.conn.execute("UPDATE patients SET address = '1 High St' WHERE id = 1")
        N.update_course(self.conn, self.c(), 1, cid, "universal_credit", "seen")
        N.record_declaration(self.conn, self.c(), 1, cid)
        N.mark_sent(self.conn, self.c(), 1, cid, "BSA-778")
        with self.assertRaises(N.NhsError):
            N.update_course(self.conn, self.c(), 1, cid, "none", "na")         # sent: locked
        with self.assertRaises(N.NhsError):
            N.mark_outcome(self.conn, self.c(), 1, cid, "rejected", "")
        N.mark_outcome(self.conn, self.c(), 1, cid, "accepted")
        self.assertEqual(N.get(self.conn, 1, cid)["status"], "accepted")
        rows = N.fp17_rows(self.conn, self.c(), "accepted")
        self.assertEqual(rows[1][0], "123456/0001")
        self.assertEqual(rows[1][1], "654321")
        self.assertEqual(rows[1][9], "band2b")


class ScreenTests(Nhs):
    def test_screens(self):
        self.switch_on()
        mgr = self.login("mgr")
        r = self.post(mgr, "/patients/1/nhs", {"what": "open", "provider_id": "1", "exemption": "none", "evidence": "na"}, follow_redirects=True)
        self.assertIn("Course of treatment started.", r.get_data(as_text=True))
        cid = self.conn.execute("SELECT id FROM nhs_courses").fetchone()[0]
        self.do("FILL")
        self.conn.commit()
        page = self.page(mgr, "/patients/1/nhs")
        self.assertIn("Band 2: choose 2a, 2b or 2c", page)
        r = self.post(mgr, "/patients/1/nhs", {"what": "close", "course_id": cid, "course_type": "band2a"}, follow_redirects=True)
        self.assertIn("FP17 is due within 62 days", r.get_data(as_text=True))
        for url in ("/nhs", "/nhs?status=open", "/nhs/setup", f"/nhs/courses/{cid}/fp17", "/patients/1"):
            self.page(mgr, url)
        self.assertIn(">NHS<", self.page(mgr, "/"))
        csv = mgr.get("/nhs/fp17.csv").get_data(as_text=True)
        self.assertIn("Mensah", csv)
        self.assertIn("not switched on", self.page(self.login("mgr2"), "/nhs"))   # a manager of a clinic without NHS work sees why
        self.conn.execute("UPDATE nhs_settings SET enabled = 0")
        self.conn.commit()
        self.assertEqual(self.login("desk").get("/nhs").status_code, 404)
        self.assertEqual(self.login("mgr2").get(f"/nhs/courses/{cid}/fp17").status_code, 404)
