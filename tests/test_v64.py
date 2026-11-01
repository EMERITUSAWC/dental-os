"""v0.64: the FHIR R4 read-only API. Keys must be marked for clinical records; one clinic never sees another's; reads are logged."""
import json
import re

from tests.test_v04 import Base
from dental_os.services import apikeys, fhir as F


class Fhir(Base):
    def setUp(self):
        super().setUp()
        self.conn = self.db()
        c1 = self.conn.execute("SELECT * FROM clinics WHERE id = 1").fetchone()
        c2 = self.conn.execute("SELECT * FROM clinics WHERE id = 2").fetchone()
        _, self.key = apikeys.create(self.conn, c1, 1, "Korle Bu referrals", "fhir")
        _, self.plain = apikeys.create(self.conn, c1, 1, "Accounts export", "read")
        _, self.other = apikeys.create(self.conn, c2, 5, "Their FHIR", "fhir")
        self.conn.execute("UPDATE providers SET reg_body = 'GMDC', reg_number = 'D-1234', qualifications = 'BDS' WHERE id = 1")
        self.conn.execute("UPDATE patients SET gender = 'Female', email = 'ama@example.com' WHERE id = 1")
        self.conn.execute("INSERT INTO chairs(clinic_id, name) VALUES (1, 'Chair 1')")
        self.conn.execute("""INSERT INTO appointments(clinic_id, patient_id, provider_id, chair_id, start_at, duration_min, status, category)
                             VALUES (1, 1, 1, 1, '2026-11-03 09:30', 45, 'scheduled', 'Fillings')""")
        self.conn.execute("""INSERT INTO appointments(clinic_id, patient_id, provider_id, chair_id, start_at, duration_min, status, category, break_reason)
                             VALUES (1, 1, 1, 1, '2026-09-01 10:00', 30, 'broken', 'Consultation', 'missed')""")
        self.conn.execute("""INSERT INTO appointments(clinic_id, patient_id, provider_id, chair_id, start_at, duration_min, status, category, break_reason)
                             VALUES (1, 1, 1, 1, '2026-08-01 10:00', 30, 'broken', 'Consultation', 'cancelled')""")
        code = self.conn.execute("INSERT INTO procedure_codes(clinic_id, code, name, category, fee_p, cdt_code) VALUES (1, 'FHIRFILL', 'Composite filling', 'Fillings', 30000, 'D2391')").lastrowid
        self.conn.execute("""INSERT INTO procedures(clinic_id, patient_id, code_id, tooth, surfaces, status, fee_p, provider_id, planned_on, done_on)
                             VALUES (1, 1, ?, 36, 'MO', 'done', 30000, 1, '2026-09-01', '2026-09-01')""", (code,))
        self.conn.execute("INSERT INTO patient_alerts(clinic_id, patient_id, kind, severity, text) VALUES (1, 1, 'allergy', 'critical', 'Penicillin, anaphylaxis')")
        self.conn.execute("INSERT INTO patient_alerts(clinic_id, patient_id, kind, severity, text) VALUES (1, 1, 'medical', 'warning', 'Asthma')")
        payer = self.conn.execute("INSERT INTO payers(clinic_id, name, kind) VALUES (1, 'Acacia Health', 'insurer')").lastrowid
        self.conn.execute("INSERT INTO patient_cover(clinic_id, patient_id, payer_id, member_no, holder_name, coverage_pct, active) VALUES (1, 1, ?, 'ACA-889', 'Ama Mensah', 80, 1)", (payer,))
        rx = self.conn.execute("INSERT INTO prescriptions(clinic_id, patient_id, provider_id, issued_on) VALUES (1, 1, 1, '2026-09-01')").lastrowid
        self.conn.execute("""INSERT INTO prescription_items(clinic_id, prescription_id, name, strength, form, dose, days, quantity)
                             VALUES (1, ?, 'Metronidazole', '400 mg', 'tablets', 'One three times a day', 3, '9 tablets')""", (rx,))
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def get(self, path, key=None):
        r = self.app.test_client().get("/api/fhir/R4" + path, headers={"Authorization": f"Bearer {key or self.key}"})
        return r, (json.loads(r.get_data(as_text=True)) if r.get_data() else None)


class AccessTests(Fhir):
    def test_keys(self):
        r, body = self.app.test_client().get("/api/fhir/R4/Patient"), None
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.mimetype, F.MIME)
        r, body = self.get("/Patient", self.plain)
        self.assertEqual(r.status_code, 403)                                           # an ordinary key never reaches clinical records
        self.assertEqual(body["resourceType"], "OperationOutcome")
        r = self.app.test_client().post("/api/fhir/R4/Patient", headers={"Authorization": f"Bearer {self.key}"}, json={})
        self.assertEqual(r.status_code, 405)
        r, body = self.app.test_client().get("/api/fhir/R4/metadata"), None
        cap = r.get_json(force=True)
        self.assertEqual((cap["resourceType"], cap["fhirVersion"]), ("CapabilityStatement", "4.0.1"))
        self.assertEqual({x["type"] for x in cap["rest"][0]["resource"]}, set(F.TYPES))

    def test_one_clinic_never_sees_another(self):
        r, body = self.get("/Patient", self.other)
        self.assertEqual([e["resource"]["id"] for e in body["entry"]], ["3"])
        for path in ("/Patient/1", "/Patient/1/$everything", "/AllergyIntolerance?patient=1", "/Procedure?patient=Patient/1", "/Organization/1"):
            r, body = self.get(path, self.other)
            self.assertTrue(r.status_code == 404 or not body.get("entry"), path)

    def test_reads_are_in_the_patients_access_log(self):
        self.get("/Patient/1/$everything")
        self.get("/AllergyIntolerance?patient=1")
        rows = self.conn.execute("SELECT detail FROM audit_log WHERE action = 'fhir.read' AND object_id = 1 ORDER BY id").fetchall()
        self.assertEqual([r[0] for r in rows], ["Korle Bu referrals: everything", "Korle Bu referrals: AllergyIntolerance"])
        page = self.page(self.login("mgr"), "/patients/1/privacy")
        self.assertIn("Korle Bu referrals", page)

    def test_the_key_screen(self):
        mgr = self.login("mgr")
        page = self.page(mgr, "/integrations")
        self.assertIn('<option value="fhir">', page)
        self.assertIn("Clinical records through FHIR, read only", page)
        self.assertIn("who looked", page)


