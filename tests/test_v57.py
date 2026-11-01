"""v0.57: speed with a large clinic.

The faster code must give exactly the answers the slow code gave, so most of these compare the new way with the
patient-by-patient way on the same made-up records. tools/bigclinic.py does the full-size timing (20,000 patients).
"""
import random
import sys
import time
import unittest
from datetime import date, timedelta
from pathlib import Path

from tests.test_v04 import Base
from tests.dbmeta import ON_PG
from dental_os.services import ageing, billing, merge, privacy, reactivation, recalls

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import bigclinic  # noqa: E402


class Seeded(Base):
    SCALE = 0.02            # 400 patients, 2,000 appointments: enough variety, quick to make

    def setUp(self):
        super().setUp()
        self.conn = self.db()
        rng = random.Random(57)
        self.pids = bigclinic.seed(self.conn, 1, self.SCALE, rng)
        today = date.today()
        # Discounts, extra charges and money given back, so every part of a balance is used.
        for i, pid in enumerate(rng.sample(self.pids, 80)):
            self.conn.execute("INSERT INTO adjustments(clinic_id, patient_id, amount_p, reason, adjusted_on) VALUES (1,?,?,?,?)",
                              (pid, rng.choice([-1, 1]) * rng.randint(1, 50) * 1000, "correction", (today - timedelta(days=rng.randint(0, 400))).isoformat()))
        for i, pid in enumerate(rng.sample(self.pids, 30)):
            self.conn.execute("""INSERT INTO refunds(clinic_id, patient_id, refund_no, amount_p, method, reason, refunded_on, voided)
                                 VALUES (1,?,?,?, 'cash', 'Test', ?, ?)""", (pid, i + 1, rng.randint(1, 30) * 1000, today.isoformat(), 1 if i % 7 == 0 else 0))
        # Some patients with a credit and some with nothing at all.
        for pid in self.pids[:5]:
            self.conn.execute("""INSERT INTO payments(clinic_id, patient_id, amount_p, method, receipt_no, received_on)
                                 VALUES (1,?, 9000000, 'cash', ?, ?)""", (pid, 900000 + pid, today.isoformat()))
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def clinic(self):
        return self.conn.execute("SELECT * FROM clinics WHERE id = 1").fetchone()


class SameAnswers(Seeded):
    def test_all_balances_matches_one_patient_at_a_time(self):
        got = billing.all_balances(self.conn, 1)
        today = date.today()
        for pid in self.pids + [1, 2]:
            bal = billing.totals(self.conn, 1, pid)["balance"]
            unpaid = sum(ageing.patient_ageing(self.conn, 1, pid, today).values())
            self.assertEqual(got.get(pid, (0, 0)), (bal, unpaid), pid)
        self.assertTrue(any(b < 0 for b, _u in got.values()))            # the credits really are there
        self.assertTrue(any(b > 0 for b, _u in got.values()))

    def test_outstanding_is_the_biggest_balances(self):
        rows = billing.outstanding(self.conn, 1, 10)
        everyone = sorted((billing.totals(self.conn, 1, p)["balance"] for p in self.pids), reverse=True)
        self.assertEqual([b for b, _r in rows], [b for b in everyone if b > 0][:10])

    def test_credit_list_matches_one_patient_at_a_time(self):
        from dental_os.services import refunds
        got = {r["p"]["id"]: r["credit"] for r in refunds.credits(self.conn, 1)}
        expect = {p: -billing.totals(self.conn, 1, p)["balance"] for p in self.pids + [1, 2] if billing.totals(self.conn, 1, p)["balance"] < 0}
        self.assertEqual(got, expect)
        self.assertTrue(got)

    def test_duplicates_match_comparing_every_pair(self):
        people = self.conn.execute("SELECT id, first_name, last_name, phone, dob FROM patients WHERE clinic_id = 1 AND status = 'active'").fetchall()
        # Make some real duplicates: a shared phone and a same-name-same-birthday pair.
        self.conn.execute("UPDATE patients SET phone = ? WHERE id = ?", (people[3]["phone"], people[4]["id"]))
        self.conn.execute("UPDATE patients SET first_name = ?, last_name = ?, dob = ? WHERE id = ?",
                          (people[5]["first_name"], people[5]["last_name"], people[5]["dob"], people[6]["id"]))
        self.conn.execute("INSERT INTO patient_not_duplicates(clinic_id, a_id, b_id) VALUES (1, ?, ?)", tuple(sorted((people[7]["id"], people[8]["id"]))))
        self.conn.execute("UPDATE patients SET phone = ? WHERE id = ?", (people[7]["phone"], people[8]["id"]))
        people = self.conn.execute("SELECT id, first_name, last_name, phone, dob FROM patients WHERE clinic_id = 1 AND status = 'active'").fetchall()
        expect = set()
        for a in people:
            for b in people:
                if b["id"] <= a["id"]:
                    continue
                if (a["phone"] and a["phone"] == b["phone"]) or (a["dob"] and a["dob"] == b["dob"] and a["first_name"].lower() == b["first_name"].lower()
                                                                  and a["last_name"].lower() == b["last_name"].lower()):
                    expect.add((a["id"], b["id"]))
        expect.discard(tuple(sorted((people[7]["id"], people[8]["id"]))))
        got = merge.candidates(self.conn, 1, limit=10 ** 6)
        self.assertEqual({(r["a_id"], r["b_id"]) for r in got}, expect)
        self.assertIn((people[3]["id"], people[4]["id"]), expect)
        why = {(r["a_id"], r["b_id"]): r["why"] for r in got}
        self.assertEqual(why[(people[3]["id"], people[4]["id"])], "phone")
        names = [(r["a_last"], r["a_first"]) for r in got]
        self.assertEqual(names, sorted(names))

    def test_retention_list_uses_the_latest_activity(self):
        self.conn.execute("UPDATE clinics SET retention_years = 3 WHERE id = 1")
        rows, cutoff = privacy.retention_candidates(self.conn, self.clinic(), limit=10 ** 6)
        for r in rows:
            self.assertLess(r["last_on"], cutoff)
            latest = max([x for x in (
                self.conn.execute("SELECT MAX(substr(start_at,1,10)) FROM appointments WHERE patient_id = ?", (r["id"],)).fetchone()[0],
                self.conn.execute("SELECT MAX(done_on) FROM procedures WHERE patient_id = ? AND status = 'done'", (r["id"],)).fetchone()[0],
                self.conn.execute("SELECT MAX(received_on) FROM payments WHERE patient_id = ?", (r["id"],)).fetchone()[0],
                (r["created_at"] or "")[:10]) if x])
            self.assertEqual(r["last_on"], latest)
            self.assertEqual(r["balance"], billing.totals(self.conn, 1, r["id"])["balance"])
        listed = {r["id"] for r in rows}
        for pid in self.pids:                                             # nobody old enough was missed
            if pid in listed:
                continue
            d = self.conn.execute("SELECT MAX(substr(start_at,1,10)) FROM appointments WHERE patient_id = ?", (pid,)).fetchone()[0]
            c = self.conn.execute("SELECT substr(created_at,1,10) FROM patients WHERE id = ?", (pid,)).fetchone()[0]
            self.assertTrue((d and d >= cutoff) or c >= cutoff or self.conn.execute(
                "SELECT 1 FROM procedures WHERE patient_id = ? AND done_on >= ? UNION SELECT 1 FROM payments WHERE patient_id = ? AND received_on >= ?",
                (pid, cutoff, pid, cutoff)).fetchone(), pid)


