"""v0.40: finding and merging duplicate patient records."""
import json

from tests.test_v04 import Base
from dental_os.services import billing, merge as MG


class Merge(Base):
    def setUp(self):
        super().setUp()
        self.conn = c = self.db()
        self.dup = c.execute("INSERT INTO patients(clinic_id, first_name, last_name, phone, email) VALUES (1,'Ama','Mensa','+233201112223','ama@example.com')").lastrowid
        c.execute("UPDATE patients SET guarantor_id = ? WHERE id = 2", (self.dup,))
        code = c.execute("SELECT id FROM procedure_codes WHERE clinic_id = 1 LIMIT 1").fetchone()[0]
        for pid, fee in ((1, 10000), (self.dup, 25000)):
            c.execute("INSERT INTO procedures(clinic_id, patient_id, code_id, status, fee_p, planned_on, done_on) VALUES (1,?,?,'done',?,'2026-10-01','2026-10-01')", (pid, code, fee))
            c.execute("INSERT INTO medical_history(clinic_id, patient_id, allergies) VALUES (1,?,?)", (pid, "none" if pid == 1 else "penicillin"))
        c.execute("INSERT INTO payments(clinic_id, patient_id, amount_p, method, receipt_no, received_on) VALUES (1,?,5000,'cash',1,'2026-10-02')", (self.dup,))
        c.execute("INSERT INTO appointments(clinic_id, patient_id, start_at, status) VALUES (1,?,NULL,'asap')", (self.dup,))
        t1 = c.execute("INSERT INTO recall_types(clinic_id, name, interval_months) VALUES (1,'Check-up',6)").lastrowid
        t2 = c.execute("INSERT INTO recall_types(clinic_id, name, interval_months) VALUES (1,'Hygiene',6)").lastrowid
        c.execute("INSERT INTO recalls(clinic_id, patient_id, type_id, due_on) VALUES (1,1,?,'2027-04-01')", (t1,))
        c.execute("INSERT INTO recalls(clinic_id, patient_id, type_id, due_on) VALUES (1,?,?,'2027-03-01')", (self.dup, t1))
        c.execute("INSERT INTO recalls(clinic_id, patient_id, type_id, due_on) VALUES (1,?,?,'2027-03-01')", (self.dup, t2))
        c.commit()

    def tearDown(self):
        self.conn.close()

    def c(self):
        return dict(self.conn.execute("SELECT * FROM clinics WHERE id = 1").fetchone())


class ServiceTests(Merge):
    def test_finder_and_not_a_duplicate(self):
        rows = MG.candidates(self.conn, 1)
        self.assertEqual([(r["a_id"], r["b_id"], r["why"]) for r in rows], [(1, self.dup, "phone")])
        MG.not_duplicate(self.conn, self.c(), 1, self.dup, 1)
        self.assertEqual(MG.candidates(self.conn, 1), [])
        self.assertIn("appointments", MG.TABLES)
        self.assertIn("implant_items", MG.TABLES)

    def test_everything_moves_and_what_cannot_is_listed(self):
        with self.assertRaises(MG.MergeError):
            MG.merge(self.conn, self.c(), 1, 1, self.dup, False)
        with self.assertRaises(MG.MergeError):
            MG.merge(self.conn, self.c(), 1, 1, 3, True)                      # clinic two's patient
        mid = MG.merge(self.conn, self.c(), 1, 1, self.dup, True)
        m = MG.get_merge(self.conn, 1, mid)
        moved, left = json.loads(m["moved"]), json.loads(m["left_behind"])
        self.assertEqual((moved["procedures"], moved["payments"], moved["appointments"], moved["recalls"]), (1, 1, 1, 1))
        self.assertNotIn("medical_history", moved)
        self.assertEqual(len(left), 2)
        self.assertEqual(billing.totals(self.conn, 1, 1)["balance"], 10000 + 25000 - 5000)
        keep = self.conn.execute("SELECT * FROM patients WHERE id = 1").fetchone()
        self.assertEqual(keep["email"], "ama@example.com")
        dup = self.conn.execute("SELECT * FROM patients WHERE id = ?", (self.dup,)).fetchone()
        self.assertEqual((dup["status"], dup["merged_into"]), ("archived", 1))
        self.assertEqual(self.conn.execute("SELECT guarantor_id FROM patients WHERE id = 2").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM medical_history WHERE patient_id = ?", (self.dup,)).fetchone()[0], 1)
        with self.assertRaises(MG.MergeError):
            MG.merge(self.conn, self.c(), 1, 1, self.dup, True)                 # already archived


class ScreenTests(Merge):
    def test_manager_compares_and_merges(self):
        mgr = self.login("mgr")
        self.assertIn("Same phone", self.page(mgr, "/patients/duplicates"))
        page = self.page(mgr, f"/patients/merge?keep=1&dup={self.dup}")
        self.assertIn("Treatment: 1", page)
        page = self.post(mgr, "/patients/merge", {"keep": "1", "dup": str(self.dup)}, follow_redirects=True).get_data(as_text=True)
        self.assertIn("Tick that you have checked", page)
        page = self.post(mgr, "/patients/merge", {"keep": "1", "dup": str(self.dup), "confirm": "1"}, follow_redirects=True).get_data(as_text=True)
        self.assertIn("Records merged", page)
        self.assertIn("Left on the old record", page)
        self.assertIn("Merged into another record", self.page(mgr, f"/patients/{self.dup}"))
        self.assertEqual(self.login("desk").get("/patients/duplicates").status_code, 403)
        self.assertEqual(self.login("mgr2").get(f"/patients/merge?keep=1&dup={self.dup}").status_code, 404)
