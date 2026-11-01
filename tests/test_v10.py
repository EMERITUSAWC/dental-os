"""v0.10: French for the daily staff screens."""
import re
import unittest

from dental_os.services import i18n_fr, i18n_fr_staff, i18n_fr_staff7
from tests.test_v09 import French, SRC, MARKED

PAGES = ["/", "/diary", "/patients", "/patients?book=1", "/patients/1", "/patients/new", "/patients/1/edit", "/patients/1/treatment",
         "/patients/1/account", "/patients/1/messages", "/messages", "/recalls", "/appointments/new?patient_id=1"]


class StaffFrenchTests(French):
    def setUp(self):
        super().setUp()
        self.aid = self.appt(days=1)
        self.sql("UPDATE patients SET reminders_ok = 1, offers_ok = 1 WHERE id = 1")

    def pages(self):
        return PAGES + ["/appointments/%d" % self.aid, "/appointments/%d/reschedule" % self.aid]

    def test_every_daily_screen_opens_in_both_languages(self):
        c = self.login("mgr")
        for lang in ("en", "fr"):
            self.fr_clinic(lang)
            for url in self.pages():
                self.text(c, url)

    def test_the_dashboard_and_the_patient_pages_are_in_french(self):
        self.fr_clinic()
        c = self.login("mgr")
        page = self.text(c, "/")
        for word in ("rendez-vous aujourd&#39;hui", "Patients du jour", "Liste d&#39;attente", "Anniversaires dans les 7 prochains jours"):
            self.assertIn(word, page)
        self.assertNotIn(">Waiting list<", page)
        page = self.text(c, "/patients/1")
        for word in ("Coordonnées", "Aperçu", "Schéma dentaire", "Prendre rendez-vous", "Messages WhatsApp"):
            self.assertIn(word, page)
        self.assertNotIn(">Overview<", page)

    def test_forms_in_french(self):
        self.fr_clinic()
        c = self.login("mgr")
        page = self.text(c, "/patients/new")
        for word in ("Nouveau patient", "Prénom", "Numéro de téléphone ou WhatsApp", "Enregistrer le patient", "Consentement"):
            self.assertIn(word, page)
        self.assertIn('value="Female"', page)               # what is stored stays the same in both languages
        self.assertIn("Féminin", page)
        page = self.text(c, "/patients/1/edit")
        self.assertIn("Modifier", page)
        r = self.post(c, "/patients/1/edit", {"first_name": "Ama", "last_name": "Mensah", "phone": "0244000001", "gender": "Female"})
        self.assertEqual(self.one("SELECT gender FROM patients WHERE id = 1")[0], "Female")

    def test_appointment_screens_in_french(self):
        self.fr_clinic()
        c = self.login("mgr")
        page = self.text(c, "/appointments/%d" % self.aid)
        for word in ("Confirmer avec le patient", "WhatsApp\u00a0: confirmer le rendez-vous", "Marquer comme terminé", "Déplacer"):
            self.assertIn(word.replace("'", "&#39;"), page)
        page = self.text(c, "/appointments/%d/reschedule" % self.aid)
        self.assertIn("Déplacer le rendez-vous", page)
        self.assertIn("Chercher des créneaux libres", page)
        page = self.text(c, "/appointments/new?patient_id=1")
        self.assertIn("Nouveau rendez-vous", page)
        self.assertIn("Chercher des créneaux libres", page)

    def test_plural_forms_on_the_dashboard(self):
        self.fr_clinic()
        self.build()
        c = self.login("mgr")
        page = self.text(c, "/")
        self.assertRegex(page, r"\d+ messages? à envoyer|À traiter|Needs attention|Personne")
        self.assertNotIn("%(n)s", page)

    def test_the_other_clinic_and_english_are_unchanged(self):
        self.fr_clinic("en")
        c = self.login("mgr")
        page = self.text(c, "/")
        self.assertIn("Today&#39;s patients", page)
        self.assertNotIn("Patients du jour", page)
        self.assertIn("Overview", self.text(c, "/patients/1"))

    def test_staff_catalogue_has_no_blank_or_clashing_lines(self):
        for k, v in i18n_fr_staff.STAFF.items():
            self.assertTrue(v.strip(), k)
            self.assertEqual(sorted(re.findall(r"%\((\w+)\)s", k)), sorted(re.findall(r"%\((\w+)\)s", v)), k)
            self.assertNotIn("—", v)
        self.assertEqual(i18n_fr.FR["Today's patients"], "Patients du jour")



class FlashMessageTests(French):
    def test_every_plain_message_after_an_action_has_french(self):
        found = set()
        for name in sorted(p.name for p in SRC.glob("*.py")):
            if not (SRC / name).exists():
                continue
            text = (SRC / name).read_text()
            for m in re.finditer(r"flash\(\s*(f?)\"((?:[^\"\\]|\\.)*)\"", text):
                if not m.group(1) and "{" not in m.group(2):
                    found.add(m.group(2))
            for m in re.finditer(r"else\s+\"((?:[^\"\\]|\\.)*)\", \"(?:ok|error)\"\)", text):
                found.add(m.group(1))
        missing = sorted(k for k in found if k.strip() and k not in i18n_fr.FR)
        # messages that carry a name, a number or an error from a service stay in English on purpose
        self.assertEqual(missing, [])
        self.assertGreater(len(found), 40)
        for k in i18n_fr_staff.FLASH:
            self.assertTrue(any(k in (SRC / n).read_text() for n in ("views.py", "comms.py", "clinical.py")), k)

    def test_every_plain_message_in_the_french_list_is_still_in_the_code(self):
        code = "\n".join(p.read_text() for p in SRC.rglob("*.py") if not p.name.startswith("i18n_fr"))
        for k in sorted(i18n_fr.MESSAGE_KEYS):
            self.assertTrue(k in code or k.replace('"', '\\"') in code, k)
            self.assertEqual(sorted(re.findall(r"%\((\w+)\)s", k)), sorted(re.findall(r"%\((\w+)\)s", i18n_fr.FR[k])), k)
            self.assertNotIn("—", i18n_fr.FR[k])

    def test_a_message_shows_in_french_and_a_built_message_falls_back(self):
        self.fr_clinic()
        c = self.login("mgr")
        r = self.post(c, "/patients/1/edit", {"first_name": "Ama", "last_name": "Mensah", "phone": "0244000001"}, follow_redirects=True)
        self.assertIn("Modifications enregistrées.", r.get_data(as_text=True))
        self.fr_clinic("en")
        r = self.post(c, "/patients/1/edit", {"first_name": "Ama", "last_name": "Mensah", "phone": "0244000001"}, follow_redirects=True)
        self.assertIn("Changes saved.", r.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
