"""v0.6: payers and price lists, cover, estimates, claims, invoices with tax, statements, ageing."""
import sqlite3
import unittest
from datetime import timedelta

from dental_os import db as dbmod
from dental_os.services import ageing as AG, billing, insurance as INS, invoices as INV, treatment as TR
from tests.test_v04 import Base
from tests.test_v05 import Money, today


class Fin(Money):
    """Clinic one: crown 1500.00, filling 300.00, consultation 100.00, scale 200.00. Patient 1 is the adult."""

    def setUp(self):
        super().setUp()
        conn = self.db()
        conn.execute("INSERT INTO payers(clinic_id, name, kind, contracted) VALUES (1, 'Acme Health', 'insurer', 0)")       # 1
        conn.execute("INSERT INTO payers(clinic_id, name, kind, contracted) VALUES (1, 'Contract Cover', 'insurer', 1)")    # 2
        conn.execute("INSERT INTO payers(clinic_id, name, kind, contracted) VALUES (2, 'Other Clinic Payer', 'insurer', 0)")  # 3
        conn.commit()
        conn.close()

    def done(self, code="FILL", pid=1, fee=None, days_ago=0):
        conn = self.db()
        clinic = self.clinic()
        pr = TR.add_planned(conn, clinic, 1, pid, self.cid(code), fee_p=fee)
        TR.mark_done(conn, clinic, 1, pr)
        if days_ago:
            conn.execute("UPDATE procedures SET done_on = ? WHERE id = ?", ((today() - timedelta(days=days_ago)).isoformat(), pr))
        conn.commit()
        conn.close()
        return pr

    def planned(self, code="FILL", pid=1):
        conn = self.db()
        pr = TR.add_planned(conn, self.clinic(), 1, pid, self.cid(code))
        conn.commit()
        conn.close()
        return pr

    def cover(self, payer=1, pct=80, maxp=None, frm=None, to=None, pid=1):
        conn = self.db()
        cv = INS.add_cover(conn, self.clinic(), 1, pid, payer, "M123", "Ama Mensah", pct, maxp, frm, to)
        conn.commit()
        conn.close()
        return cv

    def row(self, sql, *args):
        return self.db().execute(sql, args).fetchone()

    def claim(self, c, procs, cover, kind="claim", pid=1):
        r = self.post(c, f"/patients/{pid}/claims", {"procedure_id": [str(p) for p in procs], "cover_id": str(cover), "kind": kind})
        self.assertEqual(r.status_code, 302)
        return int(r.headers["Location"].rsplit("/", 1)[1]) if "/claims/" in r.headers["Location"] else None


