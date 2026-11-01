"""v0.53 and v0.54: sign-up, the operator console, support access, paused accounts, plans and bills."""
import datetime
import json
import re
import urllib.parse

from tests.test_v04 import Base, PW
from dental_os import create_app
from dental_os.services import platform as PF, platformbill as PB

GOOD = "plantain kente harmattan lagoon"


class Pay:
    def __init__(self):
        self.paid, self.sessions = set(), {}

    def __call__(self, method, url, headers, body=None, timeout=20):
        if url.endswith("/transaction/initialize"):
            d = json.loads(body)
            return 200, json.dumps({"status": True, "data": {"authorization_url": "https://checkout.paystack.com/abc", "reference": d["reference"]}})
        if "/transaction/verify/" in url:
            ref = urllib.parse.unquote(url.rsplit("/", 1)[1])
            return 200, json.dumps({"status": True, "data": {"status": "success" if ref in self.paid else "abandoned", "amount": 50000, "currency": "GHS"}})
        raise AssertionError(url)


class Platform(Base):
    def setUp(self):
        super().setUp()
        self.conn = self.db()

    def tearDown(self):
        self.conn.close()

    def c(self, cid=1):
        return self.conn.execute("SELECT * FROM clinics WHERE id = ?", (cid,)).fetchone()


class ServiceTests(Platform):
    def test_signup_makes_a_trial_clinic_and_refuses_bad_forms(self):
        form = {"clinic_name": "Sunrise Dental", "country": "GH", "your_name": "Efua Owusu", "username": "efua", "password": GOOD, "password2": GOOD, "agree": "1"}
        with self.assertRaises(PF.PlatformError):
            PF.signup(self.conn, form, "ip1", allowed=False)
        for bad in ({"username": "mgr"}, {"password": "password123", "password2": "password123"}, {"password2": "different words here"}, {"agree": ""},
                    {"country": "XX"}):
            with self.assertRaises(PF.PlatformError):
                PF.signup(self.conn, {**form, **bad}, "ip1")
        with self.assertRaises(PF.PlatformError):
            PF.signup(self.conn, form, "ip1")                                   # five tries from that address in the hour
        cid, uid = PF.signup(self.conn, form, "ip2")
        c = self.c(cid)
        self.assertEqual((c["plan_state"], c["currency"]), ("trial", "GHS"))
        self.assertEqual(PF.access(self.conn, c), "full")
        self.assertEqual(self.conn.execute("SELECT role FROM users WHERE id = ?", (uid,)).fetchone()[0], "manager")

    def test_access_follows_the_bill(self):
        today = datetime.date.today()
        self.conn.execute("UPDATE clinics SET plan_state = 'trial', trial_ends_on = ? WHERE id = 1", ((today - datetime.timedelta(days=3)).isoformat(),))
        self.assertEqual(PF.state(self.c()), "past_due")
        self.assertEqual(PF.access(self.conn, self.c()), "full")                 # inside the grace period
        self.conn.execute("UPDATE clinics SET trial_ends_on = ? WHERE id = 1", ((today - datetime.timedelta(days=PF.GRACE_DAYS + 1)).isoformat(),))
        self.assertEqual(PF.access(self.conn, self.c()), "look")
        with self.assertRaises(PF.PlatformError):
            PF.set_state(self.conn, None, 1, "suspended")                        # a reason is needed
        PF.set_state(self.conn, None, 1, "suspended", "Asked to stop for now")
        self.assertEqual(PF.access(self.conn, self.c()), "closed")

    def test_plans_bills_and_paying(self):
        pid = PB.save_plan(self.conn, "Small practice", {"GHS": 50000, "GBP": 4900}, 3)
        with self.assertRaises(PB.BillError):
            PB.save_plan(self.conn, "No price", {})
        PB.put_on_plan(self.conn, None, 1, pid, (datetime.date.today() - datetime.timedelta(days=1)).isoformat())
        with self.assertRaises(PB.BillError):
            PB.put_on_plan(self.conn, None, 2, pid)                              # clinic two pays in dollars; the plan has no USD price
        self.assertEqual(PB.make_bills(self.conn), 1)
        self.assertEqual(PB.make_bills(self.conn), 0)                             # one open bill at a time
        bill = PB.bills(self.conn, 1)[0]
        self.assertEqual((bill["amount_p"], bill["currency"]), (50000, "GHS"))
        PB.make_bills(self.conn, datetime.date.today() + datetime.timedelta(days=10))
        self.assertEqual(self.c()["plan_state"], "past_due")
        cfg = {"PLATFORM_PAYSTACK_SECRET": "sk_test_x"}
        fake = Pay()
        url = PB.start_payment(self.conn, cfg, self.c(), bill["id"], "desk@clinic.example", "https://x/back", transport=fake)
        self.assertEqual(url, "https://checkout.paystack.com/abc")
        self.assertFalse(PB.confirm(self.conn, cfg, self.c(), bill["id"], transport=fake))
        fake.paid.add(PB.get_bill(self.conn, 1, bill["id"])["pay_ref"])
        self.assertTrue(PB.confirm(self.conn, cfg, self.c(), bill["id"], transport=fake))
        self.assertEqual((self.c()["plan_state"], self.c()["paid_until"]), ("active", bill["period_to"]))
        with self.assertRaises(PB.BillError):
            PB.mark_paid(self.conn, None, bill["id"], "bank_transfer", "X")       # already paid

    def test_support_grant(self):
        gid = PF.grant_support(self.conn, self.c(), 1, 4, "The diary looks wrong")
        self.assertEqual(PF.live_grant(self.conn, 1)["id"], gid)
        self.assertIsNone(PF.live_grant(self.conn, 1, datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=5)))
        with self.assertRaises(PF.PlatformError):
            PF.grant_support(self.conn, self.c(), 1, 1000)
        PF.end_support(self.conn, self.c(), 1)
        self.assertIsNone(PF.live_grant(self.conn, 1))


