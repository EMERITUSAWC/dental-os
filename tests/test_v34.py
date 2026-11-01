"""v0.34: the form builder. Clinics make their own forms; patients fill them in by a one-time private link."""
import datetime
import re

from werkzeug.datastructures import MultiDict

from tests.test_v04 import Base
from dental_os.services import formbuilder as FB


def builder_form(**extra):
    d = {"title": "New patient questions", "intro": "Takes two minutes.",
         "q0_label": "About you", "q0_kind": "heading",
         "q1_label": "How did you hear about us?", "q1_kind": "choice", "q1_options": "Friend\nRadio\nInstagram", "q1_required": "1",
         "q2_label": "Do you smoke?", "q2_kind": "yesno", "q2_required": "1",
         "q3_label": "What worries you about your teeth?", "q3_kind": "long",
         "q4_label": "Which days suit you?", "q4_kind": "multi", "q4_options": "Mon\nWed\nSat",
         "q5_label": "Signature", "q5_kind": "signature", "q5_required": "1",
         "q6_label": "", "q6_kind": "text"}
    d.update(extra)
    return d


class Forms(Base):
    def setUp(self):
        super().setUp()
        self.conn = self.db()

    def tearDown(self):
        self.conn.close()

    def c(self):
        return dict(self.conn.execute("SELECT * FROM clinics WHERE id = 1").fetchone())

    def make(self):
        fid = FB.save(self.conn, self.c(), 1, "New patient questions", None, FB.parse_fields(MultiDict(builder_form())))
        self.conn.commit()
        return fid


class ServiceTests(Forms):
    def test_parse_drops_empty_rows_and_checks_choices(self):
        fields = FB.parse_fields(MultiDict(builder_form()))
        self.assertEqual([f["kind"] for f in fields], ["heading", "choice", "yesno", "long", "multi", "signature"])
        self.assertFalse(fields[0]["required"])
        self.assertEqual(fields[1]["options"], ["Friend", "Radio", "Instagram"])
        with self.assertRaises(FB.FormError):
            FB.parse_fields(MultiDict(builder_form(q1_options="Friend")))
        with self.assertRaises(FB.FormError):
            FB.parse_fields(MultiDict({"q0_label": "Only a heading", "q0_kind": "heading"}))
        with self.assertRaises(FB.FormError):
            FB.parse_fields(MultiDict(builder_form(q2_kind="bogus")))

    def test_editing_questions_makes_a_new_version_but_old_links_keep_theirs(self):
        fid = self.make()
        token, rid = FB.send(self.conn, self.c(), 1, 1, fid, 14)
        FB.save(self.conn, self.c(), 1, "Renamed only", None, FB.fields_of(FB.get(self.conn, 1, fid)), fid)
        self.assertEqual(FB.get(self.conn, 1, fid)["version"], 1)
        FB.save(self.conn, self.c(), 1, "Renamed only", None, FB.parse_fields(MultiDict({"q0_label": "Just one", "q0_kind": "text"})), fid)
        self.assertEqual(FB.get(self.conn, 1, fid)["version"], 2)
        r = FB.resolve(self.conn, token)
        self.assertEqual(r["version"], 1)
        self.assertIn("Do you smoke?", r["fields"])

    def test_answers_are_checked_then_the_link_closes(self):
        fid = self.make()
        token, rid = FB.send(self.conn, self.c(), 1, 1, fid, 14)
        row = self.conn.execute("SELECT token_hash FROM form_responses WHERE id = ?", (rid,)).fetchone()
        self.assertNotIn(token, row[0])
        with self.assertRaises(FB.FormError) as e:
            FB.submit(self.conn, token, MultiDict({"f2": "Bus", "f3": "maybe"}))
        self.assertEqual(set(e.exception.labels), {"How did you hear about us?", "Do you smoke?", "Signature"})
        FB.submit(self.conn, token, MultiDict([("f2", "Radio"), ("f3", "no"), ("f5", "Mon"), ("f5", "Sat"), ("f5", "Sun"), ("f6", "Ama Mensah")]))
        r = FB.response(self.conn, 1, rid)
        self.assertEqual(r["status"], "submitted")
        shown = {x["f"]["label"]: x["value"] for x in FB.shown(r)}
        self.assertEqual(shown["Which days suit you?"], "Mon, Sat")
        with self.assertRaises(FB.FormError):
            FB.submit(self.conn, token, MultiDict({"f2": "Radio", "f3": "no", "f6": "Ama Mensah"}))
        self.assertEqual(len(FB.to_review(self.conn, 1)), 1)
        FB.file_response(self.conn, self.c(), 1, rid, "Looked at by the nurse")
        self.assertEqual(FB.response(self.conn, 1, rid)["status"], "filed")
        self.assertEqual(FB.to_review(self.conn, 1), [])

    def test_links_expire_and_can_be_cancelled_and_stay_in_their_clinic(self):
        fid = self.make()
        token, rid = FB.send(self.conn, self.c(), 1, 1, fid, 1)
        past = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")
        self.conn.execute("UPDATE form_responses SET expires_at = ? WHERE id = ?", (past, rid))
        self.assertEqual(FB.resolve(self.conn, token)["status"], "expired")
        token2, rid2 = FB.send(self.conn, self.c(), 1, 1, fid, 14)
        FB.cancel(self.conn, self.c(), 1, rid2)
        with self.assertRaises(FB.FormError):
            FB.submit(self.conn, token2, MultiDict({}))
        with self.assertRaises(FB.FormError):
            FB.send(self.conn, self.c(), 1, 3, fid, 14)                  # clinic two's patient
        with self.assertRaises(FB.FormError):
            FB.send(self.conn, self.c(), 1, 1, fid, 90)
        self.assertIsNone(FB.response(self.conn, 2, rid))
        self.assertIsNone(FB.resolve(self.conn, "frm_nothing"))


