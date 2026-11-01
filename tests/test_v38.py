"""v0.38: staff registration, indemnity and CPD."""
import datetime

from werkzeug.datastructures import MultiDict

from tests.test_v04 import Base
from dental_os.services import staffcpd as SC

DENT = 2          # the "dent" login


class Cpd(Base):
    def setUp(self):
        super().setUp()
        self.conn = self.db()

    def tearDown(self):
        self.conn.close()

    def c(self):
        return dict(self.conn.execute("SELECT * FROM clinics WHERE id = 1").fetchone())

    def gdc(self, **extra):
        d = {"title": "dentist", "body": "General Dental Council (UK)", "reg_number": "123456", "scheme": "gdc", "cycle_start": "2023-01-01",
             "reg_expires_on": "2026-12-31", "indemnity_by": "A defence union", "indemnity_expires_on": "2026-11-15"}
        d.update(extra)
        SC.save_registration(self.conn, self.c(), 1, DENT, MultiDict(d))

    def cpd(self, day, hours, verifiable="1", outcomes=("C",)):
        return SC.add_cpd(self.conn, self.c(), DENT, DENT, MultiDict([("title", "Course"), ("done_on", day), ("hours", hours), ("verifiable", verifiable)]
                                                                     + [("outcomes", o) for o in outcomes]), today=datetime.date(2026, 10, 29))


class ServiceTests(Cpd):
    def test_gdc_cycle_progress_and_the_two_year_rule(self):
        self.gdc()
        self.cpd("2023-03-01", "4")
        self.cpd("2025-06-01", "3.5")
        self.cpd("2026-02-01", "2", verifiable="0", outcomes=())       # not verifiable: does not count for the GDC
        p = SC.progress(self.conn, 1, DENT, datetime.date(2026, 10, 29))
        self.assertEqual((p["start"], p["end"], p["target"], p["done"]), ("2023-01-01", "2027-12-31", 1000, 75))
        self.assertTrue(p["two_years_low"])
        self.assertTrue(p["behind"])
        with self.assertRaises(SC.CpdError):
            self.cpd("2026-05-01", "1", outcomes=())                     # GDC verifiable CPD needs an outcome
        for bad in ("0", "abc", "1.25"):
            with self.assertRaises(SC.CpdError):
                self.cpd("2026-05-01", bad)
        with self.assertRaises(SC.CpdError):
            self.cpd("2027-01-01", "1")
        rid = self.cpd("2026-09-01", "7")
        self.assertFalse(SC.progress(self.conn, 1, DENT, datetime.date(2026, 10, 29))["two_years_low"])
        SC.strike(self.conn, self.c(), DENT, rid, "Entered twice", own_uid=DENT)
        self.assertEqual(SC.progress(self.conn, 1, DENT, datetime.date(2026, 10, 29))["done"], 75)
        with self.assertRaises(SC.CpdError):
            SC.strike(self.conn, self.c(), 3, rid, "x", own_uid=3)          # someone else's entry

    def test_next_cycle_rolls_on_and_titles_set_the_hours(self):
        self.gdc(title="nurse", cycle_start="2020-08-01")
        p = SC.progress(self.conn, 1, DENT, datetime.date(2026, 10, 29))
        self.assertEqual((p["start"], p["target"]), ("2025-08-01", 500))
        with self.assertRaises(SC.CpdError):
            self.gdc(title="other")

    def test_yearly_target_and_expiry_warnings(self):
        SC.save_registration(self.conn, self.c(), 1, DENT, MultiDict({"title": "dentist", "body": "Medical and Dental Council (Ghana)", "scheme": "yearly",
                                                                     "yearly_target": "20", "reg_expires_on": "2026-12-01"}))
        self.cpd("2026-03-01", "6", outcomes=())
        self.cpd("2025-03-01", "9", outcomes=())
        p = SC.progress(self.conn, 1, DENT, datetime.date(2026, 10, 29))
        self.assertEqual((p["target"], p["done"], p["left"]), (200, 60, 140))
        w = SC.warnings(self.conn, 1, datetime.date(2026, 10, 29))
        self.assertEqual([(x["u"]["id"], x["warn"]) for x in w], [(DENT, [("Registration", "runs out", "2026-12-01")])])
        self.assertEqual(SC.warnings(self.conn, 1, datetime.date(2026, 6, 1)), [])
        self.assertEqual(SC.warnings(self.conn, 1, datetime.date(2027, 1, 1))[0]["warn"][0][1], "ran out")


class ScreenTests(Cpd):
    def test_people_keep_their_own_and_the_manager_sees_all(self):
        dent = self.login("dent")
        page = self.post(dent, "/cpd/mine", {"title": "Radiography update", "done_on": "2026-10-01", "hours": "5", "verifiable": "1",
                                             "outcomes": "C"}, follow_redirects=True).get_data(as_text=True)
        self.assertIn("CPD added.", page)
        self.assertIn("Radiography update", page)
        self.assertEqual(self.post(dent, "/cpd/mine", {"what": "registration", "title": "dentist", "scheme": "none"}).status_code, 403)
        self.assertEqual(dent.get("/cpd").status_code, 403)
        self.assertEqual(dent.get("/cpd/people/1").status_code, 403)
        mgr = self.login("mgr")
        page = self.post(mgr, f"/cpd/people/{DENT}", {"what": "registration", "title": "dentist", "scheme": "gdc", "cycle_start": "2026-01-01",
                                                      "body": "General Dental Council (UK)", "indemnity_expires_on": (datetime.date.today() + datetime.timedelta(days=10)).isoformat()},
                         follow_redirects=True).get_data(as_text=True)
        self.assertIn("Registration saved.", page)
        page = self.page(mgr, "/cpd")
        self.assertIn("5 / 100", page)
        self.assertIn("Indemnity runs out", page)
        self.assertIn("Indemnity runs out", self.page(mgr, "/governance"))
        self.assertIn("My CPD", self.page(self.login("desk"), "/"))
        self.assertEqual(self.login("mgr2").get(f"/cpd/people/{DENT}").status_code, 404)
