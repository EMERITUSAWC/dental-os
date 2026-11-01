"""v0.7: message wording, the send list, replies, campaigns, online booking, a patient's private link and health form."""
import json
import re
import unittest
from datetime import timedelta

from tests import dbmeta
from dental_os.services import (booking, campaigns as CAMP, inbox, links, medical, msgtemplates as MT, outbox, plans as PL,
                                scheduling)
from dental_os.services.timeutil import now_local
from tests.test_v05 import Money


def now():
    return now_local("Africa/Accra")


class Comms(Money):
    def appt(self, days=3, hour=10, pid=1, status="scheduled", booked_days_ago=10, confirmation="none", minute=0):
        start = (now() + timedelta(days=days)).replace(hour=hour, minute=minute, second=0, microsecond=0)
        conn = self.db()
        cur = conn.execute(
            """INSERT INTO appointments(clinic_id, patient_id, provider_id, chair_id, start_at, duration_min, status, confirmation, created_at)
               VALUES (1,?,1,1,?,30,?,?, datetime('now', ?))""",
            (pid, start.strftime("%Y-%m-%d %H:%M"), status, confirmation, f"-{booked_days_ago} days"))
        conn.commit()
        return cur.lastrowid

    def sql(self, q, *a):
        conn = self.db()
        conn.execute(q, a)
        conn.commit()

    def one(self, q, *a):
        return self.db().execute(q, a).fetchone()

    def build(self):
        conn = self.db()
        out = outbox.build(conn, self.clinic())
        outbox.refresh(conn, self.clinic())
        conn.commit()
        return out

    def queued(self, kind=None):
        rows = self.db().execute("SELECT * FROM outbox WHERE status = 'queued'" + (f" AND kind = '{kind}'" if kind else "")).fetchall()
        return rows

    def consent(self, pid=1, reminders=1, offers=0):
        self.sql("UPDATE patients SET reminders_ok = ?, offers_ok = ? WHERE id = ?", reminders, offers, pid)


class TemplateTests(Comms):
    def test_render_and_validation(self):
        self.assertEqual(MT.render("Hi {first_name}, {clinic}", {"first_name": "Ama", "clinic": "C"}), "Hi Ama, C")
        with self.assertRaises(MT.TemplateError):
            MT.render("Hi {first_name} {day}", {"first_name": "Ama"})
        with self.assertRaises(MT.TemplateError):
            MT.validate("Your balance is {balance}")
        with self.assertRaises(MT.TemplateError):
            MT.validate("Hello {first_name")
        with self.assertRaises(MT.TemplateError):
            MT.validate("Please review us", required=("review_link",))
        with self.assertRaises(MT.TemplateError):
            MT.validate("x" * 601)

    def test_manager_edits_wording_and_manual_reminder_uses_it(self):
        c = self.login("mgr")
        r = self.post(c, "/messages/setup", {"action": "template", "kind": "reminder",
                                             "body": "Hi {first_name}! See you on {day} at {time} - {clinic}"})
        self.assertEqual(r.status_code, 302)
        a = self.appt()
        conn = self.db()
        self.consent()
        from dental_os.services import messaging, patients as P
        appt = scheduling.get_appointment(conn, 1, a)
        text = messaging.appointment_text(self.clinic(), P.get(conn, 1, 1), appt, "reminder", conn)
        self.assertTrue(text.startswith("Hi Ama! See you on "))
        self.assertIn("Clinic One", text)

    def test_bad_wording_is_refused_and_kept_out(self):
        c = self.login("mgr")
        before = MT.body_for(self.db(), 1, "reminder")
        self.post(c, "/messages/setup", {"action": "template", "kind": "reminder", "body": "You owe {balance}"})
        self.assertEqual(MT.body_for(self.db(), 1, "reminder"), before)
        self.post(c, "/messages/setup", {"action": "template", "kind": "review_request", "body": "Please review us"})
        self.assertIn("{review_link}", MT.body_for(self.db(), 1, "review_request"))

    def test_reset_to_starter(self):
        c = self.login("mgr")
        self.post(c, "/messages/setup", {"action": "template", "kind": "recall", "body": "Come back {first_name}"})
        self.post(c, "/messages/setup", {"action": "template_reset", "kind": "recall"})
        self.assertEqual(MT.body_for(self.db(), 1, "recall"), MT.KINDS["recall"][1])

    def test_setup_is_manager_only(self):
        for u in ("dent", "desk", "asst"):
            c = self.login(u)
            self.assertEqual(c.get("/messages/setup").status_code, 403, u)
            self.assertEqual(self.post(c, "/messages/setup", {"action": "template", "kind": "recall", "body": "x"}).status_code, 403, u)


