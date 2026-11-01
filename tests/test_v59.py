"""v0.59: ready to go live. The checklist, problem reports, error references and the side-by-side check with the old system."""
import datetime
import re

from tests.test_v04 import Base
from dental_os import create_app
from dental_os.services import offsite as OS, pilot as P, platform as PF
from dental_os.services.timeutil import now_local

GOOD = "plantain kente harmattan lagoon"


class Pilot(Base):
    def setUp(self):
        super().setUp()
        self.conn = self.db()

    def tearDown(self):
        self.conn.close()

    def clinic(self, cid=1):
        return self.conn.execute("SELECT * FROM clinics WHERE id = ?", (cid,)).fetchone()

    def today(self):
        return now_local(self.clinic()["timezone"]).date()


class ChecklistTests(Pilot):
    def test_a_new_clinic_is_not_ready_and_says_why(self):
        items = P.checklist(self.conn, self.clinic(), self.app.config)
        self.assertFalse(P.ready(items))
        undone = {k for k, _l, done, must, _f in items if must and not done}
        self.assertTrue({"two_step", "copies", "drill", "price_list"} <= undone)
        self.assertNotIn("dentists", undone)                                     # Base adds a dentist
        mgr = self.login("mgr")
        page = self.page(mgr, "/settings/go-live")
        self.assertIn("Some things that must be done are not done yet", page)
        self.assertIn('href="/settings/copies"', page)
        self.assertEqual(self.login("desk").get("/settings/go-live").status_code, 403)
        self.assertIn("/settings/go-live", self.page(mgr, "/settings"))

    def test_everything_done_means_ready(self):
        mgr = self.login("mgr")                                                   # before two-step login is switched on below
        self.conn.execute("UPDATE procedure_codes SET fee_p = 10000 WHERE clinic_id = 1")
        from dental_os.services import treatment
        treatment.ensure_catalogue(self.conn, 1)
        self.conn.execute("UPDATE procedure_codes SET fee_p = 10000 WHERE clinic_id = 1")
        self.conn.execute("INSERT INTO chairs(clinic_id, name) VALUES (1, 'Chair 1')")
        self.conn.execute("UPDATE users SET totp_enabled = 1 WHERE clinic_id = 1 AND role = 'manager'")
        self.conn.execute("INSERT INTO offsite_log(ok, detail, size_bytes) VALUES (1, 'usb', 100)")
        self.conn.commit()
        OS.record_drill(self.conn, "c.dosc", {"patients": 3, "files": 0, "made_at": "2026-10-31 01:00:00"})
        for item in P.BY_HAND:
            P.tick(self.conn, self.clinic(), 1, item)
        self.conn.commit()
        items = P.checklist(self.conn, self.clinic(), self.app.config)
        self.assertTrue(P.ready(items), [k for k, _l, d, m, _f in items if m and not d])
        self.assertIn("Everything that must be done is done", self.page(mgr, "/settings/go-live"))
        P.tick(self.conn, self.clinic(), 1, "training", on=False)
        self.assertFalse(P.ready(P.checklist(self.conn, self.clinic(), self.app.config)))
        with self.assertRaises(P.PilotError):
            P.tick(self.conn, self.clinic(), 1, "made_up")

    def test_ticking_from_the_page_names_who_did_it(self):
        mgr = self.login("mgr")
        self.post(mgr, "/settings/go-live", {"item": "prices", "on": "1"})
        page = self.page(mgr, "/settings/go-live")
        self.assertIn("Mgr,", page)
        self.assertEqual(self.conn.execute("SELECT done_by FROM golive_ticks WHERE clinic_id = 1 AND item = 'prices'").fetchone()[0], 1)
        self.post(mgr, "/settings/go-live", {"item": "prices", "on": "0"})
        self.assertIsNone(self.conn.execute("SELECT 1 FROM golive_ticks WHERE item = 'prices'").fetchone())

    def test_doctor_lists_what_must_be_fixed(self):
        out = self.app.test_cli_runner().invoke(args=["doctor"])
        self.assertEqual(out.exit_code, 1)
        self.assertIn("Clinic 1: Clinic One (not ready)", out.output)
        self.assertIn("[MUST] Every manager uses two-step login", out.output)
        self.assertIn("[ok] At least one dentist is set up", out.output)


