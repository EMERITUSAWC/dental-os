"""v0.37: complaints and patient safety incidents."""
import datetime

from tests.test_v04 import Base
from dental_os.services import governance as GV


class Gov(Base):
    def setUp(self):
        super().setUp()
        self.conn = self.db()

    def tearDown(self):
        self.conn.close()

    def c(self):
        return dict(self.conn.execute("SELECT * FROM clinics WHERE id = 1").fetchone())


class ComplaintTests(Gov):
    def test_working_days_skip_the_weekend(self):
        self.assertEqual(GV.add_working_days("2026-10-29", 3), "2026-11-03")       # Thursday -> Tuesday
        self.assertEqual(GV.add_working_days("2026-10-31", 3), "2026-11-04")       # Saturday -> Wednesday

    def test_a_complaint_goes_through_every_step(self):
        kid = GV.add_complaint(self.conn, self.c(), 1, {"patient_id": "1", "channel": "phone", "category": "cost",
                                                         "summary": "Charged twice for the filling", "received_on": "2026-10-29"})
        k = GV.complaint(self.conn, 1, kid)
        self.assertEqual((k["ack_due"], k["status"]), ("2026-11-03", "open"))
        self.assertEqual(len(GV.late(self.conn, 1, datetime.date(2026, 11, 4))), 1)
        with self.assertRaises(GV.GovError):
            GV.close(self.conn, self.c(), 1, kid, "Learned")
        with self.assertRaises(GV.GovError):
            GV.acknowledge(self.conn, self.c(), 1, kid, "2026-10-28", "2026-11-20")
        GV.acknowledge(self.conn, self.c(), 1, kid, "2026-11-02", "2026-11-20", "Phoned back")
        GV.add_note(self.conn, self.c(), 1, kid, "Checked the account: the payment was taken twice by the card machine")
        self.assertEqual(GV.late(self.conn, 1, datetime.date(2026, 11, 4)), [])
        self.assertEqual(len(GV.late(self.conn, 1, datetime.date(2026, 11, 21))), 1)
        GV.respond(self.conn, self.c(), 1, kid, "2026-11-10", "upheld", "Apologised and refunded the second charge")
        with self.assertRaises(GV.GovError):
            GV.close(self.conn, self.c(), 1, kid, " ")
        GV.close(self.conn, self.c(), 1, kid, "Card machine receipts checked at cash-up")
        self.assertEqual(len(GV.steps(self.conn, kid)), 4)
        s = GV.complaint_summary(self.conn, 1, "2026-01-01", "2026-12-31")
        self.assertEqual((s["total"], s["ack_on_time"], s["answered_on_time"], s["by_outcome"]), (1, 1, 1, {"upheld": 1}))
        with self.assertRaises(GV.GovError):
            GV.add_note(self.conn, self.c(), 1, kid, "Too late")
        self.assertIsNone(GV.complaint(self.conn, 2, kid))

    def test_bad_complaints_are_refused(self):
        for bad in ({"summary": "x"}, {"complainant": "Mrs A", "summary": ""}, {"complainant": "Mrs A", "summary": "x", "channel": "fax"},
                    {"patient_id": "3", "summary": "x"}):
            with self.assertRaises(GV.GovError):
                GV.add_complaint(self.conn, self.c(), 1, bad)


class IncidentTests(Gov):
    def test_harm_to_a_patient_needs_candour_before_closing(self):
        iid = GV.add_incident(self.conn, self.c(), 3, {"kind": "wrong_site", "harm": "moderate", "patient_id": "1",
                                                        "what_happened": "Filling started on 36 instead of 46", "occurred_on": "2026-10-20"},
                              today=datetime.date(2026, 10, 29))
        self.assertTrue(GV.candour_needed(GV.incident(self.conn, 1, iid)))
        with self.assertRaises(GV.GovError):
            GV.close_incident(self.conn, self.c(), 1, iid)
        GV.review(self.conn, self.c(), 1, iid, "Chart and X-ray mirrored on the screen", "Say the tooth aloud and check before starting")
        with self.assertRaises(GV.GovError):
            GV.close_incident(self.conn, self.c(), 1, iid)
        with self.assertRaises(GV.GovError):
            GV.candour(self.conn, self.c(), 1, iid, "2026-10-21", "Dr 1", False)
        GV.candour(self.conn, self.c(), 1, iid, "2026-10-21", "Dr 1", True)
        with self.assertRaises(GV.GovError):
            GV.close_incident(self.conn, self.c(), 1, iid)                     # the written follow-up is still missing
        GV.candour(self.conn, self.c(), 1, iid, "2026-10-21", "Dr 1", True, "2026-10-23")
        GV.close_incident(self.conn, self.c(), 1, iid)
        self.assertEqual(GV.incident(self.conn, 1, iid)["status"], "closed")

    def test_staff_injury_needs_no_candour_and_future_dates_are_refused(self):
        iid = GV.add_incident(self.conn, self.c(), 3, {"kind": "sharps", "harm": "low", "what_happened": "Needlestick while recapping"})
        GV.review(self.conn, self.c(), 1, iid, "Recapping by hand", "Use the recapping block", reported_to="Occupational health")
        GV.close_incident(self.conn, self.c(), 1, iid)
        with self.assertRaises(GV.GovError):
            GV.add_incident(self.conn, self.c(), 3, {"kind": "fall", "what_happened": "x", "occurred_on": "2099-01-01"})
        self.assertEqual(GV.incident_summary(self.conn, 1, "2000-01-01", "2099-01-01")["by_kind"], {"sharps": 1})


class ScreenTests(Gov):
    def test_anyone_reports_managers_handle(self):
        desk = self.login("desk")
        page = self.post(desk, "/governance/complaints/new", {"patient_id": "1", "channel": "in_person", "category": "waiting",
                                                              "summary": "Waited an hour"}, follow_redirects=True).get_data(as_text=True)
        self.assertIn("Complaint written down", page)
        page = self.post(desk, "/governance/incidents/new", {"kind": "fall", "harm": "none", "what_happened": "Slipped on a wet floor"},
                         follow_redirects=True).get_data(as_text=True)
        self.assertIn("Thank you for reporting it", page)
        self.assertEqual(desk.get("/governance").status_code, 403)
        kid = self.conn.execute("SELECT id FROM complaints").fetchone()[0]
        self.assertEqual(desk.get(f"/governance/complaints/{kid}").status_code, 403)
        self.assertEqual(self.login("dent").get(f"/governance/complaints/{kid}").status_code, 403)
        mgr = self.login("mgr")
        page = self.page(mgr, "/governance")
        self.assertIn("Waited an hour", self.page(mgr, f"/governance/complaints/{kid}"))
        self.assertIn("Slip, trip or fall", page)
        page = self.post(mgr, f"/governance/complaints/{kid}", {"what": "ack", "on": datetime.date.today().isoformat(),
                                                                "response_due": "2099-01-01"}, follow_redirects=True).get_data(as_text=True)
        self.assertIn("Acknowledged.", page)
        self.assertIn("Write down a complaint", self.page(mgr, "/patients/1"))
        self.assertIn("Report an incident", self.page(desk, "/"))
        self.assertIn("Report an incident", self.page(mgr, "/tools"))
        self.assertEqual(self.login("mgr2").get(f"/governance/complaints/{kid}").status_code, 404)
        iid = self.conn.execute("SELECT id FROM safety_incidents").fetchone()[0]
        self.assertIn("Slipped", self.page(self.login("dent"), f"/governance/incidents/{iid}"))
