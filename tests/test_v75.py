"""v0.75: AI help for daily work, step one, all on the clinic's own server. It suggests; a person decides. Sorting patient replies
with free times and a draft reply (never for a clinical worry, never money), who may not come, a brief before each patient, the
manager's morning summary with money patterns, and help articles for support tickets at HQ."""
import datetime
import json
import re

from tests.test_v04 import Base
from tests.test_v71 import HQBase
from tests import a11y
from dental_os.services import aidesk as A, hq as H


class Sorting(Base):
    def test_intents(self):
        cases = {
            "Yes I will be there": "confirm", "ok": "confirm", "1": "confirm",
            "I can't make it tomorrow, sorry": "cancel",
            "Can I come on Thursday afternoon instead?": "reschedule", "please move my appointment to next week": "reschedule",
            "How much is a crown?": "price", "Does NHIS cover cleaning": "price",
            "My tooth still hurts and my gum is swollen": "clinical", "There is blood since the extraction": "clinical",
            "I am not happy with how I was treated": "complaint",
            "What time do you open?": "question", "Thanks": "other",
        }
        for text, want in cases.items():
            self.assertEqual(A.classify(text)[0], want, text)

    def test_a_clinical_worry_wins_over_anything_else(self):
        self.assertEqual(A.classify("Yes I'll come but the pain is bad")[0], "clinical")

    def test_preferences(self):
        p = A.preference("Can I come Thursday afternoon around 3pm?")
        self.assertEqual((p["weekday"], p["part"], p["time"]), (4, "afternoon", "15:00"))
        self.assertTrue(A.preference("tomorrow morning please")["tomorrow"])
        self.assertTrue(A.preference("next week is better")["next_week"])
        self.assertEqual(A.preference("at 9:30")["time"], "09:30")

    def setup_clinic(self):
        conn = self.db()
        conn.execute("UPDATE clinics SET working_days = '1,2,3,4,5', open_time = '08:00', close_time = '17:00' WHERE id = 1")
        if not conn.execute("SELECT 1 FROM chairs WHERE clinic_id = 1").fetchone():
            conn.execute("INSERT INTO chairs(clinic_id, name) VALUES (1, 'Chair 1')")
        conn.execute("UPDATE patients SET reminders_ok = 1 WHERE id = 1")
        conn.commit()
        return conn

    def test_free_times_fit_the_wish(self):
        conn = self.setup_clinic()
        c = conn.execute("SELECT * FROM clinics WHERE id = 1").fetchone()
        today = datetime.date(2026, 11, 4)
        slots = A.suggest_slots(conn, c, 1, A.preference("Thursday afternoon"), today=today)
        self.assertTrue(slots)
        for s in slots:
            self.assertEqual(s["day"].isoweekday(), 4)
            self.assertGreaterEqual(int(s["time"][:2]), 12)
        conn.close()

    def test_drafts_never_for_clinical_worries_and_never_money(self):
        conn = self.setup_clinic()
        c = conn.execute("SELECT * FROM clinics WHERE id = 1").fetchone()
        p = conn.execute("SELECT * FROM patients WHERE id = 1").fetchone()
        self.assertIsNone(A.draft_reply(conn, c, p, "clinical"))
        from dental_os.services import msgtemplates
        for intent in ("confirm", "cancel", "reschedule", "price", "complaint", "question", "other"):
            text = A.draft_reply(conn, c, p, intent)
            self.assertTrue(text and "Ama" in text, intent)
            self.assertIsNone(msgtemplates.MONEY_WORDS.search(text), intent)
        conn.close()

    def test_messages_page_drafts_a_reply_and_logs_its_use(self):
        conn = self.setup_clinic()
        conn.execute("INSERT INTO messages(clinic_id, patient_id, direction, kind, body) VALUES (1, 1, 'in', 'reply', 'Sorry I cannot come')")
        conn.commit()
        conn.close()
        c = self.login("desk")
        page = self.page(c, "/patients/1/messages")
        self.assertIn("Suggested by the AI helper", page)
        draft = re.search(r'<textarea id="text"[^>]*>([^<]+)</textarea>', page).group(1)
        self.assertIn("another time", draft)
        self.assertEqual(a11y.check(page), [])
        self.post(c, "/patients/1/messages", {"action": "send", "text": draft.replace("&#39;", "'"), "ai_draft": draft.replace("&#39;", "'")})
        conn = self.db()
        self.assertIn("reply draft, sent as written", [r[0] for r in conn.execute("SELECT detail FROM audit_log WHERE action = 'ai.used'")])
        conn.close()

    def test_clinical_worry_page_has_no_draft(self):
        conn = self.setup_clinic()
        conn.execute("INSERT INTO messages(clinic_id, patient_id, direction, kind, body) VALUES (1, 1, 'in', 'reply', 'my jaw is swollen')")
        conn.commit()
        conn.close()
        page = self.page(self.login("desk"), "/patients/1/messages")
        self.assertIn("Pass it to a dentist", page)
        self.assertRegex(page, r'<textarea id="text"[^>]*></textarea>')


