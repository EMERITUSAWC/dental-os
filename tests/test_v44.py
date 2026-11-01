"""v0.44: aftercare sheets, approved by a dentist and printed for patients."""
import datetime

from werkzeug.datastructures import MultiDict

from tests.test_v04 import Base
from dental_os.services import aftercare as AC


class Care(Base):
    def setUp(self):
        super().setUp()
        self.conn = c = self.db()
        self.code = c.execute("SELECT id FROM procedure_codes WHERE clinic_id = 1 LIMIT 1").fetchone()[0]
        c.execute("INSERT INTO procedures(clinic_id, patient_id, code_id, status, fee_p, planned_on, done_on) VALUES (1,1,?,'done',100,?,?)",
                  (self.code, datetime.date.today().isoformat(), datetime.date.today().isoformat()))
        c.commit()

    def tearDown(self):
        self.conn.close()

    def c(self):
        return dict(self.conn.execute("SELECT * FROM clinics WHERE id = 1").fetchone())

    def user(self, name):
        return self.conn.execute("SELECT * FROM users WHERE username = ?", (name,)).fetchone()


class ServiceTests(Care):
    def test_starter_needs_a_dentist_and_changes_need_approval_again(self):
        s = AC.sheets(self.conn, 1)[0]
        self.assertIn("30 minutes", s["body"])
        self.assertIsNone(s["approved_at"])
        with self.assertRaises(AC.AftercareError):
            AC.give(self.conn, self.c(), 1, 1, [s["id"]])
        with self.assertRaises(AC.AftercareError):
            AC.approve(self.conn, self.c(), self.user("mgr"), s["id"])            # a manager who is not a dentist
        AC.save(self.conn, self.c(), 1, MultiDict([("title", s["title"]), ("body", s["body"]), ("codes", str(self.code))]), s["id"])
        AC.approve(self.conn, self.c(), self.user("dent"), s["id"])
        self.assertEqual([x["id"] for x in AC.suggested(self.conn, 1, 1)], [s["id"]])
        AC.give(self.conn, self.c(), 3, 1, [s["id"]])
        self.assertEqual(len(AC.given(self.conn, 1, 1)), 1)
        AC.save(self.conn, self.c(), 1, MultiDict([("title", s["title"]), ("body", s["body"] + "\nNew line"), ("codes", str(self.code))]), s["id"])
        self.assertIsNone(AC.get(self.conn, 1, s["id"])["approved_at"])
        self.assertEqual(AC.suggested(self.conn, 1, 1), [])

    def test_french_falls_back_to_english(self):
        sid = AC.save(self.conn, self.c(), 1, MultiDict({"title": "After whitening", "body": "Avoid coloured food for 48 hours."}))
        s = AC.get(self.conn, 1, sid)
        self.assertEqual(AC.text_in(s, "fr")[1], "Avoid coloured food for 48 hours.")
        AC.save(self.conn, self.c(), 1, MultiDict({"title": "After whitening", "body": "Avoid coloured food for 48 hours.", "title_fr": "Après un blanchiment",
                                                   "body_fr": "Évitez les aliments colorés pendant 48 heures."}), sid)
        self.assertEqual(AC.text_in(AC.get(self.conn, 1, sid), "fr")[0], "Après un blanchiment")


class ScreenTests(Care):
    def test_dentist_approves_and_prints(self):
        dent = self.login("dent")
        page = self.page(dent, "/aftercare")
        self.assertIn("After a tooth extraction", page)
        sid = self.conn.execute("SELECT id FROM aftercare_sheets").fetchone()[0]
        self.post(dent, f"/aftercare/{sid}", {"title": "After a tooth extraction", "body": "Bite on gauze for 30 minutes.", "codes": str(self.code)})
        page = self.post(dent, f"/aftercare/{sid}", {"what": "approve"}, follow_redirects=True).get_data(as_text=True)
        self.assertIn("Approved.", page)
        page = self.page(dent, "/patients/1/aftercare")
        self.assertIn("for this patient", page)
        page = self.post(dent, "/patients/1/aftercare", {"sheet": str(sid)}).get_data(as_text=True)
        self.assertIn("Bite on gauze for 30 minutes.", page)
        self.assertIn("For Ama Mensah", page)
        self.assertEqual(self.login("desk").get("/patients/1/aftercare").status_code, 403)
        self.assertEqual(self.login("mgr2").get("/patients/1/aftercare").status_code, 404)
        self.assertEqual(self.login("mgr2").get(f"/aftercare/{sid}").status_code, 404)
