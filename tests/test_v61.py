"""v0.61: dictation in clinical notes (on the device only) and chairside mode for a tablet at the chair."""
import json
import subprocess
from pathlib import Path

from tests.test_v04 import Base
from dental_os.services.timeutil import now_local

ROOT = Path(__file__).resolve().parent.parent


class DictationTests(Base):
    def test_the_tidying_rules(self):
        res = subprocess.run(["node", str(ROOT / "tests" / "js" / "dictate_cases.js")], capture_output=True, text=True, timeout=60)
        self.assertEqual(res.returncode, 0, res.stderr)
        cases = [json.loads(line) for line in res.stdout.splitlines() if line.strip()]
        self.assertGreaterEqual(len(cases), 10)
        self.assertEqual([c["name"] for c in cases if not c["ok"]], [])

    def test_it_only_ever_listens_on_the_device(self):
        js = (ROOT / "dental_os" / "static" / "dictate.js").read_text()
        self.assertIn("rec.processLocally = true", js)
        self.assertNotIn("processLocally = false", js)
        self.assertNotIn("cloud", js.lower().replace("no online fallback", ""))
        self.assertIn('"processLocally" in Rec.prototype', js)                      # no on-device listening: no button at all

    def test_off_until_a_manager_switches_it_on(self):
        dent = self.login("dent")
        page = self.page(dent, "/patients/1/notes/new")
        self.assertNotIn("data-dictate", page)
        self.assertNotIn("dictate.js", page)
        self.assertEqual(self.post(dent, "/settings", {"action": "voice_perio", "notes": "1"}).status_code, 403)
        mgr = self.login("mgr")
        self.post(mgr, "/settings", {"action": "voice_perio", "notes": "1"})
        conn = self.db()
        row = conn.execute("SELECT voice_perio, voice_cloud, voice_notes FROM clinics WHERE id = 1").fetchone()
        conn.close()
        self.assertEqual(tuple(row), (0, 0, 1))
        page = self.page(dent, "/patients/1/notes/new")
        self.assertIn("data-dictate", page)
        self.assertIn("dictate.js", page)
        self.assertIn('data-t-dictate="Dictate"', page)
        self.assertNotIn("data-dictate", self.page(self.login("mgr2"), "/patients/3/notes/new"))   # the other clinic did not switch it on


class Chair(Base):
    def setUp(self):
        super().setUp()
        self.conn = self.db()
        self.chair = self.conn.execute("INSERT INTO chairs(clinic_id, name) VALUES (1, 'Surgery 2')").lastrowid
        self.other_chair = self.conn.execute("INSERT INTO chairs(clinic_id, name) VALUES (2, 'Their chair')").lastrowid
        clinic = self.conn.execute("SELECT * FROM clinics WHERE id = 1").fetchone()
        self.today = now_local(clinic["timezone"]).date().isoformat()
        self.aid = self.conn.execute("""INSERT INTO appointments(clinic_id, patient_id, provider_id, chair_id, start_at, duration_min, status, category)
                                        VALUES (1, 1, 1, ?, ?, 30, 'scheduled', 'Consultation')""", (self.chair, f"{self.today} 23:30")).lastrowid
        self.conn.execute("INSERT INTO patient_alerts(clinic_id, patient_id, kind, severity, text) VALUES (1, 1, 'allergy', 'critical', 'Penicillin, anaphylaxis')")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def picked(self, who="dent"):
        c = self.login(who)
        self.post(c, "/chair/pick", {"chair_id": str(self.chair)})
        return c


class ChairTests(Chair):
    def test_clinical_staff_only(self):
        self.assertIn('href="/chair"', self.page(self.login("dent"), "/"))
        self.assertNotIn('href="/chair"', self.page(self.login("desk"), "/"))
        self.assertEqual(self.login("desk").get("/chair").status_code, 403)
        self.assertIn("Which chair is this tablet at?", self.page(self.login("asst"), "/chair"))

    def test_picking_a_chair_and_seeing_its_day(self):
        dent = self.login("dent")
        page = self.page(dent, "/chair")
        self.assertIn("Surgery 2", page)
        self.assertNotIn("Their chair", page)                                         # only this clinic's chairs
        self.assertEqual(self.post(dent, "/chair/pick", {"chair_id": str(self.other_chair)}).status_code, 404)
        self.post(dent, "/chair/pick", {"chair_id": str(self.chair)})
        page = self.page(dent, "/chair")
        self.assertIn("Surgery 2", page)
        self.assertIn("Ama Mensah", page)
        self.assertIn("Still to come", page)
        self.assertIn("Which chair is this tablet at?", self.page(dent, "/chair?pick=1"))

    def test_the_visit_shows_alerts_first_and_moves_the_patient(self):
        dent = self.picked()
        page = self.page(dent, f"/chair/visit/{self.aid}")
        self.assertLess(page.index("Penicillin, anaphylaxis"), page.index("Tooth chart"))
        self.assertIn('class="flash error" role="alert"', page)
        self.assertIn(f'href="/patients/1/perio/new"', page)
        self.post(dent, f"/chair/visit/{self.aid}/stage", {"action": "seat"})
        self.assertIn("In the chair", self.page(dent, "/chair"))
        self.assertIn("Finished, patient leaving", self.page(dent, f"/chair/visit/{self.aid}"))
        r = self.post(dent, f"/chair/visit/{self.aid}/stage", {"action": "leave"})
        self.assertTrue(r.headers["Location"].endswith("/chair"))
        row = self.conn.execute("SELECT status, seated_at, dismissed_at FROM appointments WHERE id = ?", (self.aid,)).fetchone()
        self.assertEqual(row["status"], "complete")
        self.assertIsNotNone(row["dismissed_at"])

    def test_another_clinic_cannot_open_the_visit(self):
        other = self.login("mgr2")
        self.assertEqual(other.get(f"/chair/visit/{self.aid}").status_code, 404)
        self.assertEqual(self.post(other, f"/chair/visit/{self.aid}/stage", {"action": "seat"}).status_code, 404)
        self.assertIsNone(self.conn.execute("SELECT seated_at FROM appointments WHERE id = ?", (self.aid,)).fetchone()[0])
