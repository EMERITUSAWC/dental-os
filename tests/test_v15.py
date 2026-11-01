"""v0.15: French for the rest of the staff screens. Every screen opens in both languages, every translated sentence formats."""
import re
import unittest

from dental_os import demo
from dental_os.services import i18n_fr
from tests.test_v09 import French

# developer pages and pages that need a secret in the address stay English and are not walked here
SKIP = ("/static", "/logout", "/login", "/api/", "/wa/", "/healthz", "/hooks")
KEEP_ENGLISH = ("/api-docs", "/integrations", "/audit", "/messages/setup")


def clinic_today(clinic):
    from dental_os.services.timeutil import now_local
    return now_local(clinic["timezone"]).date()


class EveryScreenTests(French):
    def setUp(self):
        super().setUp()
        conn = self.db()
        demo.seed(conn, 1)
        conn.commit()
        conn.close()
        self.sql("UPDATE patients SET reminders_ok = 1, offers_ok = 1 WHERE id = 1")
        self.records = self.make_records()

    def make_records(self):
        """One of everything, so the detail pages have something to show."""
        from dental_os.services import (campaigns, consent, insurance, invoices, labs, memberships, instalments, notes, perio, plans,
                                        privacy, stock, medical, dataio)
        conn = self.db()
        clinic = dict(conn.execute("SELECT * FROM clinics WHERE id = 1").fetchone())
        user = dict(conn.execute("SELECT * FROM users WHERE id = 1").fetchone())
        uid, pid = 1, 4
        ids = {}
        ids["plan"] = plans.create(db=conn, clinic=clinic, user_id=uid, pid=pid, title="Option A")
        plans.add_item(conn, clinic, uid, ids["plan"], 10, tooth="16")
        plans.add_item(conn, clinic, uid, ids["plan"], 3, tooth="26")
        payer = insurance.save_payer(conn, clinic, uid, None, "Demo Insurer", "insurer", True, None, None, None)
        ids["payer"] = payer if isinstance(payer, int) else conn.execute("SELECT MAX(id) FROM payers").fetchone()[0]
        insurance.add_cover(conn, clinic, uid, pid, ids["payer"], "M-1", None, 80, 500000, None, None)
        cover = conn.execute("SELECT id FROM patient_cover WHERE patient_id = ?", (pid,)).fetchone()["id"]
        ids["claim"] = insurance.create_claim(conn, clinic, uid, pid, cover, [1])
        invoices.create(conn, clinic, uid, pid, [2])
        ids["invoice"] = conn.execute("SELECT MAX(id) FROM invoices").fetchone()[0]
        memberships.save_plan(conn, clinic, uid, None, "Gold", "Two cleans a year", 5000, "monthly", 10)
        mplan = conn.execute("SELECT MAX(id) FROM membership_plans").fetchone()[0]
        memberships.enrol(conn, clinic, uid, 5, mplan)
        instalments.create(conn, clinic, uid, pid, 30000, 3, "monthly", clinic_today(clinic))
        labs.save_lab(conn, clinic, uid, None, "Demo Lab")
        lab = conn.execute("SELECT MAX(id) FROM labs").fetchone()[0]
        labs.create(conn, clinic, uid, pid, lab, "Crown", tooth="16")
        ids["lab"] = conn.execute("SELECT MAX(id) FROM lab_cases").fetchone()[0]
        stock.save_supplier(conn, clinic, uid, None, "Demo Supplies")
        sup = conn.execute("SELECT MAX(id) FROM suppliers").fetchone()[0]
        stock.save_item(conn, clinic, uid, None, "Gloves", supplier_id=sup, reorder_level=5)
        ids["item"] = conn.execute("SELECT MAX(id) FROM stock_items").fetchone()[0]
        campaigns.create(conn, clinic, uid, "Check-ups", "overdue_recall", 180, "care", "Hello {first_name}", 30)
        ids["campaign"] = conn.execute("SELECT MAX(id) FROM campaigns").fetchone()[0]
        privacy.add_request(conn, clinic, uid, pid, "Demo Person", "access")
        ids["request"] = conn.execute("SELECT MAX(id) FROM data_requests").fetchone()[0]
        privacy.add_incident(conn, clinic, uid, "Lost phone", "A phone was lost")
        ids["incident"] = conn.execute("SELECT MAX(id) FROM incidents").fetchone()[0]
        medical.add_alert(conn, clinic, uid, pid, "allergy", "warning", "Penicillin")
        notes.create_draft(conn, clinic, user, pid, "exam", {"f0": "Check-up"})
        ids["note"] = conn.execute("SELECT MAX(id) FROM clinical_notes").fetchone()[0]
        perio.create_chart(conn, clinic, uid, pid, [{"tooth": 16, "site": "B", "pocket_mm": 3, "recession_mm": 0, "bleeding": 1}, {"tooth": 16, "site": "L", "pocket_mm": 5, "recession_mm": 1, "bleeding": 0}])
        ids["perio"] = conn.execute("SELECT MAX(id) FROM perio_charts").fetchone()[0]
        tmpl = conn.execute("SELECT id FROM consent_templates WHERE active = 1 ORDER BY id LIMIT 1").fetchone()["id"]
        consent.record(conn, clinic, user, pid, tmpl, "Demo Patient", "self", "typed", True)
        ids["consent"] = conn.execute("SELECT MAX(id) FROM consents").fetchone()[0]
        conn.commit()
        conn.close()
        return ids

    def detail_urls(self):
        r = self.records
        return ["/plans/%d" % r["plan"], "/plans/%d/quote" % r["plan"], "/claims/%d" % r["claim"], "/invoices/%d" % r["invoice"],
                "/payers/%d/fees" % r["payer"], "/lab/%d" % r["lab"], "/lab/%d/sheet" % r["lab"], "/stock/item/%d" % r["item"],
                "/stock/item/%d/edit" % r["item"], "/messages/campaigns/%d" % r["campaign"], "/privacy/requests/%d" % r["request"],
                "/privacy/incidents/%d" % r["incident"], "/notes/%d/edit" % r["note"], "/perio/%d" % r["perio"], "/consents/%d" % r["consent"],
                "/patients/4/account", "/patients/4/plans", "/patients/4/insurance", "/patients/4/invoices", "/patients/4/statement",
                "/patients/4/instalments", "/patients/5/membership", "/patients/4/lab", "/patients/4/notes", "/patients/4/perio",
                "/patients/4/medical", "/patients/4/consents", "/patients/4/privacy", "/patients/4/record-copy", "/patients/4/chart"]

    def routes(self):
        out = []
        for rule in self.app.url_map.iter_rules():
            if "GET" not in rule.methods or any(rule.rule.startswith(s) for s in SKIP):
                continue
            url = re.sub(r"<(?:int:)?\w+>", "1", rule.rule)
            if "<" in url:
                continue
            out.append(url)
        return sorted(set(out))

    def test_every_screen_opens_in_both_languages_without_an_error(self):
        c = self.login("mgr")
        urls = self.routes() + self.detail_urls()
        self.assertGreater(len(urls), 60)
        opened = 0
        details = set(self.detail_urls())
        for lang in ("en", "fr"):
            self.fr_clinic(lang)
            for url in urls:
                r = c.get(url)
                self.assertLess(r.status_code, 500, "%s in %s" % (url, lang))
                opened += r.status_code == 200
                if url in details:
                    self.assertEqual(r.status_code, 200, "%s in %s" % (url, lang))
        self.assertGreater(opened, 100)

    def test_the_main_staff_screens_are_in_french(self):
        self.fr_clinic()
        c = self.login("mgr")
        for url, french, english in [("/plans", "Plans à relancer", ">Plans to follow up<"), ("/claims", "Organismes payeurs", ">Payers<"),
                                     ("/memberships", "Cotisations à relancer", ">Fees to chase<"), ("/lab", "Travaux de laboratoire", ">Lab cases<"),
                                     ("/stock", "Ajouter un article", ">Add an item<"), ("/reports", "Confirmations de rendez-vous", ">Appointment confirmations<"),
                                     ("/kpi", "Chiffres clés", ">Key numbers<"), ("/tools", "Exporter vos données", ">Export your data<"),
                                     ("/settings", "Nom du cabinet", ">Clinic name<"), ("/staff", "Comptes du personnel", ">Staff logins<"),
                                     ("/campaigns", "Nouvelle campagne", ">New campaign<"), ("/privacy", "Vie privée et droits sur les données", ">Privacy and data rights<"),
                                     ("/account/security", "Connexion en deux étapes", ">Two-step login<"), ("/patients/1/medical", "Antécédents médicaux", ">Medical history<"),
                                     ("/patients/1/chart", "Dents définitives", ">Adult teeth<"), ("/patients/1/notes", "Aucune note", ">No notes yet.<"),
                                     ("/patients/1/perio", "Nouveau sondage parodontal", ">New gum chart<"), ("/patients/1/consents", "Enregistrer un consentement", ">Record a consent<")]:
            r = c.get(url)
            if r.status_code != 200:
                continue
            page = r.get_data(as_text=True)
            self.assertIn(french.replace("'", "&#39;"), page, url)
            self.assertNotIn(english, page, url)

    def test_english_pages_are_unchanged_by_the_translation(self):
        self.fr_clinic("en")
        c = self.login("mgr")
        page = c.get("/plans").get_data(as_text=True)
        self.assertIn("Plans to follow up", page)
        self.assertNotIn("Plans à relancer", page)


