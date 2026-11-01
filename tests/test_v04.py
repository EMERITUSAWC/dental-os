"""v0.4: the clinical record. Medical history and alerts, tooth chart, gum charts, notes, consents, files, export, import."""
import io
import re
import tempfile
import unittest
import zipfile
from pathlib import Path

from werkzeug.security import generate_password_hash

from dental_os import create_app, db as dbmod

PW = "correct-horse-battery"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = create_app({"TESTING": True, "DATABASE": str(Path(self.tmp.name) / "t.sqlite"), "SECRET_KEY": "test",
                               "FILES_PATH": str(Path(self.tmp.name) / "files")})
        r = self.app.test_cli_runner()
        r.invoke(args=["create-clinic", "--name", "Clinic One", "--country", "GH"])
        r.invoke(args=["create-clinic", "--name", "Clinic Two", "--country", "US"])
        conn = dbmod.connect(self.app.config["DATABASE"])
        for cid in (1, 2):
            conn.execute("INSERT INTO providers(clinic_id, name) VALUES (?, ?)", (cid, f"Dr {cid}"))
        users = [("mgr", 1, "manager"), ("dent", 1, "dentist"), ("asst", 1, "assistant"), ("desk", 1, "front_desk"),
                 ("mgr2", 2, "manager")]
        for name, cid, role in users:
            conn.execute("INSERT INTO users(clinic_id, username, display_name, role, password_hash) VALUES (?,?,?,?,?)",
                         (cid, name, name.title(), role, generate_password_hash(PW)))
        conn.execute("INSERT INTO patients(clinic_id, first_name, last_name, phone, dob, reminders_ok) VALUES "
                     "(1,'Ama','Mensah','+233201112223','1990-05-01',1)")                # patient 1, adult
        conn.execute("INSERT INTO patients(clinic_id, first_name, last_name, phone, dob) VALUES "
                     "(1,'Kofi','Young','+233201112224','2016-05-01')")                  # patient 2, child
        conn.execute("INSERT INTO patients(clinic_id, first_name, last_name, phone) VALUES "
                     "(2,'Zed','Other','+12025550147')")                                  # patient 3, clinic two
        conn.commit()
        conn.close()

    def db(self):
        return dbmod.connect(self.app.config["DATABASE"])

    def login(self, name):
        c = self.app.test_client()
        page = c.get("/login").get_data(as_text=True)
        token = re.search(r'name="_csrf" value="([^"]+)"', page).group(1)
        c.post("/login", data={"_csrf": token, "username": name, "password": PW})
        return c

    def tok(self, c):
        return re.search(r'name="_csrf" value="([^"]+)"', c.get("/").get_data(as_text=True)).group(1)

    def post(self, c, url, data=None, **kw):
        d = {"_csrf": self.tok(c)}
        d.update(data or {})
        return c.post(url, data=d, **kw)

    def page(self, c, url):
        r = c.get(url)
        self.assertEqual(r.status_code, 200, f"{url}: {r.get_data(as_text=True)[:300]}")
        return r.get_data(as_text=True)