class QueueTests(Comms):
    def test_reminder_is_queued_once_when_due(self):
        self.consent()
        a = self.appt(days=2)
        self.assertEqual(self.build()["queued"], 1)
        self.assertEqual(self.build()["queued"], 0)                       # running again adds nothing
        rows = self.queued("reminder")
        self.assertEqual((len(rows), rows[0]["appointment_id"]), (1, a))

    def test_not_due_yet(self):
        self.consent()
        self.appt(days=6)
        self.build()
        self.assertEqual(self.queued(), [])

    def test_patient_who_has_not_agreed_is_never_queued(self):
        self.consent(reminders=0)
        self.appt(days=2)
        self.assertEqual(self.build(), {"queued": 0, "no_consent": 1})
        self.assertEqual(self.queued(), [])

    def test_booked_inside_the_window_is_not_reminded(self):
        self.consent()
        self.appt(days=2, booked_days_ago=0)
        self.build()
        self.assertEqual(self.queued(), [])

    def test_confirmed_visit_skipped_but_rule_can_include_it(self):
        self.consent()
        self.appt(days=2, confirmation="confirmed")
        self.build()
        self.assertEqual(self.queued(), [])
        c = self.login("mgr")
        self.post(c, "/messages/setup", {"action": "rule", "days_before": "2", "active": "1"})     # skip_confirmed unticked
        self.build()
        self.assertEqual(len(self.queued("reminder")), 1)

    def test_queue_drops_cancelled_visit_and_withdrawn_consent(self):
        self.consent()
        a = self.appt(days=2)
        self.build()
        self.sql("UPDATE appointments SET status = 'broken', break_reason = 'cancelled' WHERE id = ?", a)
        self.build()
        self.assertEqual(self.queued(), [])
        self.assertEqual(self.one("SELECT skip_reason FROM outbox")[0], "The visit is no longer booked")
        b = self.appt(days=2, hour=11)
        self.build()
        self.consent(reminders=0)
        self.build()
        self.assertEqual(self.queued(), [])

    def test_open_whatsapp_logs_message_and_marks_sent(self):
        self.consent()
        a = self.appt(days=2)
        self.build()
        c = self.login("desk")
        oid = self.queued()[0]["id"]
        page = self.page(c, "/messages")
        self.assertIn("Ama Mensah", page)
        self.assertIn("a reminder from Clinic One", page)
        r = self.post(c, f"/messages/queue/{oid}/open")
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r.headers["Location"].startswith("https://wa.me/233201112223?text="))
        o = self.one("SELECT * FROM outbox WHERE id = ?", oid)
        self.assertEqual((o["status"], o["done_by"] is not None), ("sent", True))
        m = self.one("SELECT * FROM messages WHERE id = ?", o["message_id"])
        self.assertEqual((m["kind"], m["status"], m["appointment_id"]), ("reminder", "opened", a))
        self.assertEqual(self.one("SELECT confirmation FROM appointments WHERE id = ?", a)[0], "sent")
        again = self.post(c, f"/messages/queue/{oid}/open")                  # a second press does nothing
        self.assertEqual(again.status_code, 302)
        self.assertEqual(self.one("SELECT COUNT(*) FROM messages")[0], 1)

    def test_open_refused_when_consent_withdrawn_between_build_and_send(self):
        self.consent()
        self.appt(days=2)
        self.build()
        self.consent(reminders=0)
        c = self.login("desk")
        oid = self.queued()[0]["id"]
        r = self.post(c, f"/messages/queue/{oid}/open", follow_redirects=True)
        self.assertIn("not agreed", r.get_data(as_text=True))
        self.assertEqual(self.one("SELECT COUNT(*) FROM messages")[0], 0)

    def test_skip_keeps_a_record(self):
        self.consent()
        self.appt(days=2)
        self.build()
        c = self.login("desk")
        oid = self.queued()[0]["id"]
        self.post(c, f"/messages/queue/{oid}/skip", {"reason": "Spoke to them"})
        o = self.one("SELECT status, skip_reason FROM outbox")
        self.assertEqual((o["status"], o["skip_reason"]), ("skipped", "Spoke to them"))
        self.build()
        self.assertEqual(self.queued(), [])                               # and it is not queued again

    def test_other_clinic_cannot_touch_the_queue(self):
        self.consent()
        self.appt(days=2)
        self.build()
        oid = self.queued()[0]["id"]
        other = self.login("mgr2")
        self.assertEqual(self.post(other, f"/messages/queue/{oid}/open").status_code, 404)
        self.assertEqual(self.post(other, f"/messages/queue/{oid}/skip").status_code, 404)

    def test_recall_auto_queue_is_opt_in(self):
        self.consent()
        conn = self.db()
        from dental_os.services import recalls
        recalls.set_due_after_visit(conn, 1, 1, now().date() - timedelta(days=200))
        conn.commit()
        self.build()
        self.assertEqual(self.queued("recall"), [])
        c = self.login("mgr")
        self.post(c, "/messages/setup", {"action": "auto", "auto_recall": "1"})
        self.build()
        self.assertEqual(len(self.queued("recall")), 1)
        oid = self.queued("recall")[0]["id"]
        self.post(self.login("desk"), f"/messages/queue/{oid}/open")
        self.assertEqual(self.one("SELECT status FROM recalls")[0], "contacted")

    def test_review_request_needs_link_consent_and_a_gap(self):
        self.consent()
        yesterday = self.appt(days=-1, status="complete")
        c = self.login("mgr")
        self.post(c, "/messages/setup", {"action": "auto", "auto_review": "1", "review_link": ""})      # refused: no link
        self.assertEqual(self.one("SELECT auto_review FROM clinic_comms")[0], 0)
        self.post(c, "/messages/setup", {"action": "auto", "auto_review": "1", "review_link": "http://insecure.example"})
        self.assertEqual(self.one("SELECT auto_review FROM clinic_comms")[0], 0)
        self.post(c, "/messages/setup", {"action": "auto", "auto_review": "1", "review_link": "https://g.page/r/abc"})
        self.build()
        q = self.queued("review_request")
        self.assertEqual(len(q), 1)
        self.post(self.login("desk"), f"/messages/queue/{q[0]['id']}/open")
        self.assertIn("https://g.page/r/abc", self.one("SELECT body FROM messages")[0])
        self.appt(days=-1, status="complete", hour=14)                    # a second visit soon after: no second request
        self.build()
        self.assertEqual(len(self.queued("review_request")), 0)

    def test_birthday_greeting_needs_offers_consent(self):
        conn = self.db()
        today = now().date()
        conn.execute("UPDATE patients SET dob = ? WHERE id = 1", (f"1990-{today.month:02d}-{today.day:02d}",))
        conn.commit()
        self.consent(reminders=1, offers=0)
        c = self.login("mgr")
        self.post(c, "/messages/setup", {"action": "auto", "auto_birthday": "1"})
        self.build()
        self.assertEqual(self.queued("birthday"), [])
        self.consent(reminders=1, offers=1)
        self.build()
        self.assertEqual(len(self.queued("birthday")), 1)
        self.build()
        self.assertEqual(len(self.queued("birthday")), 1)

    def _presented_plan(self):
        conn = self.db()
        pid = PL.create(conn, self.clinic(), 1, 1, "Option A")
        PL.add_item(conn, self.clinic(), 1, pid, self.cid("FILL"))
        PL.present(conn, self.clinic(), {"id": 1}, pid, 7)
        conn.execute("UPDATE treatment_plans SET follow_up_on = ? WHERE id = ?", ((now().date() - timedelta(days=1)).isoformat(), pid))
        conn.commit()
        return pid

    def test_quote_followup_moves_the_follow_up_date_and_stops_after_three(self):
        self.consent()
        pid = self._presented_plan()
        c = self.login("mgr")
        self.post(c, "/messages/setup", {"action": "auto", "auto_plan": "1"})
        d = self.login("desk")
        for n in range(1, 4):
            self.build()
            q = self.queued("plan_followup")
            self.assertEqual(len(q), 1, n)
            self.post(d, f"/messages/queue/{q[0]['id']}/open")
            self.sql("UPDATE treatment_plans SET follow_up_on = ? WHERE id = ?", (now().date() - timedelta(days=n + 1)).isoformat(), pid)
        self.build()
        self.assertEqual(self.queued("plan_followup"), [])
        self.assertEqual(self.one("SELECT COUNT(*) FROM plan_events WHERE kind = 'followed up'")[0], 3)

    def test_queue_screen_visible_to_all_roles_and_dashboard_badge(self):
        self.consent()
        self.appt(days=2)
        for u in ("desk", "dent", "asst", "mgr"):
            c = self.login(u)
            self.assertEqual(c.get("/messages").status_code, 200, u)
        self.assertIn("1 message to send", self.page(self.login("desk"), "/"))