class Indexes(Base):
    @unittest.skipIf(ON_PG, "reads SQLite's own list of indexes")
    def test_every_patient_table_is_indexed_by_patient(self):
        conn = self.db()
        missing = []
        for (t,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall():
            cols = [r[1] for r in conn.execute(f"PRAGMA table_info({t})")]
            if "patient_id" not in cols:
                continue
            want = ["clinic_id", "patient_id"] if "clinic_id" in cols else ["patient_id"]
            ok = False
            for idx in conn.execute(f"PRAGMA index_list({t})").fetchall():
                icols = [r[2] for r in conn.execute(f"PRAGMA index_info({idx[1]})")]
                ok = ok or icols[:len(want)] == want or icols[:1] == ["patient_id"]
            if not ok:
                missing.append(t)
        conn.close()
        self.assertEqual(missing, [], "Add CREATE INDEX ... (clinic_id, patient_id) for these")

    @unittest.skipIf(ON_PG, "reads SQLite's query plan")
    def test_the_upcoming_booking_check_uses_the_patient_index(self):
        conn = self.db()
        plan = " ".join(str(r[3]) for r in conn.execute(
            """EXPLAIN QUERY PLAN SELECT 1 FROM recalls r WHERE r.clinic_id = 1 AND NOT EXISTS (SELECT 1 FROM appointments a
               WHERE a.clinic_id = r.clinic_id AND a.patient_id = r.patient_id AND a.status = 'scheduled' AND a.start_at >= '2026')"""))
        conn.close()
        self.assertIn("idx_appt_patient_status", plan)


@unittest.skipIf(ON_PG, "timings are for the SQLite a single clinic runs on")
class Speed(Seeded):
    SCALE = 0.25            # 5,000 patients. The old code took over a minute here on the slowest of these.

    def test_the_slow_lists_are_now_quick(self):
        clinic = self.clinic()
        today = date.today()
        jobs = {"ageing": lambda: ageing.report(self.conn, clinic), "duplicates": lambda: merge.candidates(self.conn, 1),
                "retention": lambda: privacy.retention_candidates(self.conn, clinic), "lapsed": lambda: reactivation.lapsed(self.conn, clinic),
                "recalls": lambda: recalls.upcoming(self.conn, 1, today), "owing": lambda: billing.outstanding(self.conn, 1, 10)}
        slow = {}
        for name, job in jobs.items():
            t = time.perf_counter()
            job()
            took = time.perf_counter() - t
            if took > 3:
                slow[name] = round(took, 1)
        self.assertEqual(slow, {})

    def test_the_recall_list_comes_in_pages(self):
        c = self.login("mgr")
        first = self.page(c, "/recalls")
        n = len(recalls.upcoming(self.conn, 1, date.today()))
        self.assertGreater(n, 200)
        self.assertEqual(first.count("recall_snooze") + first.count("/snooze"), 200)
        self.assertIn(f"of {n}", first)
        last = self.page(c, "/recalls?page=999")                          # past the end shows the last page
        self.assertEqual(last.count("/snooze"), n - 200 * ((n - 1) // 200))
