"""v0.72: HQ engineering and compliance. Clinic servers report their version, copies and free disk with a key shown once; features
clinics ask for are ranked by how many clinics want them; release notes reach clinics under Help; compliance keeps incidents with a
72-hour clock, clinics' requests about their data, the outside companies that touch clinic data, registrations to renew and a check
of HQ's own access."""
import datetime
import re

from tests import a11y
from tests.test_v71 import HQBase
from dental_os.services import hq as H, hqtech as T, opteam as OT


class Engineering(HQBase):
    def test_a_clinic_server_reports_in_with_its_key(self):
        conn = self.db()
        iid, key = T.add_install(conn, 1, "Osu server", 1)
        conn.commit()
        row = conn.execute("SELECT * FROM hq_installs WHERE id = ?", (iid,)).fetchone()
        self.assertNotIn(key, str(dict(row)))                                   # only the hash is kept
        conn.close()
        c = self.app.test_client()
        r = c.post("/hq/checkin", json={"version": "0.70", "disk_free_pct": 9, "last_copy_at": "2026-11-04T01:10:00Z",
                                        "patients": 9999, "secret": "x"}, headers={"Authorization": "Bearer " + key})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["ok"])
        self.assertEqual(c.post("/hq/checkin", json={}, headers={"Authorization": "Bearer wrong"}).status_code, 401)
        self.assertEqual(c.post("/hq/checkin", json={}).status_code, 401)
        conn = self.db()
        row = conn.execute("SELECT * FROM hq_installs WHERE id = ?", (iid,)).fetchone()
        self.assertEqual((row["version"], row["disk_free_pct"], row["last_copy_at"]), ("0.70", 9, "2026-11-04 01:10:00"))
        flags = T.installs(conn, datetime.datetime(2026, 11, 4, 12, 0, tzinfo=datetime.timezone.utc))[0]["flags"]
        self.assertIn("old_version", flags)
        self.assertIn("disk", flags)
        self.assertIn("drill_due", flags)
        T.switch_install(conn, 1, iid, False)
        conn.commit()
        conn.close()
        self.assertEqual(c.post("/hq/checkin", json={}, headers={"Authorization": "Bearer " + key}).status_code, 401)

    def test_this_server_sends_its_own_report(self):
        conn = self.db()
        conn.execute("INSERT INTO offsite_log(ok, detail) VALUES (1, 'ok')")
        sent = {}

        def transport(url, headers, body):
            sent.update(url=url, auth=headers["Authorization"], body=body)
            return 200
        self.assertEqual(T.send_report({"HQ_URL": "https://hq.example.com", "HQ_INSTALL_KEY": "dosi_abc"}, conn, transport), 200)
        self.assertEqual(sent["url"], "https://hq.example.com/hq/checkin")
        self.assertEqual(sent["auth"], "Bearer dosi_abc")
        self.assertIn(b'"version"', sent["body"])
        self.assertNotIn(b"Ama", sent["body"])
        with self.assertRaises(H.HQError):
            T.send_report({"HQ_URL": "http://hq.example.com", "HQ_INSTALL_KEY": "k"}, conn, transport)   # https only
        conn.close()
        r = self.app.test_cli_runner().invoke(args=["hq-report"])
        self.assertIn("HQ_URL is not set", r.output)

    def test_server_health(self):
        conn = self.db()
        now = datetime.datetime(2026, 11, 4, 12, 0, tzinfo=datetime.timezone.utc)
        h = T.server_health(conn, now)
        self.assertTrue(h["copy_stale"] and h["drill_due"])
        conn.execute("INSERT INTO offsite_log(ok, detail, created_at) VALUES (1, 'ok', '2026-11-04 02:10:00')")
        conn.execute("INSERT INTO error_log(ref, endpoint, kind, created_at) VALUES ('r1', 'views.diary', 'KeyError', '2026-11-04 11:00:00')")
        h = T.server_health(conn, now)
        self.assertFalse(h["copy_stale"])
        self.assertEqual((h["errors_day"], h["top"][0]["endpoint"]), (1, "views.diary"))
        conn.close()

    def test_feature_requests_rank_by_clinics(self):
        conn = self.db()
        a = T.save_request(conn, 1, None, "SMS recalls", "")
        b = T.save_request(conn, 1, None, "Dark mode", "")
        self.assertTrue(T.want(conn, 1, a, 1))
        self.assertFalse(T.want(conn, 1, a, 1))                                   # each clinic counts once
        T.want(conn, 1, a, 2)
        T.want(conn, 1, b, 2)
        rows = T.requests(conn)
        self.assertEqual([(r["title"], r["clinics"]) for r in rows], [("SMS recalls", 2), ("Dark mode", 1)])
        T.save_request(conn, 1, a, "SMS recalls", "", "done", "0.72")
        self.assertEqual([r["title"] for r in T.requests(conn)], ["Dark mode"])
        conn.close()

    def test_support_adds_clinics_but_only_engineering_decides(self):
        self.person("sup", ["support"])
        c, _ = self.op("sup")
        r = self.send(c, "/hq/requests", {"title": "Print day list", "clinic_id": "1"})
        url = r.headers["Location"]
        self.assertEqual(self.send(c, url, {"what": "save", "title": "x", "status": "done"}).status_code, 403)
        self.send(c, url, {"what": "want", "clinic_id": "2"})
        self.assertIn("Clinic Two", c.get(url).get_data(as_text=True))
        self.assertEqual(c.get("/hq/engineering").status_code, 403)

    def test_release_notes_reach_clinics_once_published(self):
        conn = self.db()
        T.save_release(conn, 1, "0.72", "2026-11-04", "HQ for the company.\n\nCompliance registers.", False)
        conn.commit()
        c = self.login("desk")
        self.assertNotIn("Compliance registers", self.page(c, "/help/whats-new"))
        T.save_release(conn, 1, "0.72", "2026-11-04", "HQ for the company.\n\nCompliance registers.", True)
        conn.commit()
        conn.close()
        self.assertIn("Compliance registers", self.page(c, "/help/whats-new"))