class EstimateTests(Fin):
    def est(self, cv, code, fee, remaining=None):
        conn = self.db()
        cover = conn.execute("SELECT * FROM patient_cover WHERE id = ?", (cv,)).fetchone()
        return INS.estimate(conn, self.clinic(), cover, self.cid(code), fee, today(), remaining)

    def test_percentage_of_the_clinic_price(self):
        cv = self.cover(pct=80)
        e = self.est(cv, "FILL", 30000)
        self.assertEqual((e["payer_p"], e["patient_p"], e["writeoff_p"]), (24000, 6000, 0))

    def test_payer_price_list_sets_the_allowed_amount(self):
        cv = self.cover(pct=80)
        conn = self.db()
        INS.set_payer_fees(conn, self.clinic(), 1, 1, {self.cid("FILL"): 25000})
        conn.commit()
        e = self.est(cv, "FILL", 30000)
        self.assertEqual(e["allowed_p"], 25000)
        self.assertEqual(e["payer_p"], 20000)
        self.assertEqual(e["patient_p"], 10000)       # not contracted: the patient pays the rest, including the price gap

    def test_contracted_payer_writes_off_the_gap(self):
        cv = self.cover(payer=2, pct=80)
        conn = self.db()
        INS.set_payer_fees(conn, self.clinic(), 1, 2, {self.cid("FILL"): 25000})
        conn.commit()
        e = self.est(cv, "FILL", 30000)
        self.assertEqual((e["payer_p"], e["writeoff_p"], e["patient_p"]), (20000, 5000, 5000))

    def test_yearly_maximum_caps_and_is_shared_across_lines(self):
        cv = self.cover(pct=100, maxp=40000)
        conn = self.db()
        cover, ests = INS.estimate_lines(conn, self.clinic(), 1, [{"code_id": self.cid("FILL"), "fee_p": 30000},
                                                                  {"code_id": self.cid("FILL"), "fee_p": 30000}])
        self.assertEqual([e["payer_p"] for e in ests], [30000, 10000])
        self.assertEqual([e["patient_p"] for e in ests], [0, 20000])

    def test_cover_outside_its_dates_pays_nothing(self):
        cv = self.cover(frm=(today() + timedelta(days=5)).isoformat())
        e = self.est(cv, "FILL", 30000)
        self.assertFalse(e["in_force"])
        self.assertEqual((e["payer_p"], e["patient_p"]), (0, 30000))
        cv2 = self.cover(to=(today() - timedelta(days=1)).isoformat())
        self.assertFalse(self.est(cv2, "FILL", 30000)["in_force"])

    def test_cover_validation(self):
        conn = self.db()
        for bad in (dict(coverage_pct=101), dict(coverage_pct=-1), dict(annual_max_p=0)):
            kw = dict(payer_id=1, member_no=None, holder_name=None, coverage_pct=80, annual_max_p=None, valid_from=None, valid_to=None)
            kw.update(bad)
            with self.assertRaises(INS.InsuranceError):
                INS.add_cover(conn, self.clinic(), 1, 1, **kw)
        with self.assertRaises(INS.InsuranceError):          # another clinic's payer
            INS.add_cover(conn, self.clinic(), 1, 1, 3, None, None, 80, None, None, None)
        with self.assertRaises(INS.InsuranceError):          # end before start
            INS.add_cover(conn, self.clinic(), 1, 1, 1, None, None, 80, None, "2026-05-02", "2026-05-01")

    def test_estimate_on_planned_work_shows_on_insurance_page(self):
        self.cover(pct=50)
        self.planned("FILL")
        c = self.login("dent")
        page = self.page(c, "/patients/1/insurance")
        self.assertIn("Estimate for planned work", page)
        self.assertIn("GH₵ 150.00", page)


