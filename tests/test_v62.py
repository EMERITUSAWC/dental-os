"""v0.62: the pharmacy check on prescriptions, the FHIR copy, and sending to an e-prescribing network."""
import datetime
import json
import re

from tests.test_v04 import Base
from dental_os.services import prescriptions as RX, rxlink as RL

KEY = "erx-test-key-0123456789"


class Fake:
    def __init__(self, code=201, answer=None):
        self.code, self.answer, self.calls = code, answer or {"resourceType": "Bundle", "id": "NET-778"}, []

    def __call__(self, url, headers, body, timeout=30):
        self.calls.append((url, headers, body))
        return self.code, json.dumps(self.answer)


class Rx(Base):
    def setUp(self):
        super().setUp()
        self.conn = self.db()
        self.conn.execute("UPDATE providers SET reg_body = 'GMDC', reg_number = 'D-1234' WHERE id = 1")
        self.conn.execute("UPDATE clinics SET address = '12 Oxford Street, Osu, Accra' WHERE id = 1")
        self.drug = self.conn.execute("""INSERT INTO drugs(clinic_id, name, strength, form, dose, days, quantity, drug_class, controlled)
                                         VALUES (1, 'Amoxicillin', '500 mg', 'capsules', 'One three times a day', 5, '15 capsules', 'penicillin', 0)""").lastrowid
        self.cd = self.conn.execute("""INSERT INTO drugs(clinic_id, name, strength, form, dose, quantity, drug_class, controlled)
                                       VALUES (1, 'Dihydrocodeine', '30 mg', 'tablets', 'One every 6 hours', '10 tablets', 'other', 1)""").lastrowid
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def write(self, drug=None, pid=1, who="dent"):
        c = self.login(who)
        r = self.post(c, f"/patients/{pid}/prescriptions", {"action": "issue", "provider_id": "1", "drug_0": str(drug or self.drug)})
        self.assertEqual(r.status_code, 302, r.get_data(as_text=True)[:500])
        rx_id = int(re.search(r"/prescriptions/(\d+)", r.headers["Location"]).group(1))
        return c, rx_id

    def link(self, c, rx_id):
        page = self.page(c, f"/prescriptions/{rx_id}")
        return re.search(r"(/rx/\d+-[a-z2-7]+)", page).group(1)


class FormTests(Rx):
    def test_form_field_names(self):
        page = self.page(self.login("dent"), "/patients/1/prescriptions")
        self.assertIn('name="drug_0"', page)