class MedicalTests(Base):
    def test_history_makes_alerts_and_they_show_everywhere(self):
        c = self.login("dent")
        r = self.post(c, "/patients/1/medical", {"allergies": "Penicillin, anaphylaxis", "blood_thinners": "1",
                                                 "conditions": "Asthma", "pregnant": "no"})
        self.assertEqual(r.status_code, 302)
        page = self.page(c, "/patients/1")
        self.assertIn("Penicillin, anaphylaxis", page)
        self.assertIn("blood thinners", page)
        conn = self.db()
        sev = conn.execute("SELECT severity FROM patient_alerts WHERE kind = 'allergy'").fetchone()[0]
        self.assertEqual(sev, "critical")
        # saving again does not duplicate the alerts
        self.post(c, "/patients/1/medical", {"allergies": "Penicillin, anaphylaxis", "blood_thinners": "1"})
        n = conn.execute("SELECT COUNT(*) FROM patient_alerts WHERE active = 1 AND patient_id = 1").fetchone()[0]
        self.assertEqual(n, 2)
        # clearing the allergy list resolves the alert but keeps the row
        self.post(c, "/patients/1/medical", {"allergies": "", "blood_thinners": "1"})
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM patient_alerts WHERE active = 1").fetchone()[0], 1)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM patient_alerts").fetchone()[0], 2)

    def test_manual_alert_and_resolve(self):
        c = self.login("asst")
        self.post(c, "/patients/1/medical", {"action": "alert", "kind": "behaviour", "severity": "warning", "text": "Very anxious"})
        self.assertIn("Very anxious", self.page(c, "/patients/1/medical"))
        aid = self.db().execute("SELECT id FROM patient_alerts").fetchone()[0]
        self.post(c, f"/alerts/{aid}/resolve")
        self.assertNotIn("Very anxious", self.page(c, "/patients/1"))

    def test_front_desk_sees_alert_but_not_the_record(self):
        c = self.login("dent")
        self.post(c, "/patients/1/medical", {"allergies": "Latex"})
        d = self.login("desk")
        self.assertIn("Latex", self.page(d, "/patients/1"))
        self.assertNotIn("/patients/1/chart", self.page(d, "/patients/1"))
        for url in ("/patients/1/medical", "/patients/1/chart", "/patients/1/perio", "/patients/1/notes",
                    "/patients/1/consents", "/patients/1/files"):
            self.assertEqual(d.get(url).status_code, 403, url)

    def test_alert_marker_in_the_diary_and_dashboard(self):
        c = self.login("dent")
        self.post(c, "/patients/1/medical", {"allergies": "Latex"})
        conn = self.db()
        today = c.get("/").get_data(as_text=True)
        self.assertNotIn(">Alert<", today)
        conn.execute("INSERT INTO chairs(clinic_id, name) VALUES (1, 'Chair 1')")
        from dental_os.services.timeutil import now_local
        now = now_local("Africa/Accra")
        conn.execute("INSERT INTO appointments(clinic_id, patient_id, provider_id, chair_id, start_at, duration_min, status, category) "
                     "VALUES (1,1,1,1,?,30,'scheduled','Check-up')", (now.strftime("%Y-%m-%d") + " 10:00",))
        conn.commit()
        self.assertIn(">Alert<", self.page(c, "/"))
        self.assertIn(">Alert<", self.page(c, "/diary"))
        self.assertIn("Latex", self.page(c, "/appointments/1"))

    def test_other_clinics_patient_is_not_found(self):
        c = self.login("dent")
        self.assertEqual(c.get("/patients/3/medical").status_code, 404)
        r = self.post(c, "/patients/3/medical", {"action": "alert", "kind": "other", "severity": "info", "text": "x"})
        self.assertEqual(r.status_code, 404)
        self.assertEqual(self.db().execute("SELECT COUNT(*) FROM patient_alerts").fetchone()[0], 0)


class ChartTests(Base):
    def test_condition_shows_and_removal_keeps_history(self):
        c = self.login("dent")
        r = self.post(c, "/patients/1/chart/conditions", {"tooth": "16", "surfaces": ["O", "D"], "condition": "caries", "note": "deep"})
        self.assertEqual(r.status_code, 302)
        page = self.page(c, "/patients/1/chart?tooth=16")
        self.assertIn("Tooth 16", page)
        self.assertIn("Decay", page)
        cid = self.db().execute("SELECT id FROM tooth_conditions").fetchone()[0]
        self.post(c, f"/chart/conditions/{cid}/remove", {"tooth": "16"})
        row = self.db().execute("SELECT status, removed_on FROM tooth_conditions WHERE id = ?", (cid,)).fetchone()
        self.assertNotEqual(row["status"], "current")
        self.assertIsNotNone(row["removed_on"])

    def test_bad_conditions_are_refused(self):
        c = self.login("dent")
        for data in ({"tooth": "19", "surfaces": ["O"], "condition": "caries"},
                     {"tooth": "16", "surfaces": ["X"], "condition": "caries"},
                     {"tooth": "16", "surfaces": ["O"], "condition": "nonsense"},
                     {"tooth": "16", "surfaces": [], "condition": "caries"},
                     {"tooth": "abc", "surfaces": ["O"], "condition": "caries"}):
            self.post(c, "/patients/1/chart/conditions", data)
        self.assertEqual(self.db().execute("SELECT COUNT(*) FROM tooth_conditions").fetchone()[0], 0)

    def test_whole_tooth_condition_and_child_chart(self):
        c = self.login("dent")
        self.post(c, "/patients/2/chart/conditions", {"tooth": "55", "surfaces": [], "condition": "missing", "child": "1"})
        page = self.page(c, "/patients/2/chart?dentition=child")
        self.assertIn("Tooth 55, missing", page)
        self.assertIn("Tooth 85", page)
        self.assertNotIn("Tooth 18,", self.page(c, "/patients/2/chart?dentition=child"))

    def test_chart_pages_for_every_tooth_render(self):
        c = self.login("dent")
        for t in (11, 28, 38, 48):
            self.page(c, f"/patients/1/chart?tooth={t}")
        self.assertEqual(c.get("/patients/3/chart").status_code, 404)


