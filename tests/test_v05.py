"""v0.5: treatment plans and quotes, memberships, instalment plans."""
import re
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from dental_os import db as dbmod
from dental_os.services import billing, instalments as INS, memberships as MEM, plans as PL, treatment as TR
from dental_os.services.timeutil import add_months, now_local
from tests.test_v04 import Base


def today():
    return now_local("Africa/Accra").date()


class Money(Base):
    """Clinic one has the starter catalogue with fees. GH cedis, so 100 = 10000 pesewas."""
    FEES = {"CONS": 10000, "FILL": 30000, "CROWN": 150000, "SCALE": 20000}

    def setUp(self):
        super().setUp()
        conn = self.db()
        TR.ensure_catalogue(conn, 1)
        TR.ensure_catalogue(conn, 2)
        for code, fee in self.FEES.items():
            conn.execute("UPDATE procedure_codes SET fee_p = ? WHERE code = ?", (fee, code))
        conn.commit()
        conn.close()

    def cid(self, code, clinic=1):
        return self.db().execute("SELECT id FROM procedure_codes WHERE clinic_id = ? AND code = ?", (clinic, code)).fetchone()[0]

    def clinic(self, n=1):
        return self.db().execute("SELECT * FROM clinics WHERE id = ?", (n,)).fetchone()

    def user(self, name):
        return self.db().execute("SELECT * FROM users WHERE username = ?", (name,)).fetchone()

    def balance(self, pid=1):
        return billing.totals(self.db(), 1, pid)["balance"]

    def new_plan(self, c, title="Option A: crown", pid=1):
        r = self.post(c, f"/patients/{pid}/plans", {"title": title})
        self.assertEqual(r.status_code, 302, r.get_data(as_text=True)[:200])
        return int(r.headers["Location"].rsplit("/", 1)[1])

    def add(self, c, plan_id, code="CROWN", **extra):
        d = {"code_id": str(self.cid(code)), "phase": "1"}
        d.update(extra)
        return self.post(c, f"/plans/{plan_id}/items", d)

    def accept(self, c, plan_id, **extra):
        d = {"signer_name": "Ama Mensah", "relation": "self", "method": "typed", "agreed": "1"}
        d.update(extra)
        return self.post(c, f"/plans/{plan_id}/accept", d)


class TimeTests(unittest.TestCase):
    def test_add_months_keeps_the_day_or_the_last_day(self):
        self.assertEqual(add_months(date(2026, 1, 31), 1), date(2026, 2, 28))
        self.assertEqual(add_months(date(2028, 1, 31), 1), date(2028, 2, 29))
        self.assertEqual(add_months(date(2026, 11, 15), 3), date(2027, 2, 15))
        self.assertEqual(add_months(date(2026, 5, 20), 12), date(2027, 5, 20))

    def test_instalment_schedule_adds_up_exactly(self):
        sched = INS.schedule(10000, 3, "monthly", date(2026, 1, 31))
        self.assertEqual(sum(a for _, a in sched), 10000)
        self.assertEqual([a for _, a in sched], [3334, 3333, 3333])
        self.assertEqual([d for d, _ in sched], [date(2026, 1, 31), date(2026, 2, 28), date(2026, 3, 31)])
        weekly = INS.schedule(5000, 2, "weekly", date(2026, 3, 1))
        self.assertEqual(weekly[1][0], date(2026, 3, 8))
        self.assertEqual(INS.schedule(5000, 2, "fortnightly", date(2026, 3, 1))[1][0], date(2026, 3, 15))

    def test_benefit_year_rolls_on_the_anniversary(self):
        self.assertEqual(MEM.benefit_year("2026-03-10", date(2026, 3, 10)), (date(2026, 3, 10), date(2027, 3, 9)))
        self.assertEqual(MEM.benefit_year("2026-03-10", date(2027, 3, 9)), (date(2026, 3, 10), date(2027, 3, 9)))
        self.assertEqual(MEM.benefit_year("2026-03-10", date(2027, 3, 10)), (date(2027, 3, 10), date(2028, 3, 9)))


