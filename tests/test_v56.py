"""v0.56: security review.

Three checks that cover the whole app rather than one feature:
  * every "go back to" address must stay on this site;
  * every page needs a login unless it is one of the few public ones;
  * a manager at clinic two opens every page with clinic one's record numbers and never sees clinic one's patients.
"""
import re
import unittest

from werkzeug.routing.converters import IntegerConverter

from tests.test_v04 import Base, PW
from tests.dbmeta import ON_PG
from dental_os.safeurl import local_path

# Pages anyone may open without logging in. Anything else must send a stranger to the login page or refuse.
PUBLIC_BLUEPRINTS = {"public", "formpub", "paypub", "signup", "static", "rxpub"}
PUBLIC_ENDPOINTS = {"fhir.metadata", "health", "auth.login", "auth.forgot", "auth.reset", "operator.login", "static", "manifest"}


def _get_rules(app):
    for rule in app.url_map.iter_rules():
        if "GET" not in rule.methods:
            continue
        yield rule


def _fill(rule, value):
    args = {}
    for name in rule.arguments:
        conv = rule._converters.get(name)
        args[name] = value if isinstance(conv, IntegerConverter) else "x"
    return args


def _url(app, rule, value):
    with app.test_request_context():
        from flask import url_for
        try:
            return url_for(rule.endpoint, **_fill(rule, value))
        except Exception:
            return None


class SafeUrlTests(Base):
    def test_only_paths_on_this_site_pass(self):
        for good in ("/", "/patients/1", "/diary?day=2026-10-31#a"):
            self.assertEqual(local_path(good), good)
        for bad in (None, "", "patients", "//evil.example", "/\\evil.example", "\\\\evil.example", "https://evil.example/",
                    "/x\nSet-Cookie: a=b", "/x\t", "javascript:alert(1)", "/%0d", 5):
            if bad == "/%0d":
                self.assertEqual(local_path(bad), bad)            # still encoded, so the browser stays on this site
                continue
            self.assertIsNone(local_path(bad), repr(bad))

    def test_a_backslash_back_address_is_ignored(self):
        c = self.login("mgr")
        self.app.test_cli_runner().invoke(args=["seed-demo", "--clinic-id", "1"])
        aid = self.db().execute("SELECT id FROM appointments WHERE clinic_id = 1").fetchone()[0]
        r = self.post(c, f"/appointments/{aid}/confirmation", {"value": "confirmed", "back": "/\\evil.example"})
        self.assertEqual(r.status_code, 302)
        self.assertNotIn("evil.example", r.headers.get("Location", ""))

    def test_https_sites_get_hsts_and_plain_ones_do_not(self):
        c = self.app.test_client()
        self.assertNotIn("Strict-Transport-Security", c.get("/login").headers)
        self.app.config["SESSION_COOKIE_SECURE"] = True
        h = c.get("/login").headers
        self.assertIn("max-age=31536000", h["Strict-Transport-Security"])
        self.assertIn("camera=()", h["Permissions-Policy"])
        self.assertEqual(h["X-Frame-Options"], "DENY")


class LoginNeededTests(Base):
    def test_every_page_needs_a_login_unless_it_is_public(self):
        c = self.app.test_client()
        open_pages = []
        for rule in _get_rules(self.app):
            bp = rule.endpoint.split(".")[0] if "." in rule.endpoint else None
            if rule.endpoint in PUBLIC_ENDPOINTS or bp in PUBLIC_BLUEPRINTS:
                continue
            url = _url(self.app, rule, 1)
            if not url:
                continue
            r = c.get(url)
            if r.status_code == 200:
                open_pages.append(url)
            elif r.status_code in (301, 302, 303, 307, 308):
                loc = r.headers["Location"]
                if "/login" not in loc and "/operator/login" not in loc and not url.startswith("/api/"):
                    open_pages.append(f"{url} -> {loc}")
        self.assertEqual(open_pages, [], "These pages open without a login")


