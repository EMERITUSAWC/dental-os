"""v0.52: diagnoses on claims, what a payer covers, monthly batches and the printed claim form."""
import datetime

from tests.test_v04 import Base
from dental_os.services import ghclaims as GC, insurance as INS


class Claims(Base):
    def setUp(self):
        super().setUp()
        self.conn = c = self.db()
        codes = c.execute("SELECT id FROM procedure_codes WHERE clinic_id = 1 ORDER BY id LIMIT 2").fetchall()
        self.ext, self.other = codes[0][0], codes[1][0]
        clinic = self.c()
        self.payer = INS.save_payer(c, clinic, 1, None, "Acme Health", "insurer", False, None, None, None)
        self.cover = INS.add_cover(c, clinic, 1, 1, self.payer, "AH-1234", "Ama Mensah", 80, None, None, None)
        self.month = datetime.date.today().strftime("%Y-%m")
        today = datetime.date.today().isoformat()
        self.p1 = c.execute("INSERT INTO procedures(clinic_id, patient_id, code_id, status, fee_p, provider_id, planned_on, done_on) VALUES (1,1,?,'done',20000,1,?,?)",
                            (self.ext, today, today)).lastrowid
        self.p2 = c.execute("INSERT INTO procedures(clinic_id, patient_id, code_id, status, fee_p, provider_id, planned_on, done_on) VALUES (1,1,?,'done',50000,1,?,?)",
                            (self.other, today, today)).lastrowid
        c.commit()

    def tearDown(self):
        self.conn.close()

    def c(self):
        return dict(self.conn.execute("SELECT * FROM clinics WHERE id = 1").fetchone())


class ServiceTests(Claims):
    def test_covered_list_needs_dx_and_the_batch(self):
        GC.set_covered(self.conn, self.c(), 1, self.payer, [self.ext], True)
        with self.assertRaises(INS.InsuranceError):
            INS.create_claim(self.conn, self.c(), 1, 1, self.cover, [self.p1, self.p2])          # no diagnosis yet
        with self.assertRaises(GC.ClaimFormError):
            GC.set_dx(self.conn, self.c(), 1, self.p1, "Z99.9")                                   # not on the list
        GC.set_dx(self.conn, self.c(), 1, self.p1, "k04.7")
        GC.set_dx(self.conn, self.c(), 1, self.p2, "K02.1")
        cid = INS.create_claim(self.conn, self.c(), 1, 1, self.cover, [self.p1, self.p2])
        items = {r["procedure_id"]: r for r in INS.claim_items(self.conn, 1, cid)}
        self.assertEqual(items[self.p1]["dx_code"], "K04.7")
        self.assertEqual(items[self.p1]["estimate_p"], 16000)                                     # 80% of 200.00
        self.assertEqual(items[self.p2]["estimate_p"], 0)
        self.assertIn("Not covered by this payer.", items[self.p2]["note"])
        bid = GC.make_batch(self.conn, self.c(), 1, self.payer, self.month)
        self.assertEqual(len(GC.batch_claims(self.conn, 1, bid)), 1)
        csv_text = GC.batch_csv(self.conn, self.c(), bid)
        self.assertIn("AH-1234", csv_text)
        self.assertIn("K04.7", csv_text)
        with self.assertRaises(GC.ClaimFormError):
            GC.make_batch(self.conn, self.c(), 1, self.payer, self.month)                        # nothing left for that month
        GC.mark_sent(self.conn, self.c(), 1, bid, None, "ACME-OCT")
        self.assertEqual(INS.get_claim(self.conn, 1, cid)["status"], "submitted")
        with self.assertRaises(GC.ClaimFormError):
            GC.mark_sent(self.conn, self.c(), 1, bid)

    def test_diagnosis_list_and_nhis(self):
        self.assertEqual(GC.diagnoses(self.conn, 1)["K05.3"], "Chronic periodontitis")
        GC.add_diagnosis(self.conn, self.c(), 1, "s02.5", "Fracture of tooth")
        self.assertIn("S02.5", GC.diagnoses(self.conn, 1))
        self.assertNotIn("S02.5", GC.diagnoses(self.conn, 2))
        for bad in (("K2", "x"), ("K02.1", "Again"), ("S03.0", "")):
            with self.assertRaises(GC.ClaimFormError):
                GC.add_diagnosis(self.conn, self.c(), 1, *bad)
        pid = GC.add_nhis(self.conn, self.c(), 1)
        p = INS.get_payer(self.conn, 1, pid)
        self.assertEqual((p["kind"], p["needs_dx"], p["contracted"]), ("government", 1, 1))
        with self.assertRaises(GC.ClaimFormError):
            GC.add_nhis(self.conn, self.c(), 1)
        self.assertTrue(GC.covers_code(self.conn, 1, pid, self.other))                         # no list yet: the usual rules


class ScreenTests(Claims):
    def test_dentist_codes_desk_batches_and_prints(self):
        dent = self.login("dent")
        self.assertIn("K04.7 Periapical abscess without sinus", self.page(dent, "/patients/1/treatment"))
        self.post(dent, f"/procedures/{self.p1}/dx", {"dx_code": "K04.7"})
        mgr = self.login("mgr")
        self.post(mgr, f"/payers/{self.payer}/covered", {"code": str(self.ext), "needs_dx": "1"})
        cid = INS.create_claim(self.conn, self.c(), 1, 1, self.cover, [self.p1])
        self.conn.commit()
        page = self.page(mgr, f"/claims/{cid}/form")
        self.assertIn("Periapical abscess without sinus", page)
        self.assertIn("AH-1234", page)
        page = self.post(mgr, "/claims/batches", {"payer_id": str(self.payer), "month": self.month}, follow_redirects=True).get_data(as_text=True)
        self.assertIn("Claims for", page)
        bid = self.conn.execute("SELECT id FROM claim_batches").fetchone()[0]
        r = mgr.get(f"/claims/batches/{bid}?csv=1")
        self.assertIn("K04.7", r.get_data(as_text=True))
        self.assertIn("text/csv", r.headers["Content-Type"])
        self.assertEqual(self.login("asst").get("/claims/batches").status_code, 403)
        self.assertEqual(self.login("mgr2").get(f"/claims/batches/{bid}").status_code, 404)
        self.assertEqual(self.login("mgr2").get(f"/claims/{cid}/form").status_code, 404)
        self.assertIn("Add the NHIS", self.page(mgr, "/payers"))