class Compliance(HQBase):
    def test_incident_clock(self):
        conn = self.db()
        iid = T.save_incident(conn, 1, None, {"title": "Wrong clinic saw a bill", "what": "A bill page showed for the wrong clinic",
                                               "found_at": "2026-11-04 08:00", "severity": "high"})
        r = conn.execute("SELECT * FROM hq_incidents WHERE id = ?", (iid,)).fetchone()
        self.assertEqual(T.incident_clock(r, datetime.datetime(2026, 11, 5, 8, 0)), 48.0)
        self.assertLess(T.incident_clock(r, datetime.datetime(2026, 11, 8, 8, 0)), 0)
        T.save_incident(conn, 1, iid, {"title": "Wrong clinic saw a bill", "what": "x", "found_at": "2026-11-04 08:00",
                                       "regulator_told_at": "2026-11-05T09:30"})
        r = conn.execute("SELECT * FROM hq_incidents WHERE id = ?", (iid,)).fetchone()
        self.assertIsNone(T.incident_clock(r))
        with self.assertRaises(H.HQError):
            T.save_incident(conn, 1, None, {"title": "x", "what": "Patient 0244123456 affected"})
        conn.close()

    def test_data_requests_default_to_thirty_days(self):
        conn = self.db()
        rid = T.save_data_request(conn, 1, None, {"clinic_id": "1", "kind": "end_of_contract", "summary": "Full copy then deletion",
                                                  "received_on": "2026-11-01"})
        r = conn.execute("SELECT * FROM hq_data_requests WHERE id = ?", (rid,)).fetchone()
        self.assertEqual((r["due_on"], r["kind"]), ("2026-12-01", "end_of_contract"))
        self.assertTrue(T.data_requests(conn, datetime.date(2026, 12, 2))[0]["late"])
        T.save_data_request(conn, 1, rid, {"clinic_id": "1", "kind": "end_of_contract", "summary": "Full copy then deletion",
                                           "received_on": "2026-11-01", "status": "done", "outcome": "Copy sent, deleted"})
        self.assertFalse(T.data_requests(conn, datetime.date(2026, 12, 2))[0]["late"])
        conn.close()

    def test_outside_companies_and_renewals(self):
        conn = self.db()
        with self.assertRaises(H.HQError):
            T.save_subprocessor(conn, 1, None, {"name": "AWS"})
        T.save_subprocessor(conn, 1, None, {"name": "AWS", "purpose": "Hosting", "data": "All clinic data", "country": "South Africa",
                                            "review_on": "2026-10-01"})
        s = T.subprocessors(conn, datetime.date(2026, 11, 4))[0]
        self.assertTrue(s["no_agreement"] and s["review_due"])
        T.save_registration(conn, 1, None, {"name": "Data Protection Commission registration", "renews_on": "2026-12-01"})
        r = T.registrations(conn, datetime.date(2026, 11, 4))[0]
        self.assertTrue(r["soon"])
        self.assertFalse(r["late"])
        conn.close()

    def test_hq_access_check(self):
        self.person("ama", ["sales"])
        conn = self.db()
        x = T.access_check(conn)
        self.assertTrue(x["review_due"])
        names = {p["o"]["username"]: p["issues"] for p in x["people"]}
        self.assertIn("no_2fa", names["ama"])
        self.assertIn("unused", names["ama"])
        OT.review(conn, self.owner_row(conn))
        conn.commit()
        self.assertFalse(T.access_check(conn)["review_due"])
        conn.close()

    def test_compliance_page_registers(self):
        self.person("cmp", ["compliance"])
        c, _ = self.op("cmp")
        self.send(c, "/hq/compliance", {"register": "datareq", "clinic_id": "1", "kind": "audit", "summary": "Security questionnaire"})
        conn = self.db()
        self.assertEqual(conn.execute("SELECT kind FROM hq_data_requests").fetchone()[0], "audit")
        conn.close()
        page = c.get("/hq/compliance").get_data(as_text=True)
        self.assertIn("Security questionnaire", page)
        self.assertIn("Security questionnaire", c.get("/hq").get_data(as_text=True))
        self.assertEqual(c.get("/hq/engineering").status_code, 403)
        self.assertEqual(c.get("/hq/compliance/nonsense/1").status_code, 404)


