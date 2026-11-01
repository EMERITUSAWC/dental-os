"""v0.12: starting tax set-ups by country."""
import re
import unittest

from dental_os.services import regions, taxes, taxpresets as TP
from tests.test_v09 import Tax


class PresetDataTests(unittest.TestCase):
    def test_every_entry_is_complete_and_honest(self):
        for code, p in TP.PRESETS.items():
            self.assertIn(code, regions.COUNTRIES, code)
            self.assertTrue(p["facts"] and p["ask"] and p["sources"], code)
            self.assertTrue(any(s["strength"] == "official" for s in p["sources"]) or all(f[1] != "official" for f in p["facts"]), code)
            for text, strength in p["facts"]:
                self.assertIn(strength, TP.STRENGTHS)
                self.assertNotIn("—", text)
            for s in p["sources"]:
                self.assertTrue(s["url"].startswith("https://"), s["url"])
                self.assertIn(s["strength"], TP.STRENGTHS)
            names = [n.lower() for n, _k, _r in p["rates"]]
            self.assertEqual(len(names), len(set(names)), code)
            for name, kind, pct in p["rates"]:
                self.assertLessEqual(len(name), 30, name)
                self.assertIn(kind, taxes.KINDS)
                if kind == "rate":
                    self.assertGreater(taxes.parse_rate(pct), 0)

    def test_a_fact_read_only_in_a_news_report_is_not_called_official(self):
        for code, p in TP.PRESETS.items():
            best = {"official": 3, "guidance": 2, "secondary": 1}
            support = max(best[s["strength"]] for s in p["sources"])
            for text, strength in p["facts"]:
                self.assertLessEqual(best[strength], support, f"{code}: {text[:40]}")

    def test_ghana_numbers(self):
        p = TP.get("gh")
        rates = {n: (k, taxes.parse_rate(r)) for n, k, r in p["rates"]}
        self.assertEqual(rates["VAT 15% + levies 5%"], ("rate", 2000))
        self.assertEqual(rates["Exempt"][0], "exempt")
        self.assertIsNone(TP.get("FR"))
        self.assertIsNone(TP.get(None))


class ApplyTests(Tax):
    def test_apply_adds_rates_and_changes_nothing_else(self):
        self.set_default("VAT", 1500, 1)
        before = self.one("SELECT tax_name, tax_rate_bp, tax_inclusive FROM clinics WHERE id = 1")
        codes = self.many("SELECT id, tax_rate_id FROM procedure_codes WHERE clinic_id = 1")
        conn = self.db()
        res = TP.apply(conn, self.clinic(1), None)
        conn.commit()
        conn.close()
        self.assertEqual(res["added"], ["Exempt", "VAT 15% + levies 5%"])
        self.assertEqual(tuple(self.one("SELECT tax_name, tax_rate_bp, tax_inclusive FROM clinics WHERE id = 1")), tuple(before))
        self.assertEqual([tuple(r) for r in self.many("SELECT id, tax_rate_id FROM procedure_codes WHERE clinic_id = 1")], [tuple(r) for r in codes])
        names = [r[0] for r in self.many("SELECT name FROM tax_rates WHERE clinic_id = 1 ORDER BY name")]
        self.assertEqual(names, ["Exempt", "VAT 15% + levies 5%"])

    def test_applying_twice_adds_nothing_new_and_keeps_the_managers_own_rate(self):
        self.rate("Exempt", "exempt", "0")
        conn = self.db()
        first = TP.apply(conn, self.clinic(1), None)
        conn.commit()
        second = TP.apply(conn, self.clinic(1), None)
        conn.commit()
        conn.close()
        self.assertEqual(first, {"added": ["VAT 15% + levies 5%"], "kept": ["Exempt"]})
        self.assertEqual(second["added"], [])
        self.assertEqual(self.one("SELECT COUNT(*) FROM tax_rates WHERE clinic_id = 1")[0], 2)

    def test_it_applies_only_to_the_clinics_own_country_and_clinic(self):
        conn = self.db()
        with self.assertRaises(taxes.TaxError):
            TP.apply(conn, self.clinic(2), None)                  # the United States has no entry
        conn.rollback()
        conn.close()
        self.assertEqual(self.one("SELECT COUNT(*) FROM tax_rates WHERE clinic_id = 2")[0], 0)

    def test_the_rates_work_on_an_invoice(self):
        conn = self.db()
        TP.apply(conn, self.clinic(1), None)
        conn.commit()
        conn.close()
        rid = self.one("SELECT id FROM tax_rates WHERE name = 'VAT 15% + levies 5%'")[0]
        self.assign("CROWN", rid)
        ids = self.work("CROWN")
        inv = self.inv(self.invoice(ids))
        self.assertEqual(inv["total_p"], 150000)
        row = self.rows(inv["id"])[0]
        self.assertEqual(row["rate_bp"], 2000)
        self.assertEqual(row["tax_p"], 25000)                      # 20% of the price before tax, and prices include tax: 1500 / 1.2 = 1250, tax 250

    def clinic(self, cid=1):
        return dict(self.db().execute("SELECT * FROM clinics WHERE id = ?", (cid,)).fetchone())


class ScreenTests(Tax):
    def test_the_settings_page_shows_facts_sources_and_questions(self):
        c = self.login("mgr")
        page = self.page(c, "/settings")
        self.assertIn("A starting point for GH", page)
        self.assertIn("This is not tax advice", page)
        self.assertIn("gra.gov.gh/domestic-tax/tax-types/vat/", page)
        self.assertIn("Ask your accountant", page)
        self.assertIn("27 October 2026", page)

    def test_a_country_without_an_entry_says_so(self):
        page = self.page(self.login("mgr2"), "/settings")
        self.assertIn("There is no starting point for this country yet", page)
        r = self.post(self.login("mgr2"), "/settings", {"action": "tax_preset"}, follow_redirects=True)
        self.assertIn("no starting point", r.get_data(as_text=True))
        self.assertEqual(self.one("SELECT COUNT(*) FROM tax_rates WHERE clinic_id = 2")[0], 0)

    def test_the_button_adds_the_rates_for_this_clinic_only(self):
        c = self.login("mgr")
        r = self.post(c, "/settings", {"action": "tax_preset"}, follow_redirects=True)
        self.assertIn("Added: Exempt, VAT 15% + levies 5%", r.get_data(as_text=True))
        self.assertEqual(self.one("SELECT COUNT(*) FROM tax_rates WHERE clinic_id = 1")[0], 2)
        self.assertEqual(self.one("SELECT COUNT(*) FROM tax_rates WHERE clinic_id = 2")[0], 0)
        self.assertTrue(self.one("SELECT 1 FROM audit_log WHERE action = 'tax.preset'"))
        again = self.post(c, "/settings", {"action": "tax_preset"}, follow_redirects=True)
        self.assertIn("Added: nothing new", again.get_data(as_text=True))

    def test_only_a_manager_can_apply_it(self):
        self.assertEqual(self.post(self.login("desk"), "/settings", {"action": "tax_preset"}).status_code, 403)


if __name__ == "__main__":
    unittest.main()