class ClaimTests(Fin):
    def test_claim_lifecycle_full_payment(self):
        cv = self.cover(pct=80)
        pr = self.done("FILL")
        c = self.login("desk")
        cid = self.claim(c, [pr], cv)
        cl = self.row("SELECT * FROM claims WHERE id = ?", cid)
        self.assertEqual((cl["claim_no"], cl["status"], cl["total_fee_p"], cl["estimate_p"]), (1, "draft", 30000, 24000))
        self.post(c, f"/claims/{cid}/submit", {"sent_on": today().isoformat(), "payer_ref": "REF9"})
        self.assertEqual(self.row("SELECT status FROM claims WHERE id = ?", cid)[0], "submitted")
        before = self.balance()
        self.assertEqual(before, 30000)
        self.post(c, f"/claims/{cid}/payment", {"amount": "240", "reference": "EFT-1"})
        cl = self.row("SELECT * FROM claims WHERE id = ?", cid)
        self.assertEqual((cl["status"], cl["paid_p"]), ("paid", 24000))
        self.assertEqual(self.balance(), 6000)                       # patient still owes their share
        pay = self.row("SELECT * FROM payments WHERE claim_id = ?", cid)
        self.assertEqual((pay["method"], pay["amount_p"], pay["purpose"]), ("insurance", 24000, "treatment"))
        self.assertIn("Receipt", self.page(c, f"/claims/{cid}"))

    def test_part_payment_then_final_with_writeoff(self):
        cv = self.cover(payer=2, pct=100)
        pr = self.done("CROWN")
        c = self.login("mgr")
        cid = self.claim(c, [pr], cv)
        self.post(c, f"/claims/{cid}/submit", {})
        self.post(c, f"/claims/{cid}/payment", {"amount": "1000", "reference": "P1"})
        self.assertEqual(self.row("SELECT status FROM claims WHERE id = ?", cid)[0], "partly_paid")
        self.post(c, f"/claims/{cid}/payment", {"amount": "400", "reference": "P2", "writeoff": "100",
                                                 "writeoff_note": "Contract price difference", "final": "1"})
        cl = self.row("SELECT * FROM claims WHERE id = ?", cid)
        self.assertEqual((cl["status"], cl["paid_p"], cl["writeoff_p"]), ("paid", 140000, 10000))
        self.assertEqual(self.balance(), 0)                           # 1500 - 1400 paid - 100 written off
        adj = self.row("SELECT reason, amount_p FROM adjustments")
        self.assertEqual((adj["reason"], adj["amount_p"]), ("write_off", -10000))

    def test_cannot_claim_the_same_work_twice(self):
        cv = self.cover()
        pr = self.done("FILL")
        c = self.login("dent")
        self.claim(c, [pr], cv)
        r = self.post(c, "/patients/1/claims", {"procedure_id": [str(pr)], "cover_id": str(cv), "kind": "claim"}, follow_redirects=True)
        self.assertIn("already be on another claim", r.get_data(as_text=True))
        self.assertEqual(self.row("SELECT COUNT(*) FROM claims")[0], 1)

    def test_reject_resubmit_and_cancel_free_the_work(self):
        cv = self.cover()
        pr = self.done("FILL")
        c = self.login("dent")
        cid = self.claim(c, [pr], cv)
        self.post(c, f"/claims/{cid}/submit", {})
        self.post(c, f"/claims/{cid}/reject", {"reason": "Missing X-ray"})
        self.assertEqual(self.row("SELECT status, note FROM claims WHERE id = ?", cid)[:], ("rejected", "Missing X-ray"))
        # the work can go on a fresh claim while the old one is rejected
        self.assertEqual(len(INS.unclaimed(self.db(), 1, 1, "claim")), 1)
        self.post(c, f"/claims/{cid}/submit", {})                                # or send the same one again
        self.assertEqual(self.row("SELECT status FROM claims WHERE id = ?", cid)[0], "submitted")
        self.post(c, f"/claims/{cid}/cancel")
        self.assertEqual(self.row("SELECT status FROM claims WHERE id = ?", cid)[0], "cancelled")
        self.assertEqual(len(INS.unclaimed(self.db(), 1, 1, "claim")), 1)

    def test_cannot_cancel_after_money_received(self):
        cv = self.cover()
        pr = self.done("FILL")
        c = self.login("dent")
        cid = self.claim(c, [pr], cv)
        self.post(c, f"/claims/{cid}/submit", {})
        self.post(c, f"/claims/{cid}/payment", {"amount": "100", "reference": "X"})
        self.post(c, f"/claims/{cid}/cancel")
        self.assertNotEqual(self.row("SELECT status FROM claims WHERE id = ?", cid)[0], "cancelled")

    def test_payment_needs_a_reference_and_a_sent_claim(self):
        cv = self.cover()
        pr = self.done("FILL")
        c = self.login("dent")
        cid = self.claim(c, [pr], cv)
        self.post(c, f"/claims/{cid}/payment", {"amount": "100", "reference": "X"})          # still a draft
        self.assertEqual(self.row("SELECT COUNT(*) FROM payments")[0], 0)
        self.post(c, f"/claims/{cid}/submit", {})
        self.post(c, f"/claims/{cid}/payment", {"amount": "100", "reference": ""})
        self.assertEqual(self.row("SELECT COUNT(*) FROM payments")[0], 0)
        self.assertEqual(self.row("SELECT status FROM claims WHERE id = ?", cid)[0], "submitted")

    def test_preauthorisation_uses_planned_work_and_is_approved_not_paid(self):
        cv = self.cover(pct=70)
        pr = self.planned("CROWN")
        c = self.login("dent")
        cid = self.claim(c, [pr], cv, kind="preauth")
        self.assertEqual(self.row("SELECT kind, estimate_p FROM claims WHERE id = ?", cid)[:], ("preauth", 105000))
        self.post(c, f"/claims/{cid}/submit", {})
        self.post(c, f"/claims/{cid}/payment", {"amount": "100", "reference": "Z"})
        self.assertEqual(self.row("SELECT COUNT(*) FROM payments")[0], 0)
        self.post(c, f"/claims/{cid}/approve", {"approved": "900", "payer_ref": "AUTH7"})
        self.assertEqual(self.row("SELECT status, estimate_p FROM claims WHERE id = ?", cid)[:], ("approved", 90000))
        # the planned crown, once done, can still be claimed for real
        self.assertEqual(len(INS.unclaimed(self.db(), 1, 1, "claim")), 0)

    def test_open_claim_counts_against_yearly_maximum(self):
        cv = self.cover(pct=100, maxp=40000)
        a, b = self.done("FILL"), self.done("FILL")
        c = self.login("dent")
        c1 = self.claim(c, [a], cv)
        c2 = self.claim(c, [b], cv)
        self.assertEqual(self.row("SELECT estimate_p FROM claims WHERE id = ?", c1)[0], 30000)
        self.assertEqual(self.row("SELECT estimate_p FROM claims WHERE id = ?", c2)[0], 10000)
        self.assertIn("left this year", self.page(c, "/patients/1/insurance"))

    def test_claims_list_and_awaiting_total(self):
        cv = self.cover(pct=80)
        pr = self.done("FILL")
        c = self.login("desk")
        cid = self.claim(c, [pr], cv)
        self.assertEqual(INS.awaiting_total(self.db(), 1), 0)               # a draft is not owed yet
        self.post(c, f"/claims/{cid}/submit", {})
        self.assertEqual(INS.awaiting_total(self.db(), 1), 24000)
        page = self.page(c, "/claims")
        self.assertIn("Acme Health", page)
        self.assertIn("GH₵ 240.00", page)
        self.assertNotIn("Acme Health", self.page(c, "/claims?status=paid"))

    def test_clinic_isolation(self):
        cv = self.cover()
        pr = self.done("FILL")
        c = self.login("dent")
        cid = self.claim(c, [pr], cv)
        other = self.login("mgr2")
        self.assertEqual(other.get(f"/claims/{cid}").status_code, 404)
        self.assertEqual(self.post(other, f"/claims/{cid}/submit", {}).status_code, 404)
        self.assertEqual(other.get("/patients/1/insurance").status_code, 404)
        self.assertEqual(other.get("/payers/1/fees").status_code, 404)
        self.assertEqual(self.post(other, f"/cover/{cv}/end").status_code, 404)