class PharmacyCheckTests(Rx):
    def test_every_new_prescription_carries_the_check(self):
        c, rx_id = self.write()
        page = self.page(c, f"/prescriptions/{rx_id}")
        self.assertIn("Pharmacy: scan to check", page)
        self.assertIn("<svg", page)
        url = self.link(c, rx_id)
        self.assertEqual(url, self.link(c, rx_id))                                          # the same link every time it is printed

    def test_the_pharmacist_sees_enough_and_no_more(self):
        c, rx_id = self.write()
        page = self.app.test_client().get(self.link(c, rx_id)).get_data(as_text=True)
        self.assertIn("Genuine and valid", page)
        self.assertIn("Amoxicillin 500 mg capsules", page)
        self.assertIn("D-1234", page)
        self.assertIn("A.M.", page)                                                         # initials only
        self.assertIn("born 1990", page)
        for private in ("Ama", "Mensah", "1990-05-01", "+233201112223", "0201112223"):
            self.assertNotIn(private, page)
        self.assertNotIn('class="top"', page)                                              # no staff menu on the public page

    def test_dispensing_is_recorded_once(self):
        c, rx_id = self.write()
        url = self.link(c, rx_id)
        pharm = self.app.test_client()
        tok = re.search(r'name="_csrf" value="([^"]+)"', pharm.get(url).get_data(as_text=True)).group(1)
        r = pharm.post(url, data={"_csrf": tok, "pharmacy": "Osu Pharmacy", "pharmacist": "K. Asante"}, follow_redirects=True)
        self.assertIn("Tick the box", r.get_data(as_text=True))
        pharm.post(url, data={"_csrf": tok, "pharmacy": "Osu Pharmacy", "pharmacist": "K. Asante", "licence": "PC-555", "checked": "1"})
        page = pharm.get(url).get_data(as_text=True)
        self.assertIn("Already dispensed", page)
        self.assertIn("Osu Pharmacy", page)
        self.assertNotIn("Mark as dispensed", page)
        r = pharm.post(url, data={"_csrf": tok, "pharmacy": "Another Pharmacy", "pharmacist": "X Y", "checked": "1"}, follow_redirects=True)
        self.assertIn("already dispensed", r.get_data(as_text=True))
        row = self.conn.execute("SELECT pharmacy, pharmacist, licence FROM rx_links WHERE prescription_id = ?", (rx_id,)).fetchone()
        self.assertEqual(tuple(row), ("Osu Pharmacy", "K. Asante", "PC-555"))
        self.assertIn("Dispensed", self.page(c, f"/prescriptions/{rx_id}"))

    def test_cancelled_out_of_date_and_forged(self):
        c, rx_id = self.write()
        url = self.link(c, rx_id)
        anon = self.app.test_client()
        self.assertEqual(anon.get(url[:-2] + ("aa" if not url.endswith("aa") else "bb")).status_code, 404)    # a changed code is refused
        self.assertEqual(anon.get(f"/rx/{rx_id + 1}-abcdef").status_code, 404)
        self.conn.execute("UPDATE prescriptions SET issued_on = ? WHERE id = ?", ((datetime.date.today() - datetime.timedelta(days=RL.VALID_DAYS + 2)).isoformat(), rx_id))
        self.conn.commit()
        self.assertIn("Out of date", anon.get(url).get_data(as_text=True))
        self.conn.execute("UPDATE prescriptions SET issued_on = ? WHERE id = ?", (datetime.date.today().isoformat(), rx_id))
        self.conn.commit()
        self.post(c, f"/prescriptions/{rx_id}/cancel", {"reason": "Wrong patient"})
        page = anon.get(url).get_data(as_text=True)
        self.assertIn("Cancelled by the dentist", page)
        self.assertNotIn("Mark as dispensed", page)

    def test_controlled_drugs_have_the_shorter_limit(self):
        c, rx_id = self.write(self.cd)
        url = self.link(c, rx_id)
        self.assertIn("Controlled drug: check the patient", self.app.test_client().get(url).get_data(as_text=True))
        self.conn.execute("UPDATE prescriptions SET issued_on = ? WHERE id = ?", ((datetime.date.today() - datetime.timedelta(days=RL.CONTROLLED_VALID_DAYS + 1)).isoformat(), rx_id))
        self.conn.commit()
        self.assertIn("Out of date", self.app.test_client().get(url).get_data(as_text=True))

    def test_guessing_is_slowed_down(self):
        anon = self.app.test_client()
        for n in range(RL.BAD_PER_HOUR):
            anon.get(f"/rx/9999-guess{n}")
        c, rx_id = self.write()
        self.assertEqual(anon.get(self.link(c, rx_id)).status_code, 404)                 # even a real link, from that address, for an hour

    def test_another_clinic_cannot_see_the_staff_side(self):
        c, rx_id = self.write()
        other = self.login("mgr2")
        for url in (f"/prescriptions/{rx_id}", f"/prescriptions/{rx_id}/fhir.json"):
            self.assertEqual(other.get(url).status_code, 404, url)
        self.assertEqual(self.post(other, f"/prescriptions/{rx_id}/link").status_code, 404)


class FhirTests(Rx):
    def test_the_copy_is_a_fhir_bundle(self):
        c, rx_id = self.write()
        r = c.get(f"/prescriptions/{rx_id}/fhir.json")
        self.assertEqual(r.mimetype, "application/fhir+json")
        b = r.get_json(force=True)
        self.assertEqual((b["resourceType"], b["type"]), ("Bundle", "collection"))
        kinds = [e["resource"]["resourceType"] for e in b["entry"]]
        self.assertEqual(kinds, ["Patient", "Practitioner", "Organization", "MedicationRequest"])
        mr = b["entry"][3]["resource"]
        self.assertEqual((mr["status"], mr["intent"]), ("active", "order"))
        self.assertEqual(mr["medicationCodeableConcept"]["text"], "Amoxicillin 500 mg capsules")
        self.assertEqual(mr["dosageInstruction"][0]["text"], "One three times a day, for 5 days")
        self.assertEqual(mr["dispenseRequest"]["quantity"]["value"], 15)
        self.assertEqual(mr["subject"]["reference"], "Patient/p1")
        self.assertEqual(b["entry"][0]["resource"]["birthDate"], "1990-05-01")
        self.assertEqual(b["entry"][1]["resource"]["identifier"][0]["value"], "D-1234")
        for e in b["entry"]:
            self.assertTrue(e["fullUrl"].endswith(e["resource"]["resourceType"] + "/" + e["resource"]["id"]))


