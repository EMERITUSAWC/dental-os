"""v0.18: insurance depth. Shares by kind of treatment, the excess, waiting periods, how-often limits, a benefit year that does
not start in January, an orthodontic limit, a second payer after the first, cover checks, and one payment from a payer split across
many claims."""
from datetime import timedelta

from tests.test_v06 import Money
from dental_os.services import benefits as BEN, insurance as INS, payerpay as PP
from dental_os.services.timeutil import now_local


class Ins(Money):
    def setUp(self):
        super().setUp()
        self.conn = self.db()
        self.conn.execute("UPDATE clinics SET payment_methods = 'cash,card,mobile_money,bank_transfer,insurance' WHERE id = 1")
        self.clinic = dict(self.conn.execute("SELECT * FROM clinics WHERE id = 1").fetchone())
        self.today = now_local(self.clinic["timezone"]).date()
        self.payer = INS.save_payer(self.conn, self.clinic, 1, None, "Acme Health", "insurer", False, None, None, None)
        self.payer2 = INS.save_payer(self.conn, self.clinic, 1, None, "Second Shield", "insurer", False, None, None, None)
        self.codes = {r["code"]: dict(r) for r in self.conn.execute("SELECT * FROM procedure_codes WHERE clinic_id = 1")}
        for code, fee in (("CONS", 10000), ("SCALE", 20000), ("CROWN", 200000), ("ORTHO-REV", 50000), ("FILL", 40000)):
            self.conn.execute("UPDATE procedure_codes SET fee_p = ? WHERE clinic_id = 1 AND code = ?", (fee, code))
            self.codes[code]["fee_p"] = fee
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        super().tearDown() if hasattr(super(), "tearDown") else None

    def cover(self, payer=None, pct=80, max_p=None, valid_from=None, pid=1):
        cid = INS.add_cover(self.conn, self.clinic, 1, pid, payer or self.payer, "M1", None, pct, max_p, valid_from, None)
        return cid

    def row(self, cover_id):
        return INS.get_cover(self.conn, 1, cover_id)

    def done(self, code, days_ago=0, pid=1):
        fee = self.codes[code]["fee_p"]
        day = (self.today - timedelta(days=days_ago)).isoformat()
        cur = self.conn.execute("""INSERT INTO procedures(clinic_id, patient_id, code_id, status, fee_p, planned_on, done_on)
                                   VALUES (1, ?, ?, 'done', ?, ?, ?)""", (pid, self.codes[code]["id"], fee, day, day))
        return cur.lastrowid

    def plan(self, *codes):
        return [{"code_id": self.codes[c]["id"], "fee_p": self.codes[c]["fee_p"]} for c in codes]


