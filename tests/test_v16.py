"""v0.16: diary depth. Dentists' own hours and time off, blocked time, appointment types, the week view, the check-in board,
booking a family back to back, filling a gap from the waiting list, and tooth numbering (FDI, Universal, Palmer)."""
import unittest
from datetime import datetime, timedelta

from tests.test_v04 import Base
from dental_os.services import diaryrules as R, scheduling as S, teeth, treatment as TR
from dental_os.services.timeutil import now_local


class ToothNumberingTests(unittest.TestCase):
    def test_known_teeth_in_each_system(self):
        self.assertEqual(teeth.show(18, "universal"), "1")
        self.assertEqual(teeth.show(11, "universal"), "8")
        self.assertEqual(teeth.show(21, "universal"), "9")
        self.assertEqual(teeth.show(28, "universal"), "16")
        self.assertEqual(teeth.show(38, "universal"), "17")
        self.assertEqual(teeth.show(36, "universal"), "19")
        self.assertEqual(teeth.show(48, "universal"), "32")
        self.assertEqual(teeth.show(55, "universal"), "A")
        self.assertEqual(teeth.show(65, "universal"), "J")
        self.assertEqual(teeth.show(75, "universal"), "K")
        self.assertEqual(teeth.show(85, "universal"), "T")
        self.assertEqual(teeth.show(36, "palmer"), "LL6")
        self.assertEqual(teeth.show(11, "palmer"), "UR1")
        self.assertEqual(teeth.show(54, "palmer"), "URD")
        self.assertEqual(teeth.show(36, "fdi"), "36")

    def test_every_tooth_goes_there_and_back(self):
        for notation in teeth.NOTATIONS:
            seen = set()
            for fdi in sorted(teeth.VALID):
                shown = teeth.show(fdi, notation)
                self.assertNotIn(shown, seen, (notation, fdi))
                seen.add(shown)
                self.assertEqual(teeth.parse(shown, notation), fdi, (notation, shown))
                self.assertEqual(teeth.parse(shown.lower(), notation), fdi)

    def test_bad_teeth_are_refused_in_the_clinics_own_words(self):
        for notation, bad in (("fdi", "19"), ("fdi", "UR6"), ("universal", "33"), ("universal", "U"), ("palmer", "UR9"),
                              ("palmer", "36"), ("palmer", "ULF")):
            with self.assertRaises(teeth.ToothError) as ctx:
                teeth.parse(bad, notation)
            self.assertEqual(str(ctx.exception), teeth.HELP[notation])
        self.assertIsNone(teeth.parse("", "universal"))
        self.assertEqual(teeth.parse("07", "universal"), 12)

    def test_services_read_the_clinics_numbering_for_one_request_only(self):
        token = teeth.NOTATION.set("universal")
        try:
            self.assertEqual(TR.clean_tooth("19"), 36)
        finally:
            teeth.NOTATION.reset(token)
        self.assertEqual(TR.clean_tooth("36"), 36)
        with self.assertRaises(TR.TreatmentError):
            TR.clean_tooth("19")

    def test_anything_that_is_not_a_tooth_is_shown_as_it_is(self):
        self.assertEqual(teeth.show(None, "universal"), "")
        self.assertEqual(teeth.show("16 and 17", "universal"), "16 and 17")
        self.assertEqual(teeth.show(99, "universal"), "99")


class Diary(Base):
    def setUp(self):
        super().setUp()
        conn = self.db()
        conn.execute("INSERT INTO providers(clinic_id, name) VALUES (1, 'Dr Second')")       # provider 3
        conn.execute("INSERT INTO chairs(clinic_id, name) VALUES (1, 'Chair 2')")            # chair 3
        conn.commit()
        self.clinic = dict(conn.execute("SELECT * FROM clinics WHERE id = 1").fetchone())
        conn.close()
        today = now_local(self.clinic["timezone"]).date()
        self.day = today + timedelta(days=(7 - today.isoweekday()) + 1 + 7)        # a Monday at least a week ahead
        self.conn = self.db()

    def tearDown(self):
        self.conn.close()
        super().tearDown() if hasattr(super(), "tearDown") else None

    def at(self, hhmm, day=None):
        return datetime.strptime(f"{(day or self.day).isoformat()} {hhmm}", "%Y-%m-%d %H:%M")

    def times(self, provider=1, duration=30, day=None, **kw):
        return [s["time"] for s in S.free_slots(self.conn, self.clinic, provider, day or self.day, duration, **kw)]


