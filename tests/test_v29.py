"""v0.29: staff messages and the equipment register."""
import datetime

from tests.test_v04 import Base
from dental_os.services import team as T
from dental_os.services.timeutil import now_local


class Team(Base):
    def setUp(self):
        super().setUp()
        self.conn = self.db()
        self.c = dict(self.conn.execute("SELECT * FROM clinics WHERE id = 1").fetchone())
        self.u = {n: self.conn.execute("SELECT * FROM users WHERE username = ?", (n,)).fetchone() for n in ("mgr", "dent", "desk", "mgr2")}

    def tearDown(self):
        self.conn.close()


class MessageTests(Team):
    def test_direct_and_everyone_messages_and_unread_counts(self):
        T.send(self.conn, self.c, self.u["mgr"], self.u["dent"]["id"], "Please sign the notes from Monday.")
        T.send(self.conn, self.c, self.u["desk"], None, "Chair 2 suction is weak.", patient_id=None)
        T.send(self.conn, self.c, self.u["dent"], None, "Ama needs a call about her crown.", patient_id=1)
        self.assertEqual(T.unread(self.conn, self.u["dent"]), 2)              # one to them, one to everyone
        self.assertEqual(T.unread(self.conn, self.u["desk"]), 1)              # only the dentist's message to everyone
        self.assertEqual(T.unread(self.conn, self.u["mgr2"]), 0)              # another clinic hears nothing
        T.mark_read(self.conn, self.u["dent"], [r["id"] for r in T.inbox(self.conn, self.u["dent"])])
        self.assertEqual(T.unread(self.conn, self.u["dent"]), 0)
        self.assertEqual(len(T.about_patient(self.conn, self.u["desk"], 1)), 1)
        for bad in ((self.u["mgr"]["id"], "x"), (self.u["mgr2"]["id"], "x"), (None, "")):
            with self.assertRaises(T.TeamError):
                T.send(self.conn, self.c, self.u["mgr"], *bad)
        with self.assertRaises(T.TeamError):
            T.send(self.conn, self.c, self.u["mgr"], None, "x", patient_id=3)

    def test_screens(self):
        dent = self.login("dent")
        r = self.post(dent, "/team", {"to_user": "", "body": "Lab work for Ama is back.", "patient_id": "1",
                                      "back": "/patients/1"}, follow_redirects=True)
        self.assertIn("Lab work for Ama is back.", r.get_data(as_text=True))       # shown on the patient page
        desk = self.login("desk")
        self.assertIn('<span class="badge">1</span>', self.page(desk, "/patients"))
        self.assertIn("Lab work for Ama", self.page(desk, "/team"))
        self.assertNotIn('<span class="badge">1</span>', self.page(desk, "/patients").split("Messages")[0])   # read now
        self.assertNotIn("Lab work", self.page(self.login("mgr2"), "/team"))


class EquipmentTests(Team):
    def today(self):
        return now_local(self.c["timezone"]).date()

    def test_service_due_faults_and_history(self):
        old = (self.today() - datetime.timedelta(days=400)).isoformat()
        iid = T.save_item(self.conn, self.c, 1, {"name": "Compressor", "kind": "compressor", "service_months": "12", "bought_on": old})
        x = T.register(self.conn, self.c)[0]
        self.assertTrue(x["overdue"])
        T.log_event(self.conn, self.c, 1, iid, "service", "", "Engineer from the supplier", "450", "Oil and filters changed")
        x = T.register(self.conn, self.c)[0]
        self.assertFalse(x["overdue"])
        self.assertEqual(x["next"], T.add_months(self.today(), 12))
        T.log_event(self.conn, self.c, 1, iid, "fault", "", "", "", "Making a loud noise")
        self.assertEqual(len(T.due_summary(self.conn, self.c)["faults"]), 1)
        T.log_event(self.conn, self.c, 1, iid, "repair", "", "Engineer", "", "Belt replaced")
        self.assertEqual(len(T.due_summary(self.conn, self.c)["faults"]), 0)
        self.assertEqual([e["kind"] for e in T.events(self.conn, 1, iid)], ["repair", "fault", "service"])
        for bad in ({"name": ""}, {"name": "X", "kind": "rocket"}, {"name": "X", "service_months": "500"}, {"name": "X", "bought_on": "soon"}):
            with self.assertRaises(T.TeamError):
                T.save_item(self.conn, self.c, 1, {"kind": "other"} | bad)
        with self.assertRaises(T.TeamError):
            T.log_event(self.conn, self.c, 1, iid, "service", "", "", "", "")
        with self.assertRaises(T.TeamError):
            T.log_event(self.conn, self.c, 1, iid, "service", (self.today() + datetime.timedelta(days=3)).isoformat(), "", "", "x")

    def test_screens(self):
        mgr = self.login("mgr")
        r = self.post(mgr, "/equipment", {"name": "Chair 1", "kind": "chair", "service_months": "6"}, follow_redirects=True)
        self.assertIn("Chair 1", r.get_data(as_text=True))
        iid = self.conn.execute("SELECT id FROM equipment").fetchone()[0]
        self.assertIn("no service date", self.page(mgr, "/equipment"))
        self.assertIn("service due", self.page(mgr, "/"))
        desk = self.login("desk")
        self.post(desk, f"/equipment/{iid}", {"kind": "fault", "notes": "Foot pedal sticks"})
        self.assertIn("Foot pedal sticks", self.page(desk, f"/equipment/{iid}"))
        self.assertEqual(self.post(desk, "/equipment", {"name": "X", "kind": "other"}).status_code, 403)
        self.assertEqual(self.login("mgr2").get(f"/equipment/{iid}").status_code, 404)
