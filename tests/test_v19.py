"""v0.19: running the practice. Staff tasks, the time clock and time cards, end-of-day cash-up, exports for the accountant, the
check-in kiosk, and the day sheet, work by treatment, treatment not booked and missed visits reports."""
import csv
import io
from datetime import timedelta

from tests.test_v05 import Money
from dental_os.services import billing, cashup as CU, diaryrules as R, kiosk as K, tasks as T, timeclock as TC, treatment as TR
from dental_os.services.timeutil import now_local


class Practice(Money):
    def setUp(self):
        super().setUp()
        self.conn = self.db()
        self.conn.execute("UPDATE clinics SET payment_methods = 'cash,card,mobile_money,bank_transfer,insurance' WHERE id = 1")
        self.conn.commit()
        self.c = dict(self.conn.execute("SELECT * FROM clinics WHERE id = 1").fetchone())
        self.today = now_local(self.c["timezone"]).date()

    def tearDown(self):
        self.conn.close()

    def u(self, name):
        return dict(self.conn.execute("SELECT * FROM users WHERE username = ?", (name,)).fetchone())


class TaskTests(Practice):
    def test_add_finish_and_repeat(self):
        desk = self.u("desk")
        tid = T.add(self.conn, self.c, 1, "Order gloves", desk["id"], self.today.isoformat(), "high", repeat="weekly")
        self.assertEqual(T.counts(self.conn, self.c, desk["id"]), {"open": 1, "due": 1})
        nxt = T.finish(self.conn, self.c, desk["id"], tid)
        new = T.get(self.conn, 1, nxt)
        self.assertEqual((new["due_on"], new["assigned_to"], new["repeat_of"]), ((self.today + timedelta(days=7)).isoformat(), desk["id"], tid))
        with self.assertRaises(T.TaskError):
            T.finish(self.conn, self.c, 1, tid)
        self.assertIsNone(T.finish(self.conn, self.c, 1, nxt, "cancelled"))         # cancelling does not make another
        for args, words in ((("",), "Write"), (("x", None, None, "panic"), "priority"), (("x", None, None, "normal", None, None, "hourly"), "repeats"),
                            (("x", 9999), "someone"), (("x", None, None, "normal", None, None, "daily"), "first due date")):
            with self.assertRaises(T.TaskError) as ctx:
                T.add(self.conn, self.c, 1, *args)
            self.assertIn(words, str(ctx.exception))

    def test_order_and_screens(self):
        T.add(self.conn, self.c, 1, "Later", None, (self.today + timedelta(days=5)).isoformat())
        T.add(self.conn, self.c, 1, "Overdue", None, (self.today - timedelta(days=2)).isoformat())
        T.add(self.conn, self.c, 1, "Someday")
        rows = T.board(self.conn, self.c, 1, mine=True)
        self.assertEqual([r["t"]["title"] for r in rows], ["Overdue", "Later", "Someday"])
        self.assertTrue(rows[0]["late"])
        self.conn.commit()
        c = self.login("desk")
        r = self.post(c, "/tasks", {"title": "Call Ama about her crown", "patient_id": "1", "back": "/patients/1"})
        self.assertEqual(r.headers["Location"].rsplit("/", 2)[-2:], ["patients", "1"])
        self.assertIn("Call Ama about her crown", self.page(c, "/patients/1"))
        self.assertIn("Call Ama about her crown", self.page(c, "/tasks"))
        self.assertNotIn("Overdue", self.page(c, "/tasks"))                         # someone else's
        self.assertIn("Overdue", self.page(c, "/tasks?view=all"))
        tid = self.db().execute("SELECT id FROM tasks WHERE title LIKE 'Call Ama%'").fetchone()[0]
        self.post(c, f"/tasks/{tid}/done")
        self.assertIn("Call Ama about her crown", self.page(c, "/tasks?view=done"))


