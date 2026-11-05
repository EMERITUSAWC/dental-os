"""v0.71: Dental OS HQ. The company's own staff each have a login and belong to departments (support, finance, sales, onboarding,
engineering, compliance); owners make up Leadership. Each department has its own workspace, heads see their team, logins have
start and end days, and nothing in HQ holds a patient's details."""
import datetime
import re

from tests.test_v04 import Base
from tests import a11y, dbmeta
from dental_os.services import hq as H, opteam as OT, platform as PF

PW = "a-long-operator-password"


class HQBase(Base):
    def setUp(self):
        super().setUp()
        self.app.config["OPERATOR_REQUIRE_2FA"] = False
        conn = self.db()
        PF.add_operator(conn, "owner", "The Owner", PW)
        conn.commit()
        conn.close()

    def person(self, username, depts, heads=(), owner=False):
        """Add someone straight into the database with a known password."""
        conn = self.db()
        oid = PF.add_operator(conn, username, username.title(), PW)
        boss = conn.execute("SELECT * FROM operators WHERE username = 'owner'").fetchone()
        OT.set_departments(conn, boss, oid, depts, heads, owner)
        conn.commit()
        conn.close()
        return oid

    def op(self, username):
        c = self.app.test_client()
        token = re.search(r'name="_csrf" value="([^"]+)"', c.get("/operator/login").get_data(as_text=True)).group(1)
        r = c.post("/operator/login", data={"_csrf": token, "username": username, "password": PW, "code": ""})
        return c, r

    def send(self, c, url, data):
        token = re.search(r'name="_csrf" value="([^"]+)"', c.get("/hq").get_data(as_text=True)).group(1)
        return c.post(url, data={"_csrf": token, **data})

    def owner_row(self, conn):
        return conn.execute("SELECT * FROM operators WHERE username = 'owner'").fetchone()


class Departments(HQBase):
    def test_sign_in_lands_on_hq(self):
        _c, r = self.op("owner")
        self.assertTrue(r.headers["Location"].endswith("/hq"))

    def test_each_department_reaches_only_its_own_work(self):
        self.person("sam", ["support"])
        c, _ = self.op("sam")
        for url in ("/hq", "/hq/support", "/hq/help", "/operator/health", "/operator"):
            self.assertEqual(c.get(url).status_code, 200, url)
        for url in ("/hq/finance", "/hq/finance/vat", "/hq/sales", "/hq/onboarding", "/hq/leadership", "/operator/team", "/operator/plans"):
            self.assertEqual(c.get(url).status_code, 403, url)
        nav = c.get("/hq").get_data(as_text=True)
        self.assertIn('href="/hq/support"', nav)
        self.assertNotIn('href="/hq/finance"', nav)

    def test_someone_can_work_in_two_departments(self):
        self.person("ama", ["sales", "onboarding"])
        c, _ = self.op("ama")
        for url in ("/hq/sales", "/hq/onboarding"):
            self.assertEqual(c.get(url).status_code, 200)
        self.assertEqual(c.get("/hq/support").status_code, 403)

    def test_owner_reaches_everything(self):
        c, _ = self.op("owner")
        for url in ("/hq/leadership", "/hq/support", "/hq/finance", "/hq/sales", "/hq/onboarding", "/operator/team", "/hq/departments/finance"):
            self.assertEqual(c.get(url).status_code, 200, url)

    def test_a_head_sees_their_team_and_nobody_else_does(self):
        self.person("kofi", ["finance"], heads=["finance"])
        self.person("efua", ["finance"])
        head, _ = self.op("kofi")
        page = head.get("/hq/departments/finance").get_data(as_text=True)
        self.assertIn("Efua", page)
        self.assertEqual(head.get("/hq/departments/sales").status_code, 403)
        self.assertEqual(head.get("/operator/team").status_code, 403)          # only owners change logins
        member, _ = self.op("efua")
        self.assertEqual(member.get("/hq/departments/finance").status_code, 403)

    def test_rules_for_placing_people(self):
        conn = self.db()
        boss = self.owner_row(conn)
        oid = PF.add_operator(conn, "xone", "X One", PW)
        with self.assertRaises(OT.TeamError):
            OT.set_departments(conn, boss, oid, [], [], False)                  # nowhere at all
        with self.assertRaises(OT.TeamError):
            OT.set_departments(conn, boss, boss["id"], ["sales"], [], False)    # your own login
        OT.set_departments(conn, boss, oid, ["sales", "nonsense"], ["sales", "finance"], False)
        self.assertEqual(OT.memberships(conn, oid), {"sales": True})            # unknown departments and heads outside them dropped
        conn.close()

    def test_older_logins_keep_working_as_their_department(self):
        conn = self.db()
        oid = PF.add_operator(conn, "old", "Old Login", PW)
        conn.execute("UPDATE operators SET role = 'finance' WHERE id = ?", (oid,))
        conn.commit()
        self.assertEqual(OT.memberships(conn, oid), {"finance": False})
        conn.close()
        c, _ = self.op("old")
        self.assertEqual(c.get("/hq/finance").status_code, 200)
        self.assertEqual(c.get("/hq/support").status_code, 403)

    def test_team_page_places_people(self):
        oid = self.person("yaw", ["support"])
        c, _ = self.op("owner")
        self.send(c, "/operator/team", {"what": "depts", "id": str(oid), "depts": ["support", "engineering"], "heads": ["engineering"]})
        conn = self.db()
        self.assertEqual(OT.memberships(conn, oid), {"support": False, "engineering": True})
        self.assertIn("team.depts", [r[0] for r in conn.execute("SELECT action FROM operator_log")])
        conn.close()
        r = self.send(c, "/operator/team", {"what": "add", "username": "nana", "display_name": "Nana", "role": "compliance", "depts": ["support"]})
        self.assertIn("<code>", r.get_data(as_text=True))
        conn = self.db()
        nana = conn.execute("SELECT id FROM operators WHERE username = 'nana'").fetchone()[0]
        self.assertEqual(set(OT.memberships(conn, nana)), {"compliance", "support"})
        conn.close()