class ProblemTests(Pilot):
    def test_staff_report_from_the_page_without_the_search_words(self):
        desk = self.login("desk")
        page = self.page(desk, "/patients")
        self.assertIn('href="/problems?page=/patients"', page)                       # the link at the bottom of every page
        r = self.post(desk, "/problems", {"page": "/patients?q=Mensah", "what": "The search took ages this morning.", "urgency": "blocking"})
        self.assertEqual(r.headers["Location"], "/patients?q=Mensah")            # back where they were
        row = self.conn.execute("SELECT * FROM problem_reports").fetchone()
        self.assertEqual((row["page"], row["urgency"], row["shared"], row["status"]), ("/patients", "blocking", 0, "open"))
        r = self.post(desk, "/problems", {"page": "//evil.example/x", "what": "Another thing went wrong."})
        self.assertNotIn("evil", r.headers["Location"])
        self.assertIsNone(self.conn.execute("SELECT page FROM problem_reports ORDER BY id DESC").fetchone()[0])
        self.assertIn("Say what went wrong", self.post(desk, "/problems", {"what": "x"}).get_data(as_text=True))

    def test_staff_see_their_own_and_managers_see_all(self):
        P.report_problem(self.conn, self.clinic(), 4, "/diary", "Desk report about the diary")
        P.report_problem(self.conn, self.clinic(), 2, "/diary", "Dentist report about the chart")
        self.conn.commit()
        desk = self.page(self.login("desk"), "/problems")
        self.assertIn("Desk report", desk)
        self.assertNotIn("Dentist report", desk)
        mgr = self.page(self.login("mgr"), "/problems")
        self.assertIn("Desk report", mgr)
        self.assertIn("Dentist report", mgr)
        self.assertNotIn("Desk report", self.page(self.login("mgr2"), "/problems"))   # another clinic sees nothing

    def test_only_managers_close_reports_and_only_their_own_clinic(self):
        pid = P.report_problem(self.conn, self.clinic(), 4, "/diary", "The diary shows the wrong week")
        self.conn.commit()
        self.assertEqual(self.post(self.login("desk"), f"/problems/{pid}", {"status": "fixed"}).status_code, 403)
        self.assertEqual(self.post(self.login("mgr2"), f"/problems/{pid}", {"status": "fixed"}).status_code, 404)
        self.post(self.login("mgr"), f"/problems/{pid}", {"status": "fixed", "note": "Week starts on Monday now"})
        row = self.conn.execute("SELECT * FROM problem_reports WHERE id = ?", (pid,)).fetchone()
        self.assertEqual((row["status"], row["note"], row["closed_by"]), ("fixed", "Week starts on Monday now", 1))
        self.assertIsNotNone(row["closed_at"])
        self.assertEqual(P.open_counts(self.conn, 1), {"open": 0, "blocking": 0})

    def test_the_operator_sees_only_shared_reports(self):
        P.report_problem(self.conn, self.clinic(), 4, "/diary", "Shared report text here", share=True)
        P.report_problem(self.conn, self.clinic(), 4, "/diary", "Private report text here")
        PF.add_operator(self.conn, "ops", "Ops Person", GOOD)
        self.conn.commit()
        op = self.app.test_client()
        tok = re.search(r'name="_csrf" value="([^"]+)"', op.get("/operator/login").get_data(as_text=True)).group(1)
        op.post("/operator/login", data={"_csrf": tok, "username": "ops", "password": GOOD})
        page = op.get("/operator/health").get_data(as_text=True)
        self.assertIn("Shared report text here", page)
        self.assertNotIn("Private report text here", page)
        self.assertEqual(self.app.test_client().get("/operator/health").status_code, 302)


class ErrorTests(Pilot):
    def _app(self):
        app = create_app({"TESTING": False, "PROPAGATE_EXCEPTIONS": False, "DATABASE": self.app.config["DATABASE"], "SECRET_KEY": "test",
                          "FILES_PATH": self.app.config["FILES_PATH"]})

        @app.get("/patients/<int:pid>/explode")
        def explode(pid):
            raise RuntimeError("boom with a secret 0244123456")
        app.logger.disabled = True
        return app

    def test_a_broken_page_shows_a_reference_and_keeps_only_the_screen(self):
        app = self._app()
        c = app.test_client()
        tok = re.search(r'name="_csrf" value="([^"]+)"', c.get("/login").get_data(as_text=True)).group(1)
        from tests.test_v04 import PW
        c.post("/login", data={"_csrf": tok, "username": "desk", "password": PW})
        r = c.get("/patients/1/explode?q=Mensah")
        self.assertEqual(r.status_code, 500)
        html = r.get_data(as_text=True)
        ref = re.search(r"<strong>(E-[A-Z2-9]{6})</strong>", html).group(1)
        row = self.conn.execute("SELECT * FROM error_log WHERE ref = ?", (ref,)).fetchone()
        self.assertEqual((row["clinic_id"], row["user_id"], row["method"], row["endpoint"], row["kind"]), (1, 4, "GET", "explode", "RuntimeError"))
        self.assertIn("test_v59.py", row["at_line"])
        everything = " ".join(str(v) for v in tuple(row))
        self.assertNotIn("Mensah", everything)                                     # no search words
        self.assertNotIn("0244123456", everything)                                 # no error message text
        self.assertIn("Report a problem", html)
        mgr = self.login("mgr")
        self.assertIn(ref, self.page(mgr, "/problems"))
        self.assertNotIn(ref, self.page(self.login("desk"), "/problems"))

    def test_the_api_gets_the_reference_as_json(self):
        app = self._app()

        @app.get("/api/v1/explode")
        def api_explode():
            raise ValueError("x")
        r = app.test_client().get("/api/v1/explode")
        self.assertEqual(r.status_code, 500)
        self.assertTrue(r.get_json()["error"]["ref"].startswith("E-"))