class PerioTests(Base):
    def form(self, **extra):
        d = {"pk_16_B": "3", "pk_16_MB": "5", "pk_16_DB": "7", "bl_16_B": "1", "rc_16_B": "1", "pk_26_B": "2"}
        d.update(extra)
        return d

    def test_save_summary_and_compare(self):
        c = self.login("dent")
        r = self.post(c, "/patients/1/perio/new", self.form(notes="first"))
        self.assertEqual(r.status_code, 302)
        page = self.page(c, "/patients/1/perio")
        self.assertIn("Sites 4 mm+", page)
        show = self.page(c, r.headers["Location"])
        self.assertIn("sites 6 mm or more", show)
        self.assertNotIn("Since the last chart", show)
        r2 = self.post(c, "/patients/1/perio/new", self.form(pk_16_DB="3", pk_16_MB="3"))
        show2 = self.page(c, r2.headers["Location"])
        self.assertIn("Since the last chart", show2)

    def test_bad_readings_refused(self):
        c = self.login("dent")
        for bad in ({"pk_16_B": "16"}, {"pk_16_B": "abc"}, {"rc_16_B": "-9"}, {"pk_16_B": "2.5"}):
            self.post(c, "/patients/1/perio/new", bad)
        self.post(c, "/patients/1/perio/new", {})
        self.assertEqual(self.db().execute("SELECT COUNT(*) FROM perio_charts").fetchone()[0], 0)

    def test_missing_teeth_are_skipped(self):
        c = self.login("dent")
        self.post(c, "/patients/1/chart/conditions", {"tooth": "16", "surfaces": [], "condition": "missing"})
        self.post(c, "/patients/1/perio/new", self.form())
        teeth = {r[0] for r in self.db().execute("SELECT tooth FROM perio_readings")}
        self.assertNotIn(16, teeth)
        self.assertIn(26, teeth)

    def test_other_clinic_chart_is_not_found(self):
        c = self.login("dent")
        self.post(c, "/patients/1/perio/new", self.form())
        other = self.login("mgr2")
        self.assertEqual(other.get("/perio/1").status_code, 404)

    def test_form_page_renders(self):
        c = self.login("asst")
        self.assertIn('name="pk_18_DB"', self.page(c, "/patients/1/perio/new"))


