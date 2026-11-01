"""v0.67: the security and compliance pack. The locked and sealed activity log, the access review, the evidence pack,
security.txt, the code checks, and a crawl that sends hostile text to every page."""
import csv
import io
import json
import re
import zipfile
from pathlib import Path

from werkzeug.routing.converters import IntegerConverter

from tests.test_v04 import Base
from tests.dbmeta import ON_PG
from dental_os import db as dbmod
from dental_os.services import compliance as C, seccheck

PROBE = "<x-dos-probe>"
HOSTILE = PROBE + "' OR '1'='1\" --;"


def unlock(conn):
    """What an attacker with database access would do first: take the locks off."""
    if ON_PG:
        for t in dbmod.LOCKED:
            conn.execute(f"DROP TRIGGER IF EXISTS {t}_keep ON {t}")
            conn.execute(f"DROP TRIGGER IF EXISTS {t}_keep_all ON {t}")
    else:
        for t in dbmod.LOCKED:
            for op in ("update", "delete"):
                conn.execute(f"DROP TRIGGER IF EXISTS {t}_no_{op}")
    conn.commit()


class Comp(Base):
    def setUp(self):
        super().setUp()
        self.conn = self.db()
        for n in range(5):
            self.conn.execute("INSERT INTO audit_log(clinic_id, user_id, action, detail) VALUES (1, 1, 'test.row', ?)", (f"row {n}",))
        self.conn.commit()

    def tearDown(self):
        self.conn.close()


class LockTests(Comp):
    def test_the_database_refuses_to_change_or_delete_the_log(self):
        self.assertTrue(dbmod.logs_locked(self.conn))
        for sql in ("UPDATE audit_log SET detail = 'changed' WHERE action = 'test.row'", "DELETE FROM audit_log WHERE action = 'test.row'"):
            with self.assertRaises(Exception) as ctx:
                self.conn.execute(sql)
            self.assertIn("cannot be changed", str(ctx.exception))
            self.conn.rollback()
        C.seal(self.conn, "test")
        with self.assertRaises(Exception):
            self.conn.execute("UPDATE audit_seals SET chain = 'x'")
        self.conn.rollback()
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'test.row'").fetchone()[0], 5)
        self.conn.execute("INSERT INTO audit_log(clinic_id, action) VALUES (1, 'still.fine')")              # adding rows still works
        self.conn.commit()


class SealTests(Comp):
    def test_an_untouched_log_is_intact(self):
        s = C.seal(self.conn, "test", 1)
        self.assertEqual(s["rows_sealed"], self.conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0])
        self.conn.execute("INSERT INTO audit_log(clinic_id, action) VALUES (1, 'later')")
        self.conn.commit()
        r = C.verify(self.conn, "test")
        self.assertTrue(r["ok"])
        self.assertEqual(r["unsealed"], 1)
        s2 = C.seal(self.conn, "test")
        self.assertEqual((s2["rows_sealed"], s2["upto_id"] > s["upto_id"]), (1, True))
        self.assertEqual(C.seal(self.conn, "test")["id"], s2["id"])                     # nothing new: no new seal
        self.assertTrue(C.verify(self.conn, "test")["ok"])

    def test_a_change_behind_the_databases_back_is_found(self):
        C.seal(self.conn, "test")
        unlock(self.conn)
        self.assertFalse(dbmod.logs_locked(self.conn))
        self.conn.execute("UPDATE audit_log SET detail = 'quietly changed' WHERE detail = 'row 2'")
        self.conn.commit()
        r = C.verify(self.conn, "test")
        self.assertFalse(r["ok"])
        self.assertIsNotNone(r["broken"])
        dbmod.lock_logs(self.conn)                                                     # the app puts the locks back when it starts
        self.assertTrue(dbmod.logs_locked(self.conn))

    def test_a_removed_row_is_found(self):
        C.seal(self.conn, "test")
        unlock(self.conn)
        self.conn.execute("DELETE FROM audit_log WHERE detail = 'row 3'")
        self.conn.commit()
        self.assertFalse(C.verify(self.conn, "test")["ok"])

    def test_a_rewritten_history_is_found_with_a_kept_value(self):
        s = C.seal(self.conn, "test")
        anchor = f"{s['upto_id']}:{s['chain']}"
        self.assertEqual(C.verify(self.conn, "test", anchor)["anchor"], "match")
        # Someone with the server's key rebuilds the whole chain after a change. Only a value kept elsewhere shows it.
        unlock(self.conn)
        self.conn.execute("UPDATE audit_log SET detail = 'rewritten' WHERE detail = 'row 1'")
        self.conn.execute("DELETE FROM audit_seals")
        self.conn.commit()
        C.seal(self.conn, "test")
        self.assertTrue(C.verify(self.conn, "test")["ok"])
        r = C.verify(self.conn, "test", anchor)
        self.assertEqual(r["anchor"], "different")
        self.assertFalse(r["ok"])
        with self.assertRaises(C.ComplianceError):
            C.verify(self.conn, "test", "nonsense")

    def test_a_new_secret_key_is_not_taken_for_tampering_but_a_forged_seal_is(self):
        C.seal(self.conn, "old-key")
        self.conn.execute("INSERT INTO audit_log(clinic_id, action) VALUES (1, 'after the change')")
        self.conn.commit()
        C.seal(self.conn, "new-key")
        r = C.verify(self.conn, "new-key")
        self.assertEqual((r["ok"], r["other_key"]), (True, 1))
        self.conn.execute("INSERT INTO audit_log(clinic_id, action) VALUES (1, 'x')")
        self.conn.execute("INSERT INTO audit_seals(upto_id, rows_sealed, chain, key_id) VALUES ((SELECT MAX(id) FROM audit_log), 1, 'f00', 'made-up')")
        self.conn.commit()
        self.assertFalse(C.verify(self.conn, "new-key")["ok"])

    def test_the_commands(self):
        r = self.app.test_cli_runner()
        out = r.invoke(args=["audit-seal"])
        self.assertIn("Seal value:", out.output)
        value = out.output.split("Seal value: ", 1)[1].strip()
        out = r.invoke(args=["audit-verify", "--anchor", value])
        self.assertEqual(out.exit_code, 0, out.output)
        self.assertIn("matches", out.output)
        unlock(self.conn)
        self.conn.execute("UPDATE audit_log SET detail = 'x' WHERE detail = 'row 0'")
        self.conn.commit()
        out = r.invoke(args=["audit-verify"])
        self.assertEqual(out.exit_code, 1)
        self.assertIn("BROKEN", out.output)