class CrossClinicCrawl(Base):
    """Clinic one gets a full set of made-up records. Clinic two's manager then tries every page with clinic one's numbers."""

    def setUp(self):
        super().setUp()
        self.app.test_cli_runner().invoke(args=["seed-demo", "--clinic-id", "1"])
        conn = self.db()
        rows = conn.execute("SELECT last_name, phone FROM patients WHERE clinic_id = 1").fetchall()
        conn.close()
        # Surnames long enough not to turn up by chance in ordinary words on a page.
        self.markers = sorted({r["last_name"] for r in rows if r["last_name"] and len(r["last_name"]) >= 5}
                              | {r["phone"] for r in rows if r["phone"]})
        self.assertGreater(len(self.markers), 5)

    def _crawl(self, client, ids):
        seen, leaks = 0, []
        pattern = re.compile("|".join(re.escape(m) for m in self.markers))
        for rule in _get_rules(self.app):
            if not rule.arguments:
                continue
            if any(rule.endpoint.startswith(p) for p in ("static", "operator.")):
                continue
            for value in ids:
                url = _url(self.app, rule, value)
                if not url:
                    continue
                r = client.get(url)
                if r.status_code != 200:
                    continue
                seen += 1
                body = r.get_data(as_text=True)
                m = pattern.search(body)
                if m:
                    leaks.append(f"{url}: {m.group(0)}")
                    break
        return seen, leaks

    def test_clinic_two_never_sees_clinic_one(self):
        ids = range(1, 6)
        # First, as clinic one's own manager: the same crawl must find clinic one's patients, or it proves nothing.
        _, own = self._crawl(self.login("mgr"), ids)
        self.assertGreater(len(own), 20, "The crawl did not reach clinic one's records, so it cannot test anything")
        _, leaks = self._crawl(self.login("mgr2"), ids)
        self.assertEqual(leaks, [], "Clinic two saw clinic one's patients here")


def _clinic_snapshot(conn, cid):
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")] if not ON_PG else \
             [r[0] for r in conn.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema()")]
    snap = {}
    for t in tables:
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({t})")] if not ON_PG else \
               [r[0] for r in conn.execute("SELECT column_name FROM information_schema.columns WHERE table_name = ?", (t,))]
        if "clinic_id" in cols:
            snap[t] = sorted(repr(tuple(r)) for r in conn.execute(f"SELECT * FROM {t} WHERE clinic_id = ?", (cid,)))
        elif t == "clinics":
            snap[t] = [repr(tuple(conn.execute("SELECT * FROM clinics WHERE id = ?", (cid,)).fetchone()))]
    return snap


class CrossClinicWrites(CrossClinicCrawl):
    def test_clinic_two_cannot_change_clinic_one(self):
        conn = self.db()
        before = _clinic_snapshot(conn, 1)
        conn.close()
        errors = self._post_everything(self.login("mgr2"))
        conn = self.db()
        after = _clinic_snapshot(conn, 1)
        conn.close()
        changed = [t for t in before if before[t] != after.get(t)]
        self.assertEqual(changed, [], "Clinic two changed clinic one's records in these tables")
        self.assertEqual(errors, [], "These forms crashed")
        # The control: clinic one's own manager sending the same forms does change things, so the check above can see a leak.
        self._post_everything(self.login("mgr"))
        conn = self.db()
        mine = _clinic_snapshot(conn, 1)
        conn.close()
        self.assertGreater(len([t for t in before if before[t] != mine.get(t)]), 3)

    def _post_everything(self, c):
        errors = []
        for rule in self.app.url_map.iter_rules():
            if "POST" not in rule.methods or not rule.arguments or rule.endpoint.startswith(("static", "operator.")):
                continue
            for value in range(1, 6):
                url = _url(self.app, rule, value)
                if not url:
                    continue
                r = self.post(c, url, {"action": "x", "id": str(value), "patient_id": str(value), "back": "/"})
                if r.status_code >= 500:
                    errors.append(f"{url}: {r.status_code}")
        return errors

    def test_clinic_two_never_sees_clinic_one(self):
        pass                                                      # run once, in the class above


