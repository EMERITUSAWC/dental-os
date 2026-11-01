"""v0.8: key numbers, groups of clinics, lab cases, stock, the API and webhooks."""
import http.server
import json
import threading
import unittest
from datetime import date, datetime, timedelta

from dental_os import api as api_module
from dental_os import db as dbmod
from dental_os.services import (apikeys, kpi, labs as LAB, orgs, scheduling, stock as STK, webhooks as WH)
from dental_os.services.timeutil import now_local
from tests.test_v05 import Money


def today():
    return now_local("Africa/Accra").date()


def workday(offset=2):
    d = today() + timedelta(days=offset)
    while d.isoweekday() == 7:
        d += timedelta(days=1)
    return d


class Ops(Money):
    def sql(self, q, *a):
        conn = self.db()
        conn.execute(q, a)
        conn.commit()
        conn.close()

    def one(self, q, *a):
        conn = self.db()
        row = conn.execute(q, a).fetchone()
        conn.close()
        return row

    def many(self, q, *a):
        conn = self.db()
        rows = conn.execute(q, a).fetchall()
        conn.close()
        return rows

    def done(self, code="FILL", day=None, pid=1, clinic=1, provider=1, fee=None):
        day = day or today()
        fee = fee if fee is not None else self.FEES[code]
        cid = self.cid(code, clinic)
        self.sql("""INSERT INTO procedures(clinic_id, patient_id, code_id, status, fee_p, provider_id, planned_on, done_on)
                    VALUES (?,?,?,'done',?,?,?,?)""", clinic, pid, cid, fee, provider, str(day), str(day))

    def pay(self, amount, day=None, pid=1, method="cash", purpose="treatment", voided=0, clinic=1):
        day = day or today()
        no = self.one("SELECT COALESCE(MAX(receipt_no),0)+1 FROM payments WHERE clinic_id = ?", clinic)[0]
        self.sql("""INSERT INTO payments(clinic_id, patient_id, amount_p, method, receipt_no, received_on, purpose, voided)
                    VALUES (?,?,?,?,?,?,?,?)""", clinic, pid, amount, method, no, str(day), purpose, voided)

    def visit(self, day=None, status="complete", reason=None, minutes=30, provider=1, hour=9, pid=1, clinic=1, chair=1):
        day = day or today()
        self.sql("""INSERT INTO appointments(clinic_id, patient_id, provider_id, chair_id, start_at, duration_min, status, break_reason)
                    VALUES (?,?,?,?,?,?,?,?)""", clinic, pid, provider, chair, f"{day} {hour:02d}:00", minutes, status, reason)

    def clinic_row(self, n=1):
        return self.one("SELECT * FROM clinics WHERE id = ?", n)

    def key(self, access="write", clinic=1, name="Test"):
        conn = self.db()
        uid = conn.execute("SELECT id FROM users WHERE clinic_id = ? AND role = 'manager'", (clinic,)).fetchone()[0]
        _, key = apikeys.create(conn, self.clinic_row(clinic), uid, name, access)
        conn.commit()
        conn.close()
        return key

    def hdr(self, key):
        return {"Authorization": f"Bearer {key}"}


# ===================================================================== key numbers
class KpiTests(Ops):
    def test_headline_money_and_visits(self):
        d = today()
        self.done("FILL", d)                      # 30000
        self.done("CROWN", d)                     # 150000
        self.done("FILL", d - timedelta(days=40))  # outside the range
        self.sql("INSERT INTO adjustments(clinic_id, patient_id, amount_p, reason, adjusted_on) VALUES (1,1,-20000,'discount',?)", str(d))
        self.pay(100000, d)
        self.pay(5000, d, purpose="membership")
        self.pay(7000, d, voided=1)
        self.pay(9999, d - timedelta(days=40))
        self.visit(d, "complete")
        self.visit(d, "complete", hour=10)
        self.visit(d, "broken", "missed", hour=11)
        self.visit(d, "broken", "cancelled", hour=12)
        self.sql("INSERT INTO patients(clinic_id, first_name, last_name, phone) VALUES (1,'New','Person','+233201119999')")
        p = kpi.period(self.db(), self.clinic_row(), d, d)
        self.assertEqual(p["gross_p"], 180000)
        self.assertEqual(p["adjust_p"], -20000)
        self.assertEqual(p["net_p"], 160000)
        self.assertEqual(p["collected_p"], 105000)             # voided and old payments are left out
        self.assertEqual(p["collected_treatment_p"], 100000)   # the membership fee is not treatment money
        self.assertEqual(p["collection_rate"], 62.5)
        self.assertEqual(p["visits"]["done"], 2)
        self.assertEqual(p["visits"]["missed"], 1)
        self.assertEqual(p["visits"]["no_show_rate"], 33.3)    # 1 of 3 that were due and not cancelled
        self.assertEqual(p["visits"]["cancel_rate"], 25.0)
        self.assertEqual(p["per_visit_p"], 90000)
        self.assertEqual(p["new_patients"], 3)        # the two patients made in setUp, and one more

    def test_empty_period_has_no_division_errors(self):
        p = kpi.period(self.db(), self.clinic_row(), today(), today())
        self.assertIsNone(p["collection_rate"])
        self.assertIsNone(p["visits"]["no_show_rate"])
        self.assertIsNone(p["per_visit_p"])
        self.assertIsNone(p["plans"]["count_rate"])
        d = kpi.dashboard(self.db(), self.clinic_row(), today(), today())
        self.assertIsNone(d["deltas"]["gross_p"])

    def test_change_and_ranges(self):
        self.assertEqual(kpi.change(150, 100), 50.0)
        self.assertEqual(kpi.change(50, 100), -50.0)
        self.assertIsNone(kpi.change(10, 0))
        s, e = kpi.previous_range(date(2026, 11, 1), date(2026, 11, 30))
        self.assertEqual((s, e), (date(2026, 10, 2), date(2026, 10, 31)))
        s, e = kpi.clamp_range(date(2020, 1, 1), date(2026, 1, 1))
        self.assertLessEqual((e - s).days, kpi.MAX_DAYS)
        s, e = kpi.clamp_range(date(2026, 5, 2), date(2026, 5, 1))
        self.assertLess(s, e)
        labels = [x[0] for x in kpi.presets(date(2026, 11, 15))]
        self.assertIn("Last month", labels)
        self.assertEqual(kpi.presets(date(2026, 11, 15))[1][1:], (date(2026, 10, 1), date(2026, 10, 31)))

    def test_comparison_with_the_period_before(self):
        d = today()
        self.done("FILL", d)
        self.done("FILL", d - timedelta(days=1), fee=15000)
        res = kpi.dashboard(self.db(), self.clinic_row(), d, d)
        self.assertEqual(res["deltas"]["gross_p"], 100.0)

    def test_plan_acceptance_counts_the_plans_presented_in_the_period(self):
        d = today()
        self.sql("INSERT INTO treatment_plans(clinic_id, patient_id, title, status, presented_on) VALUES (1,1,'A','accepted',?)", str(d))
        self.sql("INSERT INTO treatment_plans(clinic_id, patient_id, title, status, presented_on) VALUES (1,1,'B','declined',?)", str(d))
        self.sql("INSERT INTO treatment_plans(clinic_id, patient_id, title, status, presented_on) VALUES (1,1,'C','presented',?)", str(d))
        self.sql("INSERT INTO treatment_plans(clinic_id, patient_id, title, status) VALUES (1,1,'D','draft')")
        code = self.cid("CROWN")
        for plan, n in ((1, 1), (2, 1), (3, 2)):
            for _ in range(n):
                self.sql("INSERT INTO plan_items(clinic_id, plan_id, code_id, fee_p) VALUES (1,?,?,100000)", plan, code)
        self.sql("INSERT INTO plan_items(clinic_id, plan_id, code_id, fee_p, removed) VALUES (1,1,?,500000,1)", code)    # removed lines do not count
        r = kpi.plan_acceptance(self.db(), 1, str(d), str(d))
        self.assertEqual((r["presented"], r["accepted"], r["declined"], r["waiting"]), (3, 1, 1, 1))
        self.assertEqual(r["presented_p"], 400000)
        self.assertEqual(r["accepted_p"], 100000)
        self.assertEqual(r["value_rate"], 25.0)
        self.assertEqual(r["count_rate"], 33.3)

    def test_chair_use(self):
        d = workday(1)
        self.visit(d, minutes=60, hour=9)
        self.visit(d, minutes=60, hour=11, status="scheduled")
        self.visit(d, minutes=60, hour=14, status="broken", reason="cancelled")      # not counted
        u = kpi.chair_use(self.db(), self.clinic_row(), d, d)
        self.assertEqual(u["booked_min"], 120)
        self.assertEqual(u["available_min"], 540)       # 08:00 to 17:00, one chair
        self.assertEqual(u["rate"], 22.2)
        self.sql("INSERT INTO chairs(clinic_id, name) VALUES (1, 'Chair 2')")
        self.assertEqual(kpi.chair_use(self.db(), self.clinic_row(), d, d)["available_min"], 1080)
        sunday = d + timedelta(days=(7 - d.isoweekday()))
        self.assertEqual(kpi.chair_use(self.db(), self.clinic_row(), sunday, sunday)["available_min"], 0)

    def test_recall_health(self):
        t = today()
        rt = self.one("SELECT id FROM recall_types WHERE clinic_id = 1")[0]
        for pid, offset, status in ((1, 10, "due"), (2, -5, "due")):
            self.sql("INSERT OR REPLACE INTO recalls(clinic_id, patient_id, type_id, due_on, status) VALUES (1,?,?,?,?)", pid, rt, str(t + timedelta(days=offset)), status)
        self.sql("INSERT INTO patients(clinic_id, first_name, last_name, phone) VALUES (1,'C','C','+233201110001')")
        self.sql("INSERT INTO patients(clinic_id, first_name, last_name, phone) VALUES (1,'D','D','+233201110002')")
        self.sql("INSERT INTO recalls(clinic_id, patient_id, type_id, due_on, status) VALUES (1,4,?,?,'due')", rt, str(t - timedelta(days=100)))
        self.sql("INSERT INTO recalls(clinic_id, patient_id, type_id, due_on, status) VALUES (1,5,?,?,'disabled')", rt, str(t - timedelta(days=100)))
        h = kpi.recall_health(self.db(), 1, t)
        self.assertEqual(h["total"], 3)              # the disabled one is not on recall
        self.assertEqual(h["overdue"], 2)
        self.assertEqual(h["overdue_30"], 1)
        self.assertEqual(h["overdue_90"], 1)
        self.assertEqual(h["on_time_rate"], 33.3)

    def test_by_dentist_and_series(self):
        d = today()
        self.sql("INSERT INTO providers(clinic_id, name) VALUES (1, 'Dr Second')")
        self.done("CROWN", d, provider=1)
        self.visit(d, "complete", provider=1)
        self.visit(d, "broken", "missed", provider=1, hour=11)
        second = self.one("SELECT id FROM providers WHERE name = 'Dr Second'")[0]
        self.done("FILL", d, provider=second)
        rows = {r["name"]: r for r in kpi.by_dentist(self.db(), self.clinic_row(), d, d)}
        self.assertEqual(rows["Dr 1"]["gross_p"], 150000)
        self.assertEqual(rows["Dr 1"]["no_show_rate"], 50.0)
        self.assertEqual(rows["Dr Second"]["gross_p"], 30000)
        self.sql("DELETE FROM procedures WHERE provider_id = ?", second)
        self.done("FILL", d, provider=2)          # provider 2 belongs to the other clinic. It must not show here.
        s = kpi.series(self.db(), 1, d - timedelta(days=2), d)
        self.assertEqual(s["by"], "day")
        self.assertEqual(len(s["rows"]), 3)
        self.assertEqual(s["rows"][-1]["production_p"], 150000 + 30000)
        self.assertEqual(s["rows"][-1]["production_w"], 100)
        long = kpi.series(self.db(), 1, d - timedelta(days=200), d)
        self.assertEqual(long["by"], "month")

    def test_other_clinics_never_leak_in(self):
        self.done("FILL", today(), pid=3, clinic=2, provider=2)
        self.pay(99999, pid=3, clinic=2)
        p = kpi.period(self.db(), self.clinic_row(1), today(), today())
        self.assertEqual((p["gross_p"], p["collected_p"]), (0, 0))

    def test_screens_and_permissions(self):
        self.done("FILL")
        m = self.login("mgr")
        page = self.page(m, "/reports/kpi")
        self.assertIn("Key numbers", page)
        self.assertIn("net production", page)
        self.page(m, f"/reports/kpi?start={today() - timedelta(days=100)}&end={today()}")
        self.assertEqual(m.get("/reports/kpi?start=garbage&end=nope").status_code, 200)
        csv = m.get("/reports/kpi.csv")
        self.assertEqual(csv.status_code, 200)
        self.assertIn("text/csv", csv.headers["Content-Type"])
        self.assertIn("Net production", csv.get_data(as_text=True))
        for name in ("dent", "desk", "asst"):
            c = self.login(name)
            self.assertEqual(c.get("/reports/kpi").status_code, 403)
            self.assertEqual(c.get("/reports/kpi.csv").status_code, 403)