class ScreenTests(Platform):
    def test_operator_sees_counts_and_support_only_looks(self):
        PF.add_operator(self.conn, "ops", "Ops Person", GOOD)
        self.conn.commit()
        op = self.app.test_client()
        tok = re.search(r'name="_csrf" value="([^"]+)"', op.get("/operator/login").get_data(as_text=True)).group(1)
        op.post("/operator/login", data={"_csrf": tok, "username": "ops", "password": GOOD})
        page = op.get("/operator").get_data(as_text=True)
        tok = re.search(r'name="_csrf" value="([^"]+)"', page).group(1)              # signing in starts a fresh session
        self.assertIn("Clinic One", page)
        self.assertNotIn("Mensah", page)                                          # counts only, never names
        page = op.post("/operator/clinics/1", data={"_csrf": tok, "what": "support"}, follow_redirects=True).get_data(as_text=True)
        self.assertIn("has not granted support access", page)
        mgr = self.login("mgr")
        self.post(mgr, "/settings/support", {"hours": "4", "why": "Help with reports"})
        op.post("/operator/clinics/1", data={"_csrf": tok, "what": "support"})
        page = op.get("/patients").get_data(as_text=True)
        self.assertIn("Mensah", page)
        self.assertIn("You are looking at this clinic as Dental OS support", page)
        self.assertEqual(op.post("/patients/new", data={"_csrf": tok, "first_name": "X"}).status_code, 403)
        self.assertGreaterEqual(self.conn.execute("SELECT COUNT(*) FROM audit_log WHERE clinic_id = 1 AND action = 'support.view'").fetchone()[0], 1)
        self.post(mgr, "/settings/support", {"what": "end"})
        self.assertEqual(op.get("/patients").status_code, 302)                   # support ended: back to the sign-in page
        self.assertEqual(self.app.test_client().get("/operator").status_code, 302)

    def test_paused_clinic_can_only_take_its_data(self):
        PF.set_state(self.conn, None, 1, "suspended", "Asked to stop for now")
        self.conn.commit()
        mgr = self.login("mgr")
        r = mgr.get("/patients")
        self.assertIn("/billing/paused", r.headers["Location"])
        page = self.page(mgr, "/billing/paused")
        self.assertIn("Asked to stop for now", page)
        self.assertIn("Download a full copy of the data", page)
        tok = re.search(r'name="_csrf" value="([^"]+)"', page).group(1)
        r = mgr.post("/tools/export", data={"_csrf": tok})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["Content-Type"], "application/zip")

    def test_signup_page_only_when_allowed(self):
        self.assertEqual(self.app.test_client().get("/signup").status_code, 404)
        import tempfile, os
        with tempfile.TemporaryDirectory() as d:
            app = create_app({"TESTING": True, "DATABASE": os.path.join(d, "s.sqlite"), "SECRET_KEY": "s", "ALLOW_SIGNUP": True})
            c = app.test_client()
            page = c.get("/signup").get_data(as_text=True)
            tok = re.search(r'name="_csrf" value="([^"]+)"', page).group(1)
            r = c.post("/signup", data={"_csrf": tok, "clinic_name": "Sunrise Dental", "country": "GH", "your_name": "Efua Owusu", "username": "efua",
                                        "password": GOOD, "password2": GOOD, "agree": "1"}, follow_redirects=True)
            self.assertIn("Free trial until", r.get_data(as_text=True))
            self.assertIn("Start a free trial", c.get("/login").get_data(as_text=True) if False else app.test_client().get("/login").get_data(as_text=True))