class NoteTests(Base):
    def draft(self, c, **extra):
        d = {"template": "exam", "f0": "Pain upper right", "f1": "Caries 16", "tooth": "16"}
        d.update(extra)
        return self.post(c, "/patients/1/notes/new", d)

    def test_draft_sign_lock_addendum(self):
        a = self.login("asst")
        self.assertEqual(self.draft(a).status_code, 302)
        self.assertIn("Pain upper right", self.page(a, "/patients/1/notes"))
        # an assistant can write a draft but not sign it
        self.assertEqual(self.post(a, "/notes/1/sign").status_code, 403)
        d = self.login("dent")
        self.post(d, "/notes/1/sign")
        row = self.db().execute("SELECT status, signed_by FROM clinical_notes WHERE id = 1").fetchone()
        self.assertEqual(row["status"], "signed")
        # a signed note cannot be edited or discarded
        self.post(d, "/notes/1/edit", {"template": "exam", "f0": "Changed"})
        self.post(d, "/notes/1/discard")
        body = self.db().execute("SELECT body, status FROM clinical_notes WHERE id = 1").fetchone()
        self.assertIn("Pain upper right", body["body"])
        self.assertEqual(body["status"], "signed")
        # corrections are addenda
        self.post(d, "/notes/1/addendum", {"text": "Correction: tooth 17"})
        page = self.page(d, "/patients/1/notes")
        self.assertIn("Correction: tooth 17", page)
        self.assertIn("Pain upper right", page)
        # an assistant cannot add an addendum
        self.post(a, "/notes/1/addendum", {"text": "Sneaky"})
        self.assertNotIn("Sneaky", self.page(d, "/patients/1/notes"))

    def test_draft_edit_and_discard(self):
        d = self.login("dent")
        self.draft(d)
        self.post(d, "/notes/1/edit", {"template": "exam", "f0": "Updated complaint", "tooth": "16"})
        self.assertIn("Updated complaint", self.page(d, "/patients/1/notes"))
        self.post(d, "/notes/1/discard")
        self.assertNotIn("Updated complaint", self.page(d, "/patients/1/notes"))
        self.assertEqual(self.db().execute("SELECT status FROM clinical_notes WHERE id = 1").fetchone()[0], "discarded")

    def test_empty_and_bad_notes_refused(self):
        d = self.login("dent")
        self.post(d, "/patients/1/notes/new", {"template": "exam"})
        self.draft(d, tooth="99")
        self.assertEqual(self.db().execute("SELECT COUNT(*) FROM clinical_notes").fetchone()[0], 0)

    def test_all_templates_open(self):
        d = self.login("dent")
        for key in ("exam", "hygiene", "restoration", "extraction", "rct", "crown", "emergency", "free"):
            self.page(d, f"/patients/1/notes/new?template={key}")
        self.page(d, "/patients/1/notes/new?template=unknown")

    def test_front_desk_and_other_clinic_blocked(self):
        d = self.login("dent")
        self.draft(d)
        self.assertEqual(self.login("desk").get("/notes/1/edit").status_code, 403)
        self.assertEqual(self.login("mgr2").get("/notes/1/edit").status_code, 404)
        self.assertEqual(self.post(self.login("mgr2"), "/notes/1/sign").status_code, 404)


class ConsentTests(Base):
    def setUp(self):
        super().setUp()
        self.m = self.login("mgr")
        self.post(self.m, "/consent-forms", {"action": "starter"})

    def sign(self, c, pid=1, **extra):
        d = {"template_id": "1", "signer_name": "Ama Mensah", "relation": "self", "method": "typed", "agreed": "1"}
        d.update(extra)
        return self.post(c, f"/patients/{pid}/consents", d)

    def test_starter_forms_exist_and_record(self):
        self.assertGreaterEqual(self.db().execute("SELECT COUNT(*) FROM consent_templates WHERE clinic_id = 1").fetchone()[0], 7)
        self.assertEqual(self.sign(self.m).status_code, 302)
        self.assertEqual(self.db().execute("SELECT COUNT(*) FROM consents").fetchone()[0], 1)
        self.assertIn("Ama Mensah", self.page(self.m, "/patients/1/consents"))
        self.page(self.m, "/consents/1")
        self.page(self.m, "/patients/1/consents?template=1")

    def test_must_tick_agree_and_name(self):
        self.sign(self.m, agreed="")
        self.sign(self.m, signer_name="")
        self.assertEqual(self.db().execute("SELECT COUNT(*) FROM consents").fetchone()[0], 0)

    def test_minor_cannot_sign_for_themselves(self):
        self.sign(self.m, pid=2, signer_name="Kofi Young")
        self.assertEqual(self.db().execute("SELECT COUNT(*) FROM consents").fetchone()[0], 0)
        self.sign(self.m, pid=2, signer_name="Esi Young", relation="parent")
        self.assertEqual(self.db().execute("SELECT COUNT(*) FROM consents").fetchone()[0], 1)

    def test_wording_is_frozen_at_signing(self):
        self.sign(self.m)
        self.post(self.m, "/consent-forms", {"id": "1", "name": "Changed name", "body": "Totally different wording", "active": "1"})
        row = self.db().execute("SELECT title, body FROM consents WHERE id = 1").fetchone()
        self.assertNotEqual(row["title"], "Changed name")
        self.assertNotIn("Totally different", row["body"])

    def test_withdraw_keeps_the_record(self):
        self.sign(self.m)
        self.post(self.m, "/consents/1/withdraw", {"reason": "Changed mind"})
        row = self.db().execute("SELECT status, withdrawn_reason FROM consents WHERE id = 1").fetchone()
        self.assertEqual((row["status"], row["withdrawn_reason"]), ("withdrawn", "Changed mind"))

    def test_only_manager_edits_forms(self):
        self.assertEqual(self.login("dent").get("/consent-forms").status_code, 403)

    def test_other_clinic_cannot_see_or_withdraw(self):
        self.sign(self.m)
        o = self.login("mgr2")
        self.assertEqual(o.get("/consents/1").status_code, 404)
        self.assertEqual(self.post(o, "/consents/1/withdraw", {"reason": "x"}).status_code, 404)