# ===================================================================== groups of clinics
class GroupTests(Ops):
    def setUp(self):
        super().setUp()
        conn = self.db()
        self.org = orgs.create(conn, "Smile Group")
        orgs.add_clinic(conn, self.org, 1)
        orgs.add_clinic(conn, self.org, 2)
        conn.commit()
        conn.close()

    def uid(self, name):
        return self.user(name)["id"]

    def test_only_managers_of_clinics_in_the_group_can_join(self):
        conn = self.db()
        with self.assertRaises(orgs.OrgError):
            orgs.add_member(conn, self.org, self.uid("dent"))
        orgs.add_member(conn, self.org, self.uid("mgr"))
        orgs.add_member(conn, self.org, self.uid("mgr"))          # twice is fine
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM org_members").fetchone()[0], 1)
        other = orgs.create(conn, "Other")
        with self.assertRaises(orgs.OrgError):
            orgs.add_member(conn, other, self.uid("mgr"))          # that clinic is not in the other group
        with self.assertRaises(orgs.OrgError):
            orgs.add_clinic(conn, other, 1)                        # already in a group
        with self.assertRaises(orgs.OrgError):
            orgs.create(conn, "  ")
        with self.assertRaises(orgs.OrgError):
            orgs.add_clinic(conn, other, 99)

    def test_group_page_shows_each_clinic_in_its_own_currency(self):
        conn = self.db()
        orgs.add_member(conn, self.org, self.uid("mgr"))
        conn.commit()
        self.done("FILL", today(), clinic=1)
        self.done("FILL", today(), pid=3, clinic=2, provider=2)
        self.pay(30000, clinic=1)
        c = self.login("mgr")
        page = self.page(c, "/group")
        self.assertIn("Clinic One", page)
        self.assertIn("Clinic Two", page)
        self.assertIn("GHS", page)
        self.assertIn("USD", page)
        self.assertNotIn("Ama", page)                 # no patient names, ever
        data = orgs.compare(self.db(), orgs.for_user(self.db(), self.user("mgr")), today(), today())
        self.assertEqual(set(data["totals"]), {"GHS", "USD"})      # never added across currencies
        self.assertEqual(data["totals"]["GHS"]["gross_p"], 30000)
        self.assertEqual(data["totals"]["USD"]["clinics"], 1)
        self.assertIn("All clinics", self.page(c, "/"))

    def test_others_cannot_see_the_group(self):
        conn = self.db()
        orgs.add_member(conn, self.org, self.uid("mgr"))
        conn.commit()
        for name in ("mgr2", "dent", "desk"):
            c = self.login(name)
            self.assertIn(c.get("/group").status_code, (403, 404))
            self.assertNotIn("All clinics", self.page(c, "/"))

    def test_leaving_the_group_removes_the_view(self):
        conn = self.db()
        orgs.add_member(conn, self.org, self.uid("mgr"))
        conn.commit()
        self.assertEqual(self.login("mgr").get("/group").status_code, 200)
        conn.execute("UPDATE clinics SET org_id = NULL WHERE id = 1")
        conn.commit()
        self.assertEqual(self.login("mgr").get("/group").status_code, 404)
        orgs.remove_member(conn, self.org, self.uid("mgr"))

    def test_viewing_is_logged_in_the_clinics_own_activity(self):
        conn = self.db()
        orgs.add_member(conn, self.org, self.uid("mgr"))
        conn.commit()
        self.login("mgr").get("/group")
        self.assertEqual(self.one("SELECT COUNT(*) FROM audit_log WHERE action = 'org.view' AND clinic_id = 1")[0], 1)

    def test_cli(self):
        r = self.app.test_cli_runner()
        out = r.invoke(args=["create-org", "--name", "Second group"]).output
        self.assertIn("Created group 2", out)
        self.assertIn("already belongs", r.invoke(args=["org-add-clinic", "--org-id", "2", "--clinic-id", "1"]).output)
        self.assertIn("Done", r.invoke(args=["org-add-user", "--org-id", str(self.org), "--username", "mgr"]).output)
        self.assertIn("Only a clinic manager", r.invoke(args=["org-add-user", "--org-id", str(self.org), "--username", "dent"]).output)
        self.assertIn("No user", r.invoke(args=["org-add-user", "--org-id", str(self.org), "--username", "nobody"]).output)