class PlanTests(Money):
    def test_plan_with_phases_totals_and_nothing_is_charged_yet(self):
        c = self.login("desk")                     # front desk may run plans
        pid = self.new_plan(c)
        self.add(c, pid, "CROWN", tooth="16", phase="2")
        self.add(c, pid, "SCALE", phase="1")
        self.add(c, pid, "FILL", tooth="26", surfaces=["O", "D"], fee="250", phase="1")
        page = self.page(c, f"/plans/{pid}")
        self.assertIn("Phase 1", page)
        self.assertIn("Phase 2", page)
        rows = PL.items(self.db(), 1, pid)
        self.assertEqual([r["fee_p"] for r in rows], [20000, 25000, 150000])         # phase order, custom fee kept
        self.assertEqual(PL.total(rows), 195000)
        self.assertEqual([n for n, _, _ in PL.phases(rows)], [1, 2])
        # a plan is only an estimate: no procedures, no charges
        self.assertEqual(self.db().execute("SELECT COUNT(*) FROM procedures").fetchone()[0], 0)
        self.assertEqual(self.balance(), 0)

    def test_accept_creates_planned_work_once_and_locks_the_plan(self):
        c = self.login("dent")
        pid = self.new_plan(c)
        self.add(c, pid, "CROWN", tooth="16")
        self.add(c, pid, "SCALE")
        self.post(c, f"/plans/{pid}/present", {"follow_up_days": "5"})
        r = self.accept(c, pid)
        self.assertEqual(r.status_code, 302)
        procs = self.db().execute("SELECT status, fee_p, tooth FROM procedures ORDER BY id").fetchall()
        self.assertEqual([(x["status"], x["fee_p"]) for x in procs], [("planned", 150000), ("planned", 20000)])
        self.assertEqual(self.db().execute("SELECT status FROM treatment_plans").fetchone()[0], "accepted")
        self.assertIn("Accepted on", self.page(c, f"/plans/{pid}"))
        self.assertEqual(self.balance(), 0)                              # still nothing charged until work is done
        # the plan is locked, and accepting twice does nothing more
        self.add(c, pid, "FILL")
        self.accept(c, pid)
        self.assertEqual(self.db().execute("SELECT COUNT(*) FROM procedures").fetchone()[0], 2)
        self.assertEqual(self.db().execute("SELECT COUNT(*) FROM plan_items WHERE removed = 0").fetchone()[0], 2)
        # finishing the work shows as progress on the plan
        self.post(c, "/procedures/1/done")
        page = self.page(c, f"/plans/{pid}")
        self.assertIn("1 of 2", page)
        self.assertEqual(self.balance(), 150000)

    def test_deposit_is_held_as_credit(self):
        c = self.login("desk")
        pid = self.new_plan(c)
        self.add(c, pid, "CROWN")
        self.accept(c, pid, deposit="500", deposit_method="cash")
        pay = self.db().execute("SELECT amount_p, plan_id, purpose FROM payments").fetchone()
        self.assertEqual((pay["amount_p"], pay["plan_id"], pay["purpose"]), (50000, pid, "treatment"))
        self.assertEqual(self.balance(), -50000)                         # credit on the account
        self.assertIn("500.00", self.page(c, f"/plans/{pid}"))

    def test_bad_deposit_undoes_the_whole_acceptance(self):
        c = self.login("desk")
        pid = self.new_plan(c)
        self.add(c, pid, "CROWN")
        self.accept(c, pid, deposit="500", deposit_method="mobile_money", deposit_reference="")    # needs a reference
        self.assertEqual(self.db().execute("SELECT status FROM treatment_plans").fetchone()[0], "draft")
        self.assertEqual(self.db().execute("SELECT COUNT(*) FROM procedures").fetchone()[0], 0)
        self.assertEqual(self.db().execute("SELECT COUNT(*) FROM payments").fetchone()[0], 0)

    def test_acceptance_rules(self):
        c = self.login("desk")
        pid = self.new_plan(c)
        self.add(c, pid, "CROWN")
        self.accept(c, pid, agreed="")
        self.accept(c, pid, signer_name="")
        self.assertEqual(self.db().execute("SELECT status FROM treatment_plans").fetchone()[0], "draft")
        # an empty plan cannot be presented or accepted
        empty = self.new_plan(c, "Empty")
        self.post(c, f"/plans/{empty}/present", {})
        self.accept(c, empty)
        self.assertEqual(self.db().execute("SELECT status FROM treatment_plans WHERE id = ?", (empty,)).fetchone()[0], "draft")
        # a child cannot agree for themselves
        kid = self.new_plan(c, "Child plan", pid=2)
        self.add(c, kid, "SCALE")
        self.accept(c, kid, signer_name="Kofi Young")
        self.assertEqual(self.db().execute("SELECT status FROM treatment_plans WHERE id = ?", (kid,)).fetchone()[0], "draft")
        self.accept(c, kid, signer_name="Esi Young", relation="parent")
        self.assertEqual(self.db().execute("SELECT status FROM treatment_plans WHERE id = ?", (kid,)).fetchone()[0], "accepted")

    def test_decline_reopen_and_follow_up_list(self):
        c = self.login("desk")
        pid = self.new_plan(c)
        self.add(c, pid, "CROWN")
        self.post(c, f"/plans/{pid}/present", {"follow_up_days": "3"})
        board = self.page(c, "/plans")
        self.assertIn("Option A: crown", board)
        self.assertIn("1,500.00", board)
        self.assertNotIn(">Due<", board)
        conn = self.db()
        conn.execute("UPDATE treatment_plans SET follow_up_on = ?", ((today() - timedelta(days=1)).isoformat(),))
        conn.commit()
        self.assertIn(">Due<", self.page(c, "/plans"))
        self.post(c, f"/plans/{pid}/followup", {"note": "Rang, no answer", "next_in_days": "10"})
        row = self.db().execute("SELECT follow_up_on FROM treatment_plans").fetchone()[0]
        self.assertEqual(row, (today() + timedelta(days=10)).isoformat())
        self.assertIn("Rang, no answer", self.page(c, f"/plans/{pid}"))
        self.post(c, f"/plans/{pid}/followup", {"note": "", "next_in_days": "10"})              # a note is needed
        self.post(c, f"/plans/{pid}/decline", {"reason": ""})
        self.assertEqual(self.db().execute("SELECT status FROM treatment_plans").fetchone()[0], "presented")
        self.post(c, f"/plans/{pid}/decline", {"reason": "Too expensive"})
        self.assertEqual(self.db().execute("SELECT status FROM treatment_plans").fetchone()[0], "declined")
        self.assertNotIn("Option A: crown", self.page(c, "/plans"))
        self.accept(c, pid)                                                                      # declined plans cannot be accepted
        self.assertEqual(self.db().execute("SELECT COUNT(*) FROM procedures").fetchone()[0], 0)
        self.post(c, f"/plans/{pid}/reopen")
        self.assertIn("Option A: crown", self.page(c, "/plans"))

    def test_several_options_are_independent(self):
        c = self.login("dent")
        a, b = self.new_plan(c, "Option A: crown"), self.new_plan(c, "Option B: filling")
        self.add(c, a, "CROWN")
        self.add(c, b, "FILL")
        self.accept(c, b)
        self.assertEqual(self.db().execute("SELECT status FROM treatment_plans WHERE id = ?", (a,)).fetchone()[0], "draft")
        self.assertEqual(self.db().execute("SELECT COUNT(*) FROM procedures").fetchone()[0], 1)

    def test_remove_and_edit_lines(self):
        c = self.login("dent")
        pid = self.new_plan(c)
        self.add(c, pid, "CROWN")
        self.post(c, "/plan-items/1/update", {"fee": "1200", "phase": "3"})
        row = self.db().execute("SELECT fee_p, phase FROM plan_items WHERE id = 1").fetchone()
        self.assertEqual((row["fee_p"], row["phase"]), (120000, 3))
        self.post(c, "/plan-items/1/update", {"fee": "-5", "phase": "3"})
        self.post(c, "/plan-items/1/update", {"fee": "100", "phase": "12"})
        self.assertEqual(self.db().execute("SELECT fee_p FROM plan_items WHERE id = 1").fetchone()[0], 120000)
        self.post(c, "/plan-items/1/remove")
        self.assertEqual(PL.items(self.db(), 1, pid), [])
        self.assertEqual(self.db().execute("SELECT COUNT(*) FROM plan_items").fetchone()[0], 1)    # kept, just hidden

    def test_bad_lines_refused(self):
        c = self.login("dent")
        pid = self.new_plan(c)
        for bad in ({"tooth": "19"}, {"surfaces": ["Z"]}, {"phase": "0"}, {"phase": "x"}, {"fee": "abc"}, {"code_id": "9999"}):
            self.add(c, pid, "CROWN", **bad)
        self.assertEqual(self.db().execute("SELECT COUNT(*) FROM plan_items").fetchone()[0], 0)

    def test_quote_page_and_clinic_isolation(self):
        c = self.login("dent")
        pid = self.new_plan(c)
        self.add(c, pid, "CROWN", tooth="16")
        quote = self.page(c, f"/plans/{pid}/quote")
        self.assertIn("Treatment quote", quote)
        self.assertIn("1,500.00", quote)
        self.assertIn("estimate", quote)
        o = self.login("mgr2")
        for url in (f"/plans/{pid}", f"/plans/{pid}/quote"):
            self.assertEqual(o.get(url).status_code, 404, url)
        for url in (f"/plans/{pid}/accept", f"/plans/{pid}/present", f"/plans/{pid}/decline", "/plan-items/1/remove", "/plan-items/1/update"):
            self.assertEqual(self.post(o, url, {"signer_name": "x", "reason": "xxx"}).status_code, 404, url)
        self.assertEqual(o.get("/patients/1/plans").status_code, 404)
        self.assertNotIn("Option A", self.page(o, "/plans"))

    def test_all_plan_screens_open(self):
        c = self.login("mgr")
        pid = self.new_plan(c)
        self.add(c, pid, "CROWN")
        self.post(c, f"/plans/{pid}/present", {})
        for url in ("/patients/1/plans", f"/plans/{pid}", f"/plans/{pid}/quote", "/plans", "/patients/1/instalments", "/patients/1/membership", "/memberships"):
            self.page(c, url)


