"""v0.73: the morning huddle with goals and pace, notes waiting to be signed, and membership savings on quotes for patients with
no insurance and no membership."""
import datetime

from tests.test_v04 import Base
from tests import a11y
from dental_os.services import huddle as HU, memberships as MEM, notes as N, plans as PL


class HBase(Base):
    def clinic(self, conn):
        return conn.execute("SELECT * FROM clinics WHERE id = 1").fetchone()

    def user(self, conn, name):
        return conn.execute("SELECT * FROM users WHERE username = ?", (name,)).fetchone()

    def code(self, conn, n=0):
        return conn.execute("SELECT id FROM procedure_codes WHERE clinic_id = 1 ORDER BY id LIMIT 1 OFFSET ?", (n,)).fetchone()[0]

    def procedure(self, conn, status, fee, on, provider=1, pid=1):
        conn.execute("""INSERT INTO procedures(clinic_id, patient_id, code_id, status, fee_p, provider_id, planned_on, done_on)
                        VALUES (1, ?, ?, ?, ?, ?, ?, ?)""", (pid, self.code(conn), status, fee, provider, on, on if status == "done" else None))


class Goals(HBase):
    def test_working_days_leave_out_weekends_and_closed_days(self):
        conn = self.db()
        conn.execute("UPDATE clinics SET working_days = '1,2,3,4,5' WHERE id = 1")
        conn.execute("INSERT INTO closed_days(clinic_id, day, name) VALUES (1, '2026-11-02', 'Holiday')")
        c = self.clinic(conn)
        self.assertEqual(HU.working_days(conn, c, datetime.date(2026, 11, 1), datetime.date(2026, 11, 30)), 20)   # 21 weekdays less one
        conn.close()

    def test_save_check_and_copy(self):
        conn = self.db()
        c = self.clinic(conn)
        HU.save_goal(conn, c, 1, "2026-10", None, {"production_p": 10000000, "collections_p": 9000000, "new_patients": 30, "acceptance_pct": 60})
        HU.save_goal(conn, c, 1, "2026-10", 1, {"production_p": 5000000})
        HU.save_goal(conn, c, 1, "2026-10", None, {"production_p": 12000000, "collections_p": None, "new_patients": None, "acceptance_pct": None})
        self.assertEqual(len(HU.goals(conn, 1, "2026-10")), 2)                     # saving again replaces, never doubles
        self.assertEqual(HU.goals(conn, 1, "2026-10")[None]["production_p"], 12000000)
        for bad in ({"acceptance_pct": 120}, {"new_patients": -1}):
            with self.assertRaises(HU.GoalError):
                HU.save_goal(conn, c, 1, "2026-10", None, bad)
        with self.assertRaises(HU.GoalError):
            HU.save_goal(conn, c, 1, "2026-13", None, {"new_patients": 1})
        with self.assertRaises(LookupError):
            HU.save_goal(conn, c, 1, "2026-10", 99, {"production_p": 1})
        self.assertEqual(HU.copy_goals(conn, c, 1, "2026-10", "2026-11"), 2)
        self.assertEqual(HU.copy_goals(conn, c, 1, "2026-10", "2026-11"), 0)       # kept, not overwritten
        HU.save_goal(conn, c, 1, "2026-11", 1, {f: None for f in HU.FIELDS})       # empty removes
        self.assertNotIn(1, HU.goals(conn, 1, "2026-11"))
        conn.close()

    def test_pace_ahead_and_behind(self):
        conn = self.db()
        conn.execute("UPDATE clinics SET working_days = '1,2,3,4,5' WHERE id = 1")
        c = self.clinic(conn)
        HU.save_goal(conn, c, 1, "2026-11", None, {"production_p": 2100000})       # 21 working days: 100,000 a day
        HU.save_goal(conn, c, 1, "2026-11", 1, {"production_p": 1050000})
        self.procedure(conn, "done", 600000, "2026-11-04")
        p = HU.progress(conn, c, datetime.date(2026, 11, 6))                        # 5 working days gone: pace 500,000
        prod = next(r for r in p["clinic"] if r["field"] == "production_p")
        self.assertEqual((p["days_done"], p["days_all"], prod["pace"], prod["actual"], prod["ahead"]), (5, 21, 500000, 600000, True))
        self.assertTrue(p["dentists"][0]["ahead"])
        self.assertFalse(next(r for r in HU.progress(conn, c, datetime.date(2026, 11, 13))["clinic"] if r["field"] == "production_p")["ahead"])
        conn.close()

    def test_goals_page(self):
        c = self.login("mgr")
        self.post(c, "/reports/goals", {"month": "2026-11", "clinic_production_p": "25,000", "clinic_new_patients": "30", "dentist_1": "12000"})
        conn = self.db()
        g = HU.goals(conn, 1, "2026-11")
        self.assertEqual((g[None]["production_p"], g[None]["new_patients"], g[1]["production_p"]), (2500000, 30, 1200000))
        conn.close()
        page = self.page(c, "/reports/goals?month=2026-11")
        self.assertIn("November 2026", page)
        self.assertEqual(self.login("dent").get("/reports/goals").status_code, 403)