class HoursAndTimeOffTests(Diary):
    def test_a_dentist_with_no_hours_works_the_clinics_hours(self):
        t = self.times()
        self.assertEqual(t[0], "08:00")
        self.assertEqual(t[-1], "16:30")

    def test_own_hours_and_a_split_day(self):
        R.save_hours(self.conn, self.clinic, 1, 1, [(1, "09:00", "12:00"), (1, "14:00", "16:00"), (2, "08:00", "12:00")])
        t = self.times()
        self.assertEqual(t[0], "09:00")
        self.assertIn("11:30", t)
        self.assertNotIn("12:00", t)
        self.assertNotIn("13:30", t)
        self.assertIn("14:00", t)
        self.assertEqual(t[-1], "15:30")
        self.assertEqual(self.times(day=self.day + timedelta(days=2)), [])       # Wednesday: not set, so not working
        with self.assertRaises(S.SchedulingError) as ctx:
            S.book(self.conn, self.clinic, 1, 1, 1, 1, self.at("12:30"), 30)
        self.assertIn("not working", str(ctx.exception))
        S.book(self.conn, self.clinic, 1, 1, 1, 1, self.at("09:00"), 30)

    def test_hours_are_checked(self):
        for rows, words in (([(1, "07:00", "12:00")], "inside the clinic"), ([(1, "12:00", "09:00")], "before its end"),
                            ([(1, "09:00", "12:00"), (1, "11:00", "13:00")], "overlap"), ([(7, "09:00", "12:00")], "closed"),
                            ([(1, "9am", "12:00")], "like 09:00"), ([(1, "08:00", "09:00"), (1, "10:00", "11:00"), (1, "12:00", "13:00")], "two spans")):
            with self.assertRaises(R.RulesError) as ctx:
                R.save_hours(self.conn, self.clinic, 1, 1, rows)
            self.assertIn(words, str(ctx.exception))
        with self.assertRaises(R.RulesError):
            R.save_hours(self.conn, self.clinic, 1, 2, [(1, "09:00", "12:00")])            # another clinic's dentist

    def test_whole_days_off_and_part_of_a_day(self):
        R.add_time_off(self.conn, self.clinic, 1, 1, self.day.isoformat(), (self.day + timedelta(days=1)).isoformat(), reason="Leave")
        self.assertEqual(self.times(), [])
        self.assertEqual(self.times(day=self.day + timedelta(days=1)), [])
        self.assertTrue(self.times(day=self.day + timedelta(days=2)))
        self.assertTrue(self.times(provider=3))                                   # the other dentist still works
        with self.assertRaises(S.SchedulingError) as ctx:
            S.book(self.conn, self.clinic, 1, 1, 1, 1, self.at("10:00"), 30)
        self.assertIn("off", str(ctx.exception))
        wed = self.day + timedelta(days=2)
        off = R.add_time_off(self.conn, self.clinic, 1, 1, wed.isoformat(), None, "10:00", "12:00", "Course")
        t = self.times(day=wed)
        self.assertIn("09:30", t)
        self.assertNotIn("10:00", t)
        self.assertNotIn("11:30", t)
        self.assertIn("12:00", t)
        R.remove_time_off(self.conn, self.clinic, 1, off)
        self.assertIn("10:00", self.times(day=wed))
        with self.assertRaises(R.RulesError):
            R.add_time_off(self.conn, self.clinic, 1, 1, wed.isoformat(), (wed + timedelta(days=1)).isoformat(), "10:00", "12:00")
        with self.assertRaises(R.RulesError):
            R.add_time_off(self.conn, self.clinic, 1, 1, wed.isoformat(), self.day.isoformat())


