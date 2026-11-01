"""v0.48 and v0.49: moving records in from Open Dental, Dentrix and spreadsheets. The Open Dental files use its own table and
column names and its documented status numbers."""
import io
import re
import zipfile

from tests.test_v04 import Base
from dental_os.services import billing, migrate as MI


def zipped(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, text in files.items():
            z.writestr(name, text)
    return buf.getvalue()


OPEN_DENTAL = {
    "provider.csv": "ProvNum,Abbr,LName,FName\n1,DOC1,Asante,Kwame\n2,HYG,Owusu,Efua\n",
    "operatory.csv": "OperatoryNum,OpName,Abbrev\n1,Op 1,O1\n",
    "procedurecode.csv": "CodeNum,ProcCode,Descript,AbbrDesc\n10,D2391,Resin-based composite - one surface posterior,Comp1P\n11,D0120,Periodic oral evaluation,PerEx\n",
    "patient.csv": ("PatNum,LName,FName,MiddleI,Preferred,PatStatus,Gender,Position,Birthdate,Address,City,HmPhone,WkPhone,WirelessPhone,Guarantor,Email,MedUrgNote\n"
                    "100,Boateng,Kofi,,,0,0,1,1985-03-02,12 Ring Road,Accra,,,024 111 2223,100,kofi@example.com,Penicillin allergy\n"
                    "101,Boateng,Akua,,,0,1,2,2015-07-09,12 Ring Road,Accra,,,,100,,\n"
                    "102,Gone,Old,,,4,0,0,0001-01-01,,,,,,102,,\n"),
    "procedurelog.csv": ("ProcNum,PatNum,AptNum,ProcDate,ProcFee,Surf,ToothNum,ProcStatus,ProvNum,CodeNum\n"
                         "500,100,900,2026-09-01,350.00,O,19,2,1,10\n"
                         "501,100,0,2026-10-01,120.00,,,1,1,11\n"
                         "502,100,0,2020-01-01,0.00,MOD,3,4,0,10\n"
                         "503,101,0,2026-09-02,99.00,,,6,1,11\n"),
    "appointment.csv": ("AptNum,PatNum,AptStatus,Pattern,Op,ProvNum,AptDateTime,Note,ProcDescript\n"
                        "900,100,2,XXXXXX,1,1,2026-09-01 10:00:00,,Comp1P\n"
                        "901,101,1,XXXX//,1,2,2026-12-15 14:30:00,,PerEx\n"
                        "902,101,7,XX,1,2,2026-12-16 09:00:00,Call mum,\n"),
    "payment.csv": "PayNum,PayType,PayDate,PayAmt,CheckNum,PayNote,PatNum\n700,69,2026-09-01,200.00,,,100\n701,69,2026-09-05,-20.00,,refund,100\n",
    "adjustment.csv": "AdjNum,AdjDate,AdjAmt,PatNum,AdjType,AdjNote\n800,2026-09-02,-30.00,100,14,Senior discount\n",
    "recall.csv": "RecallNum,PatNum,DateDue,RecallTypeNum\n600,100,2027-03-01,1\n",
    "commlog.csv": "CommlogNum,PatNum,CommDateTime,Note\n650,100,2026-08-01 11:00:00,Called about the crown\n",
}

PLAIN = {
    "patients.csv": "Chart Number,Surname,First Name,Mobile,Date of Birth,Sex\nA1,Mensah,Yaw,0244556677,02/03/1990,M\nA2,Mensah,Abena,,15/11/1992,F\n",
    "treatment.csv": "Patient ID,Procedure Code,Description,Tooth,Surface,Status,Date,Fee\nA1,EXT,Extraction,36,,Done,01/10/2026,250\nA2,SCALE,Scale and polish,,,Planned,,180\n",
    "balances.csv": "Patient ID,Balance\nA1,100\nA2,-40\n",
    "mystery.csv": "Who,Owes\nA1,5\n",
}


class Move(Base):
    def setUp(self):
        super().setUp()
        self.conn = self.db()

    def tearDown(self):
        self.conn.close()

    def c(self):
        return dict(self.conn.execute("SELECT * FROM clinics WHERE id = 1").fetchone())

    def count(self, table):
        return self.conn.execute(f"SELECT COUNT(*) FROM {table} WHERE clinic_id = 1").fetchone()[0]


class OpenDentalTests(Move):
    def test_preview_keeps_nothing_and_the_import_is_right(self):
        files = MI.files_in("od.zip", zipped(OPEN_DENTAL))
        info = MI.describe(files)
        self.assertEqual({f["name"]: f["kind"] for f in info}["procedurelog.csv"], "treatment")
        self.assertTrue(all(f["source"] == "opendental" and not f["missing"] for f in info))
        before = self.count("patients")
        pre = MI.run(self.conn, self.c(), 1, files, notation="universal")
        self.assertEqual(self.count("patients"), before)
        self.assertEqual(pre["counts"]["patients"], 2)
        self.assertEqual(pre["skipped"]["Deleted in the old system"], 2)                      # the deleted patient and the deleted procedure
        res = MI.run(self.conn, self.c(), 1, files, notation="universal", keep=True)
        self.assertEqual(res["counts"], pre["counts"])
        kofi = self.conn.execute("SELECT * FROM patients WHERE first_name = 'Kofi' AND last_name = 'Boateng'").fetchone()
        akua = self.conn.execute("SELECT * FROM patients WHERE first_name = 'Akua'").fetchone()
        self.assertEqual((kofi["phone"], kofi["dob"], kofi["gender"]), ("+233241112223", "1985-03-02", "Male"))
        self.assertEqual(akua["guarantor_id"], kofi["id"])
        self.assertEqual(akua["phone"], "")
        self.assertEqual(res["no_phone"], 1)
        self.assertEqual(kofi["reminders_ok"], 0)                                               # consent is never brought across
        done = self.conn.execute("SELECT * FROM procedures WHERE patient_id = ? AND status = 'done'", (kofi["id"],)).fetchone()
        self.assertEqual((done["tooth"], done["surfaces"], done["fee_p"], done["done_on"]), (36, "O", 35000, "2026-09-01"))   # Universal 19 is FDI 36
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM procedures WHERE patient_id = ? AND status = 'planned'", (kofi["id"],)).fetchone()[0], 1)
        self.assertEqual(res["skipped"]["Existing work from before (not charged here)"], 1)
        appts = self.conn.execute("SELECT status, duration_min, start_at FROM appointments WHERE clinic_id = 1 ORDER BY id").fetchall()
        self.assertEqual([tuple(a) for a in appts], [("complete", 30, "2026-09-01 10:00"), ("scheduled", 30, "2026-12-15 14:30")])
        t = billing.totals(self.conn, 1, kofi["id"])
        self.assertEqual(t["balance"], 35000 - 20000 + 2000 - 3000)
        self.assertEqual(self.conn.execute("SELECT text FROM patient_alerts WHERE patient_id = ?", (kofi["id"],)).fetchone()[0], "Penicillin allergy")
        self.assertEqual(self.count("recalls"), 1)
        self.assertEqual(self.conn.execute("SELECT summary FROM contact_log WHERE patient_id = ?", (kofi["id"],)).fetchone()[0], "Called about the crown")
        again = MI.run(self.conn, self.c(), 1, files, keep=True)                               # the same files again: nothing doubles
        self.assertEqual(sum(again["counts"].values()), 0)
        self.assertEqual(again["already"]["patients"], 2)
        self.assertEqual(billing.totals(self.conn, 1, kofi["id"])["balance"], t["balance"])


class PlainTests(Move):
    def test_spreadsheet_layout_balances_and_matching_by_hand(self):
        files = MI.files_in("dentrix.zip", zipped(PLAIN))
        info = {f["name"]: f for f in MI.describe(files)}
        self.assertEqual(info["mystery.csv"]["kind"], None)
        res = MI.run(self.conn, self.c(), 1, files, notation="fdi", keep=True)
        yaw = self.conn.execute("SELECT * FROM patients WHERE first_name = 'Yaw'").fetchone()
        abena = self.conn.execute("SELECT * FROM patients WHERE first_name = 'Abena'").fetchone()
        self.assertEqual((yaw["dob"], yaw["phone"]), ("1990-03-02", "+233244556677"))          # day first, as Ghana writes dates
        self.assertEqual(billing.totals(self.conn, 1, yaw["id"])["balance"], 10000)           # the opening balance wins over the imported work
        self.assertEqual(billing.totals(self.conn, 1, abena["id"])["balance"], -4000)
        self.assertEqual(self.conn.execute("SELECT tooth FROM procedures WHERE patient_id = ?", (yaw["id"],)).fetchone()[0], 36)
        self.assertEqual(res["counts"]["treatment"], 2)
        idx = next(f["index"] for f in MI.describe(files) if f["name"] == "mystery.csv")
        res2 = MI.run(self.conn, self.c(), 1, files, {str(idx): {"kind": "balances", "map": {"0": "patient_id", "1": "balance"}}}, "fdi")
        self.assertEqual(res2["already"]["balances"], 3)                                     # both rows of balances.csv and the one matched by hand

    def test_bad_files(self):
        with self.assertRaises(MI.MigrateError):
            MI.files_in("x.zip", b"PK\x03\x04broken")
        with self.assertRaises(MI.MigrateError):
            MI.files_in("x.zip", zipped({"readme.md": "hello"}))


class ScreenTests(Move):
    def test_manager_uploads_previews_and_imports(self):
        mgr = self.login("mgr")
        self.assertIn("Move in from another system", self.page(mgr, "/tools/move-in"))
        tok = re.search(r'name="_csrf" value="([^"]+)"', self.page(mgr, "/tools/move-in")).group(1)
        r = mgr.post("/tools/move-in", data={"_csrf": tok, "file": (io.BytesIO(zipped(OPEN_DENTAL)), "od.zip")}, content_type="multipart/form-data")
        url = r.headers["Location"]
        page = self.page(mgr, url)
        self.assertIn("What would be brought in", page)
        self.assertIn("Open Dental layout", page)
        self.assertEqual(self.count("patients"), 2)
        page = self.post(mgr, url, {"what": "import", "notation": "universal"}, follow_redirects=True).get_data(as_text=True)
        self.assertIn("Imported.", page)
        self.assertIn("The uploaded file has been deleted.", page)
        self.assertEqual(self.count("patients"), 4)
        self.assertEqual(self.login("dent").get("/tools/move-in").status_code, 403)
        self.assertEqual(self.login("mgr2").get(url).status_code, 404)


class CommandTests(Move):
    def test_command_line_preview_then_import(self):
        import tempfile, os
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "od.zip")
            with open(path, "wb") as fh:
                fh.write(zipped(OPEN_DENTAL))
            out = self.app.test_cli_runner().invoke(args=["move-in", "--clinic", "1", "--user", "mgr", "--file", path]).output
            self.assertIn("Patients: 2 new", out)
            self.assertIn("Preview only", out)
            self.assertEqual(self.count("patients"), 2)
            out = self.app.test_cli_runner().invoke(args=["move-in", "--clinic", "1", "--user", "mgr", "--file", path, "--import"]).output
            self.assertIn("Imported.", out)
        conn = self.db()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM patients WHERE clinic_id = 1").fetchone()[0], 4)
        conn.close()