class ResourceTests(Fhir):
    def test_patient(self):
        r, p = self.get("/Patient/1")
        self.assertEqual((p["resourceType"], p["gender"], p["birthDate"]), ("Patient", "female", "1990-05-01"))
        self.assertEqual(p["name"][0], {"use": "official", "family": "Mensah", "given": ["Ama"]})
        self.assertEqual({t["system"] for t in p["telecom"]}, {"phone", "email"})
        r, b = self.get("/Patient?family=mens")
        self.assertEqual([e["resource"]["id"] for e in b["entry"]], ["1"])
        r, b = self.get("/Patient?birthdate=2016-05-01")
        self.assertEqual([e["resource"]["id"] for e in b["entry"]], ["2"])
        r, b = self.get("/Patient?phone=0201112223")
        self.assertEqual([e["resource"]["id"] for e in b["entry"]], ["1"])
        r, b = self.get("/Patient?colour=blue")
        self.assertEqual((r.status_code, b["issue"][0]["code"]), (400, "not-supported"))

    def test_clinical_resources(self):
        r, b = self.get("/Procedure?patient=1")
        pr = b["entry"][0]["resource"]
        self.assertEqual(pr["status"], "completed")
        self.assertEqual(pr["code"]["coding"][0], {"system": F.CDT_SYSTEM, "code": "D2391"})
        self.assertEqual(pr["bodySite"][0]["coding"][0], {"system": F.TOOTH_SYSTEM, "code": "36"})
        self.assertEqual(pr["performedDateTime"], "2026-09-01")
        r, b = self.get("/AllergyIntolerance?patient=1")
        self.assertEqual([e["resource"]["code"]["text"] for e in b["entry"]], ["Penicillin, anaphylaxis"])      # asthma is not an allergy
        self.assertEqual(b["entry"][0]["resource"]["criticality"], "high")
        r, b = self.get("/MedicationRequest?patient=1")
        mr = b["entry"][0]["resource"]
        self.assertEqual((mr["medicationCodeableConcept"]["text"], mr["dosageInstruction"][0]["text"]), ("Metronidazole 400 mg tablets", "One three times a day, for 3 days"))
        r, b = self.get("/Coverage?patient=1")
        self.assertEqual((b["entry"][0]["resource"]["subscriberId"], b["entry"][0]["resource"]["payor"][0]["display"]), ("ACA-889", "Acacia Health"))
        r, b = self.get("/Practitioner/1")
        self.assertEqual(b["identifier"][0]["value"], "D-1234")

    def test_appointments_and_dates(self):
        r, b = self.get("/Appointment?patient=1")
        got = {e["resource"]["status"]: e["resource"] for e in b["entry"]}
        self.assertEqual(set(got), {"booked", "noshow", "cancelled"})
        self.assertEqual(got["booked"]["start"], "2026-11-03T09:30:00+00:00")             # Accra is on UTC
        self.assertEqual(got["booked"]["end"], "2026-11-03T10:15:00+00:00")
        self.assertNotIn("cancelationReason", got["noshow"])
        self.assertEqual(got["cancelled"]["cancelationReason"]["coding"][0]["code"], "pat")
        r, b = self.get("/Appointment?patient=1&date=ge2026-10-01")
        self.assertEqual([e["resource"]["status"] for e in b["entry"]], ["booked"])

    def test_paging(self):
        for i in range(5):
            self.conn.execute("INSERT INTO patients(clinic_id, first_name, last_name, phone) VALUES (1, 'Page', ?, ?)", (f"Test{i}", f"+23350000{i:04d}"))
        self.conn.commit()
        r, b = self.get("/Patient?_count=3")
        self.assertEqual(len(b["entry"]), 3)
        nxt = next(l["url"] for l in b["link"] if l["relation"] == "next")
        seen = [e["resource"]["id"] for e in b["entry"]]
        while nxt:
            path = re.sub(r"^.*?/api/fhir/R4", "", nxt)
            r, b = self.get(path)
            seen += [e["resource"]["id"] for e in b["entry"]]
            nxt = next((l["url"] for l in b["link"] if l["relation"] == "next"), None)
        self.assertEqual(len(seen), len(set(seen)))
        self.assertEqual(len(seen), 7)                                                  # two from the start, five added

    def test_everything(self):
        r, b = self.get("/Patient/1/$everything")
        kinds = [e["resource"]["resourceType"] for e in b["entry"]]
        self.assertEqual(kinds[0], "Patient")
        for k in ("Appointment", "Procedure", "AllergyIntolerance", "MedicationRequest", "Coverage", "Practitioner", "Organization"):
            self.assertIn(k, kinds)
        self.assertEqual(kinds.count("Practitioner"), 1)
        self.assertEqual(self.get("/Patient/99/$everything")[0].status_code, 404)
        for e in b["entry"]:
            self.assertTrue(e["fullUrl"].endswith(f"/{e['resource']['resourceType']}/{e['resource']['id']}"))