class PayerScreens(Fin):
    def test_manager_adds_payer_and_price_list(self):
        c = self.login("mgr")
        r = self.post(c, "/payers", {"name": "New Scheme", "kind": "government", "contracted": "1", "active": "1"})
        self.assertEqual(r.status_code, 302)
        pid = self.row("SELECT id FROM payers WHERE name = 'New Scheme'")[0]
        self.post(c, f"/payers/{pid}/fees", {f"fee_{self.cid('FILL')}": "250", f"fee_{self.cid('CROWN')}": ""})
        self.assertEqual(self.row("SELECT fee_p FROM payer_fees WHERE payer_id = ? AND code_id = ?", pid, self.cid("FILL"))[0], 25000)
        self.assertIsNone(self.row("SELECT fee_p FROM payer_fees WHERE payer_id = ? AND code_id = ?", pid, self.cid("CROWN")))
        self.assertIn("New Scheme", self.page(c, "/payers"))
        self.assertIn("250", self.page(c, f"/payers/{pid}/fees"))
        self.post(c, f"/payers/{pid}/fees", {f"fee_{self.cid('FILL')}": ""})           # empty goes back to the clinic price
        self.assertIsNone(self.row("SELECT fee_p FROM payer_fees WHERE payer_id = ?", pid))

    def test_duplicate_payer_name_refused(self):
        c = self.login("mgr")
        self.post(c, "/payers", {"name": "acme health", "kind": "insurer", "active": "1"})
        self.assertEqual(self.row("SELECT COUNT(*) FROM payers WHERE clinic_id = 1")[0], 2)

    def test_only_managers_manage_payers(self):
        for u in ("dent", "desk", "asst"):
            c = self.login(u)
            self.assertEqual(c.get("/payers").status_code, 403, u)
            self.assertEqual(c.get("/payers/1/fees").status_code, 403, u)
            self.assertEqual(self.post(c, "/payers", {"name": "X", "kind": "insurer"}).status_code, 403, u)

    def test_front_desk_can_add_and_end_cover(self):
        c = self.login("desk")
        r = self.post(c, "/patients/1/insurance", {"payer_id": "1", "member_no": "A1", "coverage_pct": "75", "annual_max": "5000"})
        self.assertEqual(r.status_code, 302)
        cv = self.row("SELECT * FROM patient_cover")
        self.assertEqual((cv["coverage_pct"], cv["annual_max_p"]), (75, 500000))
        self.assertIn("Acme Health", self.page(c, "/patients/1/insurance"))
        self.post(c, f"/cover/{cv['id']}/end")
        self.assertEqual(self.row("SELECT active FROM patient_cover")[0], 0)

    def test_bad_cover_form_shows_message(self):
        c = self.login("desk")
        r = self.post(c, "/patients/1/insurance", {"payer_id": "1", "coverage_pct": "150"}, follow_redirects=True)
        self.assertIn("0 to 100", r.get_data(as_text=True))
        self.assertEqual(self.row("SELECT COUNT(*) FROM patient_cover")[0], 0)