class Pages(HQBase):
    def test_every_new_page_passes_accessibility_checks(self):
        conn = self.db()
        T.add_install(conn, 1, "Osu server", 1)
        rid = T.save_request(conn, 1, None, "A request", "")
        T.want(conn, 1, rid, 1)
        T.save_release(conn, 1, "0.72", "2026-11-04", "Notes", True)
        ids = {"incident": T.save_incident(conn, 1, None, {"title": "I", "what": "W"}),
               "datareq": T.save_data_request(conn, 1, None, {"summary": "S"}),
               "outside": T.save_subprocessor(conn, 1, None, {"name": "N", "purpose": "P", "data": "D", "country": "C"}),
               "registration": T.save_registration(conn, 1, None, {"name": "R"})}
        conn.commit()
        conn.close()
        c, _ = self.op("owner")
        urls = ["/hq", "/hq/engineering", "/hq/requests", f"/hq/requests/{rid}", "/hq/compliance"] + [f"/hq/compliance/{k}/{v}" for k, v in ids.items()]
        for url in urls:
            r = c.get(url)
            self.assertEqual(r.status_code, 200, url)
            self.assertEqual(a11y.check(r.get_data(as_text=True)), [], url)
        desk = self.login("desk")
        for url in ("/help", "/help/whats-new"):
            self.assertEqual(a11y.check(self.page(desk, url)), [], url)

    def test_ids_on_the_compliance_page_are_unique(self):
        c, _ = self.op("owner")
        page = c.get("/hq/compliance").get_data(as_text=True)
        ids = re.findall(r'\sid="([^"]+)"', page)
        self.assertEqual(len(ids), len(set(ids)))
