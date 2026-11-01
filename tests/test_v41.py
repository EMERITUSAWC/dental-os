"""v0.41: patients not seen for a while."""
import datetime

from tests.test_v04 import Base
from dental_os.services import outbox, reactivation as RE

TODAY = datetime.date(2026, 10, 29)


class React(Base):
    def setUp(self):
        super().setUp()
        self.conn = c = self.db()
        chair = c.execute("SELECT id FROM chairs WHERE clinic_id = 1 LIMIT 1").fetchone()
        chair = chair[0] if chair else c.execute("INSERT INTO chairs(clinic_id, name) VALUES (1, 'Chair 1')").lastrowid
        self.extra = c.execute("INSERT INTO patients(clinic_id, first_name, last_name, phone) VALUES (1,'Esi','Quaye','+233201112230')").lastrowid
        for pid, day in ((1, "2025-08-10 09:00"), (2, "2026-09-01 09:00"), (self.extra, "2024-05-01 09:00")):
            c.execute("INSERT INTO appointments(clinic_id, patient_id, provider_id, chair_id, start_at, status) VALUES (1,?,1,?,?,'complete')", (pid, chair, day))
        c.commit()

    def tearDown(self):
        self.conn.close()

    def c(self):
        return dict(self.conn.execute("SELECT * FROM clinics WHERE id = 1").fetchone())


class ServiceTests(React):
    def test_who_is_on_the_list(self):
        self.assertEqual([r["id"] for r in RE.lapsed(self.conn, self.c(), 18, TODAY)], [self.extra])
        self.assertEqual([r["id"] for r in RE.lapsed(self.conn, self.c(), 12, TODAY)], [1, self.extra])
        self.conn.execute("INSERT INTO appointments(clinic_id, patient_id, status) VALUES (1, 1, 'asap')")
        self.assertEqual([r["id"] for r in RE.lapsed(self.conn, self.c(), 12, TODAY)], [self.extra])     # on the waiting list
        RE.hold(self.conn, self.c(), 1, self.extra, "moved", 6, today=TODAY)
        self.assertEqual(RE.lapsed(self.conn, self.c(), 12, TODAY), [])
        self.assertEqual([r["id"] for r in RE.lapsed(self.conn, self.c(), 12, datetime.date(2027, 5, 1))], [self.extra])
        with self.assertRaises(RE.ReactError):
            RE.hold(self.conn, self.c(), 1, self.extra, "bogus", 6)

    def test_messages_only_with_consent_once_a_month(self):
        with self.assertRaises(RE.ReactError):
            RE.queue_message(self.conn, self.c(), 1, self.extra, TODAY)               # not agreed to messages
        RE.queue_message(self.conn, self.c(), 1, 1, TODAY)
        with self.assertRaises(RE.ReactError):
            RE.queue_message(self.conn, self.c(), 1, 1, TODAY)
        o = self.conn.execute("SELECT * FROM outbox WHERE kind = 'reactivation'").fetchone()
        text = outbox.text_for(self.conn, self.c(), o)[1]
        self.assertIn("since we last saw you", text)
        self.assertEqual(RE.queue_all(self.conn, self.c(), 1, 12, TODAY), 0)
        self.assertEqual(RE.lapsed(self.conn, self.c(), 12, TODAY)[0]["queued"], 1)

    def test_french_starter(self):
        self.conn.execute("UPDATE patients SET language = 'fr' WHERE id = 1")
        RE.queue_message(self.conn, self.c(), 1, 1, TODAY)
        o = self.conn.execute("SELECT * FROM outbox WHERE kind = 'reactivation'").fetchone()
        self.assertIn("Cela fait un moment", outbox.text_for(self.conn, self.c(), o)[1])


class ScreenTests(React):
    def test_desk_works_the_list(self):
        desk = self.login("desk")
        page = self.page(desk, "/reactivation?months=12")
        self.assertIn("Esi", page)
        self.assertIn("No messages agreed", page)
        page = self.post(desk, "/reactivation", {"what": "all", "months": "12"}, follow_redirects=True).get_data(as_text=True)
        self.assertIn("Messages added to the send list.", page)
        self.assertIn("Message waiting", page)
        page = self.post(desk, "/reactivation", {"what": "hold", "pid": str(self.extra), "reason": "elsewhere", "hold_months": "12", "months": "12"},
                         follow_redirects=True).get_data(as_text=True)
        self.assertIn("Put aside.", page)
        self.assertNotIn("Esi", page)
        self.assertIn("Not seen for a while", self.page(desk, "/recalls"))
        self.assertNotIn("Ama", self.page(self.login("mgr2"), "/reactivation?months=12"))