class MembershipTests(Money):
    def setUp(self):
        super().setUp()
        self.m = self.login("mgr")
        conn = self.db()
        self.plan_id = MEM.save_plan(conn, self.clinic(), 1, None, "Gold", "Two check-ups a year", 12000, "monthly", 10)
        MEM.set_benefit(conn, self.clinic(), 1, self.plan_id, self.cid("CONS"), 2)
        conn.commit()

    def enrol(self, c=None, **extra):
        d = {"plan_id": str(self.plan_id), "start_on": today().isoformat()}
        d.update(extra)
        return self.post(c or self.m, "/patients/1/membership", d)

    def do(self, code, c=None, pid=1):
        c = c or self.m
        self.post(c, f"/patients/{pid}/treatment", {"code_id": str(self.cid(code))})
        proc = self.db().execute("SELECT id FROM procedures ORDER BY id DESC LIMIT 1").fetchone()[0]
        self.post(c, f"/procedures/{proc}/done")
        return proc

    def test_enrol_first_fee_does_not_pay_for_treatment(self):
        self.enrol(pay_method="cash")
        m = MEM.current_for(self.db(), 1, 1)
        self.assertEqual((m["plan_name"], m["fee_p"], m["status"]), ("Gold", 12000, "active"))
        self.assertEqual(self.db().execute("SELECT status FROM membership_charges").fetchone()[0], "paid")
        pay = self.db().execute("SELECT purpose, amount_p FROM payments").fetchone()
        self.assertEqual((pay["purpose"], pay["amount_p"]), ("membership", 12000))
        self.assertEqual(self.balance(), 0)                              # the fee is not a credit against treatment
        acct = self.page(self.m, "/patients/1/account")
        self.assertIn("Membership fee", acct)
        taken = billing.daily_takings(self.db(), 1, today().isoformat())
        self.assertEqual(taken["total"], 12000)                          # but it is in the day's takings
        self.assertEqual(MEM.monthly_value(MEM.members(self.db(), 1)), 12000)

    def test_one_open_membership_per_patient(self):
        self.enrol()
        self.enrol()
        self.assertEqual(self.db().execute("SELECT COUNT(*) FROM memberships").fetchone()[0], 1)

    def test_included_service_then_discount(self):
        self.enrol(pay_method="cash")
        a, b = self.do("CONS"), self.do("CONS")
        self.assertEqual(self.balance(), 0)                              # two included check-ups, both free
        status = MEM.benefit_status(self.db(), self.clinic(), MEM.current_for(self.db(), 1, 1))
        self.assertEqual(status["items"][0]["left"], 0)
        c = self.do("CONS")                                              # no more included: 10% off
        self.assertEqual(self.balance(), 9000)
        d = self.do("FILL")                                              # not included: 10% off 300.00
        self.assertEqual(self.balance(), 9000 + 27000)
        self.assertEqual(self.db().execute("SELECT COUNT(*) FROM membership_uses WHERE kind = 'included'").fetchone()[0], 2)
        self.assertIn("Membership discount 10%", self.page(self.m, "/patients/1/account"))
        self.assertIn("Included this year", self.page(self.m, "/patients/1/membership"))
        # marking done again does not apply a second time
        self.post(self.m, f"/procedures/{d}/done")
        self.assertEqual(self.balance(), 9000 + 27000)

    def test_void_gives_the_benefit_back(self):
        self.enrol(pay_method="cash")
        proc = self.do("CONS")
        self.assertEqual(self.balance(), 0)
        self.post(self.m, f"/procedures/{proc}/void", {"reason": "Entered by mistake"})
        self.assertEqual(self.balance(), 0)                              # charge voided, benefit reversed: no net effect
        self.assertEqual(self.db().execute("SELECT reversed FROM membership_uses").fetchone()[0], 1)
        status = MEM.benefit_status(self.db(), self.clinic(), MEM.current_for(self.db(), 1, 1))
        self.assertEqual(status["items"][0]["left"], 2)

    def test_non_members_and_other_patients_are_not_discounted(self):
        self.enrol()
        self.post(self.m, "/patients/2/treatment", {"code_id": str(self.cid("FILL"))})
        self.post(self.m, "/procedures/1/done")
        self.assertEqual(billing.totals(self.db(), 1, 2)["balance"], 30000)

    def test_renewals_create_fees_and_lapse_after_the_grace_period(self):
        self.enrol(pay_method="cash")
        conn = self.db()
        old = (today() - timedelta(days=20)).isoformat()
        conn.execute("UPDATE memberships SET next_due_on = ?", (old,))
        conn.commit()
        created = MEM.run_renewals(conn, self.clinic())
        conn.commit()
        self.assertGreaterEqual(created, 1)
        self.assertEqual(MEM.run_renewals(conn, self.clinic()), 0)       # running again adds nothing
        m = MEM.current_for(conn, 1, 1)
        self.assertEqual(m["status"], "lapsed")                          # 20 days late is past the 14 day grace
        self.assertGreater(m["next_due_on"], today().isoformat())
        # benefits pause while lapsed
        self.do("CONS")
        self.assertEqual(self.balance(), 10000)
        self.assertEqual(len(MEM.attention(conn, self.clinic())), 1)
        self.assertIn("Fees to chase", self.page(self.m, "/memberships"))
        # paying brings it back
        charge = conn.execute("SELECT id FROM membership_charges WHERE status = 'due'").fetchone()[0]
        self.post(self.m, f"/membership-charges/{charge}/pay", {"method": "cash"})
        self.assertEqual(MEM.current_for(self.db(), 1, 1)["status"], "active")
        self.assertEqual(self.db().execute("SELECT COUNT(*) FROM payments WHERE purpose = 'membership'").fetchone()[0], 2)

    def test_failed_fee_needs_a_note_and_stays_on_the_list(self):
        self.enrol()
        charge = self.db().execute("SELECT id FROM membership_charges").fetchone()[0]
        self.post(self.m, f"/membership-charges/{charge}/fail", {"note": ""})
        self.assertEqual(self.db().execute("SELECT status FROM membership_charges").fetchone()[0], "due")
        self.post(self.m, f"/membership-charges/{charge}/fail", {"note": "Mobile money not received"})
        self.assertEqual(self.db().execute("SELECT status FROM membership_charges").fetchone()[0], "failed")
        self.assertEqual(len(MEM.attention(self.db(), self.clinic())), 1)
        self.post(self.m, f"/membership-charges/{charge}/pay", {"method": "mobile_money", "reference": ""})      # needs a reference
        self.assertEqual(self.db().execute("SELECT status FROM membership_charges").fetchone()[0], "failed")
        self.post(self.m, f"/membership-charges/{charge}/pay", {"method": "mobile_money", "reference": "MM123"})
        self.assertEqual(self.db().execute("SELECT status FROM membership_charges").fetchone()[0], "paid")

    def test_cancel_stops_benefits_and_drops_unpaid_fees(self):
        self.enrol()
        mid = MEM.current_for(self.db(), 1, 1)["id"]
        self.post(self.m, f"/memberships/{mid}/cancel", {"reason": ""})
        self.assertEqual(MEM.current_for(self.db(), 1, 1)["status"], "active")
        self.post(self.m, f"/memberships/{mid}/cancel", {"reason": "Moving away"})
        self.assertIsNone(MEM.current_for(self.db(), 1, 1))
        self.assertEqual(self.db().execute("SELECT status FROM membership_charges").fetchone()[0], "waived")
        self.do("CONS")
        self.assertEqual(self.balance(), 10000)
        self.enrol()                                                     # can join again afterwards
        self.assertEqual(self.db().execute("SELECT COUNT(*) FROM memberships").fetchone()[0], 2)

    def test_plan_changes_do_not_change_existing_members(self):
        self.enrol()
        self.post(self.m, "/membership-plans", {"plan_id": str(self.plan_id), "name": "Gold", "fee": "200", "cycle": "yearly",
                                                "discount_pct": "50", "active": "1"})
        m = MEM.current_for(self.db(), 1, 1)
        self.assertEqual((m["fee_p"], m["cycle"], m["discount_pct"]), (12000, "monthly", 10))
        self.assertEqual(self.db().execute("SELECT fee_p FROM membership_plans").fetchone()[0], 20000)

    def test_plan_validation(self):
        for bad in ({"fee": "0"}, {"fee": "abc"}, {"cycle": "weekly"}, {"discount_pct": "101"}, {"name": ""}, {"name": "gold"}):
            d = {"name": "Silver", "fee": "50", "cycle": "monthly", "discount_pct": "5", "active": "1"}
            d.update(bad)
            self.post(self.m, "/membership-plans", d)
        self.assertEqual(self.db().execute("SELECT COUNT(*) FROM membership_plans").fetchone()[0], 1)

    def test_permissions_and_isolation(self):
        self.enrol()
        d = self.login("desk")
        self.assertEqual(d.get("/membership-plans").status_code, 403)
        self.assertEqual(self.post(d, "/memberships/1/cancel", {"reason": "xxx"}).status_code, 403)
        self.assertEqual(self.post(d, "/membership-charges/1/waive", {"note": "xxx"}).status_code, 403)
        self.page(d, "/memberships")
        self.page(d, "/patients/1/membership")
        o = self.login("mgr2")
        self.assertEqual(o.get("/patients/1/membership").status_code, 404)
        self.assertEqual(self.post(o, "/membership-charges/1/pay", {"method": "cash"}).status_code, 404)
        self.assertEqual(self.post(o, "/memberships/1/cancel", {"reason": "xxx"}).status_code, 404)
        self.assertNotIn("Mensah", self.page(o, "/memberships"))
        # clinic two cannot enrol using clinic one's plan
        conn = self.db()
        with self.assertRaises(MEM.MembershipError):
            MEM.enrol(conn, self.clinic(2), 5, 3, self.plan_id)

    def test_waive_by_manager(self):
        self.enrol()
        self.post(self.m, "/membership-charges/1/waive", {"note": "Goodwill"})
        self.assertEqual(self.db().execute("SELECT status FROM membership_charges").fetchone()[0], "waived")

    def test_run_renewals_command(self):
        self.enrol(pay_method="cash")
        conn = self.db()
        conn.execute("UPDATE memberships SET next_due_on = ?", (today().isoformat(),))
        conn.commit()
        res = self.app.test_cli_runner().invoke(args=["run-renewals"])
        self.assertEqual(res.exit_code, 0, res.output)
        self.assertIn("1 membership fees created", res.output)
        self.assertEqual(self.db().execute("SELECT COUNT(*) FROM membership_charges").fetchone()[0], 2)