class BlockTests(Diary):
    def test_lunch_every_day_cannot_be_booked(self):
        R.add_block(self.conn, self.clinic, 1, "lunch", self.day.isoformat(), "13:00", "14:00", repeat="daily")
        for d in range(0, 6):
            t = self.times(day=self.day + timedelta(days=d))
            self.assertNotIn("13:00", t)
            self.assertNotIn("13:30", t)
            self.assertNotIn("12:30", self.times(day=self.day + timedelta(days=d), duration=60))
            self.assertIn("14:00", t)
        with self.assertRaises(S.SchedulingError) as ctx:
            S.book(self.conn, self.clinic, 1, 1, 1, 1, self.at("13:00"), 30, allow_held=True)
        self.assertIn("blocked", str(ctx.exception))

    def test_held_back_time_is_left_free_unless_asked(self):
        R.add_block(self.conn, self.clinic, 1, "emergency", self.day.isoformat(), "10:00", "11:00", provider_id=1, repeat="weekly")
        self.assertNotIn("10:00", self.times())
        self.assertIn("10:00", self.times(provider=3))                                       # only that dentist's time is held
        self.assertIn("10:00", self.times(allow_held=True))
        self.assertIn("10:00", self.times(day=self.day + timedelta(days=1)))                  # weekly: Mondays only
        self.assertNotIn("10:00", self.times(day=self.day + timedelta(days=7)))
        with self.assertRaises(S.SchedulingError) as ctx:
            S.book(self.conn, self.clinic, 1, 1, 1, 1, self.at("10:00"), 30)
        self.assertIn("held back", str(ctx.exception))
        S.book(self.conn, self.clinic, 1, 1, 1, 1, self.at("10:00"), 30, allow_held=True)

    def test_an_emergency_type_may_use_emergency_time(self):
        R.add_block(self.conn, self.clinic, 1, "emergency", self.day.isoformat(), "10:00", "11:00")
        tid = R.save_type(self.conn, self.clinic, 1, None, "Emergency", 30, "red", "Emergency or pain", "emergency", [],
                          S.DURATIONS, S.CATEGORIES)
        row = R.get_type(self.conn, 1, tid)
        self.assertIn("10:00", self.times(type_row=row))
        S.book(self.conn, self.clinic, 1, 1, 1, 1, self.at("10:00"), 30, type_id=tid)
        other = R.save_type(self.conn, self.clinic, 1, None, "Check-up", 30, "green", "Consultation", None, [], S.DURATIONS, S.CATEGORIES)
        with self.assertRaises(S.SchedulingError):
            S.book(self.conn, self.clinic, 1, 2, 1, 3, self.at("10:30"), 30, type_id=other)

    def test_a_chair_block_only_stops_that_chair(self):
        R.add_block(self.conn, self.clinic, 1, "closed", self.day.isoformat(), "09:00", "10:00", chair_id=1, label="Repair")
        slots = S.free_slots(self.conn, self.clinic, 1, self.day, 30)
        nine = next(s for s in slots if s["time"] == "09:00")
        self.assertEqual(nine["chair_id"], 3)
        with self.assertRaises(S.SchedulingError):
            S.book(self.conn, self.clinic, 1, 1, 1, 1, self.at("09:00"), 30)
        S.book(self.conn, self.clinic, 1, 1, 1, 3, self.at("09:00"), 30)

    def test_blocks_are_checked_and_can_be_removed(self):
        for kw, words in ((dict(kind="nap"), "kept for"), (dict(repeat="hourly"), "repeats"), (dict(start_time="14:00", end_time="13:00"), "before its end"),
                          (dict(provider_id=2), "dentist"), (dict(chair_id=2), "chair")):
            args = dict(kind="lunch", start_date=self.day.isoformat(), start_time="13:00", end_time="14:00")
            args.update(kw)
            with self.assertRaises(R.RulesError) as ctx:
                R.add_block(self.conn, self.clinic, 1, **args)
            self.assertIn(words, str(ctx.exception).lower())
        bid = R.add_block(self.conn, self.clinic, 1, "lunch", self.day.isoformat(), "13:00", "14:00")
        R.remove_block(self.conn, self.clinic, 1, bid)
        self.assertIn("13:00", self.times())
        with self.assertRaises(R.RulesError):
            R.remove_block(self.conn, self.clinic, 1, bid)

    def test_the_day_view_shows_blocks_and_time_not_worked(self):
        R.save_hours(self.conn, self.clinic, 1, 1, [(1, "10:00", "16:00")])
        R.add_block(self.conn, self.clinic, 1, "lunch", self.day.isoformat(), "13:00", "14:00")
        R.add_time_off(self.conn, self.clinic, 1, 3, self.day.isoformat(), None, reason="Course")
        view = S.day_view(self.conn, self.clinic, self.day)
        col1 = [x for x in view["shade"] if x["col"] == 2]
        self.assertEqual([(x["grid_row"], x["span"]) for x in col1], [(2, 4), (18, 2)])        # 08:00-10:00 and 16:00-17:00
        self.assertEqual(len([b for b in view["blocks"] if b["kind"] == "lunch"]), 2)            # every dentist
        self.assertEqual(view["off"], {3: ["Course"]})