class TaxTests(unittest.TestCase):
    def test_exclusive_and_inclusive_split(self):
        self.assertEqual(INV.tax_split(10000, 1500, False), (10000, 1500, 11500))
        self.assertEqual(INV.tax_split(11500, 1500, True), (10000, 1500, 11500))
        self.assertEqual(INV.tax_split(10000, 0, True), (10000, 0, 10000))

    def test_rounds_half_up_on_the_whole_invoice(self):
        self.assertEqual(INV.tax_split(333, 1250, False), (333, 42, 375))      # 41.625 -> 42
        sub, tax, total = INV.tax_split(1000, 1250, True)                        # 1000 * 12.5 / 112.5 = 111.11
        self.assertEqual((sub, tax, total), (889, 111, 1000))


class InvoiceTests(Fin):
    def settings(self, c, **kw):
        d = {"action": "tax", "tax_name": "VAT", "tax_rate": "15", "tax_inclusive": "0", "tax_number": "C000123", "invoice_footer": "Bank: 123"}
        d.update(kw)
        r = self.post(c, "/settings", d)
        self.assertEqual(r.status_code, 302, r.get_data(as_text=True)[:200])

    def test_numbering_tax_and_snapshot(self):
        a, b = self.done("FILL"), self.done("CONS")
        c = self.login("mgr")
        self.settings(c)
        r = self.post(c, "/patients/1/invoices", {"procedure_id": [str(a)]})
        iid = int(r.headers["Location"].rsplit("/", 1)[1])
        inv = self.row("SELECT * FROM invoices WHERE id = ?", iid)
        self.assertEqual((inv["invoice_no"], inv["subtotal_p"], inv["tax_p"], inv["total_p"]), (1, 30000, 4500, 34500))
        # change the tax afterwards. The issued invoice keeps what it had.
        self.settings(c, tax_rate="0", tax_name="")
        inv = self.row("SELECT * FROM invoices WHERE id = ?", iid)
        self.assertEqual((inv["tax_rate_bp"], inv["total_p"], inv["tax_number"]), (1500, 34500, "C000123"))
        page = self.page(c, f"/invoices/{iid}")
        self.assertIn("VAT 15%", page)
        self.assertIn("GH₵ 345.00", page)
        r = self.post(c, "/patients/1/invoices", {"procedure_id": [str(b)]})
        self.assertEqual(self.row("SELECT invoice_no, tax_p, total_p FROM invoices ORDER BY id DESC")[:], (2, 0, 10000))

    def test_inclusive_prices_do_not_change_the_total(self):
        a = self.done("FILL")
        c = self.login("mgr")
        self.settings(c, tax_inclusive="1")
        self.post(c, "/patients/1/invoices", {"procedure_id": [str(a)]})
        inv = self.row("SELECT * FROM invoices")
        self.assertEqual((inv["total_p"], inv["tax_p"], inv["subtotal_p"]), (30000, 3913, 26087))

    def test_work_goes_on_one_invoice_only_until_voided(self):
        a = self.done("FILL")
        c = self.login("mgr")
        self.post(c, "/patients/1/invoices", {"procedure_id": [str(a)]})
        r = self.post(c, "/patients/1/invoices", {"procedure_id": [str(a)]}, follow_redirects=True)
        self.assertIn("already be on an invoice", r.get_data(as_text=True))
        self.assertEqual(self.row("SELECT COUNT(*) FROM invoices")[0], 1)
        self.post(c, "/invoices/1/void", {"reason": "Wrong patient details"})
        self.assertEqual(self.row("SELECT status, void_reason FROM invoices")[:], ("void", "Wrong patient details"))
        self.assertEqual(len(INV.uninvoiced(self.db(), 1, 1)), 1)
        self.post(c, "/patients/1/invoices", {"procedure_id": [str(a)]})
        self.assertEqual(self.row("SELECT MAX(invoice_no) FROM invoices")[0], 2)          # the voided number is never reused

    def test_only_managers_void_and_set_tax(self):
        a = self.done("FILL")
        c = self.login("desk")
        self.post(c, "/patients/1/invoices", {"procedure_id": [str(a)]})
        self.assertEqual(self.post(c, "/invoices/1/void", {"reason": "oops"}).status_code, 403)
        self.assertEqual(self.row("SELECT status FROM invoices")[0], "issued")
        self.post(c, "/settings", {"action": "tax", "tax_name": "VAT", "tax_rate": "15", "tax_inclusive": "0"})
        self.assertEqual(self.row("SELECT tax_rate_bp FROM clinics WHERE id = 1")[0], 0)

    def test_tax_settings_validation(self):
        c = self.login("mgr")
        r = self.post(c, "/settings", {"action": "tax", "tax_name": "", "tax_rate": "15", "tax_inclusive": "1"}, follow_redirects=True)
        self.assertIn("Name the tax", r.get_data(as_text=True))
        self.post(c, "/settings", {"action": "tax", "tax_name": "VAT", "tax_rate": "abc", "tax_inclusive": "1"})
        self.post(c, "/settings", {"action": "tax", "tax_name": "VAT", "tax_rate": "150", "tax_inclusive": "1"})
        self.assertEqual(self.row("SELECT tax_rate_bp FROM clinics WHERE id = 1")[0], 0)
        self.assertIn("Tax on invoices", self.page(c, "/settings"))

    def test_free_work_and_other_patients_work_are_not_invoiceable(self):
        a = self.done("FILL", fee=0)
        k = self.done("FILL", pid=2)
        c = self.login("mgr")
        self.assertEqual(len(INV.uninvoiced(self.db(), 1, 1)), 0)
        r = self.post(c, "/patients/1/invoices", {"procedure_id": [str(k)]}, follow_redirects=True)
        self.assertEqual(self.row("SELECT COUNT(*) FROM invoices")[0], 0)

    def test_invoice_of_another_clinic_is_not_found(self):
        a = self.done("FILL")
        c = self.login("mgr")
        self.post(c, "/patients/1/invoices", {"procedure_id": [str(a)]})
        self.assertEqual(self.login("mgr2").get("/invoices/1").status_code, 404)