class InstalmentTests(Money):
    def setUp(self):
        super().setUp()
        self.m = self.login("desk")

    def create(self, **extra):
        d = {"total": "1000", "count": "4", "frequency": "monthly", "start_on": today().isoformat()}
        d.update(extra)
        return self.post(self.m, "/patients/1/instalments", d)

    def test_create_pay_and_complete(self):
        self.create()
        rows = self.db().execute("SELECT amount_p FROM instalments ORDER BY due_on").fetchall()
        self.assertEqual([r[0] for r in rows], [25000] * 4)
        r = self.post(self.m, "/instalments/1/pay", {"method": "cash"})
        self.assertEqual(r.status_code, 302)
        self.assertIn("/receipt", r.headers["Location"])
        self.assertEqual(self.balance(), -25000)                         # an ordinary payment on the account
        for i in (2, 3, 4):
            self.post(self.m, f"/instalments/{i}/pay", {"method": "cash"})
        self.assertEqual(self.db().execute("SELECT status FROM payment_plans").fetchone()[0], "completed")
        self.post(self.m, "/instalments/1/pay", {"method": "cash"})      # cannot pay twice
        self.assertEqual(self.db().execute("SELECT COUNT(*) FROM payments").fetchone()[0], 4)

    def test_reference_needed_for_mobile_money(self):
        self.create()
        self.post(self.m, "/instalments/1/pay", {"method": "mobile_money", "reference": ""})
        self.assertEqual(self.db().execute("SELECT status FROM instalments WHERE id = 1").fetchone()[0], "due")

    def test_overdue_list(self):
        self.create(start_on=(today() - timedelta(days=5)).isoformat())
        self.assertEqual(len(INS.overdue(self.db(), self.clinic())), 1)
        self.assertIn("5 days late", self.page(self.m, "/plans"))

    def test_bad_plans_refused(self):
        for bad in ({"total": "0"}, {"total": "abc"}, {"count": "1"}, {"count": "40"}, {"frequency": "daily"},
                    {"start_on": "2020-01-01"}, {"start_on": "garbage"}, {"treatment_plan_id": "999"}):
            self.create(**bad)
        self.assertEqual(self.db().execute("SELECT COUNT(*) FROM payment_plans").fetchone()[0], 0)

    def test_cancel_is_manager_only_and_keeps_paid_instalments(self):
        self.create()
        self.post(self.m, "/instalments/1/pay", {"method": "cash"})
        self.assertEqual(self.post(self.m, "/payment-plans/1/cancel", {"reason": "xxx"}).status_code, 403)
        mg = self.login("mgr")
        self.post(mg, "/payment-plans/1/cancel", {"reason": ""})
        self.assertEqual(self.db().execute("SELECT status FROM payment_plans").fetchone()[0], "active")
        self.post(mg, "/payment-plans/1/cancel", {"reason": "Paid in full instead"})
        self.assertEqual(self.db().execute("SELECT status FROM payment_plans").fetchone()[0], "cancelled")
        status = [r[0] for r in self.db().execute("SELECT status FROM instalments ORDER BY id")]
        self.assertEqual(status, ["paid", "waived", "waived", "waived"])
        self.assertIn("Cancelled", self.page(mg, "/patients/1/instalments"))

    def test_isolation(self):
        self.create()
        o = self.login("mgr2")
        self.assertEqual(o.get("/patients/1/instalments").status_code, 404)
        self.assertEqual(self.post(o, "/instalments/1/pay", {"method": "cash"}).status_code, 404)
        self.assertEqual(self.post(o, "/payment-plans/1/cancel", {"reason": "xxx"}).status_code, 404)

    def test_links_to_an_accepted_plan_only_for_the_same_patient(self):
        c = self.login("dent")
        pid = int(self.post(c, "/patients/2/plans", {"title": "Kofi plan"}).headers["Location"].rsplit("/", 1)[1])
        self.create(treatment_plan_id=str(pid))                          # that plan belongs to patient 2, not 1
        self.assertEqual(self.db().execute("SELECT COUNT(*) FROM payment_plans").fetchone()[0], 0)