class ReviewTests(Base):
    def test_every_login_needs_a_decision_and_remove_switches_it_off(self):
        mgr = self.login("mgr")
        page = self.page(mgr, "/settings/access-review")
        for name in ("Mgr", "Dent", "Asst", "Desk"):
            self.assertIn(name, page)
        self.assertNotIn("Mgr2", page)                                                  # clinic two's manager is not ours to review
        self.assertIn("No two-step login", page)
        conn = self.db()
        ids = {r["username"]: r["id"] for r in conn.execute("SELECT id, username FROM users WHERE clinic_id = 1")}
        conn.close()
        part = {f"d{ids['mgr']}": "keep", f"d{ids['dent']}": "keep", f"d{ids['asst']}": "change"}
        self.assertIn("for every login", self.post(mgr, "/settings/access-review", part, follow_redirects=True).get_data(as_text=True))
        part[f"d{ids['desk']}"] = "remove"
        self.assertIn("a short note is needed", self.post(mgr, "/settings/access-review", part, follow_redirects=True).get_data(as_text=True))
        part.update({f"n{ids['asst']}": "Should not see money", f"n{ids['desk']}": "Left the clinic"})
        self.assertIn("Access review saved", self.post(mgr, "/settings/access-review", part, follow_redirects=True).get_data(as_text=True))
        conn = self.db()
        self.assertEqual(conn.execute("SELECT active FROM users WHERE id = ?", (ids["desk"],)).fetchone()[0], 0)
        rev = conn.execute("SELECT * FROM access_reviews WHERE clinic_id = 1").fetchone()
        self.assertEqual((rev["logins"], rev["kept"], rev["changed"], rev["removed"]), (4, 2, 1, 1))
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM access_review_items WHERE review_id = ?", (rev["id"],)).fetchone()[0], 4)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'access.review'").fetchone()[0], 1)
        conn.close()
        self.assertIn("Left the clinic", self.page(mgr, f"/settings/access-review/{rev['id']}"))
        self.assertEqual(self.login("mgr2").get(f"/settings/access-review/{rev['id']}").status_code, 404)
        self.assertNotIn("Due now", self.page(mgr, "/settings/access-review").split("Earlier reviews")[0])

    def test_a_manager_cannot_remove_their_own_login(self):
        mgr = self.login("mgr")
        conn = self.db()
        ids = {r["username"]: r["id"] for r in conn.execute("SELECT id, username FROM users WHERE clinic_id = 1")}
        conn.close()
        data = {f"d{i}": "keep" for i in ids.values()}
        data.update({f"d{ids['mgr']}": "remove", f"n{ids['mgr']}": "me"})
        self.assertIn("cannot remove your own login", self.post(mgr, "/settings/access-review", data, follow_redirects=True).get_data(as_text=True))
        self.assertEqual(self.login("dent").get("/settings/access-review").status_code, 403)

    def test_last_sign_in_comes_from_the_locked_log(self):
        self.login("dent")
        conn = self.db()
        rows = {r["u"]["username"]: r for r in C.access_list(conn, 1)}
        conn.close()
        self.assertTrue(rows["dent"]["last"])
        self.assertIn("manager", rows["mgr"]["flags"])
        self.assertIn("no_two_step", rows["dent"]["flags"])


