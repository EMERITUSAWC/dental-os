import re
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from werkzeug.security import generate_password_hash

from dental_os import create_app, db as dbmod
from dental_os.services import messaging, patients as P, scheduling as S
from dental_os.services.phone import local_display, normalise_phone
from dental_os.services.timeutil import now_local, parse_dt


def next_weekday(weekday=1, hours=9):
    """A future Monday (default) at the given hour, as a naive datetime."""
    d = now_local().replace(hour=hours, minute=0) + timedelta(days=1)
    while d.isoweekday() != weekday:
        d += timedelta(days=1)
    return d


def make_conn():
    conn = dbmod.connect(":memory:")
    dbmod.init_schema(conn)
    for name in ("Clinic A", "Clinic B"):
        conn.execute("INSERT INTO clinics(name, whatsapp, country, currency, timezone, payment_methods) "
                         "VALUES (?, '+233504771418', 'GH', 'GHS', 'Africa/Accra', 'cash,mobile_money,card,bank_transfer,insurance')", (name,))
    for cid in (1, 2):
        conn.execute("INSERT INTO providers(clinic_id, name) VALUES (?, ?)", (cid, f"Dr {cid}"))
        conn.execute("INSERT INTO chairs(clinic_id, name) VALUES (?, 'Chair 1')", (cid,))
        conn.execute("INSERT INTO chairs(clinic_id, name) VALUES (?, 'Chair 2')", (cid,))
    conn.commit()
    return conn


def clean_gh(form, *args, **kwargs):
    """The tests' clinics are in Ghana, so local phone numbers are read as Ghana numbers."""
    return P.clean(form, *args, country="GH", **kwargs)


def patient(conn, cid, first="Ama", last="Mensah", phone="0201112223", reminders=1):
    data = clean_gh({"first_name": first, "last_name": last, "phone": phone, "reminders_ok": reminders})
    return P.create(conn, cid, None, data, allow_duplicates=True)


def clinic(conn, cid=1):
    return conn.execute("SELECT * FROM clinics WHERE id = ?", (cid,)).fetchone()


class PhoneTests(unittest.TestCase):
    def test_formats(self):
        for raw in ("050 477 1418", "0504771418", "+233504771418", "233504771418", "00233504771418", "504771418"):
            self.assertEqual(normalise_phone(raw, "GH"), "+233504771418")
        self.assertEqual(local_display("+233504771418", "GH"), "050 477 1418")

    def test_bad_numbers(self):
        for raw in ("", "abc", "12345", "+2335047714", "050-477-14180"):
            with self.assertRaises(ValueError):
                normalise_phone(raw, "GH")

    def test_other_country_kept(self):
        self.assertEqual(normalise_phone("+44 7700 900123", "GH"), "+447700900123")

    def test_no_country_means_no_guessing(self):
        with self.assertRaises(ValueError):
            normalise_phone("0504771418")
        self.assertEqual(normalise_phone("+233 50 477 1418"), "+233504771418")

    def test_other_countries(self):
        cases = [("07700 900123", "GB", "+447700900123"), ("0803 123 4567", "NG", "+2348031234567"),
                 ("0712 345 678", "KE", "+254712345678"), ("(212) 555-0147", "US", "+12125550147"),
                 ("212-555-0147", "CA", "+12125550147"), ("06 12 34 56 78", "FR", "+33612345678"),
                 ("082 123 4567", "ZA", "+27821234567"), ("77 123 45 67", "SN", "+221771234567")]
        for raw, cc, want in cases:
            self.assertEqual(normalise_phone(raw, cc), want, msg=f"{cc} {raw}")

    def test_wrong_length_for_the_country_is_refused(self):
        for raw, cc in (("0803 123 456", "NG"), ("07700 9001", "GB"), ("555-0147", "US"), ("+2348031234", "GH")):
            with self.assertRaises(ValueError, msg=f"{cc} {raw}"):
                normalise_phone(raw, cc)

    def test_display_in_other_countries(self):
        self.assertEqual(local_display("+12125550147", "US"), "(212) 555-0147")
        self.assertEqual(local_display("+2348031234567", "NG"), "080 312 34567")
        self.assertEqual(local_display("+233504771418", "GB"), "+233504771418")   # not a UK number: left as stored
        self.assertEqual(local_display("+233504771418", None), "+233504771418")


class PatientTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_conn()

    def test_validation_collects_errors(self):
        with self.assertRaises(P.ValidationError) as ctx:
            clean_gh({"first_name": "", "last_name": "X", "phone": "bad", "dob": "2999-01-01", "email": "nope"})
        self.assertEqual(set(ctx.exception.errors), {"first_name", "phone", "dob", "email"})

    def test_duplicate_warning_then_override(self):
        patient(self.conn, 1)
        data = clean_gh({"first_name": "ama", "last_name": "MENSAH", "phone": "0209998887"})
        with self.assertRaises(P.DuplicateWarning):
            P.create(self.conn, 1, None, data)
        self.assertTrue(P.create(self.conn, 1, None, data, allow_duplicates=True))

    def test_same_phone_is_flagged(self):
        patient(self.conn, 1, "Kofi", "Boateng", "0201112223")
        data = clean_gh({"first_name": "Yaa", "last_name": "Other", "phone": "0201112223"})
        with self.assertRaises(P.DuplicateWarning):
            P.create(self.conn, 1, None, data)

    def test_clinics_do_not_see_each_other(self):
        pid = patient(self.conn, 1)
        self.assertIsNone(P.get(self.conn, 2, pid))
        rows, total = P.search(self.conn, 2, "Ama")
        self.assertEqual(total, 0)
        with self.assertRaises(LookupError):
            P.update(self.conn, 2, None, pid, clean_gh({"first_name": "A", "last_name": "B", "phone": "0201112223"}))

    def test_search_by_name_and_phone_and_wildcards(self):
        patient(self.conn, 1, "Ama", "Mensah", "0201112223")
        patient(self.conn, 1, "Kofi", "Asare", "0244000111")
        self.assertEqual(P.search(self.conn, 1, "mens")[1], 1)
        self.assertEqual(P.search(self.conn, 1, "0244000")[1], 1)
        self.assertEqual(P.search(self.conn, 1, "%")[1], 0)       # wildcard is treated as plain text

    def test_archive_hides_but_keeps(self):
        pid = patient(self.conn, 1)
        P.set_archived(self.conn, 1, None, pid, True)
        self.assertEqual(P.search(self.conn, 1, "")[1], 0)
        self.assertEqual(P.search(self.conn, 1, "", include_archived=True)[1], 1)

    def test_guarantor_must_be_same_clinic(self):
        other = patient(self.conn, 2)
        with self.assertRaises(P.ValidationError):
            clean_gh({"first_name": "A", "last_name": "B", "phone": "0201112223", "guarantor_id": str(other)},
                    self.conn, 1)

    def test_birthdays_including_leap_day(self):
        today = datetime(2027, 2, 25).date()
        self.conn.execute("INSERT INTO patients(clinic_id, first_name, last_name, dob, phone, offers_ok) "
                          "VALUES (1,'Leap','Baby','2000-02-29','+233201112223',1)")
        self.conn.execute("INSERT INTO patients(clinic_id, first_name, last_name, dob, phone, offers_ok) "
                          "VALUES (1,'No','Consent','2000-02-26','+233201112224',0)")
        found = P.birthdays(self.conn, 1, today, 7)
        self.assertEqual([r["first_name"] for _, _, r in found], ["Leap"])


class SchedulingTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_conn()
        self.c = clinic(self.conn)
        self.pid = patient(self.conn, 1)
        self.start = next_weekday(1, 9)

    def book(self, pid=None, prov=1, chair=1, start=None, duration=30):
        return S.book(self.conn, self.c, None, pid or self.pid, prov, chair, start or self.start, duration)

    def test_books_and_audits(self):
        aid = self.book()
        self.assertEqual(S.get_appointment(self.conn, 1, aid)["status"], "scheduled")
        n = self.conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'appointment.book'").fetchone()[0]
        self.assertEqual(n, 1)

    def test_same_dentist_clash(self):
        self.book()
        other = patient(self.conn, 1, "Kofi", "Asare", "0244000111")
        with self.assertRaisesRegex(S.SchedulingError, "dentist"):
            self.book(other, prov=1, chair=2, start=self.start + timedelta(minutes=15))

    def test_same_chair_clash(self):
        self.book()
        self.conn.execute("INSERT INTO providers(clinic_id, name) VALUES (1, 'Dr Two')")
        other = patient(self.conn, 1, "Kofi", "Asare", "0244000111")
        with self.assertRaisesRegex(S.SchedulingError, "chair"):
            self.book(other, prov=3, chair=1)

    def test_patient_cannot_be_in_two_places(self):
        self.book()
        self.conn.execute("INSERT INTO providers(clinic_id, name) VALUES (1, 'Dr Two')")
        with self.assertRaisesRegex(S.SchedulingError, "patient"):
            self.book(self.pid, prov=3, chair=2)

    def test_back_to_back_is_fine(self):
        self.book()
        other = patient(self.conn, 1, "Kofi", "Asare", "0244000111")
        self.book(other, start=self.start + timedelta(minutes=30))

    def test_outside_hours_and_closed_day(self):
        with self.assertRaisesRegex(S.SchedulingError, "opening hours"):
            self.book(start=next_weekday(1, 6))
        with self.assertRaisesRegex(S.SchedulingError, "closed"):
            self.book(start=next_weekday(7, 9))

    def test_past_time_refused(self):
        with self.assertRaisesRegex(S.SchedulingError, "passed"):
            self.book(start=datetime(2020, 1, 6, 9, 0))

    def test_cannot_use_another_clinics_dentist_chair_or_patient(self):
        with self.assertRaises(S.SchedulingError):
            S.book(self.conn, self.c, None, self.pid, 2, 1, self.start, 30)      # Clinic B dentist
        with self.assertRaises(S.SchedulingError):
            S.book(self.conn, self.c, None, self.pid, 1, 4, self.start, 30)      # Clinic B chair
        foreign = patient(self.conn, 2)
        with self.assertRaises(S.SchedulingError):
            S.book(self.conn, self.c, None, foreign, 1, 1, self.start, 30)

    def test_free_slots_skip_booked_and_use_other_chair(self):
        self.book()
        slots = S.free_slots(self.conn, self.c, 1, self.start.date(), 30, now=self.start - timedelta(days=2))
        times = [s["time"] for s in slots]
        self.assertNotIn("09:00", times)
        self.assertIn("09:30", times)
        self.assertIn("08:00", times)
        self.assertEqual(S.free_slots(self.conn, self.c, 2, self.start.date(), 30), [])   # not our dentist

    def test_free_slots_respect_duration_and_closing_time(self):
        slots = S.free_slots(self.conn, self.c, 1, self.start.date(), 60, now=self.start - timedelta(days=2))
        self.assertEqual(slots[-1]["time"], "16:00")

    def test_status_flow(self):
        aid = self.book()
        S.set_confirmation(self.conn, 1, None, aid, "confirmed")
        with self.assertRaises(S.SchedulingError):
            S.set_status(self.conn, 1, None, aid, "broken")          # reason needed
        S.set_status(self.conn, 1, None, aid, "broken", "missed")
        with self.assertRaises(S.SchedulingError):
            S.set_status(self.conn, 1, None, aid, "complete")        # closed appointments stay closed
        free = S.free_slots(self.conn, self.c, 1, self.start.date(), 30, now=self.start - timedelta(days=2))
        self.assertIn("09:00", [s["time"] for s in free])            # a broken appointment frees the slot

    def test_waiting_list_then_place(self):
        aid = S.add_to_waiting_list(self.conn, self.c, None, self.pid, "asap", provider_id=1)
        self.assertEqual(S.get_appointment(self.conn, 1, aid)["status"], "asap")
        S.place(self.conn, self.c, None, aid, 1, 1, self.start)
        self.assertEqual(S.get_appointment(self.conn, 1, aid)["status"], "scheduled")

    def test_reschedule_resets_confirmation_and_ignores_itself(self):
        aid = self.book()
        S.set_confirmation(self.conn, 1, None, aid, "confirmed")
        S.place(self.conn, self.c, None, aid, 1, 1, self.start + timedelta(minutes=15))   # overlaps own old slot
        a = S.get_appointment(self.conn, 1, aid)
        self.assertEqual(a["confirmation"], "none")

    def test_other_clinic_cannot_touch_appointment(self):
        aid = self.book()
        with self.assertRaises(LookupError):
            S.set_status(self.conn, 2, None, aid, "complete")
        with self.assertRaises(LookupError):
            S.set_confirmation(self.conn, 2, None, aid, "confirmed")


class MessagingTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_conn()
        self.c = clinic(self.conn)
        self.start = next_weekday(1, 9)

    def appt(self, pid):
        aid = S.book(self.conn, self.c, None, pid, 1, 1, self.start, 30)
        return S.get_appointment(self.conn, 1, aid)

    def test_link_and_log(self):
        pid = patient(self.conn, 1, "Ama", "Mensah")
        a = self.appt(pid)
        link = messaging.prepare(self.conn, self.c, None, P.get(self.conn, 1, pid), a, "confirmation")
        self.assertTrue(link.startswith("https://wa.me/233201112223?text=Hello%20Ama"))
        self.assertEqual(S.get_appointment(self.conn, 1, a["id"])["confirmation"], "sent")
        self.assertEqual(len(messaging.history(self.conn, 1, pid)), 1)

    def test_text_has_no_treatment_details(self):
        pid = patient(self.conn, 1)
        a = self.appt(pid)
        text = messaging.appointment_text(self.c, P.get(self.conn, 1, pid), a, "confirmation")
        self.assertNotIn(a["category"], text)
        self.assertIn("9 am", text)

    def test_no_consent_no_message(self):
        pid = patient(self.conn, 1, reminders=0)
        a = self.appt(pid)
        with self.assertRaises(PermissionError):
            messaging.prepare(self.conn, self.c, None, P.get(self.conn, 1, pid), a, "reminder")


class WebTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = create_app({"TESTING": True, "DATABASE": str(Path(self.tmp.name) / "t.sqlite"),
                               "SECRET_KEY": "test"})
        conn = dbmod.connect(self.app.config["DATABASE"])
        for name in ("Clinic A", "Clinic B"):
            conn.execute("INSERT INTO clinics(name, whatsapp, country, currency, timezone, payment_methods) "
                         "VALUES (?, '+233504771418', 'GH', 'GHS', 'Africa/Accra', 'cash,mobile_money,card,bank_transfer,insurance')", (name,))
        for cid in (1, 2):
            conn.execute("INSERT INTO providers(clinic_id, name) VALUES (?, ?)", (cid, f"Dr {cid}"))
            conn.execute("INSERT INTO chairs(clinic_id, name) VALUES (?, 'Chair 1')", (cid,))
        for uname, cid, role in (("manager", 1, "manager"), ("desk", 1, "front_desk"), ("other", 2, "manager")):
            conn.execute("INSERT INTO users(clinic_id, username, display_name, role, password_hash) VALUES (?,?,?,?,?)",
                         (cid, uname, uname.title(), role, generate_password_hash("correct-horse-battery")))
        conn.commit()
        conn.close()
        self.client = self.app.test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def token(self, path="/login"):
        html = self.client.get(path).get_data(as_text=True)
        return re.search(r'name="_csrf" value="([^"]+)"', html).group(1)

    def login(self, user="manager", password="correct-horse-battery"):
        return self.client.post("/login", data={"username": user, "password": password, "_csrf": self.token()})

    def post(self, path, page=None, **data):
        data["_csrf"] = self.token(page or "/")
        return self.client.post(path, data=data)

    def test_login_required(self):
        r = self.client.get("/patients")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/login", r.headers["Location"])

    def test_bad_password_and_lockout(self):
        for _ in range(5):
            r = self.login(password="wrong")
            self.assertIn("do not match", r.get_data(as_text=True))
        r = self.login()                                   # correct password, but locked out
        self.assertIn("Too many tries", r.get_data(as_text=True))

    def test_open_redirect_blocked(self):
        r = self.client.post("/login?next=//evil.example", data={"username": "manager", "password": "correct-horse-battery",
                                                                  "_csrf": self.token()})
        self.assertEqual(r.headers["Location"], "/")

    def test_csrf_enforced(self):
        self.login()
        self.assertEqual(self.client.post("/patients/new", data={"first_name": "A"}).status_code, 400)

    def test_create_patient_and_view(self):
        self.login()
        r = self.post("/patients/new", "/patients/new", first_name="Ama", last_name="Mensah", phone="0201112223",
                      reminders_ok="1")
        self.assertEqual(r.status_code, 302)
        page = self.client.get(r.headers["Location"]).get_data(as_text=True)
        self.assertIn("Ama Mensah", page)
        self.assertIn("020 111 2223", page)

    def test_validation_errors_shown(self):
        self.login()
        r = self.post("/patients/new", "/patients/new", first_name="", last_name="X", phone="1")
        html = r.get_data(as_text=True)
        self.assertEqual(r.status_code, 200)
        self.assertIn("First name is needed", html)

    def test_other_clinic_gets_404(self):
        self.login()
        self.post("/patients/new", "/patients/new", first_name="Ama", last_name="Mensah", phone="0201112223")
        self.client.post("/logout", data={"_csrf": self.token("/")})
        self.login("other")
        self.assertEqual(self.client.get("/patients/1").status_code, 404)
        self.assertEqual(self.client.get("/patients/1/edit").status_code, 404)
        self.assertNotIn("Ama", self.client.get("/patients").get_data(as_text=True))

    def test_staff_cannot_open_manager_pages(self):
        self.login("desk")
        for path in ("/staff", "/settings", "/audit"):
            self.assertEqual(self.client.get(path).status_code, 403)

    def test_full_booking_flow_and_whatsapp(self):
        self.login()
        self.post("/patients/new", "/patients/new", first_name="Ama", last_name="Mensah", phone="0201112223",
                  reminders_ok="1")
        day = next_weekday(1, 9)
        page = self.client.get(f"/appointments/new?patient_id=1&provider_id=1&date={day.date()}&duration=30&find=1")
        html = page.get_data(as_text=True)
        self.assertIn("Free times", html)
        slot = re.search(r'name="slot" value="([^"]+)"', html).group(1)
        r = self.post("/appointments", f"/appointments/new?patient_id=1", patient_id="1", provider_id="1",
                      date=str(day.date()), duration="30", category="Consultation", slot=slot)
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r.headers["Location"], "/appointments/1")
        r = self.post("/appointments/1/whatsapp", "/appointments/1", kind="confirmation")
        self.assertTrue(r.headers["Location"].startswith("https://wa.me/233201112223?text="))
        r = self.post("/appointments/1/confirmation", "/appointments/1", value="confirmed")
        self.assertIn("Confirmed", self.client.get("/appointments/1").get_data(as_text=True))
        self.assertIn("appointment.book", self.client.get("/audit").get_data(as_text=True))
        diary = self.client.get(f"/diary?date={day.date()}").get_data(as_text=True)
        self.assertIn("Ama Mensah", diary)

    def test_double_booking_refused_in_web(self):
        self.login()
        for first, phone in (("Ama", "0201112223"), ("Kofi", "0244000111")):
            self.post("/patients/new", "/patients/new", first_name=first, last_name="Test", phone=phone)
        day = next_weekday(1, 9)
        for pid in ("1", "2"):
            self.post("/appointments", "/", patient_id=pid, provider_id="1", date=str(day.date()), duration="30",
                      category="Consultation", slot="09:00|1")
        count = sqlite3.connect(self.app.config["DATABASE"]).execute("SELECT COUNT(*) FROM appointments").fetchone()[0]
        self.assertEqual(count, 1)

    def test_security_headers(self):
        r = self.client.get("/login")
        self.assertEqual(r.headers["X-Frame-Options"], "DENY")
        self.assertEqual(r.headers["Cache-Control"], "no-store")


if __name__ == "__main__":
    unittest.main()