class LoginDates(HQBase):
    def test_before_the_start_and_after_the_end_the_login_does_not_work(self):
        oid = self.person("temp", ["support"])
        conn = self.db()
        tomorrow = (datetime.date.today() + datetime.timedelta(days=1)).isoformat()
        OT.set_dates(conn, self.owner_row(conn), oid, tomorrow, None)
        conn.commit()
        conn.close()
        _c, r = self.op("temp")
        self.assertNotIn("/hq", r.headers.get("Location", ""))
        conn = self.db()
        OT.set_dates(conn, self.owner_row(conn), oid, None, None)
        conn.commit()
        conn.close()
        c, r = self.op("temp")
        self.assertIn("/hq", r.headers["Location"])
        conn = self.db()
        yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
        conn.execute("UPDATE operators SET ends_on = ? WHERE id = ?", (yesterday, oid))
        conn.commit()
        conn.close()
        self.assertIn("/operator/login", c.get("/hq").headers["Location"])        # an open session stops too

    def test_dates_are_checked(self):
        conn = self.db()
        oid = PF.add_operator(conn, "done", "D One", PW)
        for start, end in (("2026-13-01", None), ("2026-05-01", "2026-04-01")):
            with self.assertRaises(OT.TeamError):
                OT.set_dates(conn, self.owner_row(conn), oid, start, end)
        conn.close()