class AgeingTests(Fin):
    def test_payments_clear_the_oldest_charges_first(self):
        self.done("FILL", days_ago=100)       # 300.00, 90+
        self.done("FILL", days_ago=40)        # 300.00, 31-60
        self.done("FILL", days_ago=5)         # 300.00, 0-30
        conn = self.db()
        billing.record_payment(conn, self.clinic(), 1, 1, 40000, "cash")
        conn.commit()
        b = AG.patient_ageing(self.db(), 1, 1, today())
        self.assertEqual(b, {"0-30": 30000, "31-60": 20000, "61-90": 0, "90+": 0})
        self.assertEqual(sum(b.values()), self.balance())

    def test_discount_counts_as_credit_and_membership_fee_does_not(self):
        self.done("FILL", days_ago=70)
        conn = self.db()
        billing.add_adjustment(conn, self.clinic(), 1, 1, -5000, "discount", "Goodwill")
        billing.record_payment(conn, self.clinic(), 1, 1, 7000, "cash", purpose="membership")
        conn.commit()
        b = AG.patient_ageing(self.db(), 1, 1, today())
        self.assertEqual(b["61-90"], 25000)

    def test_insurers_owe_and_report_page(self):
        cv = self.cover(pct=80)
        pr = self.done("FILL", days_ago=95)
        c = self.login("mgr")
        cid = self.claim(c, [pr], cv)
        self.post(c, f"/claims/{cid}/submit", {})
        self.assertEqual(AG.insurers_owe(self.db(), 1, 1), 24000)
        rep = AG.report(self.db(), self.clinic())
        self.assertEqual(rep["totals"]["90+"], 30000)
        self.assertEqual(rep["insurers"], 24000)
        page = self.page(c, "/reports/ageing")
        self.assertIn("Ama Mensah", page)
        self.assertIn("GH₵ 300.00", page)
        self.post(c, f"/claims/{cid}/payment", {"amount": "240", "reference": "R"})
        self.assertEqual(AG.insurers_owe(self.db(), 1, 1), 0)
        self.assertEqual(AG.report(self.db(), self.clinic())["totals"]["90+"], 6000)

    def test_ageing_report_is_for_managers_only(self):
        self.assertEqual(self.login("dent").get("/reports/ageing").status_code, 403)
        self.assertEqual(self.login("desk").get("/reports/ageing").status_code, 403)

    def test_report_ignores_other_clinics_and_paid_up_patients(self):
        self.done("FILL")
        conn = self.db()
        billing.record_payment(conn, self.clinic(), 1, 1, 30000, "cash")
        conn.commit()
        self.assertEqual(AG.report(self.db(), self.clinic())["rows"], [])


