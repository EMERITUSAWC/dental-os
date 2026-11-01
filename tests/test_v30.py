"""v0.30: permission groups narrow what a role allows, area by area, and never widen it."""
from tests.test_v04 import Base
from dental_os.services import permgroups as PG


class Groups(Base):
    def setUp(self):
        super().setUp()
        self.conn = self.db()
        self.c = dict(self.conn.execute("SELECT * FROM clinics WHERE id = 1").fetchone())

    def tearDown(self):
        self.conn.close()

    def uid(self, name):
        return self.conn.execute("SELECT id FROM users WHERE username = ?", (name,)).fetchone()[0]

    def test_every_screen_has_an_area_or_is_always_open_or_is_for_managers(self):
        self.assertEqual(PG.area_for("finance.claims_list"), "insurance")
        self.assertEqual(PG.area_for("steri.patient_instruments"), "clinical")     # longest match wins
        self.assertEqual(PG.area_for("steri.home"), "sterilisation")
        self.assertEqual(PG.area_for("record.import_start"), "patients")
        self.assertEqual(PG.area_for("clinical.recall_list"), "messages")
        self.assertEqual(PG.area_for("clinical.payment_void"), "money_changes")
        for ep in ("finance.ageing_report",):
            self.assertEqual(PG.area_for(ep), "reports")

    def test_a_front_desk_group_without_money(self):
        gid = PG.save(self.conn, self.c, 1, "Front desk, no money", ["diary", "patients", "messages"])
        PG.assign(self.conn, self.c, 1, self.uid("desk"), gid)
        with self.assertRaises(PG.GroupError):
            PG.assign(self.conn, self.c, 1, self.uid("mgr"), gid)                 # managers never in a group
        with self.assertRaises(PG.GroupError):
            PG.save(self.conn, self.c, 1, "Front desk, no money", [])
        self.conn.commit()
        desk = self.login("desk")
        self.page(desk, "/")
        self.page(desk, "/patients/1")
        self.page(desk, "/diary")
        self.page(desk, "/team")
        for url in ("/patients/1/account", "/claims", "/statements", "/patients/1/insurance"):
            self.assertEqual(desk.get(url).status_code, 403, url)
        nav = self.page(desk, "/")
        self.assertNotIn('href="/claims"', nav)
        self.assertIn('href="/diary"', nav)
        PG.assign(self.conn, self.c, 1, self.uid("desk"), None)
        self.conn.commit()
        self.page(desk, "/patients/1/account")

    def test_a_group_never_widens_a_role(self):
        gid = PG.save(self.conn, self.c, 1, "Everything", list(PG.AREAS))
        PG.assign(self.conn, self.c, 1, self.uid("desk"), gid)
        self.conn.commit()
        self.assertEqual(self.login("desk").get("/patients/1/medical").status_code, 403)   # clinical records stay closed to front desk
        self.assertEqual(self.login("desk").get("/settings").status_code, 403)

    def test_the_groups_screen(self):
        mgr = self.login("mgr")
        r = self.post(mgr, "/staff/groups", {"name": "Assistants, no reports", "area": ["clinical", "diary", "sterilisation"]}, follow_redirects=True)
        self.assertIn("Assistants, no reports", r.get_data(as_text=True))
        gid = self.conn.execute("SELECT id FROM perm_groups").fetchone()[0]
        self.post(mgr, "/staff/groups", {"what": "assign", "user_id": self.uid("asst"), "group_id": gid})
        asst = self.login("asst")
        self.page(asst, "/steri")
        self.assertEqual(asst.get("/ortho").status_code, 200)
        self.assertEqual(asst.get("/lab").status_code, 403)
        self.assertEqual(self.login("dent").get("/staff/groups").status_code, 403)
        self.assertIn("/staff/groups", self.page(mgr, "/staff"))