class TypeTests(Diary):
    def test_a_type_sets_length_reason_and_plans_its_treatments(self):
        codes = {r["code"]: r["id"] for r in self.conn.execute("SELECT id, code FROM procedure_codes WHERE clinic_id = 1")}
        tid = R.save_type(self.conn, self.clinic, 1, None, "New patient", 45, "blue", "Consultation", "new_patients",
                          [codes["CONS"], codes["XRAY-PA"], codes["CONS"], 99999], S.DURATIONS, S.CATEGORIES)
        self.assertEqual(R.get_type(self.conn, 1, tid)["procedure_ids"], f"{codes['CONS']},{codes['XRAY-PA']}")
        existing = TR.add_planned(self.conn, self.clinic, 1, 1, codes["CONS"])
        aid = S.book(self.conn, self.clinic, 1, 1, 1, 1, self.at("09:00"), 45, "Not sure yet", type_id=tid)
        appt = S.get_appointment(self.conn, 1, aid)
        self.assertEqual(appt["type_name"], "New patient")
        self.assertEqual(appt["category"], "Consultation")
        linked = self.conn.execute("SELECT id, code_id FROM procedures WHERE appointment_id = ? ORDER BY id", (aid,)).fetchall()
        self.assertEqual([r["code_id"] for r in linked], [codes["CONS"], codes["XRAY-PA"]])
        self.assertEqual(linked[0]["id"], existing)                         # the planned consultation was linked, not doubled

    def test_types_are_checked(self):
        args = dict(name="Clean", duration=30, colour="teal", category="Consultation", block_kind=None, procedure_ids=[])
        for change, words in ((dict(name=""), "name"), (dict(duration=33), "length"), (dict(colour="gold"), "colour"),
                              (dict(category="Haircut"), "reason"), (dict(block_kind="lunch"), "held-back")):
            a = dict(args)
            a.update(change)
            with self.assertRaises(R.RulesError) as ctx:
                R.save_type(self.conn, self.clinic, 1, None, a["name"], a["duration"], a["colour"], a["category"], a["block_kind"],
                            a["procedure_ids"], S.DURATIONS, S.CATEGORIES)
            self.assertIn(words, str(ctx.exception).lower())
        R.save_type(self.conn, self.clinic, 1, None, *args.values(), S.DURATIONS, S.CATEGORIES)
        with self.assertRaises(R.RulesError):
            R.save_type(self.conn, self.clinic, 1, None, *args.values(), S.DURATIONS, S.CATEGORIES)
        self.assertEqual(R.add_starter_types(self.conn, self.clinic, 1), len(R.STARTER_TYPES))
        self.assertEqual(R.add_starter_types(self.conn, self.clinic, 1), 0)

    def test_a_switched_off_type_cannot_be_booked(self):
        tid = R.save_type(self.conn, self.clinic, 1, None, "Clean", 30, "teal", "Consultation", None, [], S.DURATIONS, S.CATEGORIES)
        R.toggle_type(self.conn, self.clinic, 1, tid)
        with self.assertRaises(S.SchedulingError):
            S.book(self.conn, self.clinic, 1, 1, 1, 1, self.at("09:00"), 30, type_id=tid)