class StatementTests(Fin):
    def test_statement_page_shows_balance_and_ageing(self):
        cv = self.cover(pct=80)
        pr = self.done("FILL", days_ago=45)
        c = self.login("desk")
        cid = self.claim(c, [pr], cv)
        self.post(c, f"/claims/{cid}/submit", {})
        page = self.page(c, "/patients/1/statement")
        self.assertIn("Amount due: GH₵ 300.00", page)
        self.assertIn("31-60", page)
        self.assertIn("insurer is expected to pay GH₵ 240.00", page)

    def test_statement_for_a_credit_balance(self):
        conn = self.db()
        billing.record_payment(conn, self.clinic(), 1, 1, 5000, "cash")
        conn.commit()
        self.assertIn("Credit: GH₵ 50.00", self.page(self.login("desk"), "/patients/1/statement"))

    def test_statement_of_another_clinic_patient_is_not_found(self):
        self.assertEqual(self.login("desk").get("/patients/3/statement").status_code, 404)


class UpgradeTests(unittest.TestCase):
    def test_old_clinics_and_payments_tables_gain_the_new_columns(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "old.sqlite")
            first = dbmod.connect(path)
            dbmod.init_schema(first)
            first.execute("INSERT INTO clinics(name, country, currency, timezone, payment_methods) VALUES ('Old','GH','GHS','Africa/Accra','cash')")
            for col in ("tax_name", "tax_rate_bp", "tax_inclusive", "tax_number", "invoice_footer"):
                first.execute(f"ALTER TABLE clinics DROP COLUMN {col}")
            first.execute("DROP INDEX IF EXISTS payments_claim")
            first.execute("ALTER TABLE payments DROP COLUMN claim_id")
            first.commit()
            first.close()
            conn = dbmod.connect(path)
            dbmod.init_schema(conn)
            cols = {r[1] for r in conn.execute("PRAGMA table_info(clinics)")}
            self.assertTrue({"tax_name", "tax_rate_bp", "tax_inclusive", "tax_number", "invoice_footer"} <= cols)
            self.assertIn("claim_id", {r[1] for r in conn.execute("PRAGMA table_info(payments)")})
            row = conn.execute("SELECT tax_rate_bp, tax_inclusive FROM clinics").fetchone()
            self.assertEqual((row[0], row[1]), (0, 1))
            conn.close()


if __name__ == "__main__":
    unittest.main()