class Support(HQBase):
    def test_answer_and_solve_times_follow_the_priority(self):
        conn = self.db()
        t0 = datetime.datetime(2026, 11, 4, 9, 0, tzinfo=datetime.timezone.utc)
        tid = H.new_ticket(conn, 1, 1, "Manager", "phone", "Cannot print receipts", "", "urgent", now=t0)
        t = H.ticket(conn, tid)
        self.assertEqual((t["reply_due"], t["solve_due"]), ("2026-11-04 10:00:00", "2026-11-04 17:00:00"))
        late = H.clocks(t, t0 + datetime.timedelta(hours=2))
        self.assertTrue(late["reply_late"])
        H.add_note(conn, 1, tid, "reply", "Called back, looking now", now=t0 + datetime.timedelta(minutes=30))
        t = H.ticket(conn, tid)
        self.assertEqual(t["status"], "open")
        self.assertFalse(H.clocks(t, t0 + datetime.timedelta(hours=2))["reply_late"])
        H.set_ticket(conn, 1, tid, status="solved", now=t0 + datetime.timedelta(hours=3))
        self.assertFalse(H.clocks(H.ticket(conn, tid), t0 + datetime.timedelta(hours=20))["solve_late"])
        H.set_ticket(conn, 1, tid, priority="low")                                # a new priority moves the times
        self.assertEqual(H.ticket(conn, tid)["solve_due"], "2026-11-09 09:00:00")
        conn.close()

    def test_no_patient_phone_numbers_in_tickets(self):
        conn = self.db()
        for text in ("Patient on 0244123456 cannot book", "Call +233244123456", "Ring 024 412 3456"):
            with self.assertRaises(H.HQError):
                H.new_ticket(conn, 1, 1, None, "phone", text, "", "normal")
        tid = H.new_ticket(conn, 1, 1, None, "phone", "Report from 2026-11-04 is blank", "Error ref 20261104", "normal")
        with self.assertRaises(H.HQError):
            H.add_note(conn, 1, tid, "internal", "patient number 0201112223")
        conn.close()

    def test_shared_problem_reports_come_in_once(self):
        conn = self.db()
        conn.execute("INSERT INTO problem_reports(clinic_id, what, urgency, shared) VALUES (1, 'Diary will not open', 'blocking', 1)")
        conn.execute("INSERT INTO problem_reports(clinic_id, what, urgency, shared) VALUES (1, 'Private one', 'idea', 0)")
        conn.commit()
        self.assertEqual(H.import_reports(conn, 1), 1)
        self.assertEqual(H.import_reports(conn, 1), 0)
        t = conn.execute("SELECT * FROM hq_tickets").fetchone()
        self.assertEqual((t["priority"], t["channel"], t["subject"]), ("urgent", "problem_report", "Diary will not open"))
        conn.close()

    def test_only_support_people_take_tickets(self):
        sales = self.person("sel", ["sales"])
        sup = self.person("sup", ["support"])
        conn = self.db()
        tid = H.new_ticket(conn, 1, 1, None, "email", "Question about plans", "", "low")
        with self.assertRaises(H.HQError):
            H.set_ticket(conn, 1, tid, assigned_to=sales)
        H.set_ticket(conn, 1, tid, assigned_to=sup)
        self.assertEqual(H.ticket(conn, tid)["assigned_to"], sup)
        conn.close()

    def test_inbox_pages(self):
        self.person("sup", ["support"])
        c, _ = self.op("sup")
        r = self.send(c, "/hq/support", {"what": "new", "clinic_id": "1", "subject": "Statements look wrong", "priority": "high", "channel": "whatsapp"})
        page = c.get(r.headers["Location"]).get_data(as_text=True)
        self.assertIn("Statements look wrong", page)
        self.send(c, r.headers["Location"], {"what": "reply", "body": "Sent the fix steps on WhatsApp"})
        self.assertIn("Told the clinic", c.get(r.headers["Location"]).get_data(as_text=True))
        self.assertIn("Statements look wrong", c.get("/hq/support").get_data(as_text=True))
        self.assertIn("Statements look wrong", c.get("/hq").get_data(as_text=True) + c.get("/hq/support?which=unassigned").get_data(as_text=True))


class HelpLibrary(HQBase):
    def test_clinics_see_only_published_clinic_articles(self):
        conn = self.db()
        live = H.save_article(conn, 1, None, "Taking a payment", "Open the account.\n\nPress Add payment.", "clinic", True)
        draft = H.save_article(conn, 1, None, "Draft one", "Not ready", "clinic", False)
        team = H.save_article(conn, 1, None, "How we refund", "Team steps", "team", True)
        conn.commit()
        conn.close()
        c = self.login("desk")
        listing = self.page(c, "/help")
        self.assertIn("Taking a payment", listing)
        self.assertNotIn("Draft one", listing)
        self.assertNotIn("How we refund", listing)
        self.assertEqual(self.page(c, f"/help/{live}").count("<p style"), 2)
        for aid in (draft, team):
            self.assertEqual(c.get(f"/help/{aid}").status_code, 404)
        self.assertIn('href="/help"', self.page(c, "/"))


