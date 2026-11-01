"""v0.22: sterilisation and instrument tracking: tests, cycles, packs, use on patients, failures and recalls."""
import datetime

from tests.test_v04 import Base
from dental_os.services import steri as ST
from dental_os.services.timeutil import now_local


class Steri(Base):
    def setUp(self):
        super().setUp()
        self.conn = self.db()

    def tearDown(self):
        self.conn.close()

    def c(self, n=1):
        return dict(self.conn.execute("SELECT * FROM clinics WHERE id = ?", (n,)).fetchone())

    def today(self):
        return now_local(self.c()["timezone"]).date()

    def machine(self, kind="vacuum"):
        sid = ST.save_steriliser(self.conn, self.c(), 1, "Autoclave 1", kind, "SN-1")
        self.conn.commit()
        return sid

    def daily(self, sid, kinds=("act", "steam_penetration")):
        for k in kinds:
            ST.record_test(self.conn, self.c(), 3, sid, k, "pass")
        self.conn.commit()

    def cycle(self, sid, packs=(("Exam kit", 3),), **kw):
        args = dict(mechanical_ok=True, indicator_ok=True, temp_c="134", pressure_kpa="210", hold_min="3.5")
        args.update(kw)
        cid = ST.record_cycle(self.conn, self.c(), 3, sid, list(packs), **args)
        self.conn.commit()
        return cid

    def codes(self, cycle_id):
        return [k["code"] for k in ST.packs_of(self.conn, 1, cycle_id)]

    def status(self, code):
        return ST.find_pack(self.conn, 1, code)["status"]


class DailyTests(Steri):
    def test_no_cycle_until_todays_tests_pass(self):
        sid = self.machine("vacuum")
        with self.assertRaises(ST.SteriError) as e:
            self.cycle(sid)
        self.assertIn("today's tests", str(e.exception))
        ST.record_test(self.conn, self.c(), 3, sid, "act", "pass")
        with self.assertRaises(ST.SteriError):
            self.cycle(sid)                          # a vacuum machine also needs its steam penetration test
        ST.record_test(self.conn, self.c(), 3, sid, "steam_penetration", "pass")
        self.cycle(sid)
        due = [d["kind"] for d in ST.checks_due(self.conn, self.c(), ST.get_steriliser(self.conn, 1, sid))]
        self.assertEqual(sorted(due), ["air_leakage", "spore", "thermometric"])

    def test_a_non_vacuum_machine_needs_only_the_control_test_and_the_rule_can_be_off(self):
        sid = self.machine("non_vacuum")
        self.daily(sid, ("act",))
        self.cycle(sid)
        sid2 = ST.save_steriliser(self.conn, self.c(), 1, "Autoclave 2", "vacuum")
        ST.save_settings(self.conn, self.c(), 1, False, "60")
        self.cycle(sid2)                              # the clinic switched the rule off
        self.assertEqual(ST.settings(self.conn, 1), {"require_daily": False, "shelf_days": 60})

    def test_bad_input_is_refused(self):
        sid = self.machine()
        self.daily(sid)
        for kw, packs in ((dict(temp_c="1340"), [("Exam kit", 1)]), (dict(hold_min="abc"), [("Exam kit", 1)]), ({}, []),
                          ({}, [("Kit", 101)])):
            with self.assertRaises(ST.SteriError):
                self.cycle(sid, packs, **kw)
        with self.assertRaises(ST.SteriError):
            ST.record_test(self.conn, self.c(), 3, sid, "spore", "pass")             # a spore test needs its lot number
        with self.assertRaises(ST.SteriError):
            ST.record_test(self.conn, self.c(), 3, sid, "act", "pending")            # only a spore test can wait
        with self.assertRaises(ST.SteriError):
            ST.save_steriliser(self.conn, self.c(), 1, "", "vacuum")
        with self.assertRaises(ST.SteriError):
            ST.save_settings(self.conn, self.c(), 1, True, "0")