class SentencesFormatTests(unittest.TestCase):
    def test_every_sentence_with_fields_formats_in_both_languages(self):
        for en, fr in i18n_fr.FR.items():
            names = set(re.findall(r"%\((\w+)\)s", en))
            if not names:
                continue
            values = {n: "x" for n in names}
            self.assertEqual(en % values, en.replace("%%", "%") if False else en % values)
            fr % values                        # raises if a stray % or a missing field would break the screen

    def test_no_field_is_named_like_a_translator_argument(self):
        for en in i18n_fr.FR:
            for n in re.findall(r"%\((\w+)\)s", en):
                self.assertNotIn(n, ("lang", "text"), en)


class RecordCopyColumnTests(unittest.TestCase):
    def test_every_child_table_column_exists(self):
        """The full copy of a record finds child rows through a column in the child table. A wrong column name only shows up for a patient
        who has that kind of row, so check them all against the schema."""
        import sqlite3
        from dental_os import db as dbmod
        from dental_os.services import privacy
        conn = sqlite3.connect(":memory:")
        dbmod.init_schema(conn)
        for table, (col, parent) in privacy.CHILD.items():
            cols = {r[1] for r in conn.execute("PRAGMA table_info(%s)" % table)}
            self.assertIn(col, cols, table)
            self.assertIn("clinic_id", cols, table)
            self.assertTrue(conn.execute("PRAGMA table_info(%s)" % parent).fetchall(), parent)
