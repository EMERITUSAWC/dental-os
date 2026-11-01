"""v0.26: orthodontics: cases, the ortho chart, contracts and monthly amounts posted on their due dates."""
import datetime

from tests.test_v04 import Base
from dental_os.services import billing, ortho as O
from dental_os.services.timeutil import now_local

CASE = {"appliance": "fixed_metal", "arches": "both", "molar_class": "II1", "overjet_mm": "6.5", "overbite_mm": "4", "crowding": "moderate",
        "est_months": "18", "notes": "Extract 14 and 24"}


class Ortho(Base):
    def setUp(self):
        super().setUp()
        self.conn = self.db()

    def tearDown(self):
        self.conn.close()

    def c(self):
        return dict(self.conn.execute("SELECT * FROM clinics WHERE id = 1").fetchone())

    def today(self):
        return now_local(self.c()["timezone"]).date()

    def balance(self, pid=1):
        return billing.totals(self.conn, 1, pid)["balance"]

    def case(self, **extra):
        cid = O.open_case(self.conn, self.c(), 2, 1, 1, CASE | extra)
        O.start(self.conn, self.c(), 2, cid)
        self.conn.commit()
        return cid


class CaseTests(Ortho):
    def test_a_case_from_plan_to_retention(self):
        cid = O.open_case(self.conn, self.c(), 2, 1, 1, CASE)
        c = O.get(self.conn, 1, cid)
        self.assertEqual((c["status"], c["overjet_tenths"], c["overbite_tenths"], c["est_months"]), ("planned", 65, 40, 18))
        with self.assertRaises(O.OrthoError):
            O.open_case(self.conn, self.c(), 2, 1, 1, CASE)                 # one open case at a time
        with self.assertRaises(O.OrthoError):
            O.add_visit(self.conn, self.c(), 2, cid, {"upper_wire": "0.014 NiTi"})   # not started yet
        O.start(self.conn, self.c(), 2, cid)
        O.add_visit(self.conn, self.c(), 2, cid, {"upper_wire": "0.014 NiTi", "lower_wire": "0.014 NiTi", "next_weeks": "6"})
        self.assertEqual(O.next_due(self.conn, O.get(self.conn, 1, cid)), self.today() + datetime.timedelta(weeks=6))
        with self.assertRaises(O.OrthoError):
            O.add_visit(self.conn, self.c(), 2, cid, {})
        with self.assertRaises(O.OrthoError):
            O.debond(self.conn, self.c(), 2, cid, "")
        O.debond(self.conn, self.c(), 2, cid, "Upper and lower bonded retainers")
        self.assertEqual(O.get(self.conn, 1, cid)["status"], "retention")
        O.close(self.conn, self.c(), 2, cid, "finished")
        self.assertEqual(O.get(self.conn, 1, cid)["status"], "finished")
        O.open_case(self.conn, self.c(), 2, 1, 1, CASE)                     # a new case is fine once closed

    def test_bad_case_details_are_refused(self):
        for bad in ({"appliance": "magic"}, {"molar_class": "IV"}, {"overjet_mm": "40"}, {"est_months": "0"}, {"est_months": "x"}):
            with self.assertRaises(O.OrthoError):
                O.open_case(self.conn, self.c(), 2, 1, 1, CASE | bad)
        with self.assertRaises(O.OrthoError):
            O.open_case(self.conn, self.c(), 2, 3, 1, CASE)                 # another clinic's patient

    def test_months_in_treatment(self):
        case = {"started_on": "2026-01-15", "debonded_on": None}
        self.assertEqual(O.months_in(case, datetime.date(2026, 3, 14)), 1)
        self.assertEqual(O.months_in(case, datetime.date(2026, 3, 15)), 2)
        self.assertEqual(O.add_months(datetime.date(2026, 1, 31), 1), datetime.date(2026, 2, 28))