class FileTests(Base):
    def upload(self, c, data=PNG, name="xray.png", pid=1, **extra):
        form = {"kind": "xray", "taken_on": "2026-10-01", "note": "bitewing", "file": (io.BytesIO(data), name)}
        form.update(extra)
        return self.post(c, f"/patients/{pid}/files", form, content_type="multipart/form-data")

    def test_upload_list_and_serve(self):
        c = self.login("dent")
        self.assertEqual(self.upload(c).status_code, 302)
        page = self.page(c, "/patients/1/files")
        self.assertIn("xray.png", page)
        r = c.get("/files/1")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.mimetype, "image/png")
        self.assertIn("sandbox", r.headers["Content-Security-Policy"])
        # stored under a random name outside the static folder, not the user's file name
        row = self.db().execute("SELECT stored_name FROM patient_files").fetchone()
        self.assertNotIn("xray", row[0])
        self.assertTrue(any(Path(self.tmp.name, "files").rglob(row[0])))

    def test_wrong_types_refused(self):
        c = self.login("dent")
        self.upload(c, data=b"<html><script>alert(1)</script></html>", name="evil.png")
        self.upload(c, data=b"MZ\x90\x00" + b"\x00" * 100, name="virus.pdf")
        self.upload(c, data=b"GIF89a....", name="x.gif")
        self.assertEqual(self.db().execute("SELECT COUNT(*) FROM patient_files").fetchone()[0], 0)

    def test_pdf_downloads_rather_than_opens(self):
        c = self.login("dent")
        self.upload(c, data=b"%PDF-1.4\n%%EOF", name="letter.pdf", kind="referral")
        r = c.get("/files/1")
        self.assertIn("attachment", r.headers.get("Content-Disposition", ""))

    def test_too_big_refused(self):
        c = self.login("dent")
        self.upload(c, data=PNG + b"\x00" * (10 * 1024 * 1024))
        self.assertEqual(self.db().execute("SELECT COUNT(*) FROM patient_files").fetchone()[0], 0)

    def test_access_rules(self):
        c = self.login("dent")
        self.upload(c)
        self.assertEqual(self.login("desk").get("/files/1").status_code, 403)
        self.assertEqual(self.login("mgr2").get("/files/1").status_code, 404)
        self.assertEqual(self.app.test_client().get("/files/1").status_code, 302)       # sent to the login page
        self.assertEqual(self.upload(self.login("mgr2"), pid=1).status_code, 404)

    def test_archive_hides_but_keeps(self):
        c = self.login("dent")
        self.upload(c)
        self.post(c, "/files/1/archive")
        self.assertNotIn("xray.png", self.page(c, "/patients/1/files"))
        self.assertEqual(self.db().execute("SELECT archived FROM patient_files WHERE id = 1").fetchone()[0], 1)