class InboxTests(Comms):
    def test_logging_a_reply_waits_in_the_inbox_until_read(self):
        c = self.login("desk")
        self.post(c, "/patients/1/messages", {"action": "log_reply", "body": "Can I come later?"})
        self.assertIn("Can I come later?", self.page(c, "/messages/replies"))
        self.assertEqual(outbox.attention(self.db(), self.clinic(), self.user("desk"))["replies"], 1)
        self.page(c, "/patients/1/messages")                               # opening the thread reads it
        self.assertNotIn("Can I come later?", self.page(c, "/messages/replies"))

    def test_yes_can_confirm_next_visit(self):
        a = self.appt(days=2)
        self.assertTrue(inbox.looks_like_yes("  Yes!! "))
        self.assertFalse(inbox.looks_like_yes("yes but later"))
        c = self.login("desk")
        self.post(c, "/patients/1/messages", {"action": "log_reply", "body": "YES", "confirms": "1"})
        self.assertEqual(self.one("SELECT confirmation FROM appointments WHERE id = ?", a)[0], "confirmed")

    def test_confirm_without_a_visit_saves_nothing(self):
        c = self.login("desk")
        r = self.post(c, "/patients/1/messages", {"action": "log_reply", "body": "YES", "confirms": "1"}, follow_redirects=True)
        self.assertIn("no booked visit", r.get_data(as_text=True))
        self.assertEqual(self.one("SELECT COUNT(*) FROM messages")[0], 0)

    def test_reply_to_a_patient_needs_consent_or_a_recent_message_from_them(self):
        self.consent(reminders=0)
        c = self.login("desk")
        r = self.post(c, "/patients/1/messages", {"action": "send", "text": "Hello"}, follow_redirects=True)
        self.assertIn("not agreed", r.get_data(as_text=True))
        self.post(c, "/patients/1/messages", {"action": "log_reply", "body": "Hi, are you open?"})
        r = self.post(c, "/patients/1/messages", {"action": "send", "text": "Yes, until 5 pm"})
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r.headers["Location"].startswith("https://wa.me/"))
        self.assertEqual(self.one("SELECT COUNT(*) FROM messages WHERE direction = 'out'")[0], 1)

    def test_other_clinics_patient_is_not_found(self):
        c = self.login("desk")
        self.assertEqual(c.get("/patients/3/messages").status_code, 404)
        self.assertEqual(self.post(c, "/patients/3/messages", {"action": "log_reply", "body": "x"}).status_code, 404)


