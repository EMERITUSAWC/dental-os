"""v0.47: days the clinic is closed."""
import datetime

from tests.test_v04 import Base
from dental_os.services import closeddays as CD, diaryrules as R, governance as GV, scheduling as S


def next_weekday(n=14):
    d = datetime.date.today() + datetime.timedelta(days=n)
    while d.weekday() >= 5:
        d += datetime.timedelta(days=1)
    return d


class Closed(Base):
    def setUp(self):
        super().setUp()
        self.conn = self.db()

    def tearDown(self):
        self.conn.close()

    def c(self):
        return dict(self.conn.execute("SELECT * FROM clinics WHERE id = 1").fetchone())


class ServiceTests(Closed):
    def test_a_closed_day_cannot_be_booked_and_offers_no_times(self):
        day = next_weekday()
        start = datetime.datetime.combine(day, datetime.time(10, 0))
        self.assertTrue(S.free_slots(self.conn, self.c(), 1, day, 30))
        CD.add(self.conn, self.c(), 1, day.isoformat(), "Founders' Day")
        with self.assertRaises(R.RulesError):
            R.check(self.conn, self.c(), 1, None, start, 30)
        self.assertEqual(S.free_slots(self.conn, self.c(), 1, day, 30), [])
        with self.assertRaises(CD.ClosedError):
            CD.add(self.conn, self.c(), 1, day.isoformat(), "Again")
        with self.assertRaises(CD.ClosedError):
            CD.add(self.conn, self.c(), 1, day.isoformat(), "")
        cd = CD.listing(self.conn, 1)[0] if day.year == datetime.date.today().year else CD.listing(self.conn, 1, day.year)[0]
        CD.remove(self.conn, self.c(), 1, cd["id"])
        self.assertTrue(S.free_slots(self.conn, self.c(), 1, day, 30))
        self.assertIsNone(CD.name_on(self.conn, 2, day))

    def test_working_days_skip_closed_days_and_years_copy(self):
        self.assertEqual(GV.add_working_days("2026-12-23", 3, {"2026-12-25"}), "2026-12-29")
        CD.add(self.conn, self.c(), 1, "2026-03-06", "Independence Day")
        CD.add(self.conn, self.c(), 1, "2026-04-03", "Good Friday")
        ids = {r["id"] for r in CD.listing(self.conn, 1, 2026) if r["name"] == "Independence Day"}
        self.assertEqual(CD.copy_year(self.conn, self.c(), 1, 2026, ids), 1)
        self.assertEqual([r["day"] for r in CD.listing(self.conn, 1, 2027)], ["2027-03-06"])
        self.assertEqual(CD.copy_year(self.conn, self.c(), 1, 2026, ids), 0)
        kid = GV.add_complaint(self.conn, self.c(), 1, {"complainant": "Mr A", "summary": "x", "received_on": "2026-03-05"})
        self.assertEqual(GV.complaint(self.conn, 1, kid)["ack_due"], "2026-03-11")          # Friday 6 March skipped


class ScreenTests(Closed):
    def test_manager_lists_days_and_the_diary_says_why(self):
        mgr = self.login("mgr")
        day = next_weekday()
        page = self.post(mgr, "/closed-days", {"day": day.isoformat(), "name": "Staff training", "year": str(day.year)},
                         follow_redirects=True).get_data(as_text=True)
        self.assertIn("Staff training", page)
        self.assertIn("The clinic is closed this day", self.page(self.login("desk"), f"/diary?date={day.isoformat()}"))
        self.assertEqual(self.login("desk").get("/closed-days").status_code, 403)
        self.assertNotIn("Staff training", self.page(self.login("mgr2"), f"/closed-days?year={day.year}"))
