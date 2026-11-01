"""v0.27: statement runs and late fees."""
import datetime

from tests.test_v05 import Money
from dental_os.services import billing, statements as S, treatment as TR
from dental_os.services.timeutil import now_local


class Statements(Money):
    def setUp(self):
        super().setUp()
        self.conn = self.db()

    def tearDown(self):
        self.conn.close()

    def c(self):
        return dict(self.conn.execute("SELECT * FROM clinics WHERE id = 1").fetchone())

    def today(self):
        return now_local(self.c()["timezone"]).date()

    def charge(self, pid, code="FILL", days_ago=0):
        pr = TR.add_planned(self.conn, self.c(), 1, pid, self.cid(code))
        TR.mark_done(self.conn, self.c(), 1, pr)
        if days_ago:
            self.conn.execute("UPDATE procedures SET done_on = ? WHERE id = ?", ((self.today() - datetime.timedelta(days=days_ago)).isoformat(), pr))
        self.conn.commit()
        return pr

    def fees_on(self, **kw):
        form = {"late_on": "1", "late_kind": "fixed", "late_value": "20", "late_after_days": "60", "late_min": "50", "late_gap_days": "30"}
        form.update(kw)
        S.save_settings(self.conn, self.c(), 1, form)
        self.conn.commit()


class RunTests(Statements):
    def test_who_is_picked(self):
        self.charge(1, days_ago=100)                      # Ama owes 300, all old
        self.charge(2, "CONS")                             # Kofi owes 100, new
        names = lambda rows: [r["patient"]["first_name"] for r in rows]           # noqa: E731
        self.assertEqual(names(S.candidates(self.conn, self.c())), ["Ama", "Kofi"])
        self.assertEqual(names(S.candidates(self.conn, self.c(), min_balance_p=20000)), ["Ama"])
        self.assertEqual(names(S.candidates(self.conn, self.c(), older_than_days=60)), ["Ama"])
        rid = S.make_run(self.conn, self.c(), 1, "1", "0", "25", False)
        self.assertEqual(len(S.run(self.conn, 1, rid)["lines"]), 2)
        self.assertEqual(S.candidates(self.conn, self.c()), [])                   # both had a statement just now
        self.assertEqual(len(S.candidates(self.conn, self.c(), gap_days=0)), 2)
        with self.assertRaises(S.StatementError):
            S.make_run(self.conn, self.c(), 1, "1", "0", "25", False)
        with self.assertRaises(S.StatementError):
            S.make_run(self.conn, self.c(), 1, "1", "0", "0", True)              # late fees are off

    def test_late_fees_follow_the_rule(self):
        self.charge(1, days_ago=100)
        self.charge(2, "CONS", days_ago=100)
        self.fees_on()
        self.assertEqual(S.late_fee_for(self.conn, self.c(), 1), (2000, None))
        self.assertEqual(S.late_fee_for(self.conn, self.c(), 2), (2000, None))
        self.conn.execute("INSERT INTO payment_plans(clinic_id, patient_id, total_p, instalment_count, frequency, start_on, status, created_by) "
                          "VALUES (1, 2, 10000, 2, 'monthly', ?, 'active', 1)", (self.today().isoformat(),))
        self.assertEqual(S.late_fee_for(self.conn, self.c(), 2)[1], "On an instalment plan.")
        before = billing.totals(self.conn, 1, 1)["balance"]
        rid = S.make_run(self.conn, self.c(), 1, "1", "0", "0", True)
        self.assertEqual(billing.totals(self.conn, 1, 1)["balance"] - before, 2000)
        d = S.run(self.conn, 1, rid)
        self.assertEqual({i["patient_id"]: i["fee_p"] for i in d["lines"]}, {1: 2000, 2: 0})
        self.assertEqual(S.late_fee_for(self.conn, self.c(), 1)[1], "A late fee was added recently.")
        self.fees_on(late_kind="percent", late_value="5", late_gap_days="7", late_cap="10")
        self.conn.execute("UPDATE procedures SET done_on = ? WHERE code_id = (SELECT id FROM procedure_codes WHERE clinic_id = 1 AND code = 'LATE-FEE')",
                          ((self.today() - datetime.timedelta(days=10)).isoformat(),))
        self.assertEqual(S.late_fee_for(self.conn, self.c(), 1), (1000, None))   # 5% of 320 is 16, capped at 10

    def test_settings_are_checked(self):
        for bad in ({"late_on": "1", "late_value": "0"}, {"late_kind": "percent", "late_value": "40"}, {"late_after_days": "0"},
                    {"late_gap_days": "1"}, {"late_kind": "weird"}):
            with self.assertRaises(S.StatementError):
                S.save_settings(self.conn, self.c(), 1, {"late_value": "10"} | bad)

    def test_overdue_pays_off_oldest_first(self):
        self.charge(1, days_ago=100)
        self.charge(1, "CONS", days_ago=5)
        billing.record_payment(self.conn, self.c(), 1, 1, 20000, "cash")
        self.assertEqual(S.overdue_p(self.conn, 1, 1, self.today(), 60), 10000)


class ScreenTests(Statements):
    def test_screens_and_whatsapp_never_carries_the_amount(self):
        self.charge(1, days_ago=100)
        self.charge(2, "CONS")
        self.conn.execute("UPDATE patients SET reminders_ok = 0 WHERE id = 2")
        self.conn.commit()
        mgr = self.login("mgr")
        self.assertIn("Ama", self.page(mgr, "/statements"))
        r = self.post(mgr, "/statements", {"min_balance": "1", "older_than_days": "0", "gap_days": "25", "patient_id": ["1", "2"]}, follow_redirects=True)
        self.assertIn("Statement run made.", r.get_data(as_text=True))
        rid = self.conn.execute("SELECT id FROM statement_runs").fetchone()[0]
        page = self.page(mgr, f"/statements/{rid}/print")
        self.assertEqual(page.count("pagebreak"), 2)
        items = {r["patient_id"]: r["id"] for r in self.conn.execute("SELECT * FROM statement_items")}
        r = self.post(mgr, f"/statements/items/{items[1]}/whatsapp")
        self.assertTrue(r.headers["Location"].startswith("https://wa.me/"))
        self.assertNotIn("300", r.headers["Location"])
        logged = self.conn.execute("SELECT body FROM messages WHERE kind = 'statement'").fetchone()[0]
        self.assertIn("[private link]", logged)
        r = self.post(mgr, f"/statements/items/{items[2]}/whatsapp", follow_redirects=True)
        self.assertIn("has not agreed to messages", r.get_data(as_text=True))
        self.post(mgr, f"/statements/{rid}/printed")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM statement_items WHERE sent_at IS NULL").fetchone()[0], 0)
        self.page(mgr, "/statements/late-fees")
        desk = self.login("desk")
        self.page(desk, f"/statements/{rid}")
        self.assertEqual(desk.get("/statements/late-fees").status_code, 403)
        self.assertEqual(self.post(desk, "/statements", {"add_fees": "1"}).status_code, 403)
        self.assertEqual(self.login("mgr2").get(f"/statements/{rid}").status_code, 404)