class LoginLimitTests(Base):
    def test_one_address_cannot_try_many_usernames(self):
        import dental_os.auth as A
        c = self.app.test_client()
        token = re.search(r'name="_csrf" value="([^"]+)"', c.get("/login").get_data(as_text=True)).group(1)
        for n in range(A.LOGIN_FAILS_PER_ADDRESS):
            c.post("/login", data={"_csrf": token, "username": f"guess{n}", "password": "password123"})
        r = c.post("/login", data={"_csrf": token, "username": "mgr", "password": "wrong"})
        self.assertIn("from this network", r.get_data(as_text=True))
        conn = self.db()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM login_attempts WHERE username = 'mgr'").fetchone()[0], 0)   # refused before checking
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM throttle WHERE bucket LIKE 'login-bad:%'").fetchone()[0], A.LOGIN_FAILS_PER_ADDRESS)
        conn.close()
        # Another address is not affected, and the right password still works from there.
        other = self.app.test_client()
        token = re.search(r'name="_csrf" value="([^"]+)"', other.get("/login").get_data(as_text=True)).group(1)
        r = other.post("/login", data={"_csrf": token, "username": "mgr", "password": PW}, environ_base={"REMOTE_ADDR": "10.9.9.9"})
        self.assertEqual(r.status_code, 302)


class SpreadsheetTests(Base):
    def test_claims_export_cannot_carry_a_formula(self):
        from dental_os.services import ghclaims as GC
        import inspect
        src = inspect.getsource(GC)
        self.assertIn("_safe_cell(v) for v in", src)
        from dental_os.services.dataio import _safe_cell
        self.assertEqual(_safe_cell("=HYPERLINK(1)"), "'=HYPERLINK(1)")
        self.assertEqual(_safe_cell("+233201112223"), "+233201112223")      # phone numbers left alone
        self.assertEqual(_safe_cell("-12.50"), "-12.50")
        self.assertEqual(_safe_cell("+cmd|' /C calc'!A0"), "'+cmd|' /C calc'!A0")


class ProxyTests(Base):
    def _locked_out_elsewhere(self, trust):
        """Thirty wrong logins come through the proxy from one visitor. Is a different visitor then refused too?"""
        import dental_os.auth as A
        from dental_os import create_app
        conn = self.db()
        conn.execute("DELETE FROM throttle")
        conn.commit()
        conn.close()
        app = create_app({"TESTING": True, "DATABASE": self.app.config["DATABASE"], "SECRET_KEY": "test", "TRUST_PROXY": trust})
        proxy = {"REMOTE_ADDR": "10.0.0.5"}
        c = app.test_client()
        token = re.search(r'name="_csrf" value="([^"]+)"', c.get("/login").get_data(as_text=True)).group(1)
        for n in range(A.LOGIN_FAILS_PER_ADDRESS):
            c.post("/login", data={"_csrf": token, "username": f"guess{n}", "password": "x"}, environ_base=proxy,
                   headers={"X-Forwarded-For": "41.66.1.2"})
        other = app.test_client()
        token = re.search(r'name="_csrf" value="([^"]+)"', other.get("/login").get_data(as_text=True)).group(1)
        r = other.post("/login", data={"_csrf": token, "username": "mgr", "password": PW}, environ_base=proxy, headers={"X-Forwarded-For": "41.66.9.9"})
        return r.status_code != 302

    def test_the_visitor_address_comes_from_the_proxy_only_when_told(self):
        self.assertTrue(self._locked_out_elsewhere(0))      # without TRUST_PROXY everyone looks like the proxy
        self.assertFalse(self._locked_out_elsewhere(1))     # with it, each visitor is counted on their own