class UpgradeTests(unittest.TestCase):
    def test_old_payments_table_gains_the_new_columns(self):
        tmp = tempfile.TemporaryDirectory()
        path = str(Path(tmp.name) / "old.sqlite")
        raw = sqlite3.connect(path)
        raw.executescript("""CREATE TABLE payments(id INTEGER PRIMARY KEY, clinic_id INTEGER, patient_id INTEGER, amount_p INTEGER,
            method TEXT, reference TEXT, note TEXT, receipt_no INTEGER, received_on TEXT, received_by INTEGER,
            voided INTEGER NOT NULL DEFAULT 0, void_reason TEXT, voided_by INTEGER, created_at TEXT);
            INSERT INTO payments(id, clinic_id, patient_id, amount_p, method, receipt_no, received_on) VALUES (1,1,1,500,'cash',1,'2026-01-01');""")
        raw.commit()
        raw.close()
        conn = dbmod.connect(path)
        dbmod.init_schema(conn)
        dbmod.init_schema(conn)                                          # twice is harmless
        row = conn.execute("SELECT purpose, plan_id FROM payments").fetchone()
        self.assertEqual((row["purpose"], row["plan_id"]), ("treatment", None))        # old payments count as treatment payments


if __name__ == "__main__":
    unittest.main()
