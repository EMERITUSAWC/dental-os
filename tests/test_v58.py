"""v0.58: accessibility across every page, and the restore drill reminder.

The page checks open every screen with made-up records in it, so a new screen that breaks the rules fails here
without anyone having to remember to write a test for it.
"""
import datetime
import re

from tests.a11y import check
from tests.contrast import report
from tests import test_v07 as V07, test_v56 as V56

SKIP = ("/api/", "/static", "/operator", "/health")


class CheckerTests(V56.CrossClinicCrawl):
    def test_the_checker_catches_the_usual_mistakes(self):
        bad = ('<html><head><title> </title></head><body><input name="q"><select name="s"></select><button></button>'
               '<a href="/x"></a><img src="a.png"><svg></svg><p id="d"></p><p id="d"></p></body></html>')
        got = " | ".join(check(bad))
        for want in ("input q has no label", "select s has no label", "button", "a /x", "a.png has no alt", "svg", "no title",
                     "no lang", "0 main headings", "id d is used 2 times"):
            self.assertIn(want, got)
        good = ('<html lang="en"><head><title>T</title></head><body><h1>H</h1><label for="q">Find</label><input id="q" name="q">'
                '<label>Day <input name="d" type="date"></label><input type="hidden" name="h"><button aria-label="Close">x</button>'
                '<a href="/y"><img src="b.png" alt="Logo"></a><svg aria-hidden="true"></svg></body></html>')
        self.assertEqual(check(good), [])

    def test_clinic_two_never_sees_clinic_one(self):
        pass                                                      # run once, in tests.test_v56


class EveryPage(V56.CrossClinicCrawl):
    def _pages(self, client, value):
        for rule in sorted(self.app.url_map.iter_rules(), key=lambda r: r.rule):
            if "GET" not in rule.methods or rule.rule.startswith(SKIP) or rule.rule.endswith(".csv") or "logout" in rule.rule or rule.rule.endswith("/exit"):
                continue
            url = V56._url(self.app, rule, value)
            if not url:
                continue
            r = client.get(url)
            if r.status_code == 200 and "text/html" in r.headers.get("Content-Type", ""):
                yield url, r.get_data(as_text=True)

    def test_every_screen_follows_the_rules(self):
        problems, seen = {}, 0
        for role in ("mgr", "desk"):
            for url, html in self._pages(self.login(role), 4):
                seen += 1
                found = check(html)
                if found:
                    problems[url] = found[:3]
        self.assertGreater(seen, 200, "The crawl did not reach the screens")
        self.assertEqual(problems, {})

    def test_every_page_has_the_skip_link(self):
        html = self.page(self.login("mgr"), "/")
        self.assertIn('class="skip" href="#main"', html)
        self.assertIn('<main id="main"', html)

    def test_clinic_two_never_sees_clinic_one(self):
        pass


class PublicPages(V07.Public):
    def test_pages_for_patients_and_strangers_follow_the_rules(self):
        token = self.open_booking()
        self.app.config["ALLOW_SIGNUP"] = True
        c = self.anon()
        for url in ("/login", "/operator/login", f"/book/{token}", "/signup"):
            r = c.get(url)
            self.assertEqual(r.status_code, 200, url)
            self.assertEqual(check(r.get_data(as_text=True)), [], url)


class ColourTests(V56.CrossClinicCrawl):
    def setUp(self):
        pass

    def tearDown(self):
        pass

    def test_text_is_readable_in_light_and_dark(self):
        from pathlib import Path
        import dental_os
        css = Path(dental_os.__file__).parent / "static" / "app.css"
        low = {k: v for k, v in report(css).items() if v < 4.5}               # WCAG AA for ordinary text
        self.assertEqual(low, {})
        text = css.read_text()
        self.assertIn(":focus-visible { outline: 3px solid", text)
        self.assertIn("prefers-reduced-motion", text)

    def test_clinic_two_never_sees_clinic_one(self):
        pass


class DrillTests(V56.Base):
    def setUp(self):
        super().setUp()
        self.conn = self.db()

    def tearDown(self):
        self.conn.close()

    def test_a_drill_is_written_down_and_shown(self):
        from dental_os.services import offsite as OS
        mgr = self.login("mgr")
        self.assertIn("No restore drill has passed yet", self.page(mgr, "/settings/copies"))
        self.assertTrue(OS.drill_status(self.conn)["due"])
        ok, _why = OS.record_drill(self.conn, "copy-1.dosc", {"patients": 3, "files": 1, "made_at": "2026-10-30 22:00:00"})
        self.assertTrue(ok)
        page = self.page(mgr, "/settings/copies")
        self.assertIn("Last good drill", page)
        self.assertIn("done this month", page)
        self.assertFalse(OS.drill_status(self.conn)["due"])
        later = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None) + datetime.timedelta(days=OS.DRILL_EVERY_DAYS + 1)
        self.assertTrue(OS.drill_status(self.conn, now=later)["due"])
        OS.record_drill(self.conn, "copy-2.dosc", error=OS.OffsiteError("That passphrase does not open this copy."))
        self.assertIn("The last drill failed", self.page(mgr, "/settings/copies"))
        self.assertIn("done this month", self.page(mgr, "/settings/copies"))      # the earlier good one still counts

    def test_a_copy_with_far_fewer_patients_does_not_pass(self):
        from dental_os.services import offsite as OS
        self.conn.executemany("INSERT INTO patients(clinic_id, first_name, last_name, phone) VALUES (1, 'Test', ?, ?)", [(f"P{i}", f"+23350900{i:04d}") for i in range(40)])
        self.conn.commit()
        ok, why = OS.record_drill(self.conn, "old.dosc", {"patients": 3, "files": 0, "made_at": "2025-01-01 00:00:00"})
        self.assertFalse(ok)
        self.assertEqual(why, OS.SHORT)
        self.assertTrue(OS.drill_status(self.conn)["due"])
        ok, _ = OS.record_drill(self.conn, "new.dosc", {"patients": 42, "files": 0, "made_at": "2026-10-31 00:00:00"})
        self.assertTrue(ok)

    @V56.unittest.skipIf(V56.ON_PG, "copies the SQLite file")
    def test_the_drill_command_records_pass_and_fail(self):
        import tempfile
        with tempfile.TemporaryDirectory() as off:
            self.app.config.update(OFFSITE_TARGET=off, OFFSITE_PASSPHRASE="correct horse battery staple")
            self.assertEqual(self.app.test_cli_runner().invoke(args=["offsite-copy"]).exit_code, 0)
            out = self.app.test_cli_runner().invoke(args=["offsite-drill"])
            self.assertIn("opens and is sound", out.output)
            self.app.config["OFFSITE_PASSPHRASE"] = "a different long passphrase"
            out = self.app.test_cli_runner().invoke(args=["offsite-drill"])
            self.assertNotEqual(out.exit_code, 0)
        rows = self.conn.execute("SELECT ok, patients FROM restore_drills ORDER BY id").fetchall()
        self.assertEqual([(r["ok"], r["patients"]) for r in rows], [(1, 3), (0, None)])