class ClockTests(Practice):
    def test_in_out_and_week(self):
        desk = self.u("desk")
        TC.clock_in(self.conn, self.c, desk)
        with self.assertRaises(TC.ClockError):
            TC.clock_in(self.conn, self.c, desk)
        e = TC.open_entry(self.conn, 1, desk["id"])
        start = (now_local(self.c["timezone"]) - timedelta(hours=8, minutes=30)).strftime("%Y-%m-%d %H:%M")
        self.conn.execute("UPDATE time_entries SET clock_in = ? WHERE id = ?", (start, e["id"]))
        with self.assertRaises(TC.ClockError):
            TC.clock_out(self.conn, self.c, desk, 9999)
        TC.clock_out(self.conn, self.c, desk, 30)
        with self.assertRaises(TC.ClockError):
            TC.clock_out(self.conn, self.c, desk)
        monday = R.monday_of(self.today)
        people = TC.week(self.conn, self.c, monday)
        self.assertEqual(people[0]["total"], 8 * 60)
        self.assertEqual(TC.hm(485), "8:05")

    def test_manager_corrects_and_adds_with_a_reason(self):
        desk = self.u("desk")
        monday = R.monday_of(self.today)
        eid = TC.add_missed(self.conn, self.c, 1, desk["id"], f"{monday} 08:00", f"{monday} 16:00", 60, "Forgot to clock")
        with self.assertRaises(TC.ClockError):
            TC.add_missed(self.conn, self.c, 1, desk["id"], f"{monday} 08:00", "", 0, "No end")
        for args, words in (((f"{monday} 08:00", f"{monday} 07:00", 0, "x"), "after"), (("bad", "", 0, "x"), "like"),
                            ((f"{monday} 08:00", f"{monday} 09:00", 120, "x"), "break"), ((f"{monday} 08:00", f"{monday} 09:00", 0, ""), "why"),
                            ((f"{monday} 00:00", f"{monday} 23:00", 0, "x"), "longer")):
            with self.assertRaises(TC.ClockError) as ctx:
                TC.correct(self.conn, self.c, 1, eid, *args)
            self.assertIn(words, str(ctx.exception).lower())
        TC.correct(self.conn, self.c, 1, eid, f"{monday} 08:00", f"{monday} 17:00", 60, "Left later")
        row = self.conn.execute("SELECT * FROM time_entries WHERE id = ?", (eid,)).fetchone()
        self.assertEqual((row["clock_out"][-5:], row["edit_reason"]), ("17:00", "Left later"))
        self.conn.commit()
        c = self.login("mgr")
        page = self.page(c, f"/timecards?date={monday}")
        self.assertIn("8:00", page)
        self.assertIn("corrected", page)
        rows = list(csv.reader(io.StringIO(c.get(f"/timecards.csv?date={monday}").get_data(as_text=True))))
        self.assertEqual(rows[1][4], "480")
        self.assertEqual(self.login("desk").get("/timecards").status_code, 403)

    def test_header_button(self):
        c = self.login("asst")
        self.assertIn("Clock in", self.page(c, "/"))
        r = self.post(c, "/clock", {"action": "in"})
        self.assertEqual(r.status_code, 302)
        self.assertIn("Clock out", self.page(c, "/"))


class CashUpTests(Practice):
    def pay(self, amount, method, pid=1):
        billing.record_payment(self.conn, self.c, 1, pid, amount, method, "REF1" if method != "cash" else None)

    def test_close_with_and_without_a_difference(self):
        self.pay(10000, "cash")
        self.pay(5000, "cash")
        self.pay(20000, "card")
        methods = ["cash", "card", "mobile_money", "bank_transfer"]
        exp = CU.expected(self.conn, 1, self.today.isoformat())
        self.assertEqual((exp["cash"]["total"], exp["cash"]["n"]), (15000, 2))
        with self.assertRaises(CU.CashUpError) as ctx:
            CU.close(self.conn, self.c, 1, self.today.isoformat(), {"cash": 15000}, None, methods)
        self.assertIn("each way of paying", str(ctx.exception))
        counted = {"cash": 14000, "card": 20000, "mobile_money": 0, "bank_transfer": 0}
        with self.assertRaises(CU.CashUpError) as ctx:
            CU.close(self.conn, self.c, 1, self.today.isoformat(), counted, None, methods)
        self.assertIn("does not match", str(ctx.exception))
        CU.close(self.conn, self.c, 1, self.today.isoformat(), counted, "Gave change from the till", methods)
        lines = CU.lines_of(CU.current(self.conn, 1, self.today.isoformat()))
        self.assertEqual(lines["cash"]["difference"], -1000)
        self.assertNotIn("insurance", lines)
        with self.assertRaises(CU.CashUpError):
            CU.close(self.conn, self.c, 1, self.today.isoformat(), counted, "again", methods)
        with self.assertRaises(CU.CashUpError):
            CU.close(self.conn, self.c, 1, (self.today + timedelta(days=1)).isoformat(), counted, "x", methods)
        CU.reopen(self.conn, self.c, 1, self.today.isoformat(), "Found the missing note")
        self.assertIsNone(CU.current(self.conn, 1, self.today.isoformat()))
        CU.close(self.conn, self.c, 1, self.today.isoformat(), dict(counted, cash=15000), None, methods)
        self.assertEqual(len(CU.history(self.conn, 1, self.today.isoformat())), 2)

    def test_screen_and_late_payment(self):
        self.pay(10000, "cash")
        self.conn.commit()
        c = self.login("desk")
        self.assertIn("Count and close", self.page(c, "/cashup"))
        form = {f"count_{m}": "0" for m in ("card", "mobile_money", "bank_transfer")}
        form["count_cash"] = "100"
        r = self.post(c, "/cashup", form)
        self.assertEqual(r.status_code, 302)
        self.conn.execute("UPDATE day_closes SET closed_at = datetime('now', '-1 hour')")
        self.pay(2000, "cash")
        self.conn.commit()
        page = self.page(c, "/cashup")
        self.assertIn("after it was closed", page)
        r = self.post(c, "/cashup", {"action": "reopen", "reason": "x"})
        self.assertEqual(r.status_code, 403)                                         # only a manager reopens
        self.assertEqual(self.login("asst").get("/cashup").status_code, 403)