class SideBySideTests(Pilot):
    def _day_with_activity(self, day):
        chair = self.conn.execute("SELECT id FROM chairs WHERE clinic_id = 1").fetchone()
        chair = chair[0] if chair else self.conn.execute("INSERT INTO chairs(clinic_id, name) VALUES (1, 'Chair 1')").lastrowid
        self.conn.execute("""INSERT INTO payments(clinic_id, patient_id, amount_p, method, receipt_no, received_on)
                             VALUES (1, 1, 25000, 'cash', ?, ?)""", (500 + day.toordinal() % 1000, day.isoformat()))
        self.conn.execute("""INSERT INTO appointments(clinic_id, patient_id, provider_id, chair_id, start_at, duration_min, status)
                             VALUES (1, 1, 1, ?, ?, 30, 'complete')""", (chair, f"{day} 10:00"))
        self.conn.execute("""INSERT INTO appointments(clinic_id, patient_id, provider_id, chair_id, start_at, duration_min, status)
                             VALUES (1, 1, 1, ?, ?, 30, 'broken')""", (chair, f"{day} 11:00"))
        self.conn.commit()

    def test_our_figures_for_a_day(self):
        day = self.today() - datetime.timedelta(days=2)
        self._day_with_activity(day)
        got = P.ours(self.conn, self.clinic(), day)
        self.assertEqual((got["takings"], got["appointments"]), (25000, 1))       # the missed one is left out
        self.assertEqual(self.clinic()["timezone"], "Africa/Accra")
        self.assertEqual(P.ours(self.conn, self.clinic(), self.today())["new_patients"], 2)      # Base adds two patients to clinic one today

    def test_matching_and_differing_days(self):
        days = [self.today() - datetime.timedelta(days=n) for n in (3, 2, 1)]
        for d in days:
            self._day_with_activity(d)
        mgr = self.login("mgr")
        for d in days[:2]:
            r = self.post(mgr, "/settings/side-by-side", {"day": d.isoformat(), "takings": "250.00", "appointments": "1", "new_patients": "0"})
            self.assertEqual(r.status_code, 302, r.get_data(as_text=True)[:300])
        self.post(mgr, "/settings/side-by-side", {"day": days[2].isoformat(), "takings": "240.00", "note": "Cash slip missing"})
        side = P.side_by_side(self.conn, self.clinic())
        self.assertEqual([d["match"] for d in side["days"]], [False, True, True])     # newest first
        self.assertEqual(side["streak"], 0)
        self.assertFalse(side["enough"])
        page = self.page(mgr, "/settings/side-by-side")
        self.assertIn("Differs", page)
        self.assertIn("Cash slip missing", page)
        self.post(mgr, "/settings/side-by-side", {"day": days[2].isoformat(), "takings": "250.00"})   # found and fixed
        self.assertEqual(P.side_by_side(self.conn, self.clinic())["streak"], 3)

    def test_bad_entries_are_refused(self):
        c = self.clinic()
        with self.assertRaises(P.PilotError):
            P.save_old_figures(self.conn, c, 1, self.today() + datetime.timedelta(days=1), {"takings": "10"})
        with self.assertRaises(P.PilotError):
            P.save_old_figures(self.conn, c, 1, self.today(), {"appointments": "two"})
        with self.assertRaises(P.PilotError):
            P.save_old_figures(self.conn, c, 1, self.today(), {"takings": "lots"})
        with self.assertRaises(P.PilotError):
            P.save_old_figures(self.conn, c, 1, self.today(), {})
        self.assertEqual(self.login("desk").get("/settings/side-by-side").status_code, 403)

    def test_ten_matching_days_are_enough(self):
        for n in range(1, P.SIDE_BY_SIDE_DAYS + 1):
            d = self.today() - datetime.timedelta(days=n)
            mine = P.ours(self.conn, self.clinic(), d)
            P.save_old_figures(self.conn, self.clinic(), 1, d, {"takings": f"{mine['takings'] / 100:.2f}", "appointments": str(mine["appointments"])})
        self.conn.commit()
        self.assertTrue(P.side_by_side(self.conn, self.clinic())["enough"])
        self.assertIn("enough to switch off the old system", self.page(self.login("mgr"), "/settings/go-live"))
