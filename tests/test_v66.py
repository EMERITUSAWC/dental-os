"""v0.66: Spanish, Portuguese and Arabic for everything patients see, Arabic right to left, and staff screens kept to English and French."""
import datetime
import re
import sys
from pathlib import Path

from tests.test_v04 import Base
from tests.test_v07 import Public
from tests.a11y import check
from dental_os.services import i18n, i18n_ar, i18n_es, i18n_pt, msgtemplates as MT

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import patient_keys  # noqa: E402

CATALOGUES = {"es": i18n_es, "pt": i18n_pt, "ar": i18n_ar}
FIELDS = re.compile(r"%\(\w+\)s|\{\w+\}")


class CatalogueTests(Base):
    def test_every_patient_line_is_in_every_language_with_the_same_fields(self):
        keys = patient_keys.all_keys()
        self.assertGreater(len(keys), 300)
        for lang, mod in CATALOGUES.items():
            missing = [k for k in keys if k not in mod.WORDS]
            self.assertEqual(missing, [], f"{lang}: add these lines")
            wrong = [k for k, v in mod.WORDS.items() if sorted(FIELDS.findall(k)) != sorted(FIELDS.findall(v))]
            self.assertEqual(wrong, [], f"{lang}: the %(fields)s differ")
            self.assertEqual([k for k, v in mod.WORDS.items() if not v.strip()], [])

    def test_message_wording_in_every_language(self):
        for lang, mod in CATALOGUES.items():
            self.assertEqual(set(mod.STARTERS), set(MT.KINDS), lang)
            for kind, (_label, english, _need) in MT.KINDS.items():
                self.assertEqual(sorted(FIELDS.findall(english)), sorted(FIELDS.findall(mod.STARTERS[kind])), f"{lang} {kind}")

    def test_dates_and_times(self):
        d = datetime.date(2026, 11, 3)
        self.assertEqual(i18n.fmt_date(d, "es"), "3 de noviembre de 2026")
        self.assertEqual(i18n.fmt_day_date(d, "es"), "martes 3 de noviembre")
        self.assertEqual(i18n.fmt_day_date(d, "pt"), "terça-feira, 3 de novembro")
        self.assertEqual(i18n.fmt_date(d, "ar"), "3 نوفمبر 2026")
        self.assertEqual(i18n.fmt_time(datetime.datetime(2026, 11, 3, 16, 5), "pt"), "16:05")
        self.assertEqual(i18n.fmt_date(d, "en"), "3 November 2026")
        self.assertEqual(i18n.fmt_date(d, "fr"), "3 novembre 2026")
        self.assertEqual(i18n.t("es", "Hello %(name)s. This page is only for you. Please do not share its address.", name="Ana"),
                         "Hola Ana. Esta página es solo para usted. Por favor, no comparta su dirección.")
        self.assertEqual(i18n.t("es", "A wording nobody translated"), "A wording nobody translated")       # English when missing

    def test_the_stylesheet_works_both_ways(self):
        css = (Path(__file__).resolve().parent.parent / "dental_os" / "static" / "app.css").read_text()
        physical = re.findall(r"(?:margin|padding|border)-(?:left|right)\b|text-align: (?:left|right)|[{;]\s*(?:left|right): ", css)
        self.assertEqual(physical, [])


class PublicPageTests(Public):
    def test_booking_page_in_spanish_and_arabic(self):
        token = self.open_booking()
        es = self.anon().get(f"/book/{token}?lang=es").get_data(as_text=True)
        self.assertIn('<html lang="es" dir="ltr">', es)
        self.assertIn("Pedir una cita", es)
        ar = self.anon().get(f"/book/{token}?lang=ar").get_data(as_text=True)
        self.assertIn('<html lang="ar" dir="rtl">', ar)
        self.assertIn("طلب موعد", ar)
        self.assertEqual(check(ar), [])
        self.assertIn('hreflang="pt"', es)                                             # the visitor can switch to any of them


class PatientSideTests(Base):
    def test_messages_and_documents_follow_the_patients_language(self):
        conn = self.db()
        conn.execute("UPDATE patients SET language = 'pt' WHERE id = 1")
        conn.commit()
        self.assertTrue(MT.body_for(conn, 1, "reminder", "pt").startswith("Olá {first_name}, um lembrete da {clinic}"))
        conn.execute("INSERT INTO message_templates(clinic_id, kind, body) VALUES (1, 'birthday', 'Our own words, {first_name}.') ON CONFLICT DO NOTHING")
        conn.execute("UPDATE message_templates SET body = 'Our own words, {first_name}.' WHERE clinic_id = 1 AND kind = 'birthday'")
        conn.commit()
        self.assertEqual(MT.body_for(conn, 1, "birthday", "pt"), "Our own words, {first_name}.")           # the clinic's own wording wins
        conn.close()

    def test_staff_screens_stay_in_english_or_french(self):
        conn = self.db()
        conn.execute("UPDATE clinics SET language = 'es' WHERE id = 1")
        conn.commit()
        conn.close()
        mgr = self.login("mgr")
        page = self.page(mgr, "/")
        self.assertIn('<html lang="en-GB" dir="ltr">', page)                           # a Spanish-speaking clinic's staff screens: English
        sec = self.page(mgr, "/account/security")
        options = re.findall(r'<option value="(\w+)"', sec.split('id="language"', 1)[1].split("</select>", 1)[0])
        self.assertEqual(options, ["en", "fr"])
        self.assertIn('<option value="ar"', self.page(mgr, "/settings"))               # but the clinic and patients may choose any
        self.assertIn('<option value="ar"', self.page(mgr, "/patients/1/edit"))
        self.assertEqual(i18n.user_lang({"language": "ar"}, {"language": "es"}), "en")
        self.assertEqual(i18n.user_lang({"language": None}, {"language": "fr"}), "fr")
