"""v0.39: fee schedules."""
from tests.test_v04 import Base
from dental_os.services import feesched as FS, plans as PL, treatment as TR


class Fees(Base):
    def setUp(self):
        super().setUp()
        self.conn = c = self.db()
        codes = c.execute("SELECT id FROM procedure_codes WHERE clinic_id = 1 ORDER BY id LIMIT 2").fetchall()
        self.a, self.b = codes[0][0], codes[1][0]
        c.execute("UPDATE procedure_codes SET fee_p = 20000 WHERE id = ?", (self.a,))
        c.execute("UPDATE procedure_codes SET fee_p = 15050 WHERE id = ?", (self.b,))
        c.commit()

    def tearDown(self):
        self.conn.close()

    def c(self):
        return dict(self.conn.execute("SELECT * FROM clinics WHERE id = 1").fetchone())

    def code(self, cid):
        return self.conn.execute("SELECT * FROM procedure_codes WHERE id = ?", (cid,)).fetchone()


class ServiceTests(Fees):
    def test_own_price_then_percentage_then_standard(self):
        sid = FS.save(self.conn, self.c(), 1, "Staff and family", "10")
        FS.set_prices(self.conn, self.c(), 1, sid, {self.a: 5000})
        self.assertEqual(FS.fee_for(self.conn, 1, 1, self.code(self.a)), 20000)          # not on the schedule yet
        FS.assign(self.conn, self.c(), 1, 1, sid)
        self.assertEqual(FS.fee_for(self.conn, 1, 1, self.code(self.a)), 5000)
        self.assertEqual(FS.fee_for(self.conn, 1, 1, self.code(self.b)), 13545)          # 150.50 less 10%, rounded
        self.assertEqual(FS.fee_for(self.conn, 1, 2, self.code(self.a)), 20000)
        pr = TR.add_planned(self.conn, self.c(), 1, 1, self.a)
        self.assertEqual(TR.get(self.conn, 1, pr)["fee_p"], 5000)
        plan = PL.create(self.conn, self.c(), 1, 1, "Option A")
        item = PL.add_item(self.conn, self.c(), 1, plan, self.b)
        self.assertEqual(self.conn.execute("SELECT fee_p FROM plan_items WHERE id = ?", (item,)).fetchone()[0], 13545)
        FS.set_prices(self.conn, self.c(), 1, sid, {self.a: 6000})
        self.assertEqual(TR.get(self.conn, 1, pr)["fee_p"], 5000)                         # what was planned keeps its price
        FS.save(self.conn, self.c(), 1, "Staff and family", "10", sid=sid, active=False)
        self.assertEqual(FS.fee_for(self.conn, 1, 1, self.code(self.a)), 20000)          # a schedule switched off is ignored

    def test_bad_input(self):
        for bad in (("", "0"), ("X", "101"), ("X", "abc"), ("X", "1.234")):
            with self.assertRaises(FS.FeeError):
                FS.save(self.conn, self.c(), 1, *bad)
        sid = FS.save(self.conn, self.c(), 1, "Students", "5")
        with self.assertRaises(FS.FeeError):
            FS.save(self.conn, self.c(), 1, "students", "5")
        with self.assertRaises(FS.FeeError):
            FS.set_prices(self.conn, self.c(), 1, sid, {self.a: -1})
        with self.assertRaises(FS.FeeError):
            FS.assign(self.conn, self.c(), 1, 3, sid)
        other = dict(self.conn.execute("SELECT * FROM clinics WHERE id = 2").fetchone())
        with self.assertRaises(FS.FeeError):
            FS.assign(self.conn, other, 5, 3, sid)


class ScreenTests(Fees):
    def test_manager_makes_a_list_and_puts_a_patient_on_it(self):
        mgr = self.login("mgr")
        r = self.post(mgr, "/fees/schedules", {"name": "Company A workers", "percent_off": "15"})
        url = r.headers["Location"]
        page = self.post(mgr, url, {"what": "prices", f"fee_{self.a}": "120"}, follow_redirects=True).get_data(as_text=True)
        self.assertIn("Prices saved.", page)
        self.assertIn("127.93", page)                                                      # 150.50 less 15%
        sid = int(url.rsplit("/", 1)[1])
        page = self.post(mgr, "/patients/1/prices", {"schedule_id": str(sid)}, follow_redirects=True).get_data(as_text=True)
        self.assertIn("Company A workers", page)
        self.assertIn("Standard prices", self.page(mgr, "/patients/2"))
        self.assertEqual(self.login("desk").get("/fees/schedules").status_code, 403)
        self.assertEqual(self.login("mgr2").get(url).status_code, 404)