class CampaignTests(Comms):
    def setUp(self):
        super().setUp()
        # three more patients in clinic one, with their last visit long ago and no visit booked
        conn = self.db()
        for i, (reminders, offers) in enumerate(((1, 0), (0, 0), (1, 1)), start=4):
            conn.execute("INSERT INTO patients(clinic_id, first_name, last_name, phone, reminders_ok, offers_ok) VALUES (1,?,?,?,?,?)",
                         (f"P{i}", "Lapsed", f"+2332011122{i}0", reminders, offers))
            conn.execute("""INSERT INTO appointments(clinic_id, patient_id, provider_id, chair_id, start_at, status, created_at)
                            VALUES (1,?,1,1,?, 'complete', datetime('now', '-300 days'))""", (i, (now() - timedelta(days=300)).strftime("%Y-%m-%d 10:00")))
        conn.commit()

    def make(self, c, **kw):
        d = {"name": "Win-back", "kind": "lapsed", "param_days": "180", "purpose": "care", "min_gap_days": "14",
             "body": "Hello {first_name}, we miss you at {clinic}."}
        d.update(kw)
        r = self.post(c, "/messages/campaigns/new", d)
        self.assertEqual(r.status_code, 302, r.get_data(as_text=True)[:300])
        return int(r.headers["Location"].rsplit("/", 1)[1])

    def test_audience_respects_consent_and_purpose(self):
        c = self.login("mgr")
        cid = self.make(c)
        aud = CAMP.audience(self.db(), self.clinic(), CAMP.get(self.db(), 1, cid))
        self.assertEqual(sorted(p["first_name"] for p in aud["send"]), ["P4", "P6"])
        self.assertEqual(aud["no_consent"], 1)
        offer = self.make(c, name="Offer", purpose="offer", body="Hello {first_name}, 10% off cleaning at {clinic}.")
        aud = CAMP.audience(self.db(), self.clinic(), CAMP.get(self.db(), 1, offer))
        self.assertEqual([p["first_name"] for p in aud["send"]], ["P6"])

    def test_build_adds_to_the_send_list_once_and_close_drops_the_rest(self):
        c = self.login("mgr")
        cid = self.make(c)
        self.post(c, f"/messages/campaigns/{cid}", {"action": "build"})
        self.assertEqual(len(self.queued("campaign")), 2)
        self.post(c, f"/messages/campaigns/{cid}", {"action": "build"})
        self.assertEqual(len(self.queued("campaign")), 2)
        q = self.queued("campaign")[0]
        self.post(self.login("desk"), f"/messages/queue/{q['id']}/open")
        self.assertIn("we miss you at Clinic One", self.one("SELECT body FROM messages")[0])
        self.post(c, f"/messages/campaigns/{cid}", {"action": "close"})
        self.assertEqual(self.queued("campaign"), [])
        self.assertEqual(self.one("SELECT status FROM campaigns")[0], "closed")
        r = self.post(c, f"/messages/campaigns/{cid}", {"action": "build"}, follow_redirects=True)
        self.assertIn("closed", r.get_data(as_text=True))

    def test_recently_messaged_patients_are_left_out(self):
        c = self.login("mgr")
        self.sql("INSERT INTO messages(clinic_id, patient_id, kind, body, status) VALUES (1,4,'reminder','x','opened')")
        cid = self.make(c)
        aud = CAMP.audience(self.db(), self.clinic(), CAMP.get(self.db(), 1, cid))
        self.assertEqual([p["first_name"] for p in aud["send"]], ["P6"])
        self.assertEqual(aud["too_soon"], 1)

    def test_results_count_replies_and_bookings(self):
        c = self.login("mgr")
        cid = self.make(c)
        self.post(c, f"/messages/campaigns/{cid}", {"action": "build"})
        d = self.login("desk")
        for q in self.queued("campaign"):
            self.post(d, f"/messages/queue/{q['id']}/open")
        self.post(d, "/patients/4/messages", {"action": "log_reply", "body": "Yes please, Friday"})
        self.appt(days=4, pid=4, booked_days_ago=0)
        res = CAMP.results(self.db(), 1, cid)
        self.assertEqual((res["sent"], res["replied"], res["booked"]), (2, 1, 1))
        self.assertIn("wrote back", self.page(c, f"/messages/campaigns/{cid}"))

    def test_overdue_recall_and_unaccepted_plan_rules(self):
        conn = self.db()
        from dental_os.services import recalls
        recalls.set_due_after_visit(conn, 1, 4, now().date() - timedelta(days=400))
        conn.commit()
        c = self.login("mgr")
        cid = self.make(c, name="Recall", kind="overdue_recall", param_days="30")
        aud = CAMP.audience(self.db(), self.clinic(), CAMP.get(self.db(), 1, cid))
        self.assertEqual([p["first_name"] for p in aud["send"]], ["P4"])
        self._plan_for(4)
        cid2 = self.make(c, name="Quotes", kind="unaccepted_plans", param_days="3")
        aud = CAMP.audience(self.db(), self.clinic(), CAMP.get(self.db(), 1, cid2))
        self.assertEqual([p["first_name"] for p in aud["send"]], ["P4"])

    def _plan_for(self, pid):
        conn = self.db()
        plan = PL.create(conn, self.clinic(), 1, pid, "A")
        PL.add_item(conn, self.clinic(), 1, plan, self.cid("FILL"))
        PL.present(conn, self.clinic(), {"id": 1}, plan, 7)
        conn.execute("UPDATE treatment_plans SET presented_on = ? WHERE id = ?", ((now().date() - timedelta(days=10)).isoformat(), plan))
        conn.commit()

    def test_validation_and_roles(self):
        c = self.login("mgr")
        r = self.post(c, "/messages/campaigns/new", {"name": "x", "kind": "lapsed", "param_days": "0", "purpose": "care", "min_gap_days": "14", "body": "Hi"},
                      follow_redirects=True)
        self.assertIn("1 to 1500", r.get_data(as_text=True))
        r = self.post(c, "/messages/campaigns/new", {"name": "x", "kind": "lapsed", "param_days": "30", "purpose": "care", "min_gap_days": "14",
                                                     "body": "Hi {first_name}, you owe {balance}"}, follow_redirects=True)
        self.assertIn("not a field", r.get_data(as_text=True))
        self.assertEqual(self.one("SELECT COUNT(*) FROM campaigns")[0], 0)
        for u in ("desk", "dent"):
            self.assertEqual(self.login(u).get("/messages/campaigns").status_code, 403, u)

    def test_other_clinic_cannot_see_a_campaign(self):
        cid = self.make(self.login("mgr"))
        self.assertEqual(self.login("mgr2").get(f"/messages/campaigns/{cid}").status_code, 404)