class NoShows(Base):
    def visit(self, conn, status, when, reason=None, confirmation="none", created="2026-11-01 10:00:00"):
        conn.execute("""INSERT INTO appointments(clinic_id, patient_id, provider_id, chair_id, start_at, duration_min, status, break_reason, confirmation, created_at)
                        VALUES (1, 1, 1, 1, ?, 30, ?, ?, ?, ?)""", (when, status, reason, confirmation, created))

    def test_history_raises_the_risk(self):
        conn = self.db()
        conn.execute("INSERT INTO chairs(clinic_id, name) VALUES (1, 'Chair 1')")
        c = conn.execute("SELECT * FROM clinics WHERE id = 1").fetchone()
        self.visit(conn, "scheduled", "2026-11-05 09:00")
        a = conn.execute("SELECT * FROM appointments ORDER BY id DESC").fetchone()
        first, why = A.no_show_risk(conn, c, a, datetime.date(2026, 11, 4))
        self.assertIn("first", [w[0] for w in why])
        for i in range(3):
            self.visit(conn, "broken", f"2026-0{i + 1}-10 09:00", "missed")
        self.visit(conn, "complete", "2026-05-10 09:00")
        worse, why = A.no_show_risk(conn, c, a, datetime.date(2026, 11, 4))
        self.assertGreater(worse, first)
        self.assertEqual([w for w in why if w[0] == "missed"][0][1:], (3, 4))
        conn.execute("UPDATE appointments SET confirmation = 'confirmed' WHERE id = ?", (a["id"],))
        a = conn.execute("SELECT * FROM appointments WHERE id = ?", (a["id"],)).fetchone()
        self.assertLess(A.no_show_risk(conn, c, a, datetime.date(2026, 11, 4))[0], worse)
        conn.close()


class Brief(Base):
    def test_brief_has_alerts_last_visit_and_planned_work_and_money_only_when_asked(self):
        conn = self.db()
        conn.execute("INSERT INTO chairs(clinic_id, name) VALUES (1, 'Chair 1')")
        conn.execute("INSERT INTO patient_alerts(clinic_id, patient_id, kind, severity, text) VALUES (1, 1, 'allergy', 'critical', 'Penicillin')")
        conn.execute("INSERT INTO appointments(clinic_id, patient_id, provider_id, chair_id, start_at, duration_min, status) VALUES (1, 1, 1, 1, '2026-03-12 09:00', 30, 'complete')")
        code = conn.execute("SELECT id FROM procedure_codes WHERE clinic_id = 1 LIMIT 1").fetchone()[0]
        conn.execute("INSERT INTO procedures(clinic_id, patient_id, code_id, tooth, status, fee_p, planned_on) VALUES (1, 1, ?, 36, 'planned', 50000, '2026-03-12')", (code,))
        conn.execute("INSERT INTO procedures(clinic_id, patient_id, code_id, status, fee_p, planned_on, done_on) VALUES (1, 1, ?, 'done', 20000, '2026-03-12', '2026-03-12')", (code,))
        c = conn.execute("SELECT * FROM clinics WHERE id = 1").fetchone()
        b = A.brief(conn, c, 1)
        self.assertEqual((b["alerts"], b["last_visit"], len(b["planned"])), (["Penicillin"], "2026-03-12", 1))
        self.assertNotIn("balance_p", b)
        self.assertEqual(A.brief(conn, c, 1, with_money=True)["balance_p"], 20000)
        conn.commit()
        conn.close()
        page = self.page(self.login("dent"), "/appointments/1")
        self.assertIn("Before this patient", page)
        self.assertNotIn("Owes", page)
        self.assertIn("Owes", self.page(self.login("mgr"), "/appointments/1"))
        self.assertNotIn("Before this patient", self.page(self.login("desk"), "/appointments/1"))