class EstimateTests(Ins):
    def test_with_no_terms_it_works_as_before(self):
        self.cover(pct=80)
        cover, res = INS.estimate_lines(self.conn, self.clinic, 1, self.plan("FILL"))
        self.assertEqual((res[0]["payer_p"], res[0]["patient_p"]), (32000, 8000))

    def test_shares_by_kind_excess_and_preventive_skipping_it(self):
        c = self.cover(pct=80)
        INS.save_benefit_terms(self.conn, self.clinic, 1, c, 5000, False, 1, None, None,
                               {"Examination": (100, 0), "Preventive": (100, 0), "Prosthetics": (50, 0)}, [])
        _cov, res = INS.estimate_lines(self.conn, self.clinic, 1, self.plan("CONS", "CROWN", "FILL"))
        self.assertEqual(res[0]["payer_p"], 10000)                       # check-up: 100%, no excess
        self.assertEqual(res[1]["payer_p"], (200000 - 5000) * 50 // 100)  # crown: excess first, then 50%
        self.assertEqual(res[2]["payer_p"], 40000 * 80 // 100)            # excess already used up by the crown
        self.assertIn("Excess applied.", res[1]["notes"])
        INS.save_benefit_terms(self.conn, self.clinic, 1, c, 5000, True, 1, None, None, {"Examination": (100, 0)}, [])
        _cov, res = INS.estimate_lines(self.conn, self.clinic, 1, self.plan("CONS"))
        self.assertEqual(res[0]["payer_p"], 5000)                         # excess now applies to check-ups too

    def test_waiting_period(self):
        start = (self.today - timedelta(days=60)).isoformat()
        c = self.cover(pct=80, valid_from=start)
        INS.save_benefit_terms(self.conn, self.clinic, 1, c, 0, False, 1, None, None, {"Prosthetics": (50, 6)}, [])
        _cov, res = INS.estimate_lines(self.conn, self.clinic, 1, self.plan("CROWN", "FILL"))
        self.assertEqual(res[0]["payer_p"], 0)
        self.assertTrue(res[0]["notes"][0].startswith("Waiting period"))
        self.assertEqual(res[1]["payer_p"], 32000)
        INS.save_benefit_terms(self.conn, self.clinic, 1, c, 0, False, 1, None, (self.today - timedelta(days=400)).isoformat(), {"Prosthetics": (50, 6)}, [])
        _cov, res = INS.estimate_lines(self.conn, self.clinic, 1, self.plan("CROWN"))
        self.assertEqual(res[0]["payer_p"], 100000)

    def test_how_often_limits_count_past_work_and_the_plan_itself(self):
        c = self.cover(pct=100)
        INS.save_benefit_terms(self.conn, self.clinic, 1, c, 0, False, 1, None, None, {}, [(self.codes["SCALE"]["id"], 2, 12)])
        self.done("SCALE", days_ago=100)
        self.done("SCALE", days_ago=500)                                  # too long ago to count
        _cov, res = INS.estimate_lines(self.conn, self.clinic, 1, self.plan("SCALE", "SCALE"))
        self.assertEqual([r["payer_p"] for r in res], [20000, 0])
        self.assertTrue(res[1]["notes"][0].startswith("Limit reached"))

    def test_yearly_maximum_and_a_benefit_year_from_july(self):
        c = self.cover(pct=100, max_p=50000)
        _cov, res = INS.estimate_lines(self.conn, self.clinic, 1, self.plan("FILL", "FILL"))
        self.assertEqual([r["payer_p"] for r in res], [40000, 10000])
        INS.save_benefit_terms(self.conn, self.clinic, 1, c, 0, False, 7, None, None, {}, [])
        start, end = BEN.benefit_year(self.row(c), self.today)
        self.assertEqual(start.month, 7)
        self.assertEqual(start.day, 1)
        self.assertEqual((end - start).days in (364, 365), True)
        self.assertTrue(start <= self.today <= end)

    def test_orthodontic_lifetime_limit(self):
        c = self.cover(pct=100)
        INS.save_benefit_terms(self.conn, self.clinic, 1, c, 0, False, 1, 60000, None, {}, [])
        _cov, res = INS.estimate_lines(self.conn, self.clinic, 1, self.plan("ORTHO-REV", "ORTHO-REV"))
        self.assertEqual([r["payer_p"] for r in res], [50000, 10000])
        p = self.done("ORTHO-REV")
        INS.create_claim(self.conn, self.clinic, 1, 1, c, [p])
        _cov, res = INS.estimate_lines(self.conn, self.clinic, 1, self.plan("ORTHO-REV"))
        self.assertEqual(res[0]["payer_p"], 10000)                        # 50 000 already claimed for orthodontics

    def test_a_second_payer_pays_what_is_left_up_to_its_own_share(self):
        self.cover(pct=80)
        self.cover(payer=self.payer2, pct=50)
        _cov, res = INS.estimate_lines(self.conn, self.clinic, 1, self.plan("FILL"))
        self.assertEqual((res[0]["first_p"], res[0]["second_p"], res[0]["patient_p"]), (32000, 8000, 0))
        self.cover(payer=self.payer2, pct=10, pid=2)
        _cov, res = INS.estimate_lines(self.conn, self.clinic, 2, self.plan("FILL"))
        self.assertEqual(res[0]["second_p"], 0)                           # patient 2 has only one cover

    def test_terms_are_checked(self):
        c = self.cover()
        for args, words in (((-1, False, 1, None, None, {}, []), "negative"), ((0, False, 13, None, None, {}, []), "month"),
                            ((0, False, 1, 0, None, {}, []), "orthodontic"), ((0, False, 1, None, None, {"Preventive": (120, 0)}, []), "0 to 100"),
                            ((0, False, 1, None, None, {"Preventive": (50, 90)}, []), "waiting"),
                            ((0, False, 1, None, None, {}, [(self.codes["SCALE"]["id"], 0, 12)]), "limit"),
                            ((0, False, 1, None, None, {}, [(99999, 1, 12)]), "procedure")):
            with self.assertRaises(INS.InsuranceError) as ctx:
                INS.save_benefit_terms(self.conn, self.clinic, 1, c, *args)
            self.assertIn(words, str(ctx.exception).lower())


class ClaimTests(Ins):
    def test_claim_lines_carry_the_excess_and_notes(self):
        c = self.cover(pct=80)
        INS.save_benefit_terms(self.conn, self.clinic, 1, c, 5000, False, 1, None, None, {}, [])
        p = self.done("FILL")
        claim = INS.create_claim(self.conn, self.clinic, 1, 1, c, [p])
        item = INS.claim_items(self.conn, 1, claim)[0]
        self.assertEqual((item["deductible_p"], item["estimate_p"]), (5000, (40000 - 5000) * 80 // 100))
        self.assertIn("Excess", item["note"])
        p2 = self.done("FILL")
        claim2 = INS.create_claim(self.conn, self.clinic, 1, 1, c, [p2])
        self.assertEqual(INS.claim_items(self.conn, 1, claim2)[0]["deductible_p"], 0)        # excess already met this year

    def test_second_payer_claim_after_the_first(self):
        c1 = self.cover(pct=80)
        c2 = self.cover(payer=self.payer2, pct=50)
        p = self.done("FILL")
        first = INS.create_claim(self.conn, self.clinic, 1, 1, c1, [p])
        self.assertEqual([r["id"] for r in INS.unclaimed(self.conn, 1, 1, "claim", c2)], [p])     # still claimable from the second
        self.assertEqual(INS.unclaimed(self.conn, 1, 1, "claim", c1), [])
        second = INS.create_claim(self.conn, self.clinic, 1, 1, c2, [p])
        self.assertEqual(INS.get_claim(self.conn, 1, first)["estimate_p"], 32000)
        self.assertEqual(INS.get_claim(self.conn, 1, second)["estimate_p"], 8000)
        with self.assertRaises(INS.InsuranceError):
            INS.create_claim(self.conn, self.clinic, 1, 1, c2, [p])

    def test_make_main_swaps_the_order(self):
        c1 = self.cover(pct=80)
        c2 = self.cover(payer=self.payer2, pct=50)
        self.assertEqual((self.row(c1)["cover_rank"], self.row(c2)["cover_rank"]), (1, 2))
        INS.make_main(self.conn, self.clinic, 1, c2)
        self.assertEqual((self.row(c1)["cover_rank"], self.row(c2)["cover_rank"]), (2, 1))
        INS.end_cover(self.conn, self.clinic, 1, c2)
        self.assertEqual(self.row(c1)["cover_rank"], 1)

    def test_cover_check(self):
        c = self.cover()
        with self.assertRaises(INS.InsuranceError):
            INS.verify(self.conn, self.clinic, 1, c, "")
        INS.verify(self.conn, self.clinic, 1, c, "Spoke to Ama at Acme, active until December")
        self.assertEqual(self.row(c)["verified_on"], self.today.isoformat())


class PayerPaymentTests(Ins):
    def claims(self):
        c1 = self.cover(pct=100)
        c2 = self.cover(pct=100, pid=2)
        a = INS.create_claim(self.conn, self.clinic, 1, 1, c1, [self.done("FILL")])
        b = INS.create_claim(self.conn, self.clinic, 1, 2, c2, [self.done("SCALE", pid=2)])
        for k in (a, b):
            INS.submit(self.conn, self.clinic, 1, k)
        return a, b

    def test_one_transfer_pays_two_claims(self):
        a, b = self.claims()
        pp = PP.post(self.conn, self.clinic, 1, self.payer, None, 55000, "TRF-001",
                     [{"claim_id": a, "amount_p": 40000}, {"claim_id": b, "amount_p": 15000, "writeoff_p": 5000}])
        self.assertEqual(INS.get_claim(self.conn, 1, a)["status"], "paid")
        self.assertEqual(INS.get_claim(self.conn, 1, b)["status"], "paid")
        lines = PP.lines(self.conn, 1, pp)
        self.assertEqual(sorted(l["amount_p"] for l in lines), [15000, 40000])
        self.assertTrue(all(l["method"] == "insurance" for l in lines))
        bal = self.conn.execute("SELECT COALESCE(SUM(amount_p), 0) FROM adjustments WHERE patient_id = 2").fetchone()[0]
        self.assertEqual(bal, -5000)
        self.assertEqual(PP.open_claims(self.conn, 1, self.payer), [])

    def test_the_shares_must_add_up_and_be_for_this_payer(self):
        a, b = self.claims()
        for args, words in ((dict(amount_p=50000, allocations=[{"claim_id": a, "amount_p": 40000}]), "add up"),
                            (dict(amount_p=10, allocations=[{"claim_id": 99999, "amount_p": 10}]), "not open"),
                            (dict(amount_p=0, allocations=[]), "at least one"),
                            (dict(amount_p=10, allocations=[{"claim_id": a, "amount_p": 10}], reference=""), "reference")):
            kw = dict(reference="R1")
            kw.update(args)
            with self.assertRaises(PP.PayerPayError) as ctx:
                PP.post(self.conn, self.clinic, 1, self.payer, None, kw["amount_p"], kw["reference"], kw["allocations"])
            self.assertIn(words, str(ctx.exception))
        PP.post(self.conn, self.clinic, 1, self.payer, None, 100, "R2", [{"claim_id": a, "amount_p": 100}])
        with self.assertRaises(PP.PayerPayError) as ctx:
            PP.post(self.conn, self.clinic, 1, self.payer, None, 100, "R2", [{"claim_id": a, "amount_p": 100}])
        self.assertIn("already recorded", str(ctx.exception))
        self.assertEqual(INS.get_claim(self.conn, 1, a)["status"], "partly_paid")

    def test_closing_a_claim_with_nothing_paid(self):
        a, b = self.claims()
        PP.post(self.conn, self.clinic, 1, self.payer, None, 20000, "R3", [{"claim_id": b, "amount_p": 20000}, {"claim_id": a, "amount_p": 0, "writeoff_p": 40000}])
        self.assertEqual(INS.get_claim(self.conn, 1, a)["status"], "paid")
        self.assertEqual(INS.get_claim(self.conn, 1, a)["writeoff_p"], 40000)

    def test_ageing_by_payer(self):
        a, b = self.claims()
        self.conn.execute("UPDATE claims SET submitted_on = ? WHERE id = ?", ((self.today - timedelta(days=75)).isoformat(), a))
        rows = PP.ageing_by_payer(self.conn, self.clinic)
        self.assertEqual(rows[0]["payer"], "Acme Health")
        self.assertEqual((rows[0]["61_90"], rows[0]["0_30"], rows[0]["claims"]), (40000, 20000, 2))

    def test_through_the_screens(self):
        a, b = self.claims()
        self.conn.commit()
        c = self.login("mgr")
        page = self.page(c, f"/payer-payments/new?payer_id={self.payer}")
        self.assertIn("Still expected", page)
        r = self.post(c, "/payer-payments/new", {"payer_id": str(self.payer), "amount": "550", "reference": "TRF-9",
                                                  f"amt_{a}": "400", f"amt_{b}": "150", f"wo_{b}": "50"})
        self.assertEqual(r.status_code, 302)
        page = self.page(c, r.headers["Location"])
        self.assertIn("TRF-9", page)
        self.assertIn("R-", page)
        self.assertIn("TRF-9", self.page(c, "/payer-payments"))
        r = self.post(c, "/payer-payments/new", {"payer_id": str(self.payer), "amount": "1", "reference": "X"}, follow_redirects=True)
        self.assertIn("no sent claims", r.get_data(as_text=True))


class ScreenTests(Ins):
    def test_cover_terms_screen_and_insurance_page(self):
        c1 = self.cover(pct=80)
        self.cover(payer=self.payer2, pct=50)
        self.conn.execute("""INSERT INTO procedures(clinic_id, patient_id, code_id, status, fee_p, planned_on) VALUES (1, 1, ?, 'planned', 40000, ?)""",
                          (self.codes["FILL"]["id"], self.today.isoformat()))
        self.conn.commit()
        c = self.login("desk")
        page = self.page(c, "/patients/1/insurance")
        for words in ("Pays first", "Pays second", "Second payer", "Cover terms", "Not checked with the payer"):
            self.assertIn(words, page)
        page = self.page(c, f"/cover/{c1}/benefits")
        self.assertIn("Share by kind of treatment", page)
        cats = [r[0] for r in self.db().execute("SELECT DISTINCT category FROM procedure_codes WHERE clinic_id = 1 ORDER BY category")]
        form = {"deductible": "50", "year_start_month": "4", "ortho_max": "", f"pct_{cats.index('Restorative')}": "60",
                "lim_code_0": str(self.codes["SCALE"]["id"]), "lim_times_0": "2", "lim_months_0": "12"}
        r = self.post(c, f"/cover/{c1}/benefits", form)
        self.assertEqual(r.status_code, 302)
        cover = self.row(c1)
        self.assertEqual((cover["deductible_p"], cover["year_start_month"]), (5000, 4))
        page = self.page(c, "/patients/1/insurance")
        self.assertIn("Excess", page)
        self.assertIn("1 how-often limits", page)
        r = self.post(c, f"/cover/{c1}/verify", {"note": "Confirmed by phone"})
        self.assertIn("Checked", self.page(c, "/patients/1/insurance"))
        self.assertEqual(c.get("/cover/99999/benefits").status_code, 404)

    def test_french(self):
        self.cover()
        self.conn.execute("UPDATE clinics SET language = 'fr' WHERE id = 1")
        self.conn.commit()
        c = self.login("mgr")
        self.assertIn("Paiements des organismes", self.page(c, "/payer-payments"))
        self.assertIn("Conditions de la couverture", self.page(c, "/patients/1/insurance"))