class Public(Comms):
    def open_booking(self, **kw):
        c = self.login("mgr")
        d = {"action": "booking", "booking_on": "1", "days_ahead": "14", "notice_hours": "2", "minutes": "30"}
        d.update(kw)
        self.post(c, "/messages/setup", d)
        token = self.one("SELECT booking_token FROM clinic_comms WHERE clinic_id = 1")[0]
        return token

    def anon(self):
        return self.app.test_client()

    def anon_post(self, c, page_url, post_url, data):
        html = c.get(page_url).get_data(as_text=True)
        tok = re.search(r'name="_csrf" value="([^"]+)"', html).group(1)
        d = {"_csrf": tok}
        d.update(data)
        return c.post(post_url, data=d)

    def first_slot(self, token):
        html = self.anon().get(f"/book/{token}").get_data(as_text=True)
        return re.search(r'name="slot" value="([^"]+)"', html).group(1)

    def request_data(self, _slot, **kw):
        d = {"slot": _slot, "first_name": "Efua", "last_name": "Boateng", "phone": "0244000111", "reason": "Consultation",
             "agrees_messages": "1"}
        d.update(kw)
        return d


class BookingTests(Public):
    def test_page_is_closed_until_opened_and_for_wrong_tokens(self):
        self.assertEqual(self.anon().get("/book/nothing").status_code, 404)
        token = self.open_booking(booking_on="")
        self.assertEqual(self.anon().get(f"/book/{token}").status_code, 404)

    def test_open_page_shows_free_times_and_no_staff_menu(self):
        token = self.open_booking()
        html = self.anon().get(f"/book/{token}").get_data(as_text=True)
        self.assertIn("Clinic One", html)
        self.assertIn('name="slot"', html)
        self.assertNotIn("Settings", html)
        self.assertNotIn("Log out", html)
        r = self.anon().get(f"/book/{token}")
        self.assertEqual(r.headers["Referrer-Policy"], "no-referrer")

    def test_a_taken_time_is_not_offered(self):
        token = self.open_booking()
        slot = self.first_slot(token)
        start = slot
        conn = self.db()
        conn.execute("INSERT INTO appointments(clinic_id, patient_id, provider_id, chair_id, start_at, duration_min, status) VALUES (1,1,1,1,?,30,'scheduled')", (start,))
        conn.commit()
        self.assertNotIn(f'value="{slot}"', self.anon().get(f"/book/{token}").get_data(as_text=True))

    def test_request_is_stored_and_does_not_touch_the_diary_or_patients(self):
        token = self.open_booking()
        slot = self.first_slot(token)
        r = self.anon_post(self.anon(), f"/book/{token}", f"/book/{token}", self.request_data(slot))
        self.assertEqual(r.status_code, 200)
        self.assertIn("not a confirmed booking", r.get_data(as_text=True))
        row = self.one("SELECT * FROM booking_requests")
        self.assertEqual((row["status"], row["phone"], row["wanted_start"]), ("new", "+233244000111", slot))
        self.assertEqual(self.one("SELECT COUNT(*) FROM patients WHERE clinic_id = 1")[0], 2)
        self.assertEqual(self.one("SELECT COUNT(*) FROM appointments")[0], 0)

    def test_bad_input_is_refused(self):
        token = self.open_booking()
        slot = self.first_slot(token)
        c = self.anon()
        for bad in (dict(first_name=""), dict(phone="abc"), dict(reason="Hack"), dict(slot="2020-01-01 10:00"), dict(slot="nonsense"),
                    dict(provider_id="999")):
            r = self.anon_post(c, f"/book/{token}", f"/book/{token}", self.request_data(slot, **bad))
            self.assertEqual(r.status_code, 200)
            self.assertNotIn("not a confirmed booking", r.get_data(as_text=True), bad)
        self.assertEqual(self.one("SELECT COUNT(*) FROM booking_requests")[0], 0)

    def test_honeypot_drops_requests_silently(self):
        token = self.open_booking()
        slot = self.first_slot(token)
        r = self.anon_post(self.anon(), f"/book/{token}", f"/book/{token}", self.request_data(slot, website="http://spam.example"))
        self.assertIn("not a confirmed booking", r.get_data(as_text=True))
        self.assertEqual(self.one("SELECT COUNT(*) FROM booking_requests")[0], 0)

    def test_one_address_cannot_flood(self):
        token = self.open_booking()
        html = self.anon().get(f"/book/{token}").get_data(as_text=True)
        slots = re.findall(r'name="slot" value="([^"]+)"', html)
        c = self.anon()
        ok = 0
        for i in range(5):
            r = self.anon_post(c, f"/book/{token}", f"/book/{token}", self.request_data(slots[i], phone=f"02440001{i:02d}"))
            ok += "not a confirmed booking" in r.get_data(as_text=True)
        self.assertEqual(ok, booking.MAX_PER_ADDRESS_HOUR)

    def test_staff_accept_creates_patient_and_visit(self):
        token = self.open_booking()
        slot = self.first_slot(token)
        self.anon_post(self.anon(), f"/book/{token}", f"/book/{token}", self.request_data(slot))
        c = self.login("desk")
        self.assertIn("Efua Boateng", self.page(c, "/messages/requests"))
        self.assertIn("Efua Boateng", self.page(c, "/messages/requests/1"))
        self.assertIn("1 booking request", self.page(c, "/"))
        day, hm = slot.split(" ")
        r = self.post(c, "/messages/requests/1/accept", {"patient": "new", "date": day, "time": hm, "provider_id": "1"})
        self.assertEqual(r.status_code, 302, r.get_data(as_text=True)[:300])
        req = self.one("SELECT * FROM booking_requests")
        self.assertEqual(req["status"], "accepted")
        appt = self.one("SELECT * FROM appointments WHERE id = ?", req["appointment_id"])
        self.assertEqual((appt["start_at"], appt["status"], appt["category"]), (slot, "scheduled", "Consultation"))
        pat = self.one("SELECT * FROM patients WHERE id = ?", req["patient_id"])
        self.assertEqual((pat["first_name"], pat["reminders_ok"], pat["offers_ok"]), ("Efua", 1, 0))
        again = self.post(c, "/messages/requests/1/accept", {"patient": "new", "date": day, "time": hm}, follow_redirects=True)
        self.assertIn("already been dealt with", again.get_data(as_text=True))

    def test_accept_can_link_to_an_existing_patient(self):
        token = self.open_booking()
        slot = self.first_slot(token)
        self.anon_post(self.anon(), f"/book/{token}", f"/book/{token}", self.request_data(slot, first_name="Ama", last_name="Mensah", phone="+233201112223"))
        c = self.login("desk")
        self.assertIn("Ama Mensah", self.page(c, "/messages/requests/1"))
        day, hm = slot.split(" ")
        self.post(c, "/messages/requests/1/accept", {"patient": "1", "date": day, "time": hm, "provider_id": "1"})
        self.assertEqual(self.one("SELECT patient_id FROM booking_requests")[0], 1)
        self.assertEqual(self.one("SELECT COUNT(*) FROM patients WHERE clinic_id = 1")[0], 2)

    def test_accept_refuses_a_time_that_got_taken(self):
        token = self.open_booking()
        slot = self.first_slot(token)
        self.anon_post(self.anon(), f"/book/{token}", f"/book/{token}", self.request_data(slot))
        self.sql("INSERT INTO appointments(clinic_id, patient_id, provider_id, chair_id, start_at, duration_min, status) VALUES (1,1,1,1,?,30,'scheduled')", slot)
        day, hm = slot.split(" ")
        r = self.post(self.login("desk"), "/messages/requests/1/accept", {"patient": "new", "date": day, "time": hm, "provider_id": "1"}, follow_redirects=True)
        self.assertIn("not free any more", r.get_data(as_text=True))
        self.assertEqual(self.one("SELECT status FROM booking_requests")[0], "new")
        self.assertEqual(self.one("SELECT COUNT(*) FROM patients WHERE clinic_id = 1")[0], 2)

    def test_decline_needs_a_reason(self):
        token = self.open_booking()
        slot = self.first_slot(token)
        self.anon_post(self.anon(), f"/book/{token}", f"/book/{token}", self.request_data(slot))
        c = self.login("desk")
        self.post(c, "/messages/requests/1/decline", {"reason": ""})
        self.assertEqual(self.one("SELECT status FROM booking_requests")[0], "new")
        self.post(c, "/messages/requests/1/decline", {"reason": "No dentist that day"})
        self.assertEqual(self.one("SELECT status, decline_reason FROM booking_requests")[:], ("declined", "No dentist that day"))

    def test_new_address_stops_the_old_one(self):
        token = self.open_booking()
        new = self.open_booking(regenerate="1")
        self.assertNotEqual(token, new)
        self.assertEqual(self.anon().get(f"/book/{token}").status_code, 404)
        self.assertEqual(self.anon().get(f"/book/{new}").status_code, 200)

    def test_requests_of_another_clinic_are_hidden(self):
        token = self.open_booking()
        slot = self.first_slot(token)
        self.anon_post(self.anon(), f"/book/{token}", f"/book/{token}", self.request_data(slot))
        other = self.login("mgr2")
        self.assertEqual(other.get("/messages/requests/1").status_code, 404)
        self.assertEqual(self.post(other, "/messages/requests/1/decline", {"reason": "nope"}).status_code, 404)
        self.assertNotIn("Efua", self.page(other, "/messages/requests"))