class PackTests(Steri):
    def test_packs_get_codes_and_a_use_by_date_and_are_used_once(self):
        sid = self.machine()
        self.daily(sid)
        cyc = self.cycle(sid, (("Exam kit", 2), ("Extraction set", 1)))
        codes = self.codes(cyc)
        self.assertEqual(codes, [f"S{sid}-00001-001", f"S{sid}-00001-002", f"S{sid}-00001-003"])
        k = ST.find_pack(self.conn, 1, codes[0].lower())                        # typing in small letters works
        self.assertEqual(k["expires_on"], (self.today() + datetime.timedelta(days=90)).isoformat())
        c = ST.get_cycle(self.conn, 1, cyc)
        self.assertEqual((c["temp_tenths"], c["pressure_kpa"], c["hold_seconds"], c["result"]), (1340, 210, 210, "pass"))
        ST.use_pack(self.conn, self.c(), 2, codes[0], 1)
        self.conn.commit()
        with self.assertRaises(ST.SteriError) as e:
            ST.use_pack(self.conn, self.c(), 2, codes[0], 1)
        self.assertIn("already used", str(e.exception))
        with self.assertRaises(ST.SteriError):
            ST.use_pack(self.conn, self.c(), 2, codes[1], 3)                    # another clinic's patient
        with self.assertRaises(ST.SteriError):
            ST.use_pack(self.conn, self.c(2), 5, codes[1], 3)                   # another clinic cannot see the pack
        self.assertEqual([r["code"] for r in ST.for_patient(self.conn, 1, 1)], [codes[0]])
        dent = self.login("dent")
        self.assertIn(codes[0], self.page(dent, "/patients/1/instruments"))
        page = self.page(dent, f"/steri/cycles/{cyc}/labels")
        self.assertIn("<svg", page)
        self.assertNotIn(codes[0], page)                                        # a used pack needs no label
        self.assertIn(codes[1], page)

    def test_out_of_date_and_sent_back_packs_are_refused(self):
        sid = self.machine()
        self.daily(sid)
        codes = self.codes(self.cycle(sid))
        self.conn.execute("UPDATE steri_packs SET expires_on = '2020-01-01' WHERE code = ?", (codes[0],))
        with self.assertRaises(ST.SteriError) as e:
            ST.use_pack(self.conn, self.c(), 2, codes[0], 1)
        self.assertIn("use-by", str(e.exception))
        ST.send_back(self.conn, self.c(), 3, codes[1], "wrap torn")
        with self.assertRaises(ST.SteriError):
            ST.use_pack(self.conn, self.c(), 2, codes[1], 1)
        with self.assertRaises(ST.SteriError):
            ST.use_pack(self.conn, self.c(), 2, "NOT-A-CODE", 1)

    def test_a_failed_cycle_makes_unusable_packs(self):
        sid = self.machine()
        self.daily(sid)
        cyc = self.cycle(sid, indicator_ok=False)
        self.assertEqual(ST.get_cycle(self.conn, 1, cyc)["result"], "fail")
        code = self.codes(cyc)[0]
        self.assertEqual(self.status(code), "failed")
        with self.assertRaises(ST.SteriError) as e:
            ST.use_pack(self.conn, self.c(), 2, code, 1)
        self.assertIn("cycle failed", str(e.exception))

    def test_an_implant_load_is_held_until_its_spore_test_passes(self):
        sid = self.machine()
        self.daily(sid)
        cyc = self.cycle(sid, (("Implant kit", 1),), implants=True)
        code = self.codes(cyc)[0]
        self.assertEqual((ST.get_cycle(self.conn, 1, cyc)["result"], self.status(code)), ("held", "held"))
        with self.assertRaises(ST.SteriError):
            ST.use_pack(self.conn, self.c(), 2, code, 1)
        tid = ST.record_test(self.conn, self.c(), 3, sid, "spore", "pending", "LOT9", cycle_id=cyc)
        self.assertEqual(self.status(code), "held")
        ST.read_result(self.conn, self.c(), 3, tid, "pass")
        self.assertEqual((ST.get_cycle(self.conn, 1, cyc)["result"], self.status(code)), ("pass", "stored"))
        ST.use_pack(self.conn, self.c(), 2, code, 1)