class DataTests(Base):
    def test_export_has_only_this_clinic(self):
        c = self.login("mgr")
        r = self.post(c, "/tools/export")
        self.assertEqual(r.mimetype, "application/zip")
        z = zipfile.ZipFile(io.BytesIO(r.data))
        names = z.namelist()
        self.assertIn("patients.csv", names)
        self.assertIn("README.txt", names)
        pats = z.read("patients.csv").decode()
        self.assertIn("Ama", pats)
        self.assertNotIn("Zed", pats)

    def test_export_guards_formulas(self):
        conn = self.db()
        conn.execute("INSERT INTO patients(clinic_id, first_name, last_name, phone) VALUES (1,'=HYPERLINK(\"x\")','Evil','+233201112225')")
        conn.commit()
        z = zipfile.ZipFile(io.BytesIO(self.post(self.login("mgr"), "/tools/export").data))
        text = z.read("patients.csv").decode()
        self.assertNotIn(",=HYPERLINK", text)
        self.assertIn("'=HYPERLINK", text)
        self.assertIn("+233201112223", text)              # phone numbers are left alone

    def test_only_managers_use_tools(self):
        for who in ("dent", "asst", "desk"):
            c = self.login(who)
            self.assertEqual(c.get("/tools").status_code, 403, who)
            self.assertEqual(self.post(c, "/tools/export").status_code, 403, who)

    def csv_upload(self, c, text):
        return self.post(c, "/tools/import", {"file": (io.BytesIO(text.encode()), "p.csv")}, content_type="multipart/form-data")

    def test_import_preview_then_confirm(self):
        c = self.login("mgr")
        text = ("First name,Surname,Mobile,Date of birth,Email\n"
                "Efua,Boateng,0244 123 456,23/04/2001,e@example.com\n"
                "Yaw,Addo,0244 123 457,2/3/1980,\n"
                "Bad,Phone,12,,\n"
                "Ama,Mensah,0201112223,,\n")
        r = self.csv_upload(c, text)
        self.assertEqual(r.status_code, 302)
        preview = self.page(c, r.headers["Location"])
        self.assertIn("Efua Boateng", preview)
        self.assertIn("Row 4", preview)                         # the bad phone
        self.assertIn("Ama Mensah", preview)                    # skipped duplicate
        self.assertEqual(self.db().execute("SELECT COUNT(*) FROM patients WHERE clinic_id = 1").fetchone()[0], 2)   # nothing yet
        self.post(c, r.headers["Location"] + "/confirm")
        rows = self.db().execute("SELECT first_name, dob, reminders_ok, offers_ok, clinic_id FROM patients ORDER BY id").fetchall()
        got = {x["first_name"]: x for x in rows}
        self.assertEqual(got["Efua"]["dob"], "2001-04-23")
        self.assertEqual(got["Yaw"]["dob"], "1980-03-02")          # Ghana reads day first
        self.assertEqual((got["Efua"]["reminders_ok"], got["Efua"]["offers_ok"]), (0, 0))
        self.assertEqual(got["Efua"]["clinic_id"], 1)
        self.assertNotIn("Bad", got)
        # confirming twice adds nothing more
        self.post(c, "/tools/import/1/confirm")
        self.assertEqual(self.db().execute("SELECT COUNT(*) FROM patients WHERE clinic_id = 1").fetchone()[0], 4)

    def test_us_clinic_reads_month_first(self):
        c = self.login("mgr2")
        r = self.csv_upload(c, "first,last,phone,dob\nJo,Smith,202 555 0111,04/23/2001\n")
        self.post(c, r.headers["Location"] + "/confirm")
        self.assertEqual(self.db().execute("SELECT dob FROM patients WHERE first_name = 'Jo'").fetchone()[0], "2001-04-23")

    def test_import_cancel_and_isolation(self):
        c = self.login("mgr")
        r = self.csv_upload(c, "first,last,phone\nEfua,Boateng,0244123456\n")
        url = r.headers["Location"]
        self.assertEqual(self.login("mgr2").get(url).status_code, 404)
        self.assertEqual(self.post(self.login("mgr2"), url + "/confirm").status_code, 302)
        self.assertEqual(self.db().execute("SELECT COUNT(*) FROM patients WHERE first_name='Efua'").fetchone()[0], 0)
        self.post(c, url + "/cancel")
        self.post(c, url + "/confirm")
        self.assertEqual(self.db().execute("SELECT COUNT(*) FROM patients WHERE first_name='Efua'").fetchone()[0], 0)

    def test_bad_files_refused(self):
        c = self.login("mgr")
        self.csv_upload(c, "")
        self.csv_upload(c, "name,age\nx,1\n")
        self.csv_upload(c, "first,last,phone\n" + "A,B,0244123456\n" * 1 + "x" * (1024 * 1024 + 10))
        self.assertEqual(self.db().execute("SELECT COUNT(*) FROM import_batches").fetchone()[0], 0)