class LinkTests(Public):
    def make_link(self, pid=1):
        c = self.login("desk")
        r = self.post(c, f"/patients/{pid}/messages", {"action": "link_create"})
        self.assertEqual(r.status_code, 200)
        return re.search(r"/p/([A-Za-z0-9_\-]+)", r.get_data(as_text=True)).group(1)

    def test_link_is_shown_once_and_only_a_hash_is_stored(self):
        token = self.make_link()
        row = self.one("SELECT * FROM patient_links")
        self.assertNotIn(token, row["token_hash"])
        self.assertEqual(len(row["token_hash"]), 64)
        self.assertNotIn(token, self.page(self.login("desk"), "/patients/1/messages"))

    def test_portal_shows_own_data_only_and_not_staff_menu(self):
        self.appt(days=2)
        self.appt(days=2, hour=15, pid=2)
        token = self.make_link()
        r = self.anon().get(f"/p/{token}")
        html = r.get_data(as_text=True)
        self.assertIn("Hello Ama", html)
        self.assertEqual(html.count("I will be there"), 1)               # only Ama's own visit
        self.assertNotIn("Kofi", html)
        self.assertNotIn("Log out", html)
        self.assertEqual(r.headers["Referrer-Policy"], "no-referrer")
        self.assertIn("noindex", r.headers["X-Robots-Tag"])

    def test_bad_expired_revoked_and_replaced_links_all_look_the_same(self):
        token = self.make_link()
        c = self.anon()
        self.assertEqual(c.get("/p/not-a-token").status_code, 404)
        newer = self.make_link()
        self.assertEqual(c.get(f"/p/{token}").status_code, 404)             # replaced
        self.assertEqual(c.get(f"/p/{newer}").status_code, 200)
        self.sql("UPDATE patient_links SET expires_on = '2020-01-01' WHERE revoked = 0")
        self.assertEqual(c.get(f"/p/{newer}").status_code, 404)
        third = self.make_link()
        self.post(self.login("desk"), "/patients/1/messages", {"action": "link_revoke"})
        self.assertEqual(c.get(f"/p/{third}").status_code, 404)

    def test_guessing_is_slowed_down(self):
        c = self.anon()
        for _ in range(25):
            c.get("/p/guess")
        token = self.make_link()
        self.assertEqual(c.get(f"/p/{token}").status_code, 404)             # this address is locked out for the hour
        self.assertEqual(self.app.test_client().get(f"/p/{token}", environ_overrides={"REMOTE_ADDR": "10.9.9.9"}).status_code, 200)

    def test_archived_patient_link_stops_working(self):
        token = self.make_link()
        self.sql("UPDATE patients SET status = 'archived' WHERE id = 1")
        self.assertEqual(self.anon().get(f"/p/{token}").status_code, 404)

    def test_patient_confirms_and_declines_visits_but_not_someone_elses(self):
        a, b = self.appt(days=2), self.appt(days=3, pid=2)
        token = self.make_link()
        c = self.anon()
        self.anon_post(c, f"/p/{token}", f"/p/{token}/visit/{a}", {"answer": "confirmed"})
        self.assertEqual(self.one("SELECT confirmation FROM appointments WHERE id = ?", a)[0], "confirmed")
        self.anon_post(c, f"/p/{token}", f"/p/{token}/visit/{a}", {"answer": "declined"})
        self.assertEqual(self.one("SELECT confirmation FROM appointments WHERE id = ?", a)[0], "declined")
        self.anon_post(c, f"/p/{token}", f"/p/{token}/visit/{b}", {"answer": "confirmed"})
        self.assertEqual(self.one("SELECT confirmation FROM appointments WHERE id = ?", b)[0], "none")
        self.anon_post(c, f"/p/{token}", f"/p/{token}/visit/{a}", {"answer": "maybe"})
        self.assertEqual(self.one("SELECT confirmation FROM appointments WHERE id = ?", a)[0], "declined")

    def _plan(self, pid=1):
        conn = self.db()
        plan = PL.create(conn, self.clinic(), 1, pid, "Option A")
        PL.add_item(conn, self.clinic(), 1, plan, self.cid("CROWN"))
        PL.present(conn, self.clinic(), {"id": 1}, plan, 7)
        conn.commit()
        return plan

    def test_adult_accepts_a_quote_on_their_phone(self):
        plan = self._plan()
        token = self.make_link()
        c = self.anon()
        self.assertIn("GH₵ 1,500.00", c.get(f"/p/{token}").get_data(as_text=True))
        self.anon_post(c, f"/p/{token}", f"/p/{token}/quote/{plan}/accept", {"signer_name": "Ama Mensah", "relation": "self"})   # box not ticked
        self.assertEqual(self.one("SELECT status FROM treatment_plans")[0], "presented")
        self.anon_post(c, f"/p/{token}", f"/p/{token}/quote/{plan}/accept", {"signer_name": "Ama Mensah", "relation": "self", "agreed": "1"})
        p = self.one("SELECT * FROM treatment_plans")
        self.assertEqual((p["status"], p["signer_name"], p["accept_method"]), ("accepted", "Ama Mensah", "typed"))
        self.assertEqual(self.one("SELECT COUNT(*) FROM procedures WHERE status = 'planned'")[0], 1)
        self.assertEqual(self.one("SELECT COUNT(*) FROM payments")[0], 0)                  # nothing is charged

    def test_child_cannot_sign_for_themselves(self):
        plan = self._plan(2)
        token = self.make_link(2)
        c = self.anon()
        self.anon_post(c, f"/p/{token}", f"/p/{token}/quote/{plan}/accept", {"signer_name": "Kofi Young", "relation": "self", "agreed": "1"})
        self.assertEqual(self.one("SELECT status FROM treatment_plans")[0], "presented")
        self.anon_post(c, f"/p/{token}", f"/p/{token}/quote/{plan}/accept", {"signer_name": "Esi Young", "relation": "parent", "agreed": "1"})
        self.assertEqual(self.one("SELECT status FROM treatment_plans")[0], "accepted")

    def test_cannot_accept_someone_elses_quote(self):
        plan = self._plan(2)
        token = self.make_link(1)
        self.anon_post(self.anon(), f"/p/{token}/health", f"/p/{token}/quote/{plan}/accept", {"signer_name": "Ama Mensah", "relation": "self", "agreed": "1"})
        self.assertEqual(self.one("SELECT status FROM treatment_plans")[0], "presented")

    def test_balance_is_shown_to_the_patient_only(self):
        self.sql("INSERT INTO procedures(clinic_id, patient_id, code_id, status, fee_p, done_on, planned_on) VALUES (1,1,?, 'done', 30000, date('now'), date('now'))", self.cid("FILL"))
        token = self.make_link()
        self.assertIn("GH₵ 300.00", self.anon().get(f"/p/{token}").get_data(as_text=True))

    def test_send_link_by_whatsapp_needs_consent_and_hides_the_secret_in_the_log(self):
        self.consent(reminders=0)
        c = self.login("desk")
        r = self.post(c, "/patients/1/messages", {"action": "link_create"})
        token = re.search(r"/p/([A-Za-z0-9_\-]+)", r.get_data(as_text=True)).group(1)
        r = self.post(c, "/patients/1/messages", {"action": "link_send", "token": token}, follow_redirects=True)
        self.assertIn("not agreed", r.get_data(as_text=True))
        self.consent()
        r = self.post(c, "/patients/1/messages", {"action": "link_send", "token": token})
        self.assertEqual(r.status_code, 302)
        self.assertIn(token, r.headers["Location"])
        body = self.one("SELECT body FROM messages WHERE kind = 'portal_link'")[0]
        self.assertIn("[private link]", body)
        self.assertNotIn(token, body)
        other = self.post(c, "/patients/2/messages", {"action": "link_send", "token": token}, follow_redirects=True)    # a link for another patient
        self.assertIn("not valid", other.get_data(as_text=True))


