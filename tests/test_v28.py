"""v0.28: the report builder: only listed data sets and columns, values as parameters, always one clinic."""
import json

from tests.test_v05 import Money
from dental_os.services import billing, reportbuilder as RB, treatment as TR


class Builder(Money):
    def setUp(self):
        super().setUp()
        self.conn = self.db()
        c = dict(self.conn.execute("SELECT * FROM clinics WHERE id = 1").fetchone())
        for pid, code in ((1, "FILL"), (1, "CONS"), (2, "CROWN")):
            pr = TR.add_planned(self.conn, c, 1, pid, self.cid(code), provider_id=1)
            TR.mark_done(self.conn, c, 1, pr)
        billing.record_payment(self.conn, c, 1, 1, 5000, "cash")
        self.conn.commit()
        self.mgr = self.conn.execute("SELECT * FROM users WHERE username = 'mgr'").fetchone()
        self.desk = self.conn.execute("SELECT * FROM users WHERE username = 'desk'").fetchone()

    def tearDown(self):
        self.conn.close()

    def rep(self, user=None, **spec):
        return RB.run(self.conn, user or self.mgr, spec)

    def test_list_filter_and_group(self):
        r = self.rep(dataset="treatment", columns=["patient", "code", "fee"], sort="fee", desc=True)
        self.assertEqual([row[1] for row in r["rows"]], ["CROWN", "FILL", "CONS"])
        self.assertEqual(r["head"][2], ("Fee", "money"))
        r = self.rep(dataset="treatment", columns=["code"], filters=[{"col": "fee", "op": "gte", "value": "200"}])
        self.assertEqual(sorted(x[0] for x in r["rows"]), ["CROWN", "FILL"])
        r = self.rep(dataset="treatment", group_by="patient", agg="sum", measure="fee", sort="_value", desc=True)
        self.assertEqual(r["rows"], [["Kofi Young", 150000], ["Ama Mensah", 40000]])
        r = self.rep(dataset="patients", columns=["first_name"], filters=[{"col": "last_name", "op": "contains", "value": "mens"}])
        self.assertEqual(r["rows"], [["Ama"]])
        r = self.rep(dataset="payments", group_by="method", agg="count")
        self.assertEqual(r["rows"], [["cash", 1]])

    def test_only_this_clinic_and_nothing_outside_the_lists(self):
        names = [row[0] for row in self.rep(dataset="patients", columns=["first_name"])["rows"]]
        self.assertNotIn("Zed", names)                                         # clinic two's patient
        for bad in ({"dataset": "users", "columns": ["id"]}, {"dataset": "patients", "columns": []},
                    {"dataset": "patients", "columns": ["id"], "filters": [{"col": "password_hash", "op": "eq", "value": "x"}]},
                    {"dataset": "patients", "columns": ["id"], "filters": [{"col": "id", "op": "drop", "value": "1"}]},
                    {"dataset": "patients", "group_by": "id", "agg": "sum", "measure": "first_name"},
                    {"dataset": "patients", "columns": ["id"], "sort": "1; DROP TABLE patients"}):
            with self.assertRaises(RB.ReportError):
                self.rep(**bad)
        r = self.rep(dataset="patients", columns=["first_name"], filters=[{"col": "first_name", "op": "eq", "value": "x' OR '1'='1"}])
        self.assertEqual(r["rows"], [])                                         # values are never part of the SQL
        r = self.rep(dataset="patients", columns=["id", "nope"])
        self.assertEqual(r["head"], [("Patient number", "number")])             # unknown columns are dropped
        with self.assertRaises(RB.ReportError):
            self.rep(self.desk, dataset="treatment", columns=["code"])          # clinical data needs clinical access

    def test_every_data_set_and_column_runs(self):
        for ds, (label, src, clin, cols) in RB.DATASETS.items():
            r = self.rep(dataset=ds, columns=list(cols))
            self.assertEqual(len(r["head"]), len(cols), ds)
            for c in cols:
                self.rep(dataset=ds, group_by=c, agg="count")

    def test_saved_reports_and_screens(self):
        c = dict(self.conn.execute("SELECT * FROM clinics WHERE id = 1").fetchone())
        rid = RB.save(self.conn, c, self.mgr, "Money by method", {"dataset": "payments", "group_by": "method", "agg": "sum", "measure": "amount"}, True)
        private = RB.save(self.conn, c, self.mgr, "Mine", {"dataset": "patients", "columns": ["first_name"]}, False)
        self.conn.commit()
        self.assertEqual(json.loads(RB.get(self.conn, 1, rid)["spec"])["measure"], "amount")
        mgr = self.login("mgr")
        page = self.page(mgr, "/reports/builder?dataset=treatment&col=code&col=fee&run=1")
        self.assertIn("CROWN", page)
        self.page(mgr, "/reports/builder?dataset=treatment&group_by=dentist&agg=sum&measure=fee&run=1")
        csv = mgr.get(f"/reports/saved/{rid}?format=csv").get_data(as_text=True)
        self.assertIn("cash,50.00", csv)
        desk = self.login("desk")
        self.assertIn("Money by method", self.page(desk, "/reports/saved"))
        self.assertNotIn("Mine", self.page(desk, "/reports/saved"))
        self.assertEqual(desk.get(f"/reports/saved/{private}").status_code, 404)
        self.assertEqual(desk.get("/reports/builder").status_code, 403)
        self.assertEqual(self.login("mgr2").get(f"/reports/saved/{rid}").status_code, 404)
        r = self.post(mgr, "/reports/builder/save", {"name": "Patients list", "dataset": "patients", "col": ["first_name", "last_name"]},
                      follow_redirects=True)
        self.assertIn("Report saved.", r.get_data(as_text=True))
        self.post(mgr, f"/reports/saved/{rid}/archive")
        self.assertNotIn("Money by method", self.page(mgr, "/reports/saved"))