class FamilyAndGapTests(Diary):
    def test_a_family_is_booked_back_to_back(self):
        self.conn.execute("UPDATE patients SET guarantor_id = 1 WHERE id = 2")
        S.book(self.conn, self.clinic, 1, 1, 1, 1, self.at("09:30"), 30)          # Ama already has 09:30
        slots = S.family_slots(self.conn, self.clinic, 1, self.day, [(1, 30), (2, 30)])
        starts = [s["time"] for s in slots]
        self.assertNotIn("09:00", starts)                      # Ama at 09:00 is fine, but the dentist is busy at 09:30 for Kofi
        self.assertNotIn("09:30", starts)
        self.assertIn("10:00", starts)
        first = slots[starts.index("10:00")]["plan"]
        self.assertEqual([(p, s.strftime("%H:%M")) for p, s, _m, _c in first], [(1, "10:00"), (2, "10:30")])
        ids = S.book_family(self.conn, self.clinic, 1, 1, self.day, "10:00", [(1, 30), (2, 30)])
        self.assertEqual(len(ids), 2)
        with self.assertRaises(S.SchedulingError):
            S.book_family(self.conn, self.clinic, 1, 1, self.day, "10:00", [(1, 30), (2, 30)])
        with self.assertRaises(S.SchedulingError):
            S.book_family(self.conn, self.clinic, 1, 1, self.day, "11:00", [(1, 30)])

    def test_a_cancelled_visit_offers_people_from_the_waiting_list(self):
        aid = S.book(self.conn, self.clinic, 1, 1, 1, 1, self.at("11:00"), 30)
        S.set_status(self.conn, 1, 1, aid, "broken", "cancelled")
        w1 = S.add_to_waiting_list(self.conn, self.clinic, 1, 2, "unscheduled", duration=30)
        w2 = S.add_to_waiting_list(self.conn, self.clinic, 1, 2, "asap", duration=30)
        S.add_to_waiting_list(self.conn, self.clinic, 1, 2, "asap", duration=60)              # too long for the gap
        S.add_to_waiting_list(self.conn, self.clinic, 1, 2, "asap", provider_id=3, duration=30)  # wants another dentist
        _a, rows = S.gap_candidates(self.conn, self.clinic, aid)
        self.assertEqual([r["id"] for r in rows], [w2, w1])
        S.place(self.conn, self.clinic, 1, w2, 1, 1, self.at("11:00"), allow_held=True)
        self.assertEqual(S.get_appointment(self.conn, 1, w2)["status"], "scheduled")


class BoardTests(Diary):
    def test_arrive_seat_and_leave(self):
        now = now_local(self.clinic["timezone"])
        start = now.replace(second=0, microsecond=0) - timedelta(minutes=20)
        self.conn.execute("""INSERT INTO appointments(clinic_id, patient_id, provider_id, chair_id, start_at, duration_min, status)
                             VALUES (1, 1, 1, 1, ?, 30, 'scheduled')""", (start.strftime("%Y-%m-%d %H:%M"),))
        aid = self.conn.execute("SELECT MAX(id) FROM appointments").fetchone()[0]
        lanes = S.board(self.conn, self.clinic, now.date())
        self.assertEqual(len(lanes["expected"]), 1)
        self.assertGreaterEqual(lanes["expected"][0]["late"], 19)
        with self.assertRaises(S.SchedulingError):
            S.move_stage(self.conn, self.clinic, 1, aid, "leave")
        S.move_stage(self.conn, self.clinic, 1, aid, "arrive")
        self.assertEqual(S.stage(S.get_appointment(self.conn, 1, aid)), "waiting")
        S.move_stage(self.conn, self.clinic, 1, aid, "seat")
        S.move_stage(self.conn, self.clinic, 1, aid, "undo")
        self.assertEqual(S.stage(S.get_appointment(self.conn, 1, aid)), "waiting")
        S.move_stage(self.conn, self.clinic, 1, aid, "seat")
        S.move_stage(self.conn, self.clinic, 1, aid, "leave")
        a = S.get_appointment(self.conn, 1, aid)
        self.assertEqual(a["status"], "complete")
        self.assertTrue(a["arrived_at"] and a["seated_at"] and a["dismissed_at"])
        self.assertEqual(len(S.board(self.conn, self.clinic, now.date())["left"]), 1)
        actions = [r[0] for r in self.conn.execute("SELECT action FROM audit_log WHERE object_id = ? AND action LIKE 'appointment.stage.%'", (aid,))]
        self.assertEqual(len(actions), 5)


