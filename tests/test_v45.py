"""v0.45: treatment kits take stock off the shelf when treatment is done."""
from tests.test_v04 import Base
from dental_os.services import kits as K, stock as ST, treatment as TR


class Kits(Base):
    def setUp(self):
        super().setUp()
        self.conn = c = self.db()
        self.code = c.execute("SELECT id FROM procedure_codes WHERE clinic_id = 1 LIMIT 1").fetchone()[0]
        self.cap = c.execute("INSERT INTO stock_items(clinic_id, name, unit, cost_p) VALUES (1, 'Composite capsule', 'each', 450)").lastrowid
        self.brush = c.execute("INSERT INTO stock_items(clinic_id, name, unit, cost_p) VALUES (1, 'Bonding brush', 'each', 20)").lastrowid
        c.commit()

    def tearDown(self):
        self.conn.close()

    def c(self):
        return dict(self.conn.execute("SELECT * FROM clinics WHERE id = 1").fetchone())


class ServiceTests(Kits):
    def test_done_takes_stock_void_puts_it_back_and_shortfalls_are_listed(self):
        ST.move(self.conn, self.c(), 1, self.cap, "receive", 3)
        ST.move(self.conn, self.c(), 1, self.brush, "receive", 1)
        K.save(self.conn, self.c(), 1, self.code, {self.cap: 2, self.brush: 2})
        self.assertEqual(next(r for r in K.kits(self.conn, 1) if r["id"] == self.code)["cost_p"], 940)
        pr = TR.add_planned(self.conn, self.c(), 1, 1, self.code)
        short = TR.mark_done(self.conn, self.c(), 1, pr)
        self.assertEqual(short, [("Bonding brush", 2, 1)])
        self.assertEqual((ST.level(self.conn, 1, self.cap), ST.level(self.conn, 1, self.brush)), (1, 0))
        self.assertEqual(len(K.shortfalls(self.conn, 1)), 1)
        TR.void(self.conn, 1, 1, pr, "Wrong patient")
        self.assertEqual((ST.level(self.conn, 1, self.cap), ST.level(self.conn, 1, self.brush)), (3, 1))
        self.assertEqual(K.shortfalls(self.conn, 1), [])
        with self.assertRaises(K.KitError):
            K.save(self.conn, self.c(), 1, self.code, {self.cap: -1})
        K.save(self.conn, self.c(), 1, self.code, {self.brush: 0})
        self.assertEqual([l["item_id"] for l in K.lines(self.conn, 1, self.code)], [self.cap])

    def test_material_cost_report(self):
        ST.move(self.conn, self.c(), 1, self.cap, "receive", 10)
        K.save(self.conn, self.c(), 1, self.code, {self.cap: 1})
        for _ in range(2):
            TR.mark_done(self.conn, self.c(), 1, TR.add_planned(self.conn, self.c(), 1, 1, self.code, fee_p=10000))
        row = [r for r in K.material_costs(self.conn, 1, "2000-01-01", "2099-12-31")][0]
        self.assertEqual((row["n"], row["fees_p"], row["cost_p"]), (2, 20000, 900))


class ScreenTests(Kits):
    def test_manager_sets_a_kit_and_the_dentist_finishes_work(self):
        mgr = self.login("mgr")
        ST.move(self.conn, self.c(), 1, self.cap, "receive", 1)
        self.conn.commit()
        page = self.post(mgr, f"/stock/kits/{self.code}", {f"qty_{self.cap}": "2"}, follow_redirects=True).get_data(as_text=True)
        self.assertIn("Kit saved.", page)
        pr = TR.add_planned(self.conn, self.c(), 1, 1, self.code)
        self.conn.commit()
        page = self.post(self.login("dent"), f"/procedures/{pr}/done", {}, follow_redirects=True).get_data(as_text=True)
        self.assertIn("shows too little for this treatment", page)
        self.assertIn("Composite capsule, needed 2, taken 1", self.page(mgr, "/stock/kits"))
        self.assertEqual(self.login("desk").get("/stock/kits").status_code, 403)
        self.assertEqual(self.login("dent").get(f"/stock/kits/{self.code}").status_code, 403)
        self.assertEqual(self.login("mgr2").get(f"/stock/kits/{self.code}").status_code, 404)
