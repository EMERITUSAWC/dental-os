"""v0.3: nothing is tied to one country or one clinic."""
import re
import tempfile
import unittest
from pathlib import Path

from werkzeug.security import generate_password_hash

from dental_os import create_app, db as dbmod
from dental_os.demo import seed
from dental_os.services import billing as B, money, regions

ROOT = Path(__file__).resolve().parent.parent


class CurrencyTests(unittest.TestCase):
    def test_two_decimal_currencies(self):
        self.assertEqual(money.fmt(125050, "NGN"), "₦1,250.50")
        self.assertEqual(money.fmt(125050, "GBP"), "£1,250.50")
        self.assertEqual(money.fmt(-5000, "USD"), "-$50.00")
        self.assertEqual(money.fmt(125050, "EUR"), "€1,250.50")
        self.assertEqual(money.fmt(125050, "KES"), "KSh 1,250.50")

    def test_no_minor_unit_currencies(self):
        self.assertEqual(money.fmt(25000, "XOF"), "CFA 25,000")
        self.assertEqual(money.parse_money("25 000", "XOF"), 25000)
        self.assertEqual(money.plain(25000, "UGX"), "25000")
        with self.assertRaises(ValueError):
            money.parse_money("250.50", "XOF")

    def test_parse_in_each_currency(self):
        self.assertEqual(money.parse_money("£1,250.50", "GBP"), 125050)
        self.assertEqual(money.parse_money("$80", "USD"), 8000)
        self.assertEqual(money.parse_money("usd 80", "USD"), 8000)
        self.assertEqual(money.parse_money("₦ 15,000", "NGN"), 1500000)
        self.assertEqual(money.parse_money("12,50", "EUR"), 1250)        # decimal comma
        self.assertEqual(money.parse_money("1,250", "EUR"), 125000)      # thousands comma

    def test_every_country_has_a_known_currency_and_timezone(self):
        from zoneinfo import ZoneInfo
        for code, row in regions.COUNTRIES.items():
            self.assertIn(row[5], regions.CURRENCIES, msg=code)
            ZoneInfo(row[4])


class SettingsAndCommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = create_app({"TESTING": True, "DATABASE": str(Path(self.tmp.name) / "t.sqlite"), "SECRET_KEY": "test"})
        self.runner = self.app.test_cli_runner()

    def make(self, name, country, *extra):
        return self.runner.invoke(args=["create-clinic", "--name", name, "--country", country, *extra])

    def test_create_clinic_sets_country_defaults(self):
        r = self.make("Test Clinic A", "GB", "--whatsapp", "07700 900123")
        self.assertEqual(r.exit_code, 0, r.output)
        conn = dbmod.connect(self.app.config["DATABASE"])
        row = conn.execute("SELECT * FROM clinics WHERE id = 1").fetchone()
        self.assertEqual((row["country"], row["currency"], row["timezone"]), ("GB", "GBP", "Europe/London"))
        self.assertEqual(row["whatsapp"], "+447700900123")
        self.assertNotIn("mobile_money", row["payment_methods"])
        self.assertGreater(conn.execute("SELECT COUNT(*) FROM procedure_codes WHERE clinic_id = 1").fetchone()[0], 10)

    def test_ghana_clinic_gets_mobile_money_and_others_do_not(self):
        self.make("Clinic G", "GH")
        self.make("Clinic U", "US")
        conn = dbmod.connect(self.app.config["DATABASE"])
        gh = conn.execute("SELECT payment_methods FROM clinics WHERE id = 1").fetchone()[0]
        us = conn.execute("SELECT payment_methods, currency FROM clinics WHERE id = 2").fetchone()
        self.assertIn("mobile_money", gh)
        self.assertNotIn("mobile_money", us[0])
        self.assertEqual(us[1], "USD")

    def test_bad_inputs_are_refused(self):
        self.assertNotEqual(self.make("X", "ZZ").exit_code, 0)
        self.assertNotEqual(self.make("X", "GH", "--currency", "XXX").exit_code, 0)
        self.assertNotEqual(self.make("X", "GH", "--timezone", "Mars/Base").exit_code, 0)
        self.assertNotEqual(self.make("X", "GH", "--whatsapp", "123").exit_code, 0)

    def test_create_user_needs_a_real_clinic(self):
        r = self.runner.invoke(args=["create-user", "--username", "a", "--name", "A", "--clinic-id", "9",
                                     "--password", "long-enough-password"])
        self.assertNotEqual(r.exit_code, 0)

    def test_no_clinic_is_assumed_anywhere(self):
        conn = dbmod.connect(self.app.config["DATABASE"])
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM clinics").fetchone()[0], 0)

    def test_demo_data_fits_the_clinic_country(self):
        self.make("Clinic NG", "NG")
        conn = dbmod.connect(self.app.config["DATABASE"])
        self.assertIn("Added", seed(conn, 1))
        phones = [r[0] for r in conn.execute("SELECT phone FROM patients")]
        self.assertTrue(all(p.startswith("+234") and len(p) == 14 for p in phones), phones)
        self.assertEqual(seed(conn, 1), "Demo data is already there.")
        self.assertIn("No clinic", seed(conn, 99))

    def test_demo_in_a_zero_decimal_currency(self):
        self.make("Clinic SN", "SN")
        conn = dbmod.connect(self.app.config["DATABASE"])
        seed(conn, 1)
        fee = conn.execute("SELECT fee_p FROM procedures WHERE notes='Demo' ORDER BY id LIMIT 1").fetchone()[0]
        self.assertEqual(fee, 200)        # 200 CFA francs-worth of made-up units, no x100


class WebSettingsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = create_app({"TESTING": True, "DATABASE": str(Path(self.tmp.name) / "t.sqlite"), "SECRET_KEY": "test"})
        r = self.app.test_cli_runner()
        r.invoke(args=["create-clinic", "--name", "Clinic One", "--country", "NG"])
        r.invoke(args=["create-clinic", "--name", "Clinic Two", "--country", "US"])
        conn = dbmod.connect(self.app.config["DATABASE"])
        for uname, cid in (("one", 1), ("two", 2)):
            conn.execute("INSERT INTO providers(clinic_id, name) VALUES (?, ?)", (cid, f"Dr {cid}"))
            conn.execute("INSERT INTO users(clinic_id, username, display_name, role, password_hash) VALUES (?,?,?,'manager',?)",
                         (cid, uname, uname.title(), generate_password_hash("correct-horse-battery")))
        conn.commit()
        conn.close()

    def login(self, name):
        c = self.app.test_client()
        page = c.get("/login").get_data(as_text=True)
        token = re.search(r'name="_csrf" value="([^"]+)"', page).group(1)
        c.post("/login", data={"_csrf": token, "username": name, "password": "correct-horse-battery"})
        return c

    def token(self, c):
        return re.search(r'name="_csrf" value="([^"]+)"', c.get("/settings").get_data(as_text=True)).group(1)

    def test_each_clinic_sees_its_own_currency_and_phone_format(self):
        a, b = self.login("one"), self.login("two")
        for c, cur in ((a, "NGN"), (b, "USD")):
            page = c.get("/settings").get_data(as_text=True)
            self.assertIn(f'<option value="{cur}" selected', page)
        # a Nigerian local number is accepted by clinic one and refused by clinic two
        tok = self.token(a)
        r = a.post("/patients/new", data={"_csrf": tok, "first_name": "Ada", "last_name": "Test", "phone": "0803 123 4567",
                                          "reminders_ok": "1"})
        self.assertEqual(r.status_code, 302, r.get_data(as_text=True)[:300])
        tok = self.token(b)
        r = b.post("/patients/new", data={"_csrf": tok, "first_name": "Ada", "last_name": "Test", "phone": "0803 123 4567"})
        self.assertEqual(r.status_code, 200)         # form shown again with an error
        self.assertIn("valid United States number", r.get_data(as_text=True))

    def test_manager_changes_country_currency_timezone_and_methods(self):
        c = self.login("two")
        r = c.post("/settings", data={"_csrf": self.token(c), "action": "clinic", "name": "Clinic Two", "open_time": "08:00",
                                      "close_time": "17:00", "slot_minutes": "30", "days": ["1", "2", "3"],
                                      "country": "CA", "currency": "CAD", "timezone": "America/Vancouver",
                                      "whatsapp": "604-555-0147", "pay_card": "on", "pay_mobile_money": "on"})
        self.assertEqual(r.status_code, 302)
        conn = dbmod.connect(self.app.config["DATABASE"])
        row = conn.execute("SELECT * FROM clinics WHERE id = 2").fetchone()
        self.assertEqual((row["country"], row["currency"], row["timezone"], row["whatsapp"]),
                         ("CA", "CAD", "America/Vancouver", "+16045550147"))
        self.assertEqual(row["payment_methods"], "cash,mobile_money,card")      # cash is always on
        other = conn.execute("SELECT country FROM clinics WHERE id = 1").fetchone()[0]
        self.assertEqual(other, "NG")                                             # clinic one untouched

    def test_bad_settings_are_refused(self):
        c = self.login("two")
        base = {"action": "clinic", "name": "X", "open_time": "08:00", "close_time": "17:00", "slot_minutes": "30",
                "days": ["1"], "country": "US", "currency": "USD", "timezone": "America/New_York"}
        for bad in ({"country": "ZZ"}, {"currency": "XXX"}, {"timezone": "Nowhere/Land"}, {"whatsapp": "12"}):
            data = {**base, "_csrf": self.token(c), **bad}
            c.post("/settings", data=data)
            conn = dbmod.connect(self.app.config["DATABASE"])
            row = conn.execute("SELECT country, currency, timezone FROM clinics WHERE id = 2").fetchone()
            self.assertEqual(tuple(row), ("US", "USD", "America/New_York"), msg=str(bad))

    def test_payment_method_must_be_switched_on(self):
        conn = dbmod.connect(self.app.config["DATABASE"])
        clinic = conn.execute("SELECT * FROM clinics WHERE id = 2").fetchone()
        self.assertNotIn("mobile_money", B.enabled_methods(clinic))
        conn.execute("INSERT INTO patients(clinic_id, first_name, last_name, phone) VALUES (2,'A','B','+12125550147')")
        with self.assertRaises(B.BillingError):
            B.record_payment(conn, clinic, None, 1, 1000, "mobile_money", "ref")
        self.assertTrue(B.record_payment(conn, clinic, None, 1, 1000, "cash"))

    def test_pages_render_in_the_clinic_currency(self):
        c = self.login("two")
        for url in ("/", "/patients", "/reports", "/reports/takings", "/catalogue", "/settings", "/recalls", "/diary"):
            r = c.get(url)
            self.assertEqual(r.status_code, 200, url)
            self.assertNotIn("GH₵", r.get_data(as_text=True), url)


class NoSingleClientTests(unittest.TestCase):
    """The product must not carry any one clinic's name, contacts or promotions."""
    BANNED = ("knightsmile", "knight smile", "oduro", "nkansah", "osei bonsu", "050 477", "020 656", "0504771418",
              "admin.knightsmiledental")

    def test_no_client_details_in_the_product(self):
        offenders = []
        for path in list((ROOT / "dental_os").rglob("*")) + [ROOT / "README.md", ROOT / "requirements.txt"]:
            if path.is_file() and path.suffix in (".py", ".html", ".css", ".js", ".sql", ".md", ".txt"):
                text = path.read_text(encoding="utf-8", errors="ignore").lower()
                for word in self.BANNED:
                    if word in text:
                        offenders.append(f"{path.relative_to(ROOT)}: {word}")
        self.assertEqual(offenders, [])

    def test_schema_has_no_country_default(self):
        sql = (ROOT / "dental_os" / "schema.sql").read_text()
        for col in ("country", "currency", "timezone"):
            line = next(l for l in sql.splitlines() if l.strip().startswith(col + " "))
            self.assertNotIn("DEFAULT", line, line)


if __name__ == "__main__":
    unittest.main()