class Huddle(HBase):
    def setup_day(self):
        conn = self.db()
        today = HU._today(self.clinic(conn))
        chair = conn.execute("SELECT id FROM chairs WHERE clinic_id = 1 LIMIT 1").fetchone()
        chair = chair[0] if chair else conn.execute("INSERT INTO chairs(clinic_id, name) VALUES (1, 'Chair 1')").lastrowid
        conn.execute("""INSERT INTO appointments(clinic_id, patient_id, provider_id, chair_id, start_at, duration_min, status)
                        VALUES (1, 1, 1, ?, ?, 30, 'scheduled')""", (chair, f"{today} 09:00"))
        self.procedure(conn, "planned", 50000, today.isoformat())
        self.procedure(conn, "done", 30000, (today - datetime.timedelta(days=40)).isoformat())          # owed: nothing paid
        conn.execute("INSERT INTO patient_alerts(clinic_id, patient_id, kind, severity, text) VALUES (1, 1, 'allergy', 'critical', 'Penicillin')")
        conn.commit()
        conn.close()
        return today

    def test_todays_patients_carry_what_to_do(self):
        self.setup_day()
        conn = self.db()
        h = HU.huddle(conn, self.clinic(conn), self.user(conn, "mgr"))
        x = h["patients"][0]
        self.assertEqual((x["planned"], x["balance_p"], x["alerts"]), (1, 30000, 1))
        self.assertEqual((h["owed_today_p"], h["planned_today"]), (30000, 1))
        conn.close()

    def test_money_only_for_managers(self):
        self.setup_day()
        mgr = self.page(self.login("mgr"), "/huddle")
        self.assertIn("Owes", mgr)
        self.assertIn("The month so far", mgr)
        for who in ("dent", "desk"):
            page = self.page(self.login(who), "/huddle")
            self.assertIn("Medical alert", page)
            self.assertIn("planned, not booked", page)
            self.assertNotIn("Owes", page)
            self.assertNotIn("Production", page)

    def test_last_working_day_skips_the_weekend(self):
        conn = self.db()
        conn.execute("UPDATE clinics SET working_days = '1,2,3,4,5' WHERE id = 1")
        self.assertEqual(HU.last_working_day(conn, self.clinic(conn), datetime.date(2026, 11, 9)), datetime.date(2026, 11, 6))
        conn.close()


class Unsigned(HBase):
    def draft(self, user, days_ago=0):
        conn = self.db()
        nid = N.create_draft(conn, self.clinic(conn), self.user(conn, user), 1, "exam", {"f0": "Check-up"})
        if days_ago:
            conn.execute("UPDATE clinical_notes SET created_at = datetime('now', ?) WHERE id = ?", (f"-{days_ago} days", nid))
        conn.commit()
        conn.close()
        return nid

    def test_each_signer_sees_their_own_and_managers_see_all(self):
        self.draft("dent", 3)
        self.draft("mgr")
        conn = self.db()
        c = self.clinic(conn)
        mine = HU.unsigned(conn, c, self.user(conn, "dent"))
        self.assertEqual(len(mine), 1)
        self.assertTrue(mine[0]["old"])
        self.assertEqual(HU.unsigned_counts(conn, c, self.user(conn, "mgr")), {"all": 2, "old": 1})
        conn.close()
        self.assertIn("Notes waiting to be signed", self.page(self.login("dent"), "/"))
        self.assertIn("older than two days", self.page(self.login("dent"), "/notes/unsigned") + self.page(self.login("dent"), "/"))
        self.assertNotIn("Notes waiting to be signed", self.page(self.login("desk"), "/"))
        self.assertEqual(self.login("desk").get("/notes/unsigned").status_code, 403)

    def test_signing_clears_it(self):
        nid = self.draft("dent")
        conn = self.db()
        N.sign(conn, self.clinic(conn), self.user(conn, "dent"), nid)
        conn.commit()
        self.assertEqual(HU.unsigned(conn, self.clinic(conn)), [])
        conn.close()