class Sales(HQBase):
    def test_pipeline_lost_reasons_and_handover(self):
        conn = self.db()
        lid = H.save_lead(conn, 1, None, {"name": "Ridge Dental", "stage": "demo_booked", "value": "650", "currency": "GHS"})
        H.save_lead(conn, 1, None, {"name": "Tema Smiles", "stage": "contacted", "value": "1,200", "currency": "GHS"})
        pipe = {s["key"]: s for s in H.pipeline(conn)}
        self.assertEqual((pipe["demo_booked"]["n"], pipe["demo_booked"]["value"]["GHS"]), (1, 65000))
        with self.assertRaises(H.HQError):
            H.save_lead(conn, 1, lid, {"name": "Ridge Dental", "stage": "lost"})
        H.save_lead(conn, 1, lid, {"name": "Ridge Dental", "stage": "won", "value": "650", "currency": "GHS"})
        self.assertTrue(H.lead(conn, lid)["won_at"])
        pid = H.handover(conn, 1, lid)
        self.assertEqual(H.project(conn, pid)["name"], "Ridge Dental")
        with self.assertRaises(H.HQError):
            H.handover(conn, 1, lid)
        conn.close()

    def test_salesperson_figures(self):
        sid = self.person("kwame", ["sales"])
        conn = self.db()
        H.save_lead(conn, sid, None, {"name": "A", "stage": "won", "owner_id": str(sid)})
        H.save_lead(conn, sid, None, {"name": "B", "stage": "contacted", "owner_id": str(sid), "next_on": "2020-01-01"})
        fig = {f["o"]["username"]: f for f in H.sales_figures(conn)}["kwame"]
        self.assertEqual((fig["won"], fig["open"], fig["late"]), (1, 1, 1))
        conn.close()


class Onboarding(HQBase):
    def test_checklist_and_go_live(self):
        conn = self.db()
        pid = H.save_project(conn, 1, None, {"name": "Clinic One", "clinic_id": "1", "go_live_on": "2026-12-01"})
        H.tick(conn, 1, pid, "kickoff", True, "Agreed 1 December")
        self.assertEqual(H.project(conn, pid)["status"], "in_progress")
        self.assertEqual([x["done"] for x in H.projects(conn)], [1])
        H.tick(conn, 1, pid, "golive", True)
        self.assertEqual(H.project(conn, pid)["status"], "live")
        H.tick(conn, 1, pid, "kickoff", False)
        self.assertNotIn("kickoff", H.steps_done(conn, pid))
        with self.assertRaises(H.HQError):
            H.tick(conn, 1, pid, "made-up", True)
        tr = H.add_training(conn, 1, pid, "2026-11-20", "Front desk", "4")
        self.assertEqual(H.training_done(conn, 1, tr), pid)
        conn.close()

    def test_project_page(self):
        self.person("obi", ["onboarding"])
        c, _ = self.op("obi")
        r = self.send(c, "/hq/onboarding", {"name": "New Clinic", "status": "planned"})
        self.send(c, r.headers["Location"], {"what": "tick", "step": "settings", "done": "1"})
        page = c.get(r.headers["Location"]).get_data(as_text=True)
        self.assertIn("Clinic settings", page)
        self.assertIn(">Undo<", page)


class Finance(HQBase):
    def bill(self, conn, number, amount, status="paid", paid_on="2026-10-15", due_on="2026-10-10"):
        conn.execute("""INSERT INTO platform_invoices(clinic_id, number, plan_name, period_from, period_to, amount_p, currency, due_on, status, paid_on)
                        VALUES (1, ?, 'Standard', '2026-10-01', '2026-11-01', ?, 'GHS', ?, ?, ?)""", (number, amount, due_on, status, paid_on if status == "paid" else None))

    def test_vat_with_prices_that_include_it_and_prices_before_it(self):
        conn = self.db()
        self.bill(conn, 1, 115000)
        self.bill(conn, 2, 65000, paid_on="2026-09-30")                       # outside the days asked for
        r = H.vat_report(conn, "2026-10-01", "2026-10-31", "15", True)
        self.assertEqual(r["totals"]["GHS"], {"net": 100000, "vat": 15000, "gross": 115000, "n": 1})
        r = H.vat_report(conn, "2026-10-01", "2026-10-31", "15", False)
        self.assertEqual(r["totals"]["GHS"], {"net": 115000, "vat": 17250, "gross": 132250, "n": 1})
        self.assertIn("2026-10-15,1,Clinic One,GHS,1000.00,150.00,1150.00", H.vat_csv(H.vat_report(conn, "2026-10-01", "2026-10-31", "15")))
        for bad in (("2026-10-31", "2026-10-01", "15"), ("2026-10-01", "2026-10-31", "fifteen"), ("2026-10-01", "2026-10-31", "150")):
            with self.assertRaises(H.HQError):
                H.vat_report(conn, *bad)
        conn.close()

    def test_overdue_and_income(self):
        conn = self.db()
        self.bill(conn, 3, 65000, status="open", due_on="2026-10-01")
        late = H.overdue(conn, datetime.date(2026, 10, 21))
        self.assertEqual((len(late), late[0]["days"]), (1, 20))
        self.assertEqual(H.owed(conn), {"GHS": 65000})
        conn.execute("INSERT INTO plans(name, prices) VALUES ('Standard', '{\"GHS\": 65000}')")
        conn.execute("UPDATE clinics SET plan_id = 1, plan_state = 'active' WHERE id = 1")
        self.assertEqual(H.monthly_income(conn), {"GHS": 65000})
        conn.close()

    def test_finance_pages_and_csv(self):
        self.person("fin", ["finance"])
        c, _ = self.op("fin")
        self.assertEqual(c.get("/hq/finance").status_code, 200)
        r = c.get("/hq/finance/vat?start=2026-10-01&end=2026-10-31&rate=15&included=1&csv=1")
        self.assertEqual(r.mimetype, "text/csv")


