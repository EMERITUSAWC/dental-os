"""v0.35: the implant and materials register, and working through a maker's recall."""
from tests.test_v04 import Base
from dental_os.services import implants as IM


def item(**extra):
    d = {"kind": "implant", "tooth": "36", "maker": "Acme Dental", "product": "Bone Level 4.1", "ref_code": "BL-041-10",
         "lot": "LX2291", "size": "4.1 x 10 mm", "expires_on": "2029-01-31", "placed_on": "2026-03-02", "provider_id": "1"}
    d.update(extra)
    return d


class Implants(Base):
    def setUp(self):
        super().setUp()
        self.conn = self.db()

    def tearDown(self):
        self.conn.close()

    def c(self):
        return dict(self.conn.execute("SELECT * FROM clinics WHERE id = 1").fetchone())


class ServiceTests(Implants):
    def test_add_checks_what_a_recall_needs(self):
        iid = IM.add(self.conn, self.c(), 1, 1, item())
        it = IM.get(self.conn, 1, iid)
        self.assertEqual((it["tooth"], it["lot"], it["provider_id"]), (36, "LX2291", 1))
        for bad in (item(lot=""), item(maker=" "), item(kind="bolt"), item(tooth="99"), item(expires_on="2026-01-01"), item(placed_on="2026-02-30")):
            with self.assertRaises(IM.ImplantError):
                IM.add(self.conn, self.c(), 1, 1, bad)
        with self.assertRaises(IM.ImplantError):
            IM.add(self.conn, self.c(), 1, 3, item())                       # clinic two's patient
        it2 = IM.get(self.conn, 1, IM.add(self.conn, self.c(), 1, 1, item(provider_id="2", tooth="")))
        self.assertIsNone(it2["provider_id"])                                # clinic two's dentist is ignored
        self.assertIsNone(it2["tooth"])

    def test_removal_keeps_the_line(self):
        iid = IM.add(self.conn, self.c(), 1, 1, item())
        with self.assertRaises(IM.ImplantError):
            IM.remove(self.conn, self.c(), 1, iid, "failed", "2026-01-01")
        IM.remove(self.conn, self.c(), 1, iid, "failed", "2026-06-01", "Mobile at review")
        with self.assertRaises(IM.ImplantError):
            IM.remove(self.conn, self.c(), 1, iid, "failed")
        self.assertEqual(len(IM.for_patient(self.conn, 1, 1)), 1)
        self.assertEqual(IM.for_patient(self.conn, 1, 1, True), [])

    def test_a_recall_finds_every_patient_and_tracks_who_was_told(self):
        IM.add(self.conn, self.c(), 1, 1, item())
        IM.add(self.conn, self.c(), 1, 2, item(tooth="", lot="lx2291", kind="membrane"))
        IM.add(self.conn, self.c(), 1, 1, item(tooth="46", lot="OTHER1"))
        other = dict(self.conn.execute("SELECT * FROM clinics WHERE id = 2").fetchone())
        IM.add(self.conn, other, 5, 3, item())                              # same lot in clinic two
        self.assertEqual(len(IM.find(self.conn, 1, lot="LX2291")), 2)
        self.assertEqual(len(IM.find(self.conn, 1, maker="acme")), 3)
        self.assertEqual(len(IM.find(self.conn, 1, maker="acme", placed_from="2026-04-01")), 0)
        with self.assertRaises(IM.ImplantError):
            IM.find(self.conn, 1)
        with self.assertRaises(IM.ImplantError):
            IM.open_recall(self.conn, self.c(), 1, "Acme", "LX2291", "")
        rid, n = IM.open_recall(self.conn, self.c(), 1, "Acme", "LX2291", "Packaging may not be sterile")
        self.assertEqual(n, 2)
        rows = IM.recall_patients(self.conn, 1, rid)
        IM.mark_contacted(self.conn, self.c(), 1, rid, rows[0]["id"], "Phoned, review booked")
        with self.assertRaises(IM.ImplantError):
            IM.mark_contacted(self.conn, self.c(), 1, rid, rows[0]["id"])
        r = IM.recalls(self.conn, 1)[0]
        self.assertEqual((r["found"], r["contacted"]), (2, 1))
        self.assertEqual(IM.recall_patients(self.conn, 2, rid), [])
        with self.assertRaises(IM.ImplantError):
            IM.mark_contacted(self.conn, other, 5, rid, rows[1]["id"])
        IM.close_recall(self.conn, self.c(), 1, rid)
        with self.assertRaises(IM.ImplantError):
            IM.close_recall(self.conn, self.c(), 1, rid)


class ScreenTests(Implants):
    def test_dentist_records_prints_card_and_runs_a_recall(self):
        self.conn.commit()
        dent = self.login("dent")
        r = self.post(dent, "/patients/1/implants", item(), follow_redirects=True)
        page = r.get_data(as_text=True)
        self.assertIn("Item added to the register.", page)
        self.assertIn("LX2291", page)
        page = self.post(dent, "/patients/1/implants", item(lot=""), follow_redirects=True).get_data(as_text=True)
        self.assertIn("lot number are both needed", page)
        card = self.page(dent, "/patients/1/implants/card")
        self.assertIn("Implant card", card)
        self.assertIn("Bone Level 4.1", card)
        page = self.page(dent, "/implants?lot=lx2291")
        self.assertIn("Ama", page)
        self.assertIn("Open a recall", page)
        r = self.post(dent, "/implants/recalls", {"maker": "", "lot": "LX2291", "reason": "Maker's notice 12"}, follow_redirects=True)
        page = r.get_data(as_text=True)
        self.assertIn("Recall opened", page)
        rid = self.conn.execute("SELECT id FROM material_recalls").fetchone()[0]
        row = self.conn.execute("SELECT id FROM material_recall_patients").fetchone()[0]
        page = self.post(dent, f"/implants/recalls/{rid}", {"row_id": str(row), "note": "Phoned"}, follow_redirects=True).get_data(as_text=True)
        self.assertIn("Marked as contacted.", page)
        iid = self.conn.execute("SELECT id FROM implant_items").fetchone()[0]
        page = self.post(dent, "/patients/1/implants", {"what": "remove", "item_id": str(iid), "reason": "recall"}, follow_redirects=True).get_data(as_text=True)
        self.assertIn("Maker&#39;s recall", page)
        self.assertNotEqual(self.login("desk").get("/patients/1/implants").status_code, 200)
        self.assertEqual(self.login("mgr2").get("/patients/1/implants").status_code, 404)
        self.assertEqual(self.login("mgr2").get(f"/implants/recalls/{rid}").status_code, 404)
        self.assertIn("Implants", self.page(dent, "/patients/1"))
