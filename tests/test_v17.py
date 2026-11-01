"""v0.17: clinical depth. Prescriptions with safety checks, referrals in and out, vital signs, letters, the contact log, X-ray layouts
and the image viewer."""
from datetime import timedelta

from tests.test_v04 import Base, PNG
from dental_os.services import (contactlog as CL, letters as L, mounts as M, prescriptions as RX, privacy as PV, referrals as REF,
                                vitals as V)
from dental_os.services.timeutil import now_local


class Care(Base):
    def setUp(self):
        super().setUp()
        self.conn = self.db()
        self.conn.execute("UPDATE clinics SET address = '12 Oxford Street, Osu, Accra', phone = '+233302000000' WHERE id = 1")
        self.conn.execute("UPDATE providers SET reg_body = 'MDC', reg_number = 'D1234', qualifications = 'BDS' WHERE id = 1")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def clinic(self):
        return dict(self.conn.execute("SELECT * FROM clinics WHERE id = 1").fetchone())

    def user(self, name="dent"):
        return dict(self.conn.execute("SELECT * FROM users WHERE username = ?", (name,)).fetchone())

    def drugs(self):
        RX.add_starter_drugs(self.conn, self.clinic(), 1)
        return {r["name"]: r["id"] for r in self.conn.execute("SELECT id, name FROM drugs WHERE clinic_id = 1")}