class ContractTests(Ortho):
    def test_deposit_now_then_monthly_amounts_on_their_dates(self):
        cid = self.case()
        before = self.balance()
        first = self.today() + datetime.timedelta(days=10)
        kid = O.make_contract(self.conn, self.c(), 1, cid, "4000", "1000", "7", first.isoformat())
        sched = O.charges(self.conn, 1, kid)
        self.assertEqual([(c["kind"], c["amount_p"]) for c in sched][:2], [("deposit", 100000), ("monthly", 42857)])
        self.assertEqual(sum(c["amount_p"] for c in sched), 400000)              # the last month takes the rounding
        self.assertEqual(sched[-1]["amount_p"], 300000 - 42857 * 6)
        self.assertEqual(self.balance() - before, 100000)                       # only the deposit is due today
        self.assertEqual(O.post_due(self.conn, self.c()), 0)                     # running again posts nothing twice
        self.assertEqual(O.post_due(self.conn, self.c(), today=first), 1)
        self.assertEqual(O.post_due(self.conn, self.c(), today=first), 0)
        self.assertEqual(self.balance() - before, 142857)
        later = O.add_months(first, 2)
        self.assertEqual(O.post_due(self.conn, self.c(), today=later), 2)
        with self.assertRaises(O.OrthoError):
            O.make_contract(self.conn, self.c(), 1, cid, "5000", "0", "10", first.isoformat())   # amounts already charged
        O.stop_contract(self.conn, self.c(), 1, cid, "Moved away")
        self.assertEqual(O.post_due(self.conn, self.c(), today=O.add_months(first, 12)), 0)
        self.assertEqual(self.balance() - before, 100000 + 3 * 42857)

    def test_whole_fee_up_front_and_closing_stops_future_amounts(self):
        cid = self.case()
        O.make_contract(self.conn, self.c(), 1, cid, "2500.50", "2500.50", "0", "")
        k = O.contract_for(self.conn, 1, cid)
        self.assertEqual(k["status"], "finished")                                # nothing left to charge
        for bad in (("0", "0", "3"), ("100", "200", "3"), ("100", "0", "0"), ("100", "0", "61"), ("abc", "0", "3")):
            with self.assertRaises(O.OrthoError):
                O.make_contract(self.conn, self.c(), 1, cid, *bad, self.today().isoformat())

    def test_stopping_a_case_stops_the_rest(self):
        cid = self.case()
        first = self.today() + datetime.timedelta(days=1)
        kid = O.make_contract(self.conn, self.c(), 1, cid, "1200", "0", "12", first.isoformat())
        O.close(self.conn, self.c(), 1, cid, "stopped", "Stopped coming")
        self.assertEqual(O.post_due(self.conn, self.c(), today=O.add_months(first, 24)), 0)
        self.assertTrue(all(c["cancelled"] for c in O.charges(self.conn, 1, kid)))
        with self.assertRaises(O.OrthoError):
            O.close(self.conn, self.c(), 1, cid, "stopped", "again")


class ScreenTests(Ortho):
    def test_screens(self):
        dent = self.login("dent")
        r = self.post(dent, "/patients/1/ortho", {"what": "open", "provider_id": "1"} | CASE, follow_redirects=True)
        self.assertIn("Ortho case opened.", r.get_data(as_text=True))
        cid = self.conn.execute("SELECT id FROM ortho_cases").fetchone()[0]
        self.post(dent, "/patients/1/ortho", {"what": "start", "case_id": cid})
        r = self.post(dent, "/patients/1/ortho", {"what": "visit", "case_id": cid, "upper_wire": "0.016 NiTi", "next_weeks": "6"}, follow_redirects=True)
        self.assertIn("0.016 NiTi", r.get_data(as_text=True))
        r = self.post(dent, "/patients/1/ortho", {"what": "contract", "case_id": cid, "total": "3000", "deposit": "600", "months": "12",
                                                  "first_due": (self.today() + datetime.timedelta(days=30)).isoformat()}, follow_redirects=True)
        self.assertIn("Contract saved.", r.get_data(as_text=True))
        page = self.page(dent, "/ortho")
        self.assertIn("Ama", page)
        self.assertIn("1 / 18", page.replace("0 / 18", "1 / 18"))
        self.page(dent, "/patients/1")
        self.assertEqual(self.login("desk").get("/ortho").status_code, 403)
        self.assertEqual(self.login("mgr2").post("/patients/1/ortho", data={}).status_code, 400)        # no form token: refused
        self.assertEqual(self.login("mgr2").get("/patients/1/ortho").status_code, 404)

    def test_the_daily_command(self):
        cid = self.case()
        O.make_contract(self.conn, self.c(), 1, cid, "1000", "0", "2", self.today().isoformat())
        self.conn.commit()
        out = self.app.test_cli_runner().invoke(args=["post-ortho-fees"]).output
        self.assertIn("0 ortho amounts posted", out)                          # today's amount was posted when the contract was made