class ExportTests(Practice):
    def test_journal_and_invoices(self):
        billing.record_payment(self.conn, self.c, 1, 1, 15000, "mobile_money", "MM-77")
        pid = billing.record_payment(self.conn, self.c, 1, 1, 5000, "cash")
        billing.void_payment(self.conn, 1, 1, pid, "Wrong patient")
        pr = TR.add_planned(self.conn, self.c, 1, 1, self.cid("FILL"))
        TR.mark_done(self.conn, self.c, 1, pr)
        from dental_os.services import invoices as INV
        INV.create(self.conn, self.c, 1, 1, [pr])
        self.conn.execute("UPDATE clinics SET export_account = '200', export_tax_type = 'Exempt' WHERE id = 1")
        self.conn.commit()
        c = self.login("mgr")
        j = list(csv.reader(io.StringIO(c.get("/reports/export/journal.csv").get_data(as_text=True))))
        self.assertEqual(j[0][:3], ["Date", "Type", "Number"])
        types = [r[1] for r in j[1:]]
        self.assertEqual(types.count("Payment"), 2)
        self.assertIn("Payment voided", types)
        self.assertIn("-50.00", [r[5] for r in j[1:]])
        self.assertRegex(j[1][0], r"^\d\d/\d\d/\d{4}$")
        inv = list(csv.reader(io.StringIO(c.get("/reports/export/invoices.csv").get_data(as_text=True))))
        self.assertEqual(inv[0][0], "ContactName")
        self.assertEqual((inv[1][0], inv[1][8], inv[1][9], inv[1][10], inv[1][11]), ("Ama Mensah", "300.00", "200", "Exempt", "GHS"))
        self.assertEqual(c.get("/reports/export/other.csv").status_code, 404)
        self.assertEqual(self.login("desk").get("/reports/export/journal.csv").status_code, 403)

    def test_cells_cannot_run_as_formulas(self):
        self.conn.execute("UPDATE patients SET first_name = '=HYPERLINK(1)' WHERE id = 1")
        billing.record_payment(self.conn, self.c, 1, 1, 15000, "cash")
        self.conn.commit()
        body = self.login("mgr").get("/reports/export/journal.csv").get_data(as_text=True)
        self.assertIn("'=HYPERLINK", body)