# ===================================================================== lab cases
class LabTests(Ops):
    def setUp(self):
        super().setUp()
        conn = self.db()
        self.lab = LAB.save_lab(conn, self.clinic_row(), 1, None, "Accra Dental Lab", "0302000000", "lab@example.com")
        self.other_lab = LAB.save_lab(conn, self.clinic_row(2), 5, None, "US Lab")
        conn.commit()
        conn.close()

    def case(self, **kw):
        conn = self.db()
        args = dict(patient_id=1, lab_id=self.lab, kind="Crown", tooth="36", shade="A2", instructions="Zirconia", due_on=str(today() + timedelta(days=7)),
                    cost_p=25000)
        args.update(kw)
        try:
            cid = LAB.create(conn, self.clinic_row(), 1, **args)
            conn.commit()
            return cid
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def run_op(self, fn, *a, **kw):
        conn = self.db()
        try:
            fn(conn, self.clinic_row(), 1, *a, **kw)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def status(self, case):
        return self.one("SELECT status FROM lab_cases WHERE id = ?", case)[0]

    def test_full_journey(self):
        case = self.case()
        self.assertEqual(self.status(case), "draft")
        self.run_op(LAB.send, case)
        row = self.one("SELECT * FROM lab_cases WHERE id = ?", case)
        self.assertEqual((row["status"], row["sent_on"]), ("sent", str(today())))
        self.run_op(LAB.receive, case)
        self.run_op(LAB.fit, case)
        self.assertEqual(self.status(case), "fitted")
        kinds = [e["kind"] for e in self.many("SELECT kind FROM lab_events WHERE case_id = ? ORDER BY id", case)]
        self.assertEqual(kinds, ["created", "sent", "received", "fitted"])

    def test_wrong_order_is_refused(self):
        case = self.case()
        for fn in (LAB.receive, LAB.fit):
            with self.assertRaises(LAB.LabError):
                self.run_op(fn, case)
        self.run_op(LAB.send, case)
        with self.assertRaises(LAB.LabError):
            self.run_op(LAB.send, case)
        with self.assertRaises(LAB.LabError):
            self.run_op(LAB.fit, case)
        with self.assertRaises(LookupError):
            self.run_op(LAB.send, 9999)

    def test_dates_make_sense(self):
        case = self.case(due_on=None)
        with self.assertRaises(LAB.LabError):
            self.run_op(LAB.send, case)                                      # a lab must be given a due date
        with self.assertRaises(LAB.LabError):
            self.run_op(LAB.send, case, "2026-05-10", "2026-05-01")          # due before sent
        with self.assertRaises(LAB.LabError):
            self.run_op(LAB.send, case, "not a date", "2026-05-01")
        self.run_op(LAB.send, case, str(today()), str(today() + timedelta(days=5)))
        with self.assertRaises(LAB.LabError):
            self.run_op(LAB.receive, case, str(today() - timedelta(days=3)))  # back before it was sent
        self.run_op(LAB.receive, case)
        with self.assertRaises(LAB.LabError):
            self.run_op(LAB.fit, case, str(today() - timedelta(days=3)))

    def test_cancel_needs_a_reason_and_keeps_the_record(self):
        case = self.case()
        with self.assertRaises(LAB.LabError):
            self.run_op(LAB.cancel, case, "  ")
        self.run_op(LAB.cancel, case, "Patient changed mind")
        self.assertEqual(self.one("SELECT cancel_reason FROM lab_cases WHERE id = ?", case)[0], "Patient changed mind")
        with self.assertRaises(LAB.LabError):
            self.run_op(LAB.send, case)
        with self.assertRaises(LAB.LabError):
            self.run_op(LAB.cancel, case, "again")

    def test_remake_starts_a_new_case_and_keeps_the_old_one(self):
        case = self.case()
        with self.assertRaises(LAB.LabError):
            self.run_op(LAB.remake, case, "too soon")                 # it has not come back yet
        self.run_op(LAB.send, case)
        self.run_op(LAB.receive, case)
        conn = self.db()
        new = LAB.remake(conn, self.clinic_row(), 1, case, "Margin open", 0)
        conn.commit()
        conn.close()
        row = self.one("SELECT * FROM lab_cases WHERE id = ?", new)
        self.assertEqual((row["status"], row["remake_of"], row["kind"], row["tooth"], row["shade"]), ("draft", case, "Crown", "36", "A2"))
        self.assertEqual(self.status(case), "received")
        notes = [e["note"] for e in self.many("SELECT note FROM lab_events WHERE case_id = ?", case)]
        self.assertTrue(any("Margin open" in (n or "") for n in notes))

    def test_edit_only_before_it_is_back(self):
        case = self.case()
        self.run_op(LAB.update, case, self.lab, "Bridge", "35-37", "A3", "New text", str(today() + timedelta(days=9)), 90000)
        row = self.one("SELECT * FROM lab_cases WHERE id = ?", case)
        self.assertEqual((row["kind"], row["cost_p"]), ("Bridge", 90000))
        self.run_op(LAB.send, case)
        self.run_op(LAB.receive, case)
        with self.assertRaises(LAB.LabError):
            self.run_op(LAB.update, case, self.lab, "Crown", "36", "A2", "", str(today()), 0)

    def test_labs_and_patients_are_this_clinics_only(self):
        with self.assertRaises(LAB.LabError):
            self.case(lab_id=self.other_lab)
        with self.assertRaises(LookupError):
            self.case(patient_id=3)
        with self.assertRaises(LAB.LabError):
            self.case(provider_id=2)                                 # dentist of the other clinic
        with self.assertRaises(LAB.LabError):
            self.case(kind="  ")
        with self.assertRaises(LAB.LabError):
            self.case(cost_p=-1)
        with self.assertRaises(LAB.LabError):
            self.case(due_on="next week")
        with self.assertRaises(LAB.LabError):
            self.case(instructions="x" * 1001)
        self.sql("INSERT INTO procedures(clinic_id, patient_id, code_id, status, fee_p, planned_on) VALUES (1,2,?,'planned',100,?)", self.cid("CROWN"), str(today()))
        with self.assertRaises(LAB.LabError):
            self.case(procedure_id=1)                                # that treatment belongs to another patient

    def test_board_lanes_and_late_cases(self):
        late = self.case()
        self.run_op(LAB.send, late, str(today() - timedelta(days=10)), str(today() - timedelta(days=2)))
        ontime = self.case(kind="Bridge")
        self.run_op(LAB.send, ontime)
        back = self.case(kind="Veneer")
        self.run_op(LAB.send, back)
        self.run_op(LAB.receive, back)
        draft = self.case(kind="Night guard")
        b = LAB.board(self.db(), self.clinic_row())
        self.assertEqual([r["id"] for r in b["late"]], [late])
        self.assertEqual([r["id"] for r in b["out"]], [ontime])
        self.assertEqual([r["id"] for r in b["ready"]], [back])
        self.assertEqual([r["id"] for r in b["draft"]], [draft])
        self.assertEqual(LAB.counts(self.db(), self.clinic_row()), {"late": 1, "ready": 1, "draft": 1, "out": 1})
        page = self.page(self.login("desk"), "/lab")
        self.assertIn("Late from the lab", page)
        self.assertIn("Today", self.page(self.login("mgr"), "/"))

    def test_lab_performance(self):
        a = self.case()
        self.run_op(LAB.send, a, str(today() - timedelta(days=10)), str(today() - timedelta(days=4)))
        self.run_op(LAB.receive, a, str(today() - timedelta(days=6)))            # 4 days, on time
        b = self.case()
        self.run_op(LAB.send, b, str(today() - timedelta(days=10)), str(today() - timedelta(days=8)))
        self.run_op(LAB.receive, b, str(today() - timedelta(days=2)))            # 8 days, late
        c = self.case()
        self.run_op(LAB.send, c, str(today() - timedelta(days=3)))               # still out
        perf = LAB.performance(self.db(), 1, today() - timedelta(days=30), today())
        self.assertEqual(len(perf), 1)
        p = perf[0]
        self.assertEqual((p["sent"], p["back"], p["avg_days"], p["on_time_rate"], p["spend_p"]), (3, 2, 6.0, 50.0, 75000))

    def test_screens_and_actions_through_the_app(self):
        c = self.login("dent")
        self.page(c, "/patients/1/lab")
        self.assertIn("Lab", self.page(c, "/patients/1"))
        page = self.page(c, "/lab/new?patient_id=1")
        self.assertIn("Accra Dental Lab", page)
        self.assertNotIn("US Lab", page)
        r = self.post(c, "/lab/new", {"patient_id": "1", "lab_id": str(self.lab), "kind": "Crown", "tooth": "46", "shade": "B1", "cost": "250.00",
                                      "due_on": str(today() + timedelta(days=6)), "instructions": "Please match 45"})
        self.assertEqual(r.status_code, 302)
        case = int(r.headers["Location"].rsplit("/", 1)[1])
        self.assertEqual(self.one("SELECT cost_p FROM lab_cases WHERE id = ?", case)[0], 25000)
        self.page(c, f"/lab/{case}")
        self.assertEqual(self.post(c, f"/lab/{case}/send", {"sent_on": str(today()), "due_on": str(today() + timedelta(days=6))}).status_code, 302)
        self.assertEqual(self.status(case), "sent")
        self.assertEqual(self.post(c, f"/lab/{case}/receive", {}).status_code, 302)
        sheet = self.page(c, f"/lab/{case}/sheet")
        self.assertIn("Lab job sheet", sheet)
        self.assertIn("Please match 45", sheet)
        self.assertNotIn("History", sheet)
        self.assertEqual(self.post(c, f"/lab/{case}/fit", {}).status_code, 302)
        self.assertEqual(self.status(case), "fitted")
        r = self.post(c, f"/lab/{case}/remake", {"reason": "Open margin", "cost": "0"})
        self.assertEqual(r.status_code, 302)
        self.page(c, r.headers["Location"])
        bad = self.post(c, f"/lab/{case}/send", {}, follow_redirects=True)
        self.assertIn("cannot be moved", bad.get_data(as_text=True))
        self.assertEqual(self.post(c, f"/lab/{case}/frobnicate", {}).status_code, 404)

    def test_linking_the_fitting_visit_shows_on_the_pages(self):
        case = self.case()
        self.run_op(LAB.send, case)
        self.run_op(LAB.receive, case)
        day = workday(3)
        self.sql("INSERT INTO appointments(clinic_id, patient_id, provider_id, chair_id, start_at, duration_min, status) VALUES (1,1,1,1,?,30,'scheduled')", f"{day} 10:00")
        self.sql("INSERT INTO appointments(clinic_id, patient_id, provider_id, chair_id, start_at, duration_min, status) VALUES (1,2,1,1,?,30,'scheduled')", f"{day} 11:00")
        c = self.login("desk")
        self.assertEqual(self.post(c, f"/lab/{case}/visit", {"appointment_id": "1"}).status_code, 302)
        self.assertEqual(self.one("SELECT fit_appointment_id FROM lab_cases WHERE id = ?", case)[0], 1)
        self.assertIn("fit ", self.page(c, "/lab"))
        self.page(c, f"/lab/{case}")
        r = self.post(c, f"/lab/{case}/visit", {"appointment_id": "2"}, follow_redirects=True)      # another patient's visit
        self.assertIn("Pick a booked visit", r.get_data(as_text=True))
        self.post(c, f"/lab/{case}/visit", {"appointment_id": ""})
        self.assertIsNone(self.one("SELECT fit_appointment_id FROM lab_cases WHERE id = ?", case)[0])

    def test_other_clinics_cannot_touch_the_case(self):
        case = self.case()
        c2 = self.login("mgr2")
        for url in (f"/lab/{case}", f"/lab/{case}/sheet"):
            self.assertEqual(c2.get(url).status_code, 404)
        self.assertEqual(self.post(c2, f"/lab/{case}/cancel", {"reason": "x"}).status_code, 404)
        self.assertEqual(self.status(case), "draft")
        self.assertEqual(c2.get("/lab/new?patient_id=1").status_code, 404)

    def test_labs_list_is_manager_only(self):
        self.assertEqual(self.login("dent").get("/lab/labs").status_code, 403)
        m = self.login("mgr")
        page = self.page(m, "/lab/labs")
        self.assertIn("Accra Dental Lab", page)
        r = self.post(m, "/lab/labs", {"name": "Second Lab", "email": "no-at-sign", "active": "1"}, follow_redirects=True)
        self.assertIn("email address", r.get_data(as_text=True))
        r = self.post(m, "/lab/labs", {"lab_id": str(self.lab), "name": "Accra Lab Renamed", "active": "1"}, follow_redirects=True)
        self.assertIn("Accra Lab Renamed", r.get_data(as_text=True))
        self.assertEqual(self.post(m, "/lab/labs", {"lab_id": str(self.other_lab), "name": "Hijack", "active": "1"}).status_code, 404)