class FailureTests(Steri):
    def test_a_failed_spore_test_takes_it_out_and_recalls_packs_since_the_last_pass(self):
        sid = self.machine()
        self.daily(sid)
        old = self.codes(self.cycle(sid))
        self.conn.execute("UPDATE steri_cycles SET started_at = '2026-01-01 09:00'")             # made before the last good spore test
        self.conn.execute("UPDATE steri_packs SET packed_at = '2026-01-01 09:00'")
        ST.record_test(self.conn, self.c(), 3, sid, "spore", "pass", "LOT1")
        self.conn.execute("UPDATE steri_tests SET done_at = '2026-01-02 09:00' WHERE kind = 'spore'")
        new = self.codes(self.cycle(sid))
        ST.use_pack(self.conn, self.c(), 2, new[0], 1)
        tid = ST.record_test(self.conn, self.c(), 3, sid, "spore", "pending", "LOT2")
        ST.read_result(self.conn, self.c(), 3, tid, "fail")
        self.conn.commit()
        s = ST.get_steriliser(self.conn, 1, sid)
        self.assertEqual((s["status"], s["out_test"]), ("out", "spore"))
        self.assertEqual([self.status(c) for c in new], ["used", "recalled", "recalled"])
        self.assertEqual([self.status(c) for c in old], ["stored"] * 3)                       # before the last pass: safe
        rec = self.conn.execute("SELECT id FROM steri_recalls").fetchone()[0]
        data = ST.recall(self.conn, 1, rec)
        self.assertEqual([k["used_on"] for k in data["used"]], [1])
        self.assertEqual(len(data["pulled"]), 2)
        with self.assertRaises(ST.SteriError) as e:
            self.cycle(sid)
        self.assertIn("out of service", str(e.exception))
        with self.assertRaises(ST.SteriError):
            ST.return_to_service(self.conn, self.c(), 1, sid, "checked")                    # needs a passing spore test first
        ST.record_test(self.conn, self.c(), 3, sid, "spore", "pass", "LOT3")
        ST.return_to_service(self.conn, self.c(), 1, sid, "Door seal replaced.")
        self.conn.commit()
        self.assertEqual(ST.get_steriliser(self.conn, 1, sid)["status"], "in")
        page = self.page(self.login("mgr"), f"/steri/recalls/{rec}")
        self.assertIn("Ama Mensah", page)
        with self.assertRaises(ST.SteriError):
            ST.close_recall(self.conn, self.c(), 1, rec, "")
        ST.close_recall(self.conn, self.c(), 1, rec, "Packs re-sterilised. Patient reviewed.")
        self.assertIsNotNone(self.conn.execute("SELECT closed_at FROM steri_recalls").fetchone()[0])

    def test_two_failed_spore_tests_in_a_row_need_three_passes(self):
        sid = self.machine()
        for _ in range(2):
            ST.record_test(self.conn, self.c(), 3, sid, "spore", "fail", "L")
        s = ST.get_steriliser(self.conn, 1, sid)
        self.assertEqual(ST.passes_needed(s), 3)
        for i in range(3):
            self.assertFalse(ST.can_return(ST.get_steriliser(self.conn, 1, sid)))
            with self.assertRaises(ST.SteriError):
                ST.return_to_service(self.conn, self.c(), 1, sid, "Engineer visit")
            ST.record_test(self.conn, self.c(), 3, sid, "spore", "pass", "L")
        ST.return_to_service(self.conn, self.c(), 1, sid, "Engineer visit")
        self.assertEqual(ST.get_steriliser(self.conn, 1, sid)["spore_fails"], 0)

    def test_a_failed_daily_test_takes_it_out_until_that_test_passes(self):
        sid = self.machine()
        ST.record_test(self.conn, self.c(), 3, sid, "act", "fail")
        s = ST.get_steriliser(self.conn, 1, sid)
        self.assertEqual((s["status"], s["out_test"]), ("out", "act"))
        ST.record_test(self.conn, self.c(), 3, sid, "spore", "pass", "L")                     # a different test does not count
        self.assertFalse(ST.can_return(ST.get_steriliser(self.conn, 1, sid)))
        ST.record_test(self.conn, self.c(), 3, sid, "act", "pass")
        ST.return_to_service(self.conn, self.c(), 1, sid, "Re-run after cleaning the chamber")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM steri_recalls").fetchone()[0], 0)


class ScreenTests(Steri):
    def test_screens_render_and_front_desk_is_kept_out(self):
        sid = self.machine()
        self.daily(sid)
        cyc = self.cycle(sid, implants=True)
        code = self.codes(cyc)[0]
        mgr = self.login("mgr")
        for url in ("/steri", "/steri/setup", f"/steri/sterilisers/{sid}", "/steri/cycles/new", "/steri/cycles", f"/steri/cycles/{cyc}",
                    "/steri/shelf", "/steri/shelf?status=held", f"/steri/packs?code={code}", "/patients/1/instruments", "/"):
            self.page(mgr, url)
        self.assertIn("Autoclave 1", self.page(mgr, "/"))                 # the Today screen warns about missing weekly checks
        desk = self.login("desk")
        for url in ("/steri", "/steri/cycles", "/patients/1/instruments"):
            self.assertEqual(desk.get(url).status_code, 403)
        self.assertEqual(self.login("asst").get("/steri/setup").status_code, 403)
        self.assertEqual(self.login("mgr2").get(f"/steri/cycles/{cyc}").status_code, 404)

    def test_the_cycle_form_and_pack_use_through_the_screens(self):
        sid = self.machine("non_vacuum")
        asst = self.login("asst")
        r = self.post(asst, "/steri/tests", {"sid": sid, "kind": "act", "result": "pass"}, follow_redirects=True)
        self.assertIn("Test recorded.", r.get_data(as_text=True))
        r = self.post(asst, "/steri/cycles/new", {"sid": sid, "what0": "Exam kit", "count0": "2", "mechanical_ok": "1", "indicator_ok": "1",
                                                  "temp_c": "121", "hold_min": "15"}, follow_redirects=True)
        self.assertIn("Print the labels", r.get_data(as_text=True))
        code = self.conn.execute("SELECT code FROM steri_packs ORDER BY id LIMIT 1").fetchone()[0]
        r = self.post(asst, "/patients/1/instruments", {"code": code}, follow_redirects=True)
        self.assertIn("Pack recorded on this patient.", r.get_data(as_text=True))
        r = self.post(asst, "/patients/1/instruments", {"code": code}, follow_redirects=True)
        self.assertIn("already used", r.get_data(as_text=True))