class PackTests(Base):
    def _pack(self, who="mgr"):
        r = self.post(self.login(who), "/settings/compliance/pack")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.mimetype, "application/zip")
        return zipfile.ZipFile(io.BytesIO(r.data))

    def test_the_pack_holds_the_evidence_and_no_patient_details(self):
        conn = self.db()
        conn.execute("""INSERT INTO incidents(clinic_id, title, found_at, what_happened, risk) VALUES
                        (1, 'Laptop with Ama Mensah list stolen', '2026-01-01 10:00', 'Mensah file on it', 'high')""")
        conn.execute("""INSERT INTO data_requests(clinic_id, patient_id, requester, kind, received_on, due_on) VALUES
                        (1, 1, 'Ama Mensah', 'access', '2026-01-01', '2026-01-31')""")
        conn.commit()
        conn.close()
        z = self._pack()
        names = set(z.namelist())
        for n in ("README.txt", "controls.csv", "settings.txt", "users.csv", "access_reviews.csv", "audit_chain.txt", "activity_summary.csv",
                  "outside_services.csv", "api_keys.csv", "backups.csv", "incidents.csv", "data_requests.csv", "sbom.cdx.json", "code_checks.txt"):
            self.assertIn(n, names)
        everything = " ".join(z.read(n).decode() for n in names)
        for private in ("Ama", "Mensah", "+233201112223", "Laptop"):
            self.assertNotIn(private, everything)
        controls = list(csv.DictReader(io.StringIO(z.read("controls.csv").decode())))
        self.assertEqual([c["key"] for c in controls], list(C.CONTROLS))
        got = {c["key"]: c for c in controls}
        self.assertEqual(got["incidents"]["state"], "Needs attention")                  # high risk, past 72 hours, regulator not told
        self.assertEqual(got["requests"]["state"], "Needs attention")
        self.assertEqual(got["log_locked"]["state"], "In place")
        self.assertEqual(got["code"]["state"], "In place")
        self.assertEqual(got["log_sealed"]["ISO/IEC 27001:2022 Annex A"], "A.8.15, A.5.28")
        chain = z.read("audit_chain.txt").decode()
        self.assertIn("Result: INTACT", chain)
        self.assertRegex(chain, r"Newest seal \(keep this\): \d+:[0-9a-f]{64}")
        users = z.read("users.csv").decode()
        self.assertIn("dent", users)
        self.assertNotIn("mgr2", users)
        self.assertNotIn("scrypt", users)
        bom = json.loads(z.read("sbom.cdx.json"))
        self.assertEqual((bom["bomFormat"], bom["specVersion"]), ("CycloneDX", "1.5"))
        self.assertIn("flask", {c["name"].lower() for c in bom["components"]})
        self.assertTrue(all(c["purl"].startswith("pkg:pypi/") for c in bom["components"]))
        conn = self.db()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'compliance.pack' AND clinic_id = 1").fetchone()[0], 1)
        conn.close()

    def test_managers_only(self):
        self.assertEqual(self.post(self.login("dent"), "/settings/compliance/pack").status_code, 403)
        self.assertEqual(self.login("desk").get("/settings/compliance").status_code, 403)

    def test_the_page_and_the_check_button(self):
        mgr = self.login("mgr")
        page = self.page(mgr, "/settings/compliance")
        for title in C.CONTROLS.values():
            self.assertIn(title.replace("'", "&#39;"), page)
        self.assertIn("Not sealed yet", page)
        page = self.post(mgr, "/settings/compliance/check").get_data(as_text=True)
        self.assertIn("The activity log is intact", page)
        self.assertRegex(page, r"<code>\d+:[0-9a-f]{64}</code>")

    def test_outside_services_follow_the_switches(self):
        conn = self.db()
        clinic = conn.execute("SELECT * FROM clinics WHERE id = 1").fetchone()
        self.assertFalse(any(s["on"] for s in C.outside_services(conn, clinic) if s["key"] != "offsite"))
        conn.execute("INSERT INTO ai_account(clinic_id, enabled, assistant) VALUES (1, 1, 0)")
        conn.commit()
        on = {s["key"] for s in C.outside_services(conn, clinic) if s["on"]}
        conn.close()
        self.assertEqual(on, {"ai_notes"})
        self.assertIn("AI help with clinical notes", self.page(self.login("mgr"), "/settings/compliance"))

    def test_the_command_writes_the_same_pack(self):
        out = Path(self.tmp.name) / "pack.zip"
        r = self.app.test_cli_runner().invoke(args=["audit-pack", "--clinic-id", "1", "--out", str(out)])
        self.assertEqual(r.exit_code, 0, r.output)
        self.assertIn("controls.csv", zipfile.ZipFile(out).namelist())