class Leadership(HQBase):
    def test_company_page(self):
        conn = self.db()
        H.new_ticket(conn, 1, 1, None, "phone", "Something", "", "urgent")
        H.save_lead(conn, 1, None, {"name": "Won Clinic", "stage": "won"})
        conn.commit()
        x = H.company(conn)
        self.assertEqual((x["support"]["open"], x["won_month"], x["clinics"]), (1, 1, 2))
        conn.close()
        c, _ = self.op("owner")
        self.assertIn("The company today", c.get("/hq/leadership").get_data(as_text=True))


class Address(HQBase):
    def test_hq_answers_only_on_its_own_address(self):
        self.app.config["HQ_HOST"] = "hq.example.com"
        c = self.app.test_client()
        self.assertEqual(c.get("/operator/login", base_url="http://clinic.example.com").status_code, 404)
        self.assertEqual(c.get("/operator/login", base_url="http://hq.example.com").status_code, 200)
        r = c.get("/login", base_url="http://hq.example.com")
        self.assertEqual((r.status_code, r.headers["Location"]), (302, "/hq"))
        self.assertEqual(c.get("/login", base_url="http://clinic.example.com").status_code, 200)


class Checks(HQBase):
    def test_no_hq_table_holds_a_patient(self):
        conn = self.db()
        for t in ("hq_members", "hq_tickets", "hq_ticket_notes", "hq_articles", "hq_leads", "hq_lead_notes", "hq_projects", "hq_project_steps", "hq_trainings"):
            cols = dbmeta.columns(conn, t)
            self.assertTrue(cols, t)
            self.assertNotIn("patient_id", cols, t)
        conn.close()

    def test_every_hq_page_passes_accessibility_checks(self):
        conn = self.db()
        tid = H.new_ticket(conn, 1, 1, None, "phone", "Something", "Body", "normal")
        lid = H.save_lead(conn, 1, None, {"name": "Lead", "stage": "won"})
        pid = H.save_project(conn, 1, None, {"name": "Project", "clinic_id": "1"})
        H.add_training(conn, 1, pid, "2026-11-20", "Front desk", "3")
        aid = H.save_article(conn, 1, None, "Article", "Text", "clinic", True)
        conn.commit()
        conn.close()
        self.person("kofi", ["finance"], heads=["finance"])
        c, _ = self.op("owner")
        for url in ("/hq", "/hq/leadership", "/hq/support", f"/hq/support/{tid}", "/hq/help", "/hq/help/new", f"/hq/help/{aid}", "/hq/finance",
                    "/hq/finance/vat?start=2026-10-01&end=2026-10-31&rate=15", "/hq/sales", f"/hq/sales/{lid}", "/hq/onboarding",
                    f"/hq/onboarding/{pid}", "/hq/departments/finance", "/operator/team", "/operator/security"):
            r = c.get(url)
            self.assertEqual(r.status_code, 200, url)
            self.assertEqual(a11y.check(r.get_data(as_text=True)), [], url)

    def test_hq_in_french(self):
        c, _ = self.op("owner")
        page = c.get("/hq", headers={"Accept-Language": "fr"}).get_data(as_text=True)
        self.assertIn("Siège Dental OS", page)
        self.assertIn("Assistance clients", page)