class PrescriptionTests(Care):
    def test_starter_medicines_carry_their_source_and_only_checked_doses(self):
        d = self.drugs()
        self.assertEqual(len(d), len(RX.STARTER_DRUGS))
        amox = self.conn.execute("SELECT * FROM drugs WHERE name = 'Amoxicillin'").fetchone()
        self.assertEqual((amox["strength"], amox["dose"], amox["days"]), ("500 mg", "1 capsule three times daily", 5))
        self.assertIn("College of General Dentistry", amox["source"])
        ibu = self.conn.execute("SELECT * FROM drugs WHERE name = 'Ibuprofen'").fetchone()
        self.assertEqual(ibu["dose"], "")                                  # no dose until the clinic's dentist writes one
        self.assertEqual(RX.add_starter_drugs(self.conn, self.clinic(), 1), 0)

    def test_write_print_and_cancel(self):
        d = self.drugs()
        rx = RX.create(self.conn, self.clinic(), self.user(), 1, 1, [{"drug_id": d["Amoxicillin"]}, {"drug_id": d["Ibuprofen"], "dose": "1 tablet three times daily after food", "days": "3"}])
        items = RX.items(self.conn, rx)
        self.assertEqual([i["dose"] for i in items], ["1 capsule three times daily", "1 tablet three times daily after food"])
        self.assertEqual(items[1]["days"], 3)
        self.conn.execute("UPDATE drugs SET dose = 'changed later' WHERE id = ?", (d["Amoxicillin"],))
        self.assertEqual(RX.items(self.conn, rx)[0]["dose"], "1 capsule three times daily")       # what was written stays
        self.conn.commit()
        c = self.login("dent")
        page = self.page(c, f"/prescriptions/{rx}")
        for words in ("12 Oxford Street", "Ama Mensah", "Amoxicillin 500 mg capsules", "MDC D1234", "BDS", "Signed:"):
            self.assertIn(words, page)
        r = self.post(c, f"/prescriptions/{rx}/cancel", {"reason": "Wrong patient"})
        self.assertEqual(r.status_code, 302)
        self.assertIn("CANCELLED", self.page(c, f"/prescriptions/{rx}"))
        with self.assertRaises(RX.RxError):
            RX.cancel(self.conn, self.clinic(), self.user(), rx, "again")

    def test_a_childs_age_is_printed(self):
        d = self.drugs()
        rx = RX.create(self.conn, self.clinic(), self.user(), 2, 1, [{"drug_id": d["Amoxicillin"]}])
        self.conn.commit()
        age = RX.age_on("2016-05-01", now_local(self.clinic()["timezone"]).date())
        self.assertIn(f"age {age}", self.page(self.login("dent"), f"/prescriptions/{rx}"))

    def test_the_paperwork_must_be_complete(self):
        d = self.drugs()
        self.conn.execute("UPDATE providers SET reg_number = NULL WHERE id = 1")
        with self.assertRaises(RX.RxError) as ctx:
            RX.create(self.conn, self.clinic(), self.user(), 1, 1, [{"drug_id": d["Amoxicillin"]}])
        self.assertIn("registration number", str(ctx.exception))
        self.conn.execute("UPDATE providers SET reg_number = 'D1' WHERE id = 1")
        self.conn.execute("UPDATE clinics SET address = NULL WHERE id = 1")
        with self.assertRaises(RX.RxError) as ctx:
            RX.create(self.conn, self.clinic(), self.user(), 1, 1, [{"drug_id": d["Amoxicillin"]}])
        self.assertIn("address", str(ctx.exception))
        self.conn.execute("UPDATE clinics SET address = 'Accra' WHERE id = 1")
        with self.assertRaises(RX.RxError) as ctx:
            RX.create(self.conn, self.clinic(), self.user(), 1, 1, [{"drug_id": d["Ibuprofen"]}])        # no dose on the list, none written
        self.assertIn("how Ibuprofen is to be taken", str(ctx.exception))
        with self.assertRaises(RX.RxError):
            RX.create(self.conn, self.clinic(), self.user("asst"), 1, 1, [{"drug_id": d["Amoxicillin"]}])
        with self.assertRaises(RX.RxError):
            RX.create(self.conn, self.clinic(), self.user(), 1, 1, [])
        with self.assertRaises(RX.RxError):
            RX.create(self.conn, self.clinic(), self.user(), 1, 2, [{"drug_id": d["Amoxicillin"]}])          # another clinic's dentist

    def test_a_penicillin_allergy_stops_amoxicillin_until_a_reason_is_given(self):
        d = self.drugs()
        self.conn.execute("INSERT INTO patient_alerts(clinic_id, patient_id, kind, severity, text) VALUES (1, 1, 'allergy', 'critical', 'Penicillin: rash')")
        warn = RX.checks(self.conn, 1, 1, [RX.get_drug(self.conn, 1, d["Amoxicillin"])])
        self.assertEqual(warn[0]["level"], "stop")
        self.assertEqual(RX.checks(self.conn, 1, 1, [RX.get_drug(self.conn, 1, d["Metronidazole"])]), [])
        with self.assertRaises(RX.RxError) as ctx:
            RX.create(self.conn, self.clinic(), self.user(), 1, 1, [{"drug_id": d["Amoxicillin"]}])
        self.assertIn("safety warning", str(ctx.exception))
        rx = RX.create(self.conn, self.clinic(), self.user(), 1, 1, [{"drug_id": d["Amoxicillin"]}], override_reason="Allergy tested negative 2025")
        self.assertEqual(RX.get(self.conn, 1, rx)["override_reason"], "Allergy tested negative 2025")

    def test_warfarin_and_metronidazole_and_other_checks(self):
        d = self.drugs()
        self.conn.execute("INSERT INTO medical_history(clinic_id, patient_id, medications, blood_thinners, pregnant) VALUES (1, 1, 'Warfarin 5 mg daily', 1, 'yes')")
        metro = RX.checks(self.conn, 1, 1, [RX.get_drug(self.conn, 1, d["Metronidazole"])])
        self.assertTrue(any(w["level"] == "stop" and "warfarin" in w["text"] and "SDCEP" in w["source"] for w in metro))
        ibu = RX.checks(self.conn, 1, 1, [RX.get_drug(self.conn, 1, d["Ibuprofen"])])
        self.assertTrue(any(w["level"] == "caution" and "blood thinners" in w["text"] for w in ibu))
        self.assertTrue(any("pregnant" in w["text"] for w in ibu))

    def test_screens_and_who_can_use_them(self):
        d = self.drugs()
        self.conn.commit()
        self.assertEqual(self.login("desk").get("/patients/1/prescriptions").status_code, 403)
        a = self.login("asst")
        page = self.page(a, "/patients/1/prescriptions")
        self.assertNotIn("Write a prescription", page)
        c = self.login("dent")
        page = self.page(c, "/patients/1/prescriptions")
        self.assertIn("Write a prescription", page)
        r = self.post(c, "/patients/1/prescriptions", {"action": "check", "provider_id": "1", "drug_0": str(d["Amoxicillin"])})
        self.assertIn("No warnings found", r.get_data(as_text=True))
        r = self.post(c, "/patients/1/prescriptions", {"action": "issue", "provider_id": "1", "drug_0": str(d["Amoxicillin"])})
        self.assertEqual(r.status_code, 302)
        self.assertIn("/prescriptions/", r.headers["Location"])
        self.assertEqual(self.login("dent").get("/tools/medicines").status_code, 403)
        m = self.login("mgr")
        self.post(m, "/tools/medicines", {"name": "Clarithromycin", "strength": "250 mg", "form": "tablets", "drug_class": "macrolide"})
        self.assertIn("Clarithromycin", self.page(m, "/tools/medicines"))
        r = self.post(m, "/tools/medicines", {"name": "", "drug_class": "macrolide"}, follow_redirects=True)
        self.assertIn("generic name", r.get_data(as_text=True))