class KioskTests(Practice):
    def tok(self, c):
        import re
        page = c.get("/kiosk", follow_redirects=True).get_data(as_text=True)
        return re.search(r'name="_csrf" value="([^"]+)"', page).group(1)

    def visit(self, pid=1, minutes_ahead=30):
        start = (now_local(self.c["timezone"]) + timedelta(minutes=minutes_ahead)).strftime("%Y-%m-%d %H:%M")
        self.conn.execute("""INSERT INTO appointments(clinic_id, patient_id, provider_id, chair_id, start_at, duration_min, status)
                             VALUES (1, ?, 1, 1, ?, 30, 'scheduled')""", (pid, start))
        self.conn.commit()
        return self.conn.execute("SELECT MAX(id) FROM appointments").fetchone()[0]

    def test_find_needs_both_and_shows_only_the_first_name(self):
        aid = self.visit()
        self.assertEqual([r["id"] for r in K.find(self.conn, self.c, "2223", "1990-05-01")], [aid])
        self.assertEqual(K.find(self.conn, self.c, "2223", "1990-05-02"), [])
        self.assertEqual(K.find(self.conn, self.c, "9999", "1990-05-01"), [])
        for args in (("22", "1990-05-01"), ("2223", "May 1")):
            with self.assertRaises(K.KioskError):
                K.find(self.conn, self.c, *args)

    def test_the_kiosk_is_locked_until_staff_leave_it(self):
        aid = self.visit()
        c = self.login("desk")
        r = self.post(c, "/kiosk/start")
        self.assertTrue(r.headers["Location"].endswith("/kiosk"))
        for url in ("/", "/patients", "/patients/1", "/diary", "/settings"):
            r = c.get(url)
            self.assertEqual(r.status_code, 302, url)
            self.assertTrue(r.headers["Location"].endswith("/kiosk"), url)
        page = self.page(c, "/kiosk")
        self.assertNotIn('href="/patients"', page)                                  # no menu
        r = self.post(c, "/kiosk", {"last4": "2223", "dob": "1990-05-01"})
        page = r.get_data(as_text=True)
        self.assertIn("Thank you, Ama", page)
        self.assertNotIn("Mensah", page)
        self.assertTrue(self.db().execute("SELECT arrived_at FROM appointments WHERE id = ?", (aid,)).fetchone()[0])
        r = self.post(c, "/kiosk", {"last4": "2223", "dob": "1990-05-01"})
        self.assertIn("could not find", r.get_data(as_text=True))                   # already checked in
        r = self.post(c, "/kiosk/exit", {"username": "asst", "password": "correct-horse-battery"}, follow_redirects=True)
        self.assertIn("did not work", r.get_data(as_text=True))                     # an assistant cannot unlock it
        r = self.post(c, "/kiosk/exit", {"username": "desk", "password": "correct-horse-battery"})
        self.assertTrue(r.headers["Location"].endswith("/board"))
        self.assertEqual(c.get("/patients").status_code, 200)

    def test_tries_are_limited(self):
        c = self.login("desk")
        self.post(c, "/kiosk/start")
        for _ in range(K.TRIES_PER_10_MIN):
            self.post(c, "/kiosk", {"last4": "0000", "dob": "2000-01-01"})
        r = self.post(c, "/kiosk", {"last4": "0000", "dob": "2000-01-01"})
        self.assertIn("Too many tries", r.get_data(as_text=True))

    def test_only_desk_and_managers_start_it(self):
        self.assertEqual(self.post(self.login("dent"), "/kiosk/start").status_code, 403)


class ReportTests(Practice):
    def test_day_sheet_production_unscheduled_and_missed(self):
        pr = TR.add_planned(self.conn, self.c, 1, 1, self.cid("CROWN"), provider_id=1)
        TR.mark_done(self.conn, self.c, 1, pr, provider_id=1)
        TR.add_planned(self.conn, self.c, 1, 1, self.cid("FILL"))
        billing.record_payment(self.conn, self.c, 1, 1, 15000, "cash")
        past = (now_local(self.c["timezone"]) - timedelta(days=2)).strftime("%Y-%m-%d 10:00")
        self.conn.execute("""INSERT INTO appointments(clinic_id, patient_id, provider_id, chair_id, start_at, status, break_reason)
                             VALUES (1, 1, 1, 1, ?, 'broken', 'missed')""", (past,))
        self.conn.commit()
        c = self.login("mgr")
        page = self.page(c, "/reports/day")
        self.assertIn("1,500.00", page)
        self.assertIn("150.00", page)
        page = self.page(c, "/reports/production")
        self.assertIn("Crown", page)
        rows = list(csv.reader(io.StringIO(c.get("/reports/production?csv=1").get_data(as_text=True))))
        self.assertEqual(rows[1][:2], ["CROWN", "Crown"])
        self.assertIn("Filling", self.page(c, "/reports/unscheduled"))
        self.assertIn("Did not come", self.page(c, "/reports/missed"))
        self.assertEqual(self.login("asst").get("/reports/unscheduled").status_code, 403)
        self.assertEqual(self.login("dent").get("/reports/production").status_code, 403)

    def test_french(self):
        self.conn.execute("UPDATE clinics SET language = 'fr' WHERE id = 1")
        self.conn.commit()
        c = self.login("mgr")
        self.assertIn("Clôture de caisse", self.page(c, "/cashup"))
        self.assertIn("Nouvelle tâche", self.page(c, "/tasks"))