class WebTests(Diary):
    def test_who_may_set_up_the_diary(self):
        for name, url, code in (("desk", "/diary/setup/hours", 403), ("desk", "/diary/setup/types", 403), ("desk", "/diary/setup/blocks", 200),
                                ("desk", "/diary/setup/time-off", 200), ("asst", "/diary/setup/blocks", 403), ("dent", "/diary/setup/time-off", 403),
                                ("mgr", "/diary/setup/hours", 200), ("mgr", "/diary/setup/types", 200), ("asst", "/board", 200), ("dent", "/diary/week", 200)):
            self.assertEqual(self.login(name).get(url).status_code, code, (name, url))

    def test_hours_time_off_blocks_and_types_through_the_screens(self):
        c = self.login("mgr")
        self.post(c, "/diary/setup/hours", {"provider_id": "1", "s1_1": "09:00", "e1_1": "12:00"})
        self.assertIn("09:00", self.page(c, "/diary/setup/hours?provider_id=1"))
        self.assertEqual(self.times()[0], "09:00")
        r = self.post(c, "/diary/setup/hours", {"provider_id": "1", "s1_1": "12:00", "e1_1": "09:00"}, follow_redirects=True)
        self.assertIn("before its end time", r.get_data(as_text=True))
        self.post(c, "/diary/setup/hours", {"provider_id": "1", "clear": "1"})
        self.assertEqual(self.times()[0], "08:00")
        d = self.login("desk")
        self.post(d, "/diary/setup/time-off", {"provider_id": "1", "start_date": self.day.isoformat(), "end_date": self.day.isoformat(), "reason": "Leave"})
        self.assertIn("Leave", self.page(d, "/diary/setup/time-off"))
        self.assertIn("Time off", self.page(d, f"/diary?date={self.day}"))
        self.post(d, "/diary/setup/blocks", {"kind": "lunch", "start_date": self.day.isoformat(), "start_time": "13:00", "end_time": "14:00",
                                             "repeat": "daily", "label": "Team lunch"})
        self.assertIn("Team lunch", self.page(d, f"/diary?date={self.day + timedelta(days=1)}"))
        self.post(c, "/diary/setup/types", {"action": "starters"})
        page = self.page(c, "/diary/setup/types")
        self.assertIn("New patient examination", page)
        tid = self.conn.execute("SELECT id FROM appointment_types WHERE name = 'Emergency'").fetchone()[0]
        page = self.page(c, f"/appointments/new?patient_id=1&type_id={tid}&find=1&provider_id=3&date={self.day}")
        self.assertIn("The length and reason come from the type.", page)
        self.assertIn('name="type_id" value="%d"' % tid, page)

    def test_booking_with_a_type_through_the_screen(self):
        c = self.login("desk")
        tid = R.save_type(self.conn, self.clinic, 1, None, "Long clean", 60, "teal", "Cleaning & preventive care", None, [], S.DURATIONS, S.CATEGORIES)
        self.conn.commit()
        r = self.post(c, "/appointments", {"patient_id": "1", "provider_id": "1", "date": self.day.isoformat(), "slot": "09:00|1",
                                           "duration": "60", "category": "Cleaning & preventive care", "type_id": str(tid)})
        self.assertEqual(r.status_code, 302)
        a = self.conn.execute("SELECT * FROM appointments ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual((a["type_id"], a["duration_min"]), (tid, 60))
        self.assertIn("Long clean", self.page(c, f"/diary?date={self.day}"))
        self.assertIn("c-teal", self.page(c, f"/diary?date={self.day}"))

    def test_family_booking_through_the_screen(self):
        self.conn.execute("UPDATE patients SET guarantor_id = 1 WHERE id = 2")
        self.conn.commit()
        c = self.login("desk")
        self.assertIn("Book the family together", self.page(c, "/patients/2"))
        page = self.page(c, f"/patients/2/family-book?find=1&provider_id=1&date={self.day}&m1=1&d1=30&m2=1&d2=45")
        self.assertIn("08:00", page)
        r = self.post(c, "/patients/2/family-book", {"provider_id": "1", "date": self.day.isoformat(), "time": "08:00", "m1": "1", "d1": "30",
                                                       "m2": "1", "d2": "45", "category": "Consultation"})
        self.assertEqual(r.status_code, 302)
        rows = self.conn.execute("SELECT patient_id, start_at, duration_min FROM appointments ORDER BY start_at").fetchall()
        self.assertEqual([(r[0], r[1][-5:], r[2]) for r in rows], [(1, "08:00", 30), (2, "08:30", 45)])

    def test_board_and_stage_buttons(self):
        now = now_local(self.clinic["timezone"])
        self.conn.execute("""INSERT INTO appointments(clinic_id, patient_id, provider_id, chair_id, start_at, duration_min, status)
                             VALUES (1, 1, 1, 1, ?, 30, 'scheduled')""", (now.strftime("%Y-%m-%d %H:%M"),))
        self.conn.commit()
        aid = self.conn.execute("SELECT MAX(id) FROM appointments").fetchone()[0]
        c = self.login("desk")
        self.assertIn("Ama", self.page(c, "/board"))
        self.assertIn("Check in", self.page(c, f"/appointments/{aid}"))
        self.post(c, f"/appointments/{aid}/stage", {"action": "arrive"})
        self.post(c, f"/appointments/{aid}/stage", {"action": "seat", "back": f"/appointments/{aid}"})
        self.assertIn("In the chair", self.page(c, "/board"))
        r = self.post(c, f"/appointments/{aid}/stage", {"action": "arrive"}, follow_redirects=True)
        self.assertIn("does not fit", r.get_data(as_text=True))
        r = self.post(c, f"/appointments/{aid}/stage", {"action": "seat", "back": "//evil.example"})
        self.assertTrue(r.headers["Location"].endswith("/board"))

    def test_gap_screen(self):
        aid = S.book(self.conn, self.clinic, 1, 1, 1, 1, self.at("11:00"), 30)
        S.set_status(self.conn, 1, 1, aid, "broken", "missed")
        w = S.add_to_waiting_list(self.conn, self.clinic, 1, 2, "asap", duration=30)
        self.conn.commit()
        c = self.login("desk")
        self.assertIn("Fill the gap", self.page(c, f"/diary?date={self.day}"))
        self.assertIn("Kofi", self.page(c, f"/diary/gap/{aid}"))
        r = self.post(c, f"/diary/gap/{aid}", {"entry_id": str(w)})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.db().execute("SELECT start_at FROM appointments WHERE id = ?", (w,)).fetchone()[0][-5:], "11:00")
        r = self.post(c, f"/diary/gap/{aid}", {"entry_id": "99999"}, follow_redirects=True)
        self.assertIn("can no longer take", r.get_data(as_text=True))

    def test_week_view(self):
        S.book(self.conn, self.clinic, 1, 1, 1, 1, self.at("09:00"), 30)
        R.add_time_off(self.conn, self.clinic, 1, 1, (self.day + timedelta(days=2)).isoformat(), None, reason="Course")
        self.conn.commit()
        c = self.login("dent")
        page = self.page(c, f"/diary/week?provider_id=1&date={self.day + timedelta(days=3)}")
        self.assertIn("Ama Mensah", page)
        self.assertIn("Course", page)
        self.assertIn("30 of 540 minutes booked", page)
        self.assertEqual(c.get("/diary/week?provider_id=2").status_code, 404)          # another clinic's dentist

    def test_universal_numbering_on_screens_and_in_forms_but_not_the_api(self):
        c = self.login("mgr")
        self.conn.execute("UPDATE clinics SET tooth_notation = 'universal' WHERE id = 1")
        self.conn.commit()
        code = self.conn.execute("SELECT id FROM procedure_codes WHERE clinic_id = 1 AND code = 'FILL'").fetchone()[0]
        r = self.post(c, "/patients/1/treatment", {"code_id": str(code), "tooth": "19", "surfaces": ["O"]})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.db().execute("SELECT tooth FROM procedures WHERE patient_id = 1").fetchone()[0], 36)
        page = self.page(c, "/patients/1/treatment")
        self.assertIn(">19 O<", page.replace("\n", "").replace("</td>", "<").replace("<td>", ">"))
        page = self.page(c, "/patients/1/chart?tooth=36")
        self.assertIn('value="19"', page)
        r = self.post(c, "/patients/1/chart/conditions", {"tooth": "19", "condition": "missing"})
        self.assertIn("tooth=36", r.headers["Location"])
        r = self.post(c, "/patients/1/treatment", {"code_id": str(code), "tooth": "36"}, follow_redirects=True)
        self.assertIn("Universal system", r.get_data(as_text=True))
        self.post(c, "/settings", {"action": "clinic", "name": "Clinic One", "open_time": "08:00", "close_time": "17:00", "slot_minutes": "30",
                                   "days": ["1", "2", "3", "4", "5", "6"], "country": "GH", "currency": "GHS", "timezone": "Africa/Accra",
                                   "language": "en", "tooth_notation": "palmer"})
        self.assertEqual(self.db().execute("SELECT tooth_notation FROM clinics WHERE id = 1").fetchone()[0], "palmer")
        self.assertIn("LL6", self.page(c, "/patients/1/treatment"))

    def test_french_board(self):
        self.conn.execute("UPDATE clinics SET language = 'fr' WHERE id = 1")
        self.conn.commit()
        page = self.page(self.login("desk"), "/board")
        self.assertIn("Tableau des arrivées", page)
        self.assertIn("Au fauteuil", page)


if __name__ == "__main__":
    unittest.main()