# ===================================================================== stock
class StockTests(Ops):
    def setUp(self):
        super().setUp()
        conn = self.db()
        self.sup = STK.save_supplier(conn, self.clinic_row(), 1, None, "Dental Depot", "0302111111")
        self.item = STK.save_item(conn, self.clinic_row(), 1, None, "Composite A2", "CMP-A2", "syringe", "Restorative", self.sup, 5, 10, 8000)
        conn.commit()
        conn.close()

    def mv(self, kind, qty, item=None, **kw):
        conn = self.db()
        try:
            out = STK.move(conn, self.clinic_row(), 1, item or self.item, kind, qty, **kw)
            conn.commit()
            return out
        finally:
            conn.close()

    def level(self, item=None):
        return STK.level(self.db(), 1, item or self.item)

    def test_levels_follow_the_moves(self):
        self.mv("receive", 12)
        self.mv("use", 3)
        self.mv("waste", 1)
        self.mv("adjust", 2, note="Found a box")
        self.assertEqual(self.level(), 10)
        self.assertEqual(self.mv("count", 10), None)              # a matching count changes nothing
        self.mv("count", 8)
        self.assertEqual(self.level(), 8)
        self.assertEqual(self.one("SELECT qty FROM stock_moves WHERE kind = 'count'")["qty"], -2)

    def test_it_never_goes_below_zero_and_input_is_checked(self):
        self.mv("receive", 2)
        for kind, qty in (("use", 3), ("waste", 3), ("adjust", -3), ("count", -1), ("use", 0), ("receive", 0), ("receive", -5)):
            with self.assertRaises(STK.StockError, msg=(kind, qty)):
                self.mv(kind, qty, note="x")
        with self.assertRaises(STK.StockError):
            self.mv("adjust", 1)                                   # a correction needs a reason
        with self.assertRaises(STK.StockError):
            self.mv("borrow", 1)
        with self.assertRaises(STK.StockError):
            self.mv("receive", "3")
        with self.assertRaises(STK.StockError):
            self.mv("receive", 1, moved_on=str(today() + timedelta(days=1)))
        with self.assertRaises(STK.StockError):
            self.mv("receive", 1, moved_on="yesterday")
        with self.assertRaises(STK.StockError):
            self.mv("receive", 1, expires_on=str(today() - timedelta(days=1)))
        with self.assertRaises(STK.StockError):
            self.mv("receive", 1, unit_cost_p=-1)
        with self.assertRaises(LookupError):
            self.mv("receive", 1, item=999)
        self.assertEqual(self.level(), 2)

    def test_soonest_expiry_is_used_first(self):
        soon, later = str(today() + timedelta(days=20)), str(today() + timedelta(days=200))
        self.mv("receive", 10, expires_on=later, unit_cost_p=9000)
        self.mv("receive", 4, expires_on=soon, unit_cost_p=8000)
        self.mv("use", 6)                                          # takes the 4 expiring soon, then 2 of the later batch
        left = STK.batches(self.db(), 1, self.item)
        self.assertEqual([(b["qty"], b["expires_on"]) for b in left], [(8, later)])
        self.assertEqual(self.level(), 8)
        row = STK.items(self.db(), self.clinic_row())[0]
        self.assertEqual(row["value_p"], 8 * 9000)
        self.assertEqual(row["next_expiry"], later)

    def test_expiry_lists(self):
        self.mv("receive", 3, expires_on=str(today() + timedelta(days=30)))
        self.mv("receive", 2, expires_on=str(today() + timedelta(days=400)))
        self.sql("INSERT INTO stock_moves(clinic_id, item_id, qty, kind, unit_cost_p, expires_on, moved_on) VALUES (1,?,4,'receive',0,?,?)",
                 self.item, str(today() - timedelta(days=3)), str(today() - timedelta(days=90)))
        rows = STK.expiring(self.db(), self.clinic_row(), 90)
        self.assertEqual([(r["qty"], r["expired"]) for r in rows], [(4, True), (3, False)])
        s = STK.summary(self.db(), self.clinic_row())
        self.assertEqual((s["expired"], s["expiring"]), (1, 1))
        self.mv("waste", 4)                                        # writing off the expired stock clears the warning
        self.assertEqual(STK.summary(self.db(), self.clinic_row())["expired"], 0)

    def test_reorder_list_groups_by_supplier(self):
        conn = self.db()
        no_sup = STK.save_item(conn, self.clinic_row(), 1, None, "Gloves M", None, "box", "Disposables", None, 4, 0, 1500)
        plenty = STK.save_item(conn, self.clinic_row(), 1, None, "Bibs", None, "pack", "Disposables", None, 2, 5, 500)
        conn.commit()
        self.mv("receive", 5)                                      # at the reorder level of 5
        self.mv("receive", 1, item=no_sup)
        self.mv("receive", 50, item=plenty)
        groups = STK.to_order(self.db(), self.clinic_row())
        self.assertEqual([g["supplier"] for g in groups], ["Dental Depot", "No supplier set"])
        self.assertEqual(groups[0]["lines"][0]["want"], 10)                       # the usual order size
        self.assertEqual(groups[0]["total_p"], 80000)
        self.assertEqual(groups[1]["lines"][0]["want"], 7)                        # twice the level (8) less what is in stock (1)
        self.assertEqual(sum(1 for r in STK.items(self.db(), self.clinic_row()) if r["low"]), 2)
        self.mv("receive", 5)
        self.assertEqual(len(STK.to_order(self.db(), self.clinic_row())), 1)

    def test_items_switch_off_and_isolation(self):
        conn = self.db()
        STK.save_item(conn, self.clinic_row(), 1, self.item, "Composite A2", "CMP-A2", "syringe", "Restorative", self.sup, 5, 10, 8000, None, False)
        conn.commit()
        with self.assertRaises(STK.StockError):
            self.mv("receive", 1)
        self.assertEqual(len(STK.items(self.db(), self.clinic_row())), 0)
        self.assertEqual(len(STK.items(self.db(), self.clinic_row(), include_off=True)), 1)
        with self.assertRaises(STK.StockError):
            STK.save_item(conn, self.clinic_row(2), 5, None, "X", supplier_id=self.sup)      # another clinic's supplier
        with self.assertRaises(STK.StockError):
            STK.save_item(conn, self.clinic_row(), 1, None, "  ")
        with self.assertRaises(STK.StockError):
            STK.save_item(conn, self.clinic_row(), 1, None, "Neg", reorder_level=-1)
        with self.assertRaises(LookupError):
            STK.save_item(conn, self.clinic_row(2), 5, self.item, "Steal")

    def test_receiving_updates_the_last_cost(self):
        self.mv("receive", 1, unit_cost_p=9500)
        self.assertEqual(STK.get_item(self.db(), 1, self.item)["cost_p"], 9500)
        self.mv("receive", 1)
        self.assertEqual(STK.get_item(self.db(), 1, self.item)["cost_p"], 9500)

    def test_screens_and_permissions(self):
        desk = self.login("desk")
        for url in ("/stock", f"/stock/item/{self.item}", "/stock/order", "/stock/expiry"):
            self.assertEqual(desk.get(url).status_code, 403, url)
        asst = self.login("asst")
        for url in ("/stock", f"/stock/item/{self.item}", "/stock/order", "/stock/expiry"):
            self.page(asst, url)
        for url in ("/stock/suppliers", "/stock/items/new", f"/stock/item/{self.item}/edit"):
            self.assertEqual(asst.get(url).status_code, 403, url)
        r = self.post(asst, f"/stock/item/{self.item}/move", {"kind": "receive", "qty": "6", "unit_cost": "85.50", "expires_on": str(today() + timedelta(days=100))})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.level(), 6)
        self.assertEqual(STK.get_item(self.db(), 1, self.item)["cost_p"], 8550)
        r = self.post(asst, f"/stock/item/{self.item}/move", {"kind": "use", "qty": "99"}, follow_redirects=True)
        self.assertIn("Only 6", r.get_data(as_text=True))
        r = self.post(asst, f"/stock/item/{self.item}/move", {"kind": "use", "qty": "abc"}, follow_redirects=True)
        self.assertIn("whole numbers", r.get_data(as_text=True))
        r = self.post(asst, f"/stock/item/{self.item}/move", {"kind": "count", "qty": "6"}, follow_redirects=True)
        self.assertIn("matched", r.get_data(as_text=True))
        m = self.login("mgr")
        self.page(m, "/stock")
        self.page(m, "/stock?q=comp&category=Restorative")
        self.page(m, "/stock/suppliers")
        self.page(m, "/stock/items/new")
        self.page(m, f"/stock/item/{self.item}/edit")
        r = self.post(m, "/stock/items/new", {"name": "Etchant", "unit": "syringe", "category": "Restorative", "reorder_level": "2", "reorder_qty": "4", "cost": "20"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.one("SELECT cost_p, reorder_qty FROM stock_items WHERE name = 'Etchant'")["cost_p"], 2000)
        r = self.post(m, "/stock/items/new", {"name": "Bad", "unit": "each", "reorder_level": "-2"}, follow_redirects=True)
        self.assertIn("cannot be negative", r.get_data(as_text=True))
        r = self.post(m, "/stock/suppliers", {"name": "Another", "active": "1"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.post(m, "/stock/suppliers", {"supplier_id": "999", "name": "x"}).status_code, 404)

    def test_other_clinics_cannot_see_or_move_it(self):
        c2 = self.login("mgr2")
        self.assertEqual(c2.get(f"/stock/item/{self.item}").status_code, 404)
        self.assertEqual(self.post(c2, f"/stock/item/{self.item}/move", {"kind": "receive", "qty": "5"}).status_code, 404)
        self.assertEqual(c2.get(f"/stock/item/{self.item}/edit").status_code, 404)
        self.assertNotIn("Composite", self.page(c2, "/stock"))
        self.assertEqual(self.level(), 0)


# ===================================================================== API
class ApiTests(Ops):
    def setUp(self):
        super().setUp()
        self.c = self.app.test_client()
        self.rw = self.key("write")
        self.ro = self.key("read")

    def get(self, url, key=None):
        return self.c.get("/api/v1" + url, headers=self.hdr(key or self.ro))

    def send(self, method, url, body, key=None, **kw):
        return self.c.open("/api/v1" + url, method=method, json=body, headers=self.hdr(key or self.rw), **kw)

    def test_keys_are_hashed_and_shown_once(self):
        row = self.one("SELECT * FROM api_keys WHERE access = 'write'")
        self.assertNotIn(self.rw, json.dumps(dict(row)))
        self.assertEqual(row["key_prefix"], self.rw[:10])
        self.assertEqual(len(row["key_hash"]), 64)
        m = self.login("mgr")
        r = self.post(m, "/integrations", {"action": "key_create", "name": "Accounts", "access": "read"})
        self.assertEqual(r.status_code, 200)
        text = r.get_data(as_text=True)
        shown = [w for w in text.replace("<", " ").replace(">", " ").split() if w.startswith("dos_") and len(w) > 30]
        self.assertEqual(len(shown), 1)
        self.assertEqual(self.get("/clinic", shown[0]).status_code, 200)
        self.assertNotIn(shown[0], self.page(m, "/integrations"))        # not shown again
        self.assertEqual(self.login("dent").get("/integrations").status_code, 403)
        r = self.post(m, "/integrations", {"action": "key_create", "name": "", "access": "read"}, follow_redirects=True)
        self.assertIn("Give the key a name", r.get_data(as_text=True))

    def test_no_key_wrong_key_revoked_key(self):
        for headers in ({}, {"Authorization": "Bearer nope"}, {"Authorization": "Basic abc"}, {"Authorization": "Bearer dos_" + "x" * 43},
                        {"Authorization": "Bearer " + "dos_" + "y" * 500}):
            r = self.c.get("/api/v1/patients", headers=headers)
            self.assertEqual(r.status_code, 401)
            self.assertEqual(r.get_json()["error"]["code"], "unauthorised")
        self.assertEqual(self.get("/patients").status_code, 200)
        m = self.login("mgr")
        key_id = self.one("SELECT id FROM api_keys WHERE access = 'read'")[0]
        self.post(m, "/integrations", {"action": "key_revoke", "key_id": str(key_id)})
        self.assertEqual(self.get("/patients").status_code, 401)
        self.assertEqual(self.post(m, "/integrations", {"action": "key_revoke", "key_id": "999"}).status_code, 404)

    def test_a_signed_in_session_does_not_open_the_api(self):
        m = self.login("mgr")
        r = m.get("/api/v1/patients")
        self.assertEqual(r.status_code, 401)
        r = m.post("/api/v1/patients", json={"first_name": "A"})
        self.assertEqual(r.status_code, 401)

    def test_read_only_keys_cannot_write(self):
        r = self.send("POST", "/patients", {"first_name": "A", "last_name": "B", "phone": "0241234567"}, key=self.ro)
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.get_json()["error"]["code"], "read_only_key")
        for method, url in (("PATCH", "/patients/1"), ("POST", "/appointments"), ("POST", "/appointments/1/status")):
            self.assertEqual(self.send(method, url, {}, key=self.ro).status_code, 403)
        self.assertEqual(self.one("SELECT COUNT(*) FROM patients")[0], 3)

    def test_a_key_sees_only_its_own_clinic(self):
        ids = [p["id"] for p in self.get("/patients?include_archived=1").get_json()["data"]]
        self.assertEqual(ids, [1, 2])
        self.assertEqual(self.get("/patients/3").status_code, 404)
        self.assertEqual(self.get("/patients/3/balance").status_code, 404)
        self.assertEqual(self.get("/clinic").get_json()["data"]["currency"], "GHS")
        key2 = self.key("read", clinic=2)
        self.assertEqual(self.get("/clinic", key2).get_json()["data"]["currency"], "USD")
        self.assertEqual([p["id"] for p in self.get("/patients", key2).get_json()["data"]], [3])
        self.assertEqual(self.get("/patients/1", key2).status_code, 404)

    def test_patient_shape_leaves_out_private_fields(self):
        self.sql("UPDATE patients SET notes = 'private note' WHERE id = 1")
        body = self.get("/patients/1").get_json()["data"]
        self.assertNotIn("private note", json.dumps(body))
        self.assertNotIn("notes", body)
        self.assertEqual((body["first_name"], body["phone"], body["agreed_to_appointment_messages"]), ("Ama", "+233201112223", True))
        self.assertNotIn("guarantor_id", body)

    def test_paging_search_and_updated_since(self):
        for i in range(5):
            self.send("POST", "/patients", {"first_name": f"Pat{i}", "last_name": "Paged", "phone": f"02412345{i:02d}"})
        page1 = self.get("/patients?limit=3").get_json()
        self.assertEqual(len(page1["data"]), 3)
        self.assertEqual(page1["next_after"], page1["data"][-1]["id"])
        page2 = self.get(f"/patients?limit=3&after={page1['next_after']}").get_json()
        self.assertEqual(len(page2["data"]), 3)
        page3 = self.get(f"/patients?limit=3&after={page2['next_after']}").get_json()
        self.assertEqual((len(page3["data"]), page3["next_after"]), (1, None))
        self.assertEqual(len(self.get("/patients?q=paged").get_json()["data"]), 5)
        self.assertEqual(len(self.get("/patients?q=%25").get_json()["data"]), 0)           # a wildcard is not a search for everything
        self.assertEqual(len(self.get("/patients?limit=9999").get_json()["data"]), 7)
        self.assertEqual(len(self.get("/patients?updated_since=2999-01-01").get_json()["data"]), 0)
        self.assertEqual(self.get("/patients?updated_since=yesterday").status_code, 400)
        self.assertEqual(len(self.get("/patients?updated_since=2000-01-01").get_json()["data"]), 7)

    def test_create_update_and_validation(self):
        r = self.send("POST", "/patients", {"first_name": " Efua ", "last_name": "Boateng", "phone": "024 123 4567", "dob": "1992-02-02",
                                              "gender": "Female", "agreed_to_appointment_messages": True, "agreed_to_offers": "false"})
        self.assertEqual(r.status_code, 201)
        body = r.get_json()["data"]
        self.assertEqual((body["first_name"], body["phone"], body["agreed_to_offers"], body["agreed_to_appointment_messages"]),
                         ("Efua", "+233241234567", False, True))
        pid = body["id"]
        r = self.send("PATCH", f"/patients/{pid}", {"email": "Efua@Example.com", "agreed_to_offers": "yes"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual((r.get_json()["data"]["email"], r.get_json()["data"]["agreed_to_offers"], r.get_json()["data"]["last_name"]), ("efua@example.com", True, "Boateng"))
        self.assertEqual(self.send("PATCH", f"/patients/{pid}", {"phone": "123"}).status_code, 422)
        bad = self.send("POST", "/patients", {"first_name": "", "last_name": "X", "phone": "nope", "dob": "2999-01-01"})
        self.assertEqual(bad.status_code, 422)
        self.assertEqual(set(bad.get_json()["error"]["fields"]), {"first_name", "phone", "dob"})
        odd = self.send("POST", "/patients", {"first_name": 12, "last_name": None, "phone": ["x"]})
        self.assertEqual(odd.status_code, 422)                 # strange types are refused, not crashes
        self.assertEqual(self.send("PATCH", "/patients/3", {"first_name": "Hijack"}).status_code, 404)
        self.assertEqual(self.one("SELECT first_name FROM patients WHERE id = 3")[0], "Zed")

    def test_duplicates_need_an_explicit_yes(self):
        body = {"first_name": "Ama", "last_name": "Mensah", "phone": "0201112223", "dob": "1990-05-01"}
        r = self.send("POST", "/patients", body)
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.get_json()["error"]["code"], "possible_duplicate")
        self.assertEqual(r.get_json()["error"]["matches"], [1])
        self.assertEqual(self.send("POST", "/patients", {**body, "allow_duplicate": True}).status_code, 201)

    def test_bad_requests(self):
        r = self.c.post("/api/v1/patients", data="not json", headers=self.hdr(self.rw))
        self.assertEqual((r.status_code, r.get_json()["error"]["code"]), (400, "bad_json"))
        r = self.c.post("/api/v1/patients", json=[1, 2], headers=self.hdr(self.rw))
        self.assertEqual(r.status_code, 400)
        for headers in (self.hdr(self.ro), {}):
            r = self.c.get("/api/v1/nothing", headers=headers)
            self.assertEqual((r.status_code, r.get_json()["error"]["code"]), (404, "not_found"))
        r = self.c.delete("/api/v1/patients/1", headers=self.hdr(self.rw))
        self.assertEqual(r.status_code, 405)                               # nothing can be deleted through the API, even with a write key
        self.assertEqual(self.get("/appointments?from=soon").status_code, 400)
        self.assertEqual(self.get("/appointments?status=weird").status_code, 400)

    def test_json_errors_for_missing_and_unsupported(self):
        r = self.c.open("/api/v1/clinic", method="PUT", headers=self.hdr(self.rw), json={})
        self.assertEqual(r.status_code, 405)
        self.assertIn("error", r.get_json())
        r = self.c.post("/api/v1/unknown", json={})                         # no form token needed, and no redirect to the login page
        self.assertEqual(r.status_code, 404)

    def test_balance(self):
        self.done("CROWN", pid=1)
        self.pay(50000, pid=1)
        b = self.get("/patients/1/balance").get_json()["data"]
        self.assertEqual((b["charges_p"], b["paid_p"], b["balance_p"], b["currency"]), (150000, 50000, 100000, "GHS"))

    def test_appointments_book_list_and_change(self):
        day = workday(2)
        slots = self.get(f"/free-slots?provider_id=1&date={day}&duration=30").get_json()["data"]
        self.assertTrue(slots)
        start = slots[0]["start_at"]
        r = self.send("POST", "/appointments", {"patient_id": 1, "provider_id": 1, "start_at": start, "duration_min": 30, "category": "Consultation", "notes": "via API"})
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        appt = r.get_json()["data"]
        self.assertEqual((appt["status"], appt["start_at"], appt["chair_id"]), ("scheduled", start, 1))
        again = self.send("POST", "/appointments", {"patient_id": 2, "provider_id": 1, "start_at": start})
        self.assertEqual((again.status_code, again.get_json()["error"]["code"]), (409, "cannot_book"))
        self.assertNotIn(start, [s["start_at"] for s in self.get(f"/free-slots?provider_id=1&date={day}&duration=30").get_json()["data"]])
        listed = self.get(f"/appointments?from={day}&to={day}").get_json()["data"]
        self.assertEqual([a["id"] for a in listed], [appt["id"]])
        self.assertEqual(self.get(f"/appointments?patient_id=2").get_json()["data"], [])
        self.assertEqual(self.get(f"/appointments/{appt['id']}").get_json()["data"]["notes"], "via API")
        r = self.send("POST", f"/appointments/{appt['id']}/status", {"status": "broken", "break_reason": "cancelled"})
        self.assertEqual(r.get_json()["data"]["break_reason"], "cancelled")
        r = self.send("POST", f"/appointments/{appt['id']}/status", {"status": "complete"})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(self.send("POST", "/appointments/999/status", {"status": "complete"}).status_code, 404)

    def test_appointment_input_checks(self):
        day = workday(2)
        good = {"patient_id": 1, "provider_id": 1, "start_at": f"{day} 10:00"}
        for change in ({"start_at": "tomorrow"}, {"patient_id": "1"}, {"provider_id": None}, {"duration_min": "30"}, {"category": "Spa day"},
                       {"duration_min": 31}):
            r = self.send("POST", "/appointments", {**good, **change})
            self.assertIn(r.status_code, (409, 422), change)
        self.assertEqual(self.send("POST", "/appointments", {**good, "patient_id": 3}).status_code, 409)      # another clinic's patient
        self.assertEqual(self.send("POST", "/appointments", {**good, "provider_id": 2}).status_code, 409)     # another clinic's dentist
        self.assertEqual(self.send("POST", "/appointments", {**good, "start_at": f"{day} 03:00"}).status_code, 409)   # closed
        self.assertEqual(self.send("POST", "/appointments", {**good, "start_at": f"{today() - timedelta(days=3)} 10:00"}).status_code, 409)
        self.assertEqual(self.one("SELECT COUNT(*) FROM appointments")[0], 0)
        self.assertEqual(self.get("/free-slots?provider_id=1&date=bad").status_code, 400)
        self.assertEqual(self.get(f"/free-slots?provider_id=1&date={day}&duration=17").status_code, 400)
        self.assertEqual(self.get(f"/free-slots?date={day}").status_code, 400)
        self.assertEqual(self.get(f"/free-slots?provider_id=2&date={day}").get_json()["data"], [])       # not this clinic's dentist

    def test_lists_of_providers_and_chairs(self):
        self.assertEqual(self.get("/providers").get_json()["data"], [{"id": 1, "name": "Dr 1"}])
        self.assertEqual(self.get("/chairs").get_json()["data"], [{"id": 1, "name": "Chair 1"}])

    def test_rate_limit(self):
        old = api_module.RATE_PER_MINUTE
        api_module.RATE_PER_MINUTE = 3
        try:
            codes = [self.get("/clinic").status_code for _ in range(5)]
        finally:
            api_module.RATE_PER_MINUTE = old
        self.assertEqual(codes, [200, 200, 200, 429, 429])
        r = self.get("/clinic")
        self.assertEqual(r.status_code, 200)

    def test_api_writes_are_audited(self):
        self.send("POST", "/patients", {"first_name": "Aud", "last_name": "Ited", "phone": "0241230000"})
        self.assertEqual(self.one("SELECT COUNT(*) FROM audit_log WHERE action = 'api.write' AND clinic_id = 1")[0], 1)
        self.assertEqual(self.one("SELECT COUNT(*) FROM audit_log WHERE action = 'patient.create'")[0], 1)

    def test_docs_page(self):
        page = self.page(self.login("mgr"), "/integrations/docs")
        self.assertIn("Authorization: Bearer", page)
        self.assertIn("X-DentalOS-Signature", page)
        self.assertEqual(self.login("desk").get("/integrations/docs").status_code, 403)


# ===================================================================== webhooks
class FakeReceiver:
    def __init__(self, codes=None):
        self.calls, self.codes = [], list(codes or [])

    def __call__(self, url, body, headers, allow_private=False):
        self.calls.append((url, body, headers))
        return self.codes.pop(0) if self.codes else 200


class WebhookTests(Ops):
    URL = "https://hooks.example.com/dental"

    def hook(self, events=None, url=None, clinic=1):
        conn = self.db()
        ep, secret = WH.add_endpoint(conn, self.clinic_row(clinic), 1, url or self.URL, events or [])
        conn.commit()
        conn.close()
        return ep, secret

    def run_op(self, fn, *a, clinic=1, user=1):
        conn = self.db()
        try:
            fn(conn, self.clinic_row(clinic), user, *a)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def run_due(self, post, now=None):
        conn = self.db()
        out = WH.deliver_due(conn, post=post, now=now)
        conn.commit()
        conn.close()
        return out

    def test_addresses_are_checked(self):
        for url in ("http://example.com/x", "ftp://example.com", "https://localhost/x", "https://127.0.0.1/x", "https://10.0.0.5/x",
                    "https://192.168.1.1/x", "https://169.254.169.254/latest", "https://[::1]/x", "https://user:pw@example.com/x",
                    "https://printer.local/x", "https://", "example.com", "https://x.internal/a", "https://example.com:99999/x", "https://a.com/" + "x" * 300):
            with self.assertRaises(WH.WebhookError, msg=url):
                WH.check_url(url)
        self.assertEqual(WH.check_url(self.URL), self.URL)
        self.assertEqual(WH.check_url("https://8.8.8.8/x"), "https://8.8.8.8/x")
        self.assertEqual(WH.check_url("http://127.0.0.1:8000/x", allow_private=True), "http://127.0.0.1:8000/x")

    def test_events_are_queued_only_for_endpoints_that_want_them(self):
        self.hook(["payment.received"])
        self.hook()                                          # everything
        conn = self.db()
        WH.emit(conn, 1, "patient.created", {"id": 1})
        WH.emit(conn, 1, "payment.received", {"id": 2})
        conn.commit()
        rows = self.many("SELECT endpoint_id, event FROM webhook_deliveries ORDER BY id")
        self.assertEqual([(r["endpoint_id"], r["event"]) for r in rows], [(2, "patient.created"), (1, "payment.received"), (2, "payment.received")])
        WH.emit(conn, 2, "patient.created", {"id": 9})        # another clinic has no endpoints
        conn.commit()
        self.assertEqual(self.one("SELECT COUNT(*) FROM webhook_deliveries")[0], 3)

    def test_delivery_is_signed_and_marked_sent(self):
        _, secret = self.hook()
        conn = self.db()
        WH.emit(conn, 1, "patient.created", {"id": 1})
        conn.commit()
        rx = FakeReceiver()
        self.assertEqual(self.run_due(rx), {"sent": 1, "retry": 0, "dead": 0})
        url, body, headers = rx.calls[0]
        self.assertEqual(url, self.URL)
        self.assertTrue(WH.verify(secret, headers["X-DentalOS-Signature"], body))
        self.assertFalse(WH.verify("whsec_wrong", headers["X-DentalOS-Signature"], body))
        self.assertFalse(WH.verify(secret, headers["X-DentalOS-Signature"], body + b" "))                      # changed in transit
        self.assertFalse(WH.verify(secret, headers["X-DentalOS-Signature"], body, now=__import__("time").time() + 4000))   # too old
        self.assertFalse(WH.verify(secret, "garbage", body))
        payload = json.loads(body)
        self.assertEqual((payload["event"], payload["clinic_id"], payload["data"]), ("patient.created", 1, {"id": 1}))
        self.assertTrue(payload["id"].startswith("evt_"))
        self.assertEqual(headers["X-DentalOS-Event"], "patient.created")
        row = self.one("SELECT * FROM webhook_deliveries")
        self.assertEqual((row["status"], row["attempts"], row["response_code"]), ("sent", 1, 200))
        self.assertEqual(self.run_due(rx), {"sent": 0, "retry": 0, "dead": 0})                                  # nothing is sent twice

    def test_failures_retry_on_a_schedule_then_give_up(self):
        self.hook()
        conn = self.db()
        WH.emit(conn, 1, "patient.created", {"id": 1})
        conn.commit()
        t = datetime.utcnow()
        rx = FakeReceiver([500] * 10)
        waits = []
        for attempt in range(1, 7):
            out = self.run_due(rx, now=t)
            row = self.one("SELECT * FROM webhook_deliveries")
            self.assertEqual(row["attempts"], attempt)
            if attempt < 6:
                self.assertEqual(out["retry"], 1)
                waits.append(round((datetime.strptime(row["next_try_at"], "%Y-%m-%d %H:%M:%S") - t.replace(microsecond=0)).total_seconds() / 60))
                self.assertEqual(self.run_due(rx, now=t), {"sent": 0, "retry": 0, "dead": 0})            # not due yet
                t = datetime.strptime(row["next_try_at"], "%Y-%m-%d %H:%M:%S")
        self.assertEqual(waits, WH.RETRY_MINUTES)
        row = self.one("SELECT * FROM webhook_deliveries")
        self.assertEqual((row["status"], row["error"]), ("dead", "The receiver answered 500"))
        self.assertEqual(len(rx.calls), 6)
        self.run_op(WH.retry, row["id"])
        self.assertEqual(self.run_due(FakeReceiver([200])), {"sent": 1, "retry": 0, "dead": 0})
        with self.assertRaises(WH.WebhookError):
            self.run_op(WH.retry, row["id"])                                       # only a dead one can be retried

    def test_connection_errors_count_as_failures(self):
        self.hook()
        conn = self.db()
        WH.emit(conn, 1, "patient.created", {"id": 1})
        conn.commit()

        def boom(*a, **k):
            raise WH.DeliveryError("TimeoutError")
        self.assertEqual(self.run_due(boom)["retry"], 1)
        self.assertEqual(self.one("SELECT error FROM webhook_deliveries")[0], "TimeoutError")

    def test_switched_off_addresses_stop_receiving(self):
        ep, _ = self.hook()
        conn = self.db()
        WH.emit(conn, 1, "patient.created", {"id": 1})
        WH.set_active(conn, self.clinic_row(), 1, ep, False)
        WH.emit(conn, 1, "patient.created", {"id": 2})
        conn.commit()
        self.assertEqual(self.one("SELECT COUNT(*) FROM webhook_deliveries")[0], 1)       # nothing new queued
        rx = FakeReceiver()
        self.assertEqual(self.run_due(rx)["dead"], 1)                                      # and the waiting one is dropped, not sent
        self.assertEqual(rx.calls, [])
        with self.assertRaises(LookupError):
            WH.set_active(conn, self.clinic_row(2), 5, ep, True)

    def test_an_address_that_later_points_inside_is_refused_at_send_time(self):
        self.sql("INSERT INTO webhook_endpoints(clinic_id, url, secret) VALUES (1, 'http://169.254.169.254/latest', 'whsec_x')")
        conn = self.db()
        WH.emit(conn, 1, "patient.created", {"id": 1})
        conn.commit()
        rx = FakeReceiver()
        self.assertEqual(self.run_due(rx)["retry"], 1)
        self.assertEqual(rx.calls, [])
        self.assertIn("https", self.one("SELECT error FROM webhook_deliveries")[0])

    def test_real_post_goes_to_the_checked_address_and_refuses_private_ones(self):
        got = {}

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                got["body"] = self.rfile.read(int(self.headers["Content-Length"]))
                got["headers"] = dict(self.headers)
                self.send_response(204)
                self.end_headers()

            def log_message(self, *a):
                pass
        srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            url = f"http://127.0.0.1:{srv.server_port}/hook?x=1"
            with self.assertRaises(WH.DeliveryError):
                WH.https_post(url, b"{}", {"Content-Type": "application/json"})                  # private: refused by default
            self.assertEqual(WH.https_post(url, b'{"a":1}', {"Content-Type": "application/json", "X-Test": "yes"}, allow_private=True), 204)
        finally:
            srv.shutdown()
        self.assertEqual(got["body"], b'{"a":1}')
        self.assertEqual(got["headers"]["X-Test"], "yes")
        with self.assertRaises(WH.DeliveryError):
            WH.https_post("https://no-such-name.invalid/x", b"{}", {})

    def test_limits(self):
        for i in range(WH.MAX_ENDPOINTS):
            self.hook(url=f"https://hooks.example.com/{i}")
        with self.assertRaises(WH.WebhookError):
            self.hook(url="https://hooks.example.com/one-too-many")
        conn = self.db()
        for _ in range(3):
            WH.emit(conn, 1, "patient.created", {"id": 1})
        conn.commit()
        self.assertEqual(self.one("SELECT COUNT(*) FROM webhook_deliveries")[0], 30)

    def test_the_main_events_are_emitted_with_limited_fields(self):
        self.hook()
        c = self.login("mgr")
        self.post(c, "/patients/new", {"first_name": "Hook", "last_name": "Test", "phone": "0241230001", "notes": "SECRET NOTE", "reminders_ok": "1"})
        pid = self.one("SELECT id FROM patients WHERE first_name = 'Hook'")[0]
        self.post(c, f"/patients/{pid}/edit", {"first_name": "Hook", "last_name": "Tested", "phone": "0241230001", "notes": "SECRET NOTE"})
        day = workday(2)
        conn = self.db()
        appt = scheduling.book(conn, self.clinic_row(), 1, pid, 1, 1, datetime.strptime(f"{day} 10:00", "%Y-%m-%d %H:%M"), 30)
        scheduling.set_status(conn, 1, 1, appt, "complete")
        from dental_os.services import billing
        billing.record_payment(conn, self.clinic_row(), 1, pid, 5000, "cash")
        conn.commit()
        conn.close()
        events = [r["event"] for r in self.many("SELECT event FROM webhook_deliveries ORDER BY id")]
        self.assertEqual(events, ["patient.created", "patient.updated", "appointment.booked", "appointment.updated", "payment.received"])
        every = " ".join(r["payload"] for r in self.many("SELECT payload FROM webhook_deliveries"))
        self.assertNotIn("SECRET NOTE", every)
        pay = json.loads(self.many("SELECT payload FROM webhook_deliveries WHERE event = 'payment.received'")[0]["payload"])["data"]
        self.assertEqual((pay["amount_p"], pay["method"]), (5000, "cash"))
        self.assertNotIn("reference", pay)

    def test_invoice_plan_and_booking_events(self):
        self.hook(["invoice.issued", "plan.accepted", "booking.requested", "appointment.booked"])
        c = self.login("mgr")
        self.done("FILL")
        proc = self.one("SELECT id FROM procedures LIMIT 1")[0]
        self.assertEqual(self.post(c, "/patients/1/invoices", {"procedure_id": str(proc)}).status_code, 302)
        plan = self.new_plan(c)
        self.add(c, plan)
        self.post(c, f"/plans/{plan}/present", {})
        self.assertEqual(self.accept(c, plan).status_code, 302)
        got = {r["event"]: json.loads(r["payload"])["data"] for r in self.many("SELECT event, payload FROM webhook_deliveries")}
        self.assertEqual(got["invoice.issued"]["total_p"], 30000)
        self.assertEqual((got["plan.accepted"]["total_p"], got["plan.accepted"]["status"]), (150000, "accepted"))

    def test_booking_request_event(self):
        self.hook(["booking.requested"])
        conn = self.db()
        conn.execute("UPDATE clinic_comms SET booking_on = 1, booking_token = 'tok123' WHERE clinic_id = 1")
        conn.commit()
        c = self.app.test_client()
        page = c.get("/book/tok123").get_data(as_text=True)
        import re
        token = re.search(r'name="_csrf" value="([^"]+)"', page).group(1)
        slot = re.search(r'name="slot" value="([^"]+)"', page).group(1)
        r = c.post("/book/tok123", data={"_csrf": token, "first_name": "Web", "last_name": "Booker", "phone": "0241230099", "reason": "Consultation", "slot": slot, "agrees_messages": "1"})
        self.assertEqual(r.status_code, 200)
        rows = self.many("SELECT payload FROM webhook_deliveries")
        self.assertEqual(len(rows), 1)
        data = json.loads(rows[0]["payload"])["data"]
        self.assertEqual(set(data), {"id", "wanted_start", "reason", "provider_id"})       # no name or phone in the message

    def test_the_page_and_cli(self):
        m = self.login("mgr")
        r = self.post(m, "/integrations", {"action": "hook_add", "url": "http://insecure.example.com", "events": "patient.created"}, follow_redirects=True)
        self.assertIn("must start with https", r.get_data(as_text=True))
        r = self.post(m, "/integrations", {"action": "hook_add", "url": self.URL, "events": ["patient.created", "bogus"], "note": "Accounts"})
        self.assertEqual(r.status_code, 200)
        text = r.get_data(as_text=True)
        self.assertIn("whsec_", text)
        self.assertEqual(self.one("SELECT events FROM webhook_endpoints")[0], "patient.created")
        self.assertNotIn("whsec_", self.page(m, "/integrations"))
        ep = self.one("SELECT id FROM webhook_endpoints")[0]
        self.assertEqual(self.post(m, "/integrations", {"action": "hook_test", "hook_id": str(ep)}).status_code, 302)
        self.assertEqual(self.one("SELECT event FROM webhook_deliveries")[0], "ping")
        self.assertIn("Waiting", self.page(m, "/integrations"))
        r = self.post(m, "/integrations", {"action": "hook_secret", "hook_id": str(ep)})
        self.assertIn("whsec_", r.get_data(as_text=True))
        self.assertEqual(self.post(m, "/integrations", {"action": "hook_off", "hook_id": str(ep)}).status_code, 302)
        self.assertEqual(self.post(m, "/integrations", {"action": "hook_on", "hook_id": "999"}).status_code, 404)
        self.assertEqual(self.post(m, "/integrations", {"action": "nonsense"}).status_code, 400)
        out = self.app.test_cli_runner().invoke(args=["deliver-webhooks"]).output
        self.assertIn("gave up", out)
        c2 = self.login("mgr2")
        self.assertEqual(self.post(c2, "/integrations", {"action": "hook_off", "hook_id": str(ep)}).status_code, 404)
        self.assertEqual(self.one("SELECT active FROM webhook_endpoints")[0], 0)           # still the first manager's change only


# ===================================================================== upgrade
class UpgradeTests(Ops):
    def test_an_older_database_gets_the_new_column_and_tables(self):
        import sqlite3
        import tempfile
        path = tempfile.mkdtemp() + "/old.sqlite"
        conn = dbmod.connect(path)
        dbmod.init_schema(conn)
        conn.execute("DROP TABLE lab_cases")
        conn.execute("DROP TABLE webhook_deliveries")
        conn.execute("ALTER TABLE clinics DROP COLUMN org_id")
        conn.commit()
        dbmod.init_schema(conn)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(clinics)")}
        self.assertIn("org_id", cols)
        for t in ("lab_cases", "webhook_deliveries", "stock_moves", "api_keys", "organisations"):
            self.assertTrue(conn.execute("SELECT 1 FROM sqlite_master WHERE name = ?", (t,)).fetchone(), t)
        conn.close()

    def test_every_new_screen_opens(self):
        conn = self.db()
        LAB.save_lab(conn, self.clinic_row(), 1, None, "L")
        conn.commit()
        for name in ("mgr",):
            c = self.login(name)
            for url in ("/reports/kpi", "/lab", "/lab/labs", "/stock", "/stock/order", "/stock/expiry", "/stock/suppliers", "/integrations",
                        "/integrations/docs", "/patients/1/lab", "/lab/new?patient_id=1", "/stock/items/new", "/"):
                self.page(c, url)


if __name__ == "__main__":
    unittest.main()