class ReferralTests(Care):
    def test_referral_out_from_draft_to_closed(self):
        clinic = self.clinic()
        sp = REF.save_referrer(self.conn, clinic, 1, None, "Dr Oral Surgeon", "specialist", "Oral surgery", "Korle Bu", "0201234567", "os@example.com")
        self.conn.execute("INSERT INTO patient_alerts(clinic_id, patient_id, kind, severity, text) VALUES (1, 1, 'allergy', 'critical', 'Latex')")
        rid = REF.create_out(self.conn, clinic, 1, 1, sp, "Impacted 38, please assess for removal", "soon", "38", "Pain for two weeks", 1)
        self.conn.commit()
        c = self.login("dent")
        page = self.page(c, f"/referrals/{rid}")
        for words in ("Dr Oral Surgeon", "Impacted 38", "Latex", "Soon (within two weeks)", "12 Oxford Street", "MDC D1234"):
            self.assertIn(words, page)
        for status in ("sent", "booked", "seen"):
            self.post(c, f"/referrals/{rid}", {"status": status})
        r = self.post(c, f"/referrals/{rid}", {"status": "report"}, follow_redirects=True)
        self.assertIn("Write what the report says", r.get_data(as_text=True))
        self.post(c, f"/referrals/{rid}", {"status": "report", "note": "38 removed, healing well"})
        self.assertIn("38 removed", self.page(c, "/referrals/%d" % rid))
        self.assertIn("Dr Oral Surgeon", self.page(c, "/referrals"))
        self.post(c, f"/referrals/{rid}", {"status": "closed"})
        self.assertNotIn("Dr Oral Surgeon", self.page(c, "/referrals"))
        with self.assertRaises(REF.ReferralError):
            REF.move(self.conn, clinic, 1, rid, "sent")
        row = self.db().execute("SELECT status, sent_on FROM referrals_out WHERE id = ?", (rid,)).fetchone()
        self.assertEqual(row["status"], "closed")
        self.assertTrue(row["sent_on"])

    def test_referrers_and_where_new_patients_came_from(self):
        clinic = self.clinic()
        dr = REF.save_referrer(self.conn, clinic, 1, None, "Dr Friendly GP", "doctor")
        with self.assertRaises(REF.ReferralError):
            REF.save_referrer(self.conn, clinic, 1, None, "X", "alien")
        with self.assertRaises(REF.ReferralError):
            REF.save_referrer(self.conn, clinic, 1, None, "X", "doctor", email="not an email")
        REF.set_source(self.conn, clinic, 1, 1, dr, "")
        REF.set_source(self.conn, clinic, 1, 2, None, "social")
        with self.assertRaises(REF.ReferralError):
            REF.set_source(self.conn, clinic, 1, 2, None, "pigeon")
        self.conn.commit()
        page = self.page(self.login("desk"), "/tools/referrers?from=2000-01-01")
        self.assertIn("Dr Friendly GP", page)
        self.assertIn("Social media", page)
        c = self.login("desk")
        r = self.post(c, "/patients/1/referral-source", {"referrer_id": "", "source": "word_of_mouth"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.db().execute("SELECT referral_source FROM patients WHERE id = 1").fetchone()[0], "word_of_mouth")
        self.assertEqual(self.login("asst").get("/tools/referrers").status_code, 403)


class VisitListTests(Care):
    def test_visit_lists_show_on_the_forms(self):
        self.conn.execute("INSERT INTO appointments(clinic_id, patient_id, provider_id, chair_id, start_at, status) VALUES (1, 1, 1, 1, '2026-01-05 09:30', 'complete')")
        self.drugs()
        self.conn.commit()
        c = self.login("dent")
        for url in ("/patients/1/vitals", "/patients/1/prescriptions", "/patients/1/notes/new"):
            self.assertIn("Consultation", self.page(c, url), url)


class VitalsTests(Care):
    def test_grades_follow_the_clinics_levels(self):
        clinic = self.clinic()
        self.assertEqual(V.grade(clinic, 120, 80), "ok")
        self.assertEqual(V.grade(clinic, 165, 85), "amber")
        self.assertEqual(V.grade(clinic, 150, 105), "amber")
        self.assertEqual(V.grade(clinic, 185, 90), "red")
        self.assertEqual(V.grade(clinic, 85, 55), "low")
        V.save_levels(self.conn, clinic, 1, "140/90", "170/105")
        self.assertEqual(V.grade(self.clinic(), 145, 80), "amber")
        for a, r in (("170/105", "140/90"), ("abc", "180/110"), ("300/100", "310/120")):
            with self.assertRaises(V.VitalsError):
                V.save_levels(self.conn, clinic, 1, a, r)

    def test_readings_are_checked_and_a_high_reading_shows_on_the_record(self):
        clinic = self.clinic()
        for form, words in (({}, "at least one"), ({"bp_systolic": "120"}, "both"), ({"bp_systolic": "80", "bp_diastolic": "90"}, "higher"),
                            ({"pulse": "500"}, "outside"), ({"temperature_c": "hot"}, "numbers")):
            with self.assertRaises(V.VitalsError) as ctx:
                V.record(self.conn, clinic, 1, 1, form)
            self.assertIn(words, str(ctx.exception))
        c = self.login("asst")
        r = self.post(c, "/patients/1/vitals", {"bp_systolic": "190", "bp_diastolic": "115", "pulse": "88", "temperature_c": "36,8"}, follow_redirects=True)
        self.assertIn("Very high: no treatment", r.get_data(as_text=True))
        self.assertEqual(self.db().execute("SELECT temperature_c FROM vitals").fetchone()[0], 36.8)
        page = self.page(c, "/patients/1/chart")
        self.assertIn("Blood pressure today", page)
        self.assertIn("190/115", page)
        self.assertEqual(self.login("desk").get("/patients/1/vitals").status_code, 403)


class LetterTests(Care):
    def test_a_new_clinic_gets_the_starter_letters(self):
        names = [r["name"] for r in L.list_templates(self.conn, 1)]
        self.assertEqual(sorted(names), sorted(n for n, _b in L.STARTERS))

    def test_fields_are_filled_and_the_words_can_be_changed_before_saving(self):
        t = self.conn.execute("SELECT id FROM letter_templates WHERE clinic_id = 1 AND name = 'Attendance letter'").fetchone()[0]
        start = (now_local(self.clinic()["timezone"]) - timedelta(days=3)).strftime("%Y-%m-%d 10:00")
        self.conn.execute("INSERT INTO appointments(clinic_id, patient_id, provider_id, chair_id, start_at, status) VALUES (1, 1, 1, 1, ?, 'complete')", (start,))
        self.conn.commit()
        c = self.login("desk")
        page = self.page(c, f"/patients/1/letters?template_id={t}&provider_id=1")
        self.assertIn("This is to confirm that Ama Mensah", page)
        self.assertNotIn("{patient_name}", page)
        r = self.post(c, "/patients/1/letters", {"template_id": str(t), "provider_id": "1", "title": "Attendance letter", "body": "Changed by hand for Ama."})
        self.assertEqual(r.status_code, 302)
        page = self.page(c, r.headers["Location"])
        self.assertIn("Changed by hand for Ama.", page)
        self.assertIn("MDC D1234", page)

    def test_template_fields_are_checked(self):
        with self.assertRaises(L.LetterError) as ctx:
            L.save_template(self.conn, self.clinic(), 1, None, "Odd", "Dear {first_name}, your {shoe_size}")
        self.assertIn("{shoe_size}", str(ctx.exception))
        with self.assertRaises(L.LetterError):
            L.save_template(self.conn, self.clinic(), 1, None, "Attendance letter", "Dup")
        tid = L.save_template(self.conn, self.clinic(), 1, None, "Thanks", "Thank you {first_name}")
        self.assertEqual(L.fill("Hi {first_name} {unknown}", {"first_name": "Ama"}), "Hi Ama {unknown}")
        self.assertTrue(tid)

    def test_anonymising_removes_the_words_of_letters_and_the_contact_log(self):
        L.save_letter(self.conn, self.clinic(), 1, 1, "Note", "Ama Mensah lives at 4 Ring Road")
        CL.add(self.conn, self.clinic(), 1, 1, "call_in", "Ama called about her bill")
        PV.anonymise(self.conn, self.clinic(), 1, 1, "Asked to be forgotten", PV.ANONYMISE_WORD)
        self.assertEqual(self.conn.execute("SELECT body FROM letters").fetchone()[0], "[removed]")
        self.assertEqual(self.conn.execute("SELECT summary FROM contact_log").fetchone()[0], "[removed]")


class ContactLogTests(Care):
    def test_log_follow_up_and_dashboard(self):
        clinic = self.clinic()
        today = now_local(clinic["timezone"]).date()
        with self.assertRaises(CL.ContactError):
            CL.add(self.conn, clinic, 1, 1, "call_in", "x", follow_up_on=(today - timedelta(days=1)).isoformat())
        with self.assertRaises(CL.ContactError):
            CL.add(self.conn, clinic, 1, 1, "pigeon", "x")
        lid = CL.add(self.conn, clinic, 1, 1, "call_in", "Asked for a quote for a crown", "Will call back", today.isoformat())
        self.conn.commit()
        c = self.login("desk")
        self.assertIn("Asked for a quote", self.page(c, "/"))
        self.post(c, f"/contacts/{lid}/done")
        self.assertNotIn("Asked for a quote", self.page(c, "/"))
        self.assertIn("Asked for a quote", self.page(c, "/patients/1/contacts"))
        r = self.post(c, "/patients/1/contacts", {"kind": "call_out", "summary": "Reminded about the visit"})
        self.assertEqual(r.status_code, 302)


class ImageTests(Care):
    def upload(self, c, pid=1, name="bw.png", mime_data=PNG):
        import io
        return self.post(c, f"/patients/{pid}/files", {"kind": "xray", "file": (io.BytesIO(mime_data), name), "taken_on": "2026-10-01"},
                         content_type="multipart/form-data")

    def test_layouts_and_the_viewer(self):
        c = self.login("dent")
        self.upload(c)
        self.upload(c, pid=2, name="other.png")
        mine = self.db().execute("SELECT id FROM patient_files WHERE patient_id = 1").fetchone()[0]
        theirs = self.db().execute("SELECT id FROM patient_files WHERE patient_id = 2").fetchone()[0]
        r = self.post(c, "/patients/1/images", {"layout": "bw4", "title": "Check-up bitewings"})
        mid = int(r.headers["Location"].rsplit("/", 1)[1])
        self.post(c, f"/mounts/{mid}", {"slot": "2", "file_id": str(mine)})
        page = self.page(c, f"/mounts/{mid}")
        self.assertIn(f"/files/{mine}/viewer", page)
        r = self.post(c, f"/mounts/{mid}", {"slot": "3", "file_id": str(theirs)}, follow_redirects=True)
        self.assertIn("Pick one of this patient", r.get_data(as_text=True))
        r = self.post(c, f"/mounts/{mid}", {"slot": "9", "file_id": str(mine)}, follow_redirects=True)
        self.assertIn("position", r.get_data(as_text=True))
        self.assertIn("1 of 4 filled", self.page(c, "/patients/1/images"))
        self.post(c, f"/mounts/{mid}", {"slot": "2", "file_id": ""})
        self.assertIn("0 of 4 filled", self.page(c, "/patients/1/images"))
        page = self.page(c, f"/files/{mine}/viewer")
        self.assertIn("viewer.js", page)
        self.assertIn(f"/files/{mine}", page)
        self.assertEqual(self.login("desk").get(f"/files/{mine}/viewer").status_code, 403)
        self.assertEqual(self.login("mgr2").get(f"/files/{mine}/viewer").status_code, 404)
        self.assertEqual(M.slot_count("fmx18"), 18)

    def test_a_pdf_does_not_open_in_the_viewer(self):
        c = self.login("dent")
        self.upload(c, name="letter.pdf", mime_data=b"%PDF-1.4\n%test\n")
        fid = self.db().execute("SELECT id FROM patient_files WHERE patient_id = 1").fetchone()[0]
        self.assertEqual(c.get(f"/files/{fid}/viewer").status_code, 404)


class RecordCopyTests(Care):
    def test_the_new_records_are_in_a_patients_copy(self):
        d = {}
        RX.add_starter_drugs(self.conn, self.clinic(), 1)
        d = self.conn.execute("SELECT id FROM drugs WHERE name = 'Amoxicillin'").fetchone()[0]
        RX.create(self.conn, self.clinic(), self.user(), 1, 1, [{"drug_id": d}])
        sp = REF.save_referrer(self.conn, self.clinic(), 1, None, "Dr S", "specialist")
        REF.create_out(self.conn, self.clinic(), 1, 1, sp, "Assess")
        V.record(self.conn, self.clinic(), 1, 1, {"pulse": "70"})
        L.save_letter(self.conn, self.clinic(), 1, 1, "Note", "Words")
        CL.add(self.conn, self.clinic(), 1, 1, "call_in", "Called")
        M.create(self.conn, self.clinic(), 1, 1, "bw2")
        data = PV.record_data(self.conn, 1, 1)
        for table in ("prescriptions", "prescription_items", "referrals_out", "vitals", "letters", "contact_log", "image_mounts"):
            self.assertTrue(data.get(table), table)
        self.conn.commit()
        self.assertEqual(self.login("mgr").get("/patients/1/record-copy").status_code, 200)


class FrenchTests(Care):
    def test_new_screens_in_french(self):
        self.conn.execute("UPDATE clinics SET language = 'fr' WHERE id = 1")
        self.conn.commit()
        c = self.login("dent")
        for url, words in (("/patients/1/prescriptions", "Rédiger une ordonnance"), ("/patients/1/vitals", "Prendre les mesures"),
                           ("/patients/1/referrals", "Adresser ce patient"), ("/patients/1/letters", "Rédiger une lettre"),
                           ("/patients/1/contacts", "Ajouter au journal"), ("/patients/1/images", "Nouvelle planche")):
            self.assertIn(words, self.page(c, url), url)