class HealthFormTests(LinkTests):
    FORM = {"conditions": "Asthma", "medications": "Salbutamol", "allergies": "Penicillin", "blood_thinners": "1", "smoker": "never",
            "agrees_messages": "1"}

    def submit(self, token, **kw):
        d = dict(self.FORM)
        d.update(kw)
        c = self.anon()
        return self.anon_post(c, f"/p/{token}/health", f"/p/{token}/health", d)

    def test_form_waits_for_a_clinician_and_does_not_touch_the_record(self):
        token = self.make_link()
        r = self.submit(token)
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.one("SELECT COUNT(*) FROM medical_history")[0], 0)
        self.assertEqual(self.one("SELECT reminders_ok FROM patients WHERE id = 1")[0], 1)       # already 1 in the test data
        self.assertEqual(self.one("SELECT status FROM intake_submissions")[0], "pending")
        self.assertIn("checking the form", self.anon().get(f"/p/{token}").get_data(as_text=True))

    def test_second_form_while_one_is_waiting_is_refused(self):
        token = self.make_link()
        self.submit(token)
        self.submit(token, conditions="Something else")
        self.assertEqual(self.one("SELECT COUNT(*) FROM intake_submissions")[0], 1)

    def test_empty_form_is_refused(self):
        token = self.make_link()
        c = self.anon()
        self.anon_post(c, f"/p/{token}/health", f"/p/{token}/health", {})
        self.assertEqual(self.one("SELECT COUNT(*) FROM intake_submissions")[0], 0)

    def test_dentist_accepts_and_it_becomes_history_and_alerts(self):
        self.consent(reminders=0)
        token = self.make_link()
        self.submit(token)
        c = self.login("dent")
        self.assertIn("1 health form to check", self.page(c, "/"))
        page = self.page(c, "/messages/forms/1")
        self.assertIn("Penicillin", page)
        self.post(c, "/messages/forms/1", {"action": "accept"})
        h = self.one("SELECT * FROM medical_history WHERE patient_id = 1")
        self.assertEqual((h["conditions"], h["allergies"], h["blood_thinners"]), ("Asthma", "Penicillin", 1))
        self.assertEqual(self.one("SELECT COUNT(*) FROM patient_alerts WHERE kind = 'allergy' AND active = 1")[0], 1)
        self.assertEqual(self.one("SELECT reminders_ok FROM patients WHERE id = 1")[0], 1)
        self.assertEqual(self.one("SELECT status FROM intake_submissions")[0], "accepted")
        again = self.post(c, "/messages/forms/1", {"action": "accept"}, follow_redirects=True)
        self.assertIn("already been dealt with", again.get_data(as_text=True))

    def test_dismissed_form_changes_nothing(self):
        token = self.make_link()
        self.submit(token)
        self.post(self.login("dent"), "/messages/forms/1", {"action": "dismiss"})
        self.assertEqual(self.one("SELECT COUNT(*) FROM medical_history")[0], 0)
        self.assertEqual(self.one("SELECT status FROM intake_submissions")[0], "dismissed")
        self.submit(token)                                                    # a new form can follow
        self.assertEqual(self.one("SELECT COUNT(*) FROM intake_submissions")[0], 2)

    def test_front_desk_cannot_read_health_forms(self):
        token = self.make_link()
        self.submit(token)
        d = self.login("desk")
        self.assertEqual(d.get("/messages/forms").status_code, 403)
        self.assertEqual(d.get("/messages/forms/1").status_code, 403)
        self.assertEqual(self.post(d, "/messages/forms/1", {"action": "accept"}).status_code, 403)
        self.assertNotIn("Health forms", self.page(d, "/messages"))

    def test_form_of_another_clinic_is_not_found(self):
        token = self.make_link()
        self.submit(token)
        self.assertEqual(self.login("mgr2").get("/messages/forms/1").status_code, 404)

    def test_long_answers_are_refused(self):
        token = self.make_link()
        self.submit(token, conditions="x" * 1600)
        self.assertEqual(self.one("SELECT COUNT(*) FROM intake_submissions")[0], 0)


class UpgradeAndCli(Comms):
    def test_build_queue_command(self):
        self.consent()
        self.appt(days=2)
        r = self.app.test_cli_runner().invoke(args=["build-queue"])
        self.assertIn("1 messages added", r.output)

    def test_old_database_gets_read_marker(self):
        cols = dbmeta.columns(self.db(), "messages")
        self.assertIn("read_at", cols)

    def test_new_clinic_starts_with_wording_and_a_two_day_reminder(self):
        self.assertEqual(self.one("SELECT COUNT(*) FROM message_templates WHERE clinic_id = 2")[0], len(MT.KINDS))
        self.assertEqual(self.one("SELECT days_before FROM reminder_rules WHERE clinic_id = 2")[0], 2)


if __name__ == "__main__":
    unittest.main()