class Savings(HBase):
    def plan_with(self, conn, fees):
        c = self.clinic(conn)
        pid = PL.create(conn, c, 1, 1, "Fillings")
        for i, fee in enumerate(fees):
            PL.add_item(conn, c, 1, pid, self.code(conn, i % 2), fee_p=fee)
        return pid

    def membership(self, conn, discount=10, include_first=False):
        c = self.clinic(conn)
        mp = MEM.save_plan(conn, c, 1, None, "Smile Club", "Two check-ups a year", 10000, "monthly", discount)
        if include_first:
            MEM.set_benefit(conn, c, 1, mp, self.code(conn, 0), 1)
        return mp

    def test_offer_counts_included_work_once_and_discounts_the_rest(self):
        conn = self.db()
        self.membership(conn, 10, include_first=True)
        plan = self.plan_with(conn, [50000, 50000, 20000])                        # codes 0, 1, 0
        offers = HU.membership_offers(conn, self.clinic(conn), 1, PL.items(conn, 1, plan))
        o = offers[0]
        # the first code is included once (50,000 free); the second code-0 line (20,000) and code-1 (50,000) get 10% off
        self.assertEqual((o["saved_p"], o["per_year_p"], o["net_p"]), (57000, 120000, -63000))
        conn.close()

    def test_no_offer_with_cover_a_membership_or_the_setting_off(self):
        conn = self.db()
        mp = self.membership(conn, 20)
        plan = self.plan_with(conn, [100000])
        c = self.clinic(conn)
        rows = PL.items(conn, 1, plan)
        self.assertTrue(HU.membership_offers(conn, c, 1, rows))
        conn.execute("UPDATE clinics SET offer_membership = 0 WHERE id = 1")
        self.assertEqual(HU.membership_offers(conn, self.clinic(conn), 1, rows), [])
        conn.execute("UPDATE clinics SET offer_membership = 1 WHERE id = 1")
        conn.execute("INSERT INTO payers(clinic_id, name) VALUES (1, 'NHIS')")
        conn.execute("INSERT INTO patient_cover(clinic_id, patient_id, payer_id) VALUES (1, 1, 1)")
        self.assertEqual(HU.membership_offers(conn, self.clinic(conn), 1, rows), [])
        conn.execute("UPDATE patient_cover SET active = 0")
        MEM.enrol(conn, self.clinic(conn), 1, 1, mp)
        self.assertEqual(HU.membership_offers(conn, self.clinic(conn), 1, rows), [])
        conn.close()

    def test_quote_shows_the_offer_with_a_whatsapp_code(self):
        conn = self.db()
        conn.execute("UPDATE clinics SET whatsapp = '0244000111' WHERE id = 1")
        self.membership(conn, 20)
        plan = self.plan_with(conn, [100000])
        conn.commit()
        conn.close()
        c = self.login("mgr")
        quote = self.page(c, f"/plans/{plan}/quote")
        self.assertIn("Save with a membership plan", quote)
        self.assertIn("<svg", quote)
        self.assertEqual(a11y.check(quote), [])
        self.assertIn("No insurance or membership: offer a plan", self.page(c, f"/plans/{plan}"))
        self.post(c, "/membership-plans", {"action": "offer"})                       # box left unticked: switch it off
        self.assertNotIn("Save with a membership plan", self.page(c, f"/plans/{plan}/quote"))

    def test_join_link_only_with_a_whatsapp_number(self):
        conn = self.db()
        self.assertIsNone(HU.join_link(self.clinic(conn), "Smile Club"))
        conn.execute("UPDATE clinics SET whatsapp = '0244000111' WHERE id = 1")
        self.assertTrue(HU.join_link(self.clinic(conn), "Smile Club").startswith("https://wa.me/233244000111?text="))
        conn.close()


class Pages(HBase):
    def test_pages_pass_accessibility_checks(self):
        Huddle.setup_day(self)
        mgr = self.login("mgr")
        for url in ("/huddle", "/reports/goals", "/notes/unsigned", "/"):
            self.assertEqual(a11y.check(self.page(mgr, url)), [], url)