class PasswordTests(Base):
    def test_manager_reset_forces_change(self):
        m = self.login("mgr")
        r = self.post(m, "/staff/4/reset-password")
        self.assertEqual(r.status_code, 302)
        page = self.page(m, "/staff")
        temp = re.search(r"temporary password for [^:]*: (\S+)", page)
        if not temp:
            temp = re.search(r"(?:password|Password)[^<]*?[:\s]([A-Za-z0-9_\-]{10,})", page)
        self.assertIsNotNone(temp, page[:1500])
        pw = temp.group(1).rstrip(".")
        d = self.app.test_client()
        t = re.search(r'name="_csrf" value="([^"]+)"', d.get("/login").get_data(as_text=True)).group(1)
        r = d.post("/login", data={"_csrf": t, "username": "desk", "password": pw})
        self.assertEqual(r.status_code, 302)
        self.assertIn("/account/password", r.headers["Location"])
        # everything else redirects to the change page until they choose their own password
        self.assertIn("/account/password", d.get("/patients").headers["Location"])
        t = re.search(r'name="_csrf" value="([^"]+)"', d.get("/account/password").get_data(as_text=True)).group(1)
        d.post("/account/password", data={"_csrf": t, "current": pw, "new": "short", "again": "short"})
        self.assertEqual(d.get("/patients").status_code, 302)
        d.post("/account/password", data={"_csrf": t, "current": pw, "new": "a-brand-new-passphrase", "again": "a-brand-new-passphrase"})
        self.assertEqual(d.get("/patients").status_code, 200)
        e = self.app.test_client()
        t = re.search(r'name="_csrf" value="([^"]+)"', e.get("/login").get_data(as_text=True)).group(1)
        e.post("/login", data={"_csrf": t, "username": "desk", "password": pw})
        self.assertEqual(e.get("/patients").status_code, 302)       # the temporary password no longer works

    def test_only_manager_and_same_clinic(self):
        self.assertEqual(self.post(self.login("dent"), "/staff/4/reset-password").status_code, 403)
        self.assertEqual(self.post(self.login("mgr2"), "/staff/4/reset-password").status_code, 404)

    def test_wrong_current_password_refused(self):
        c = self.login("dent")
        self.post(c, "/account/password", {"current": "nope", "new": "a-brand-new-passphrase", "again": "a-brand-new-passphrase"})
        c2 = self.login("dent")
        self.assertEqual(c2.get("/patients").status_code, 200)


class BackupTests(Base):
    def test_backup_command_writes_a_readable_copy(self):
        out = Path(self.tmp.name) / "bk"
        res = self.app.test_cli_runner().invoke(args=["backup", "--to", str(out), "--keep", "2"])
        self.assertEqual(res.exit_code, 0, res.output)
        files = list(out.glob("*.sqlite"))
        self.assertEqual(len(files), 1)
        conn = dbmod.connect(str(files[0]))
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM patients").fetchone()[0], 3)


class UpgradeTests(unittest.TestCase):
    def test_old_database_is_upgraded_in_place(self):
        import sqlite3
        tmp = tempfile.TemporaryDirectory()
        path = str(Path(tmp.name) / "old.sqlite")
        conn = sqlite3.connect(path)
        conn.executescript("""CREATE TABLE clinics(id INTEGER PRIMARY KEY, name TEXT);
            CREATE TABLE users(id INTEGER PRIMARY KEY, clinic_id INTEGER, username TEXT, display_name TEXT, role TEXT, password_hash TEXT);
            INSERT INTO clinics VALUES (1,'Old');""")
        conn.commit()
        conn.close()
        dbmod.ensure_column  # exists
        c = dbmod.connect(path)
        dbmod.ensure_column(c, "users", "must_change_password", "INTEGER NOT NULL DEFAULT 0")
        dbmod.ensure_column(c, "users", "must_change_password", "INTEGER NOT NULL DEFAULT 0")     # twice is harmless
        cols = [r[1] for r in c.execute("PRAGMA table_info(users)")]
        self.assertIn("must_change_password", cols)


class PageSmokeTests(Base):
    """Every new screen renders for the roles that may open it."""
    def test_all_screens_open(self):
        m = self.login("mgr")
        self.post(m, "/consent-forms", {"action": "starter"})
        self.post(m, "/patients/1/notes/new", {"template": "exam", "f0": "x"})
        self.post(m, "/patients/1/perio/new", {"pk_16_B": "4"})
        for url in ("/patients/1", "/patients/1/treatment", "/patients/1/account", "/patients/1/medical", "/patients/1/chart",
                    "/patients/1/perio", "/perio/1", "/patients/1/perio/new", "/patients/1/notes", "/notes/1/edit",
                    "/patients/1/consents", "/patients/1/consents?template=1", "/consent-forms", "/patients/1/files",
                    "/tools", "/tools/import", "/staff", "/account/password", "/", "/diary"):
            self.page(m, url)


if __name__ == "__main__":
    unittest.main()