class SecurityTxtTests(Base):
    def test_published_only_with_a_contact(self):
        c = self.app.test_client()
        self.assertEqual(c.get("/.well-known/security.txt").status_code, 404)
        self.app.config.update(SECURITY_CONTACT="mailto:security@example.com", PUBLIC_URL="https://clinic.example.com")
        r = c.get("/.well-known/security.txt")
        self.assertEqual((r.status_code, r.mimetype), (200, "text/plain"))
        text = r.get_data(as_text=True)
        self.assertIn("Contact: mailto:security@example.com", text)
        self.assertRegex(text, r"Expires: \d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ")
        self.assertIn("Canonical: https://clinic.example.com/.well-known/security.txt", text)


class CodeCheckTests(Base):
    def test_no_findings_in_the_app(self):
        self.assertEqual(seccheck.run(), [])
        self.assertIn("Findings: 0", seccheck.report())

    def test_the_checks_catch_the_mistakes(self):
        bad = Path(self.tmp.name) / "bad.py"
        bad.write_text("import pickle\nfrom flask import request\n"
                       "def f(db):\n    eval('1')\n    db.execute(f\"SELECT * FROM t WHERE a = {request.args['a']}\")\n"
                       "    run('ls', shell=True)\n    get('u', verify=False)\n")
        old = seccheck.ROOT
        seccheck.ROOT = Path(self.tmp.name)
        try:
            rules = {f[2] for f in seccheck.check_python(bad)}
        finally:
            seccheck.ROOT = old
        self.assertEqual(rules, {"unsafe-load", "code-from-text", "sql-from-request", "shell", "no-tls-check"})


class HostileInputCrawl(Base):
    """Every page, opened by a manager with hostile text in the address and the usual query fields. Nothing may crash, and
    the text must never come back unescaped (that would be a cross-site scripting hole)."""
    FIELDS = ("q", "day", "start", "end", "status", "page", "kind", "back", "next", "lang", "date", "month", "id", "patient", "sort")

    def test_no_page_crashes_or_echoes_hostile_text(self):
        self.app.test_cli_runner().invoke(args=["seed-demo", "--clinic-id", "1"])
        c = self.login("mgr")
        crashes, echoes, seen = [], [], 0
        from urllib.parse import urlencode
        query = urlencode({f: HOSTILE for f in self.FIELDS})
        with self.app.test_request_context():
            from flask import url_for
            urls = []
            for rule in self.app.url_map.iter_rules():
                if "GET" not in rule.methods or rule.endpoint.startswith(("static", "operator.")) or rule.endpoint in ("auth.logout",):
                    continue
                args = {n: (1 if isinstance(rule._converters.get(n), IntegerConverter) else HOSTILE) for n in rule.arguments}
                try:
                    urls.append(url_for(rule.endpoint, **args))
                except Exception:                                     # noqa: BLE001 - a converter that refuses the text
                    continue
        for url in urls:
            r = c.get(url + ("&" if "?" in url else "?") + query)
            seen += 1
            if r.status_code >= 500:
                crashes.append(f"{url}: {r.status_code}")
            elif PROBE in r.get_data(as_text=True):
                echoes.append(url)
        self.assertGreater(seen, 250)
        self.assertEqual(crashes, [], "These pages crashed on hostile text")
        self.assertEqual(echoes, [], "These pages printed hostile text without escaping it")
        self.assertIn('href="/settings/compliance"', self.page(c, "/settings"))
        self.assertTrue(re.search(r"logs_locked", Path(dbmod.__file__).read_text()))