class ScreenTests(Forms):
    def test_manager_builds_desk_sends_patient_fills_staff_files(self):
        mgr = self.login("mgr")
        r = self.post(mgr, "/forms/builder/new", builder_form(), follow_redirects=True)
        page = r.get_data(as_text=True)
        self.assertIn("Form saved.", page)
        fid = self.conn.execute("SELECT id FROM custom_forms").fetchone()[0]
        self.assertIn("Do you smoke?", self.page(mgr, f"/forms/builder/{fid}/preview"))
        self.assertIn("Patient forms", self.page(mgr, "/forms/builder"))
        self.assertEqual(self.login("desk").get("/forms/builder").status_code, 403)

        desk = self.login("desk")
        page = self.post(desk, "/patients/1/forms", {"form_id": str(fid), "days": "7"}).get_data(as_text=True)
        token = re.search(r"(frm_[A-Za-z0-9_\-]+)", page).group(1)
        self.assertIn("wa.me", page)
        self.assertIn("<svg", page)

        visitor = self.app.test_client()
        page = visitor.get(f"/f/{token}").get_data(as_text=True)
        self.assertIn("Clinic One", page)
        self.assertIn("How did you hear about us?", page)
        tok = re.search(r'name="_csrf" value="([^"]+)"', page).group(1)
        page = visitor.post(f"/f/{token}", data={"_csrf": tok, "f2": "Radio"}).get_data(as_text=True)
        self.assertIn("Please check the questions marked below.", page)
        self.assertIn('value="Radio" checked', page)
        page = visitor.post(f"/f/{token}", data={"_csrf": tok, "f2": "Radio", "f3": "yes", "f6": "Ama Mensah"}).get_data(as_text=True)
        self.assertIn("The clinic has your answers", page)
        self.assertIn("can no longer be used", visitor.get(f"/f/{token}").get_data(as_text=True))
        self.assertEqual(visitor.get("/f/frm_wrong").status_code, 404)
        self.assertEqual(visitor.post(f"/f/{token}", data={"f2": "Radio"}).status_code, 400)        # no CSRF token

        rid = self.conn.execute("SELECT id FROM form_responses").fetchone()[0]
        self.assertIn("New patient questions", self.page(desk, "/forms/review"))
        page = self.page(desk, f"/forms/responses/{rid}")
        self.assertIn("Ama Mensah", page)
        self.assertIn("Yes", page)
        page = self.post(desk, f"/forms/responses/{rid}", {"note": "Fine"}, follow_redirects=True).get_data(as_text=True)
        self.assertIn("Form filed on the record.", page)
        self.assertIn("Reviewed and filed", self.page(desk, "/patients/1/forms"))
        self.assertEqual(self.login("mgr2").get(f"/forms/responses/{rid}").status_code, 404)
        self.assertEqual(self.login("mgr2").get("/patients/1/forms").status_code, 404)

    def test_french_patient_sees_the_page_in_french(self):
        self.conn.execute("UPDATE patients SET language = 'fr' WHERE id = 1")
        fid = self.make()
        token, _ = FB.send(self.conn, self.c(), 1, 1, fid, 14)
        self.conn.commit()
        page = self.app.test_client().get(f"/f/{token}").get_data(as_text=True)
        self.assertIn("Envoyer au cabinet", page)
