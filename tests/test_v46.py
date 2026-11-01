"""v0.46: auto codes pick the code from the tooth and surfaces."""
from tests.test_v04 import Base
from dental_os.services import autocodes as AUTO


class Auto(Base):
    def setUp(self):
        super().setUp()
        self.conn = c = self.db()
        self.codes = [r[0] for r in c.execute("SELECT id FROM procedure_codes WHERE clinic_id = 1 ORDER BY id LIMIT 4")]
        clinic = dict(c.execute("SELECT * FROM clinics WHERE id = 1").fetchone())
        self.gid = AUTO.save_group(c, clinic, 1, "Composite filling")
        AUTO.add_rule(c, clinic, 1, self.gid, 1, "any", self.codes[0])
        AUTO.add_rule(c, clinic, 1, self.gid, 2, "any", self.codes[1])
        AUTO.add_rule(c, clinic, 1, self.gid, 2, "back", self.codes[2])
        AUTO.add_rule(c, clinic, 1, self.gid, 0, "front", self.codes[3])
        c.commit()

    def tearDown(self):
        self.conn.close()


class ServiceTests(Auto):
    def test_the_most_exact_rule_wins(self):
        self.assertEqual(AUTO.resolve(self.conn, 1, self.gid, 36, "MO"), self.codes[2])     # two surfaces, back
        self.assertEqual(AUTO.resolve(self.conn, 1, self.gid, 11, "MD"), self.codes[1])     # two surfaces beats front-only
        self.assertEqual(AUTO.resolve(self.conn, 1, self.gid, 11, "MID"), self.codes[3])    # three surfaces, front: only the front rule fits
        self.assertEqual(AUTO.resolve(self.conn, 1, self.gid, 53, "B"), self.codes[0])
        with self.assertRaises(AUTO.AutoError):
            AUTO.resolve(self.conn, 1, self.gid, 46, "MOD")                                  # no rule for three surfaces at the back
        with self.assertRaises(AUTO.AutoError):
            AUTO.add_rule(self.conn, {"id": 1}, 1, self.gid, 1, "any", self.codes[1])        # the case already has a rule
        self.assertEqual((AUTO.region(13), AUTO.region(14), AUTO.region(83), AUTO.region(None)), ("front", "back", "front", None))


class ScreenTests(Auto):
    def test_picking_by_type_on_the_treatment_screen(self):
        dent = self.login("dent")
        self.assertIn("Composite filling", self.page(dent, "/patients/1/treatment"))
        self.post(dent, "/patients/1/treatment", {"code_id": f"auto:{self.gid}", "tooth": "36", "surfaces": ["M", "O"]})
        self.assertEqual(self.conn.execute("SELECT code_id, surfaces FROM procedures WHERE patient_id = 1").fetchone()[:], (self.codes[2], "MO"))
        page = self.post(dent, "/patients/1/treatment", {"code_id": f"auto:{self.gid}", "tooth": "46", "surfaces": ["M", "O", "D"]},
                         follow_redirects=True).get_data(as_text=True)
        self.assertIn("No rule of this auto code fits", page)
        mgr = self.login("mgr")
        self.assertIn("Composite filling", self.page(mgr, "/autocodes"))
        self.assertIn("Back tooth", self.page(mgr, f"/autocodes/{self.gid}"))
        self.assertEqual(dent.get("/autocodes").status_code, 403)
        self.assertEqual(self.login("mgr2").get(f"/autocodes/{self.gid}").status_code, 404)