class Summary(Base):
    def test_money_patterns(self):
        conn = self.db()
        c = conn.execute("SELECT * FROM clinics WHERE id = 1").fetchone()
        desk = conn.execute("SELECT id FROM users WHERE username = 'desk'").fetchone()[0]
        mgr = conn.execute("SELECT id FROM users WHERE username = 'mgr'").fetchone()[0]
        for _ in range(8):
            conn.execute("INSERT INTO audit_log(clinic_id, user_id, action, at) VALUES (1, ?, 'payment.void', datetime('now'))", (desk,))
        conn.execute("INSERT INTO audit_log(clinic_id, user_id, action, at) VALUES (1, ?, 'payment.void', datetime('now'))", (mgr,))
        today = datetime.date.today()
        for i in range(3):
            conn.execute("INSERT INTO day_closes(clinic_id, day, lines, status, closed_at) VALUES (1, ?, ?, 'closed', datetime('now'))",
                         ((today - datetime.timedelta(days=i + 1)).isoformat(), json.dumps({"cash": {"expected": 1000, "counted": 900, "difference": -100}})))
        kinds = {x["kind"]: x for x in A.money_patterns(conn, c)}
        self.assertEqual((kinds["voids"]["who"], kinds["voids"]["n"], kinds["voids"]["total"]), ("Desk", 8, 9))
        self.assertEqual(kinds["cash_short"]["n"], 3)
        conn.commit()
        conn.close()
        page = self.page(self.login("mgr"), "/huddle")
        self.assertIn("Money patterns worth a look", page)
        self.assertNotIn("Money patterns", self.page(self.login("dent"), "/huddle"))

    def test_spread_out_voids_are_not_flagged(self):
        conn = self.db()
        c = conn.execute("SELECT * FROM clinics WHERE id = 1").fetchone()
        for name in ("desk", "mgr", "dent"):
            uid = conn.execute("SELECT id FROM users WHERE username = ?", (name,)).fetchone()[0]
            for _ in range(3):
                conn.execute("INSERT INTO audit_log(clinic_id, user_id, action, at) VALUES (1, ?, 'payment.void', datetime('now'))", (uid,))
        self.assertEqual(A.money_patterns(conn, c), [])
        conn.close()


class Support(HQBase):
    def test_articles_matched_and_draft_used(self):
        conn = self.db()
        H.save_article(conn, 1, None, "Taking a mobile money payment", "Open the account and press Add payment.\n\nChoose mobile money.", "clinic", True)
        H.save_article(conn, 1, None, "Moving a booking", "Open the visit and press Move.", "clinic", True)
        tid = H.new_ticket(conn, 1, 1, "Ama, manager", "whatsapp", "How do we record mobile money payment?", "", "normal")
        conn.commit()
        self.assertEqual(A.match_articles(conn, "How do we record mobile money payment?", "")[0]["title"], "Taking a mobile money payment")
        conn.close()
        c, _ = self.op("owner")
        page = c.get(f"/hq/support/{tid}").get_data(as_text=True)
        self.assertIn("Help articles that may answer this", page)
        aid = re.search(r"use=(\d+)", page).group(1)
        page = c.get(f"/hq/support/{tid}?use={aid}").get_data(as_text=True)
        draft = re.search(r'<textarea id="body"[^>]*>([^<]+)</textarea>', page).group(1)
        self.assertIn("Hello Ama", draft)
        self.send(c, f"/hq/support/{tid}", {"what": "reply", "body": "Hello Ama, done.", "ai_draft": "something else"})
        conn = self.db()
        self.assertEqual(conn.execute("SELECT detail FROM operator_log WHERE action = 'ai.used'").fetchone()[0], "support draft, edited")
        conn.close()