class NetworkTests(Rx):
    def switch_on(self, fake):
        self.app.config["ERX_TRANSPORT"] = fake
        r = self.post(self.login("mgr"), "/settings/e-prescribing", {"name": "Test Network", "endpoint": "https://erx.example.com/fhir/Bundle",
                                                                      "api_key": KEY, "enabled": "1"})
        self.assertEqual(r.status_code, 302, r.get_data(as_text=True)[:400])

    def test_setup_is_careful(self):
        self.assertEqual(self.login("dent").get("/settings/e-prescribing").status_code, 403)
        mgr = self.login("mgr")
        for bad in ({"endpoint": "http://erx.example.com"}, {"endpoint": "https://192.168.1.4/x"}, {"api_key": ""}):
            self.post(mgr, "/settings/e-prescribing", {"name": "N", "endpoint": "https://erx.example.com/x", "api_key": KEY, "enabled": "1", **bad})
            self.assertFalse(RL.network_on(self.conn, 1), bad)
        self.switch_on(Fake())
        self.assertTrue(RL.network_on(self.conn, 1))
        self.assertNotIn(KEY, self.conn.execute("SELECT api_key FROM erx_network").fetchone()[0])
        self.assertNotIn(KEY, self.page(mgr, "/settings/e-prescribing"))

    def test_sending(self):
        fake = Fake()
        self.switch_on(fake)
        c, rx_id = self.write()
        self.assertIn("Send to the e-prescribing network", self.page(c, f"/prescriptions/{rx_id}"))
        self.assertEqual(self.post(self.login("asst"), f"/prescriptions/{rx_id}/send").status_code, 403)
        self.post(c, f"/prescriptions/{rx_id}/send")
        url, headers, body = fake.calls[0]
        self.assertEqual((url, headers["Authorization"], headers["Content-Type"]), ("https://erx.example.com/fhir/Bundle", "Bearer " + KEY, "application/fhir+json"))
        self.assertEqual(json.loads(body)["resourceType"], "Bundle")
        row = self.conn.execute("SELECT status, network_ref, network FROM erx_sends").fetchone()
        self.assertEqual(tuple(row), ("accepted", "NET-778", "Test Network"))
        self.assertIn("NET-778", self.page(c, f"/prescriptions/{rx_id}"))
        self.assertIn("already been accepted", self.post(c, f"/prescriptions/{rx_id}/send", follow_redirects=True).get_data(as_text=True))
        self.assertEqual(len(fake.calls), 1)

    def test_refusals_are_kept(self):
        c, rx_id = self.write()
        for code, words in ((401, "refused the clinic's key"), (422, "did not accept"), (503, "could not take it")):
            self.switch_on(Fake(code))
            self.post(c, f"/prescriptions/{rx_id}/send")
            self.assertIn(words, self.conn.execute("SELECT detail FROM erx_sends ORDER BY id DESC").fetchone()[0])
        self.post(c, f"/prescriptions/{rx_id}/cancel", {"reason": "Changed my mind"})
        self.assertIn("cancelled prescription cannot be sent", self.post(c, f"/prescriptions/{rx_id}/send", follow_redirects=True).get_data(as_text=True))


class PageRulesTests(Rx):
    def test_the_pharmacy_page_follows_the_accessibility_rules(self):
        from tests.a11y import check
        c, rx_id = self.write()
        html = self.app.test_client().get(self.link(c, rx_id)).get_data(as_text=True)
        self.assertEqual(check(html), [])
        self.assertEqual(check(self.page(c, f"/prescriptions/{rx_id}")), [])
        self.assertEqual(check(self.page(self.login("mgr"), "/settings/e-prescribing")), [])
