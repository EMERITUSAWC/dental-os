"""v0.23: US electronic claims. Claim files (837D) are built and read back segment by segment; remittances (835), cover check
answers (271) and receipts (999) are written here the way payers and clearinghouses send them."""
import datetime

from tests.test_v04 import Base
from dental_os.services import edi as E, insurance as INS, treatment as TR
from dental_os.services.timeutil import now_local

MGR2 = 5          # the manager of clinic two (US)


def isa(kind_gs="HP", st="835", body=()):
    head = ("ISA*00*          *00*          *ZZ*CLEARHOUSE     *ZZ*SUBMIT01       *261020*1200*^*00501*000000777*0*T*:~"
            f"GS*{kind_gs}*CLEARHOUSE*SUBMIT01*20261020*1200*777*X*005010~ST*{st}*0001~")
    return head + "~".join(body) + "~SE*9*0001~GE*1*777~IEA*1*000000777~"


class Edi(Base):
    def setUp(self):
        super().setUp()
        self.conn = self.db()
        TR.ensure_catalogue(self.conn, 2)
        self.conn.execute("UPDATE procedure_codes SET fee_p = 15000 WHERE clinic_id = 2")
        self.conn.execute("UPDATE patients SET dob = '1984-03-09', gender = 'Female' WHERE id = 3")
        self.conn.execute("INSERT INTO payers(clinic_id, name, kind) VALUES (2, 'Delta Test Dental', 'insurer')")
        self.payer = self.conn.execute("SELECT id FROM payers WHERE clinic_id = 2").fetchone()[0]
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def c(self):
        return dict(self.conn.execute("SELECT * FROM clinics WHERE id = 2").fetchone())

    def code(self, code):
        return self.conn.execute("SELECT id FROM procedure_codes WHERE clinic_id = 2 AND code = ?", (code,)).fetchone()[0]

    def set_up_all(self):
        E.save_settings(self.conn, self.c(), MGR2, {"submitter_id": "SUBMIT01", "submitter_name": "Smile Dental", "contact_name": "Front Desk",
                                                    "contact_phone": "(202) 555-0100", "receiver_id": "CLEARHOUSE", "receiver_name": "Clear House",
                                                    "billing_npi": "1234567893", "tax_id": "12-3456789", "taxonomy": "1223g0001x",
                                                    "billing_address": "1 Main St", "billing_city": "Austin", "billing_state": "tx",
                                                    "billing_zip": "78701-1234", "usage": "T"})
        codes = {self.code(k): v for k, v in (("CONS", "D0150"), ("FILL", "D2391"), ("SCALE", "D1110"))}
        E.save_codes(self.conn, self.c(), MGR2, codes, {2: "1234567893"}, {self.payer: "DTD01"})
        cover = INS.add_cover(self.conn, self.c(), MGR2, 3, self.payer, "W123456789", "Zed Other", 80, None, None, None)
        E.save_cover_details(self.conn, self.c(), MGR2, cover, {"relationship": "self", "group_no": "G-77", "holder_address": "9 Elm Rd",
                                                                "holder_city": "Austin", "holder_state": "TX", "holder_zip": "78702"})
        self.conn.commit()
        return cover

    def claim(self, cover, codes=(("FILL", 36, "MO"), ("CONS", None, None)), kind="claim"):
        ids = []
        for code, tooth, surf in codes:
            pr = TR.add_planned(self.conn, self.c(), MGR2, 3, self.code(code), tooth=tooth, surfaces=surf, provider_id=2)
            if kind == "claim":
                TR.mark_done(self.conn, self.c(), MGR2, pr)
            ids.append(pr)
        cl = INS.create_claim(self.conn, self.c(), MGR2, 3, cover, ids, kind)
        self.conn.commit()
        return cl


class CheckTests(Edi):
    def test_npi_check_digit_and_amounts(self):
        self.assertTrue(E.npi_ok("1234567893"))           # the example NPI in the US government's published check-digit method
        self.assertFalse(E.npi_ok("1234567890"))
        self.assertFalse(E.npi_ok("123456789"))
        self.assertEqual([E.amount(x) for x in (15000, 15050, 15055, 5, 0)], ["150", "150.5", "150.55", "0.05", "0"])
        self.assertEqual([E.to_minor(x) for x in ("150", "150.5", "150.55", "-12.30")], [15000, 15050, 15055, -1230])
        self.assertEqual(E.clean("O'Neil*Smith~ & Co:"), "O'NEIL SMITH & CO")

    def test_setup_is_checked(self):
        bad = [{"billing_npi": "1234567890"}, {"tax_id": "123"}, {"taxonomy": "1223"}, {"billing_state": "Texas"},
               {"billing_zip": "78701"}, {"submitter_id": "a b"}, {"usage": "X"}]
        for b in bad:
            form = {"usage": "T"} | b
            with self.assertRaises(E.EdiError):
                E.save_settings(self.conn, self.c(), MGR2, form)
        with self.assertRaises(E.EdiError):
            E.save_codes(self.conn, self.c(), MGR2, {self.code("FILL"): "2391"}, {}, {})
        with self.assertRaises(E.EdiError):
            E.save_codes(self.conn, self.c(), MGR2, {}, {2: "1111111111"}, {})

    def test_a_claim_that_is_not_ready_says_why(self):
        cover = INS.add_cover(self.conn, self.c(), MGR2, 3, self.payer, "", "Zed Other", 80, None, None, None)
        cl = self.claim(cover)
        msgs = " ".join(E.claim_problems(self.conn, self.c(), cl))
        for word in ("payer ID", "member ID", "US claim details", "CDT code"):
            self.assertIn(word, msgs)
        with self.assertRaises(E.EdiError) as e:
            E.build_837(self.conn, self.c(), MGR2, [cl])
        self.assertIn("set-up is missing", str(e.exception))


class ClaimFileTests(Edi):
    def segments(self, fid):
        body = E.get_file(self.conn, 2, fid)["body"]
        segs, comp = E.parse(body)
        return body, segs

    def test_the_837_has_the_right_shape(self):
        cover = self.set_up_all()
        cl = self.claim(cover)
        fid = E.build_837(self.conn, self.c(), MGR2, [cl], now=datetime.datetime(2026, 10, 20, 9, 30))
        body, segs = self.segments(fid)
        isa_line = body.split("~")[0]
        self.assertEqual(len(isa_line), 105)                       # ISA is fixed width: 106 with the terminator
        self.assertEqual(isa_line[103], "*")
        self.assertEqual(isa_line[104], ":")
        tags = [s[0] for s in segs]
        self.assertEqual(tags[:3], ["ISA", "GS", "ST"])
        self.assertEqual(tags[-3:], ["SE", "GE", "IEA"])
        by = {}
        for s in segs:
            by.setdefault(s[0], []).append(s)
        st, se = by["ST"][0], by["SE"][0]
        self.assertEqual(st, ["ST", "837", "0001", "005010X224A2"])
        self.assertEqual(int(se[1]), tags.index("SE") - tags.index("ST") + 1)       # SE counts ST to SE
        self.assertEqual(by["GS"][0][1], "HC")
        self.assertEqual(by["GS"][0][8], "005010X224A2")
        self.assertEqual(by["BHT"][0][1:3], ["0019", "00"])
        self.assertEqual([h[1:] for h in by["HL"]], [["1", "", "20", "1"], ["2", "1", "22", "0"]])
        nm1 = {s[1]: s for s in by["NM1"]}
        self.assertEqual(nm1["85"][-2:], ["XX", "1234567893"])
        self.assertEqual(nm1["IL"][3:5], ["OTHER", "ZED"])
        self.assertEqual(nm1["IL"][-1], "W123456789")
        self.assertEqual(nm1["PR"][-1], "DTD01")
        self.assertEqual(nm1["82"][-1], "1234567893")
        self.assertEqual(by["SBR"][0][1:4], ["P", "18", "G-77"])
        self.assertEqual(by["SBR"][0][9], "CI")
        self.assertIn(["REF", "EI", "123456789"], by["REF"])
        self.assertIn(["N4", "AUSTIN", "TX", "787011234"], by["N4"])
        self.assertEqual(by["DMG"][0], ["DMG", "D8", "19840309", "F"])
        clm = by["CLM"][0]
        self.assertEqual(clm[1], "C1")
        self.assertEqual(clm[2], "300")
        self.assertEqual(clm[5], "11:B:1")
        self.assertEqual([s[1:3] for s in by["SV3"]], [["AD:D2391", "150"], ["AD:D0150", "150"]])
        self.assertEqual(by["SV3"][0][6], "1")
        self.assertEqual(by["TOO"], [["TOO", "JP", "19", "M:O"]])        # FDI 36 is tooth 19 in the US numbering
        self.assertEqual(len(by["DTP"]), 2)
        self.assertEqual(by["LX"], [["LX", "1"], ["LX", "2"]])
        self.assertEqual(INS.get_claim(self.conn, 2, cl)["status"], "submitted")
        fid2 = E.build_837(self.conn, self.c(), MGR2, [cl])
        self.assertEqual(E.get_file(self.conn, 2, fid2)["control_no"], E.get_file(self.conn, 2, fid)["control_no"] + 1)

    def test_dependant_and_predetermination(self):
        cover = self.set_up_all()
        E.save_cover_details(self.conn, self.c(), MGR2, cover, {"relationship": "child", "holder_first": "Mary", "holder_last": "Other",
                                                                "holder_address": "9 Elm Rd", "holder_city": "Austin", "holder_state": "TX",
                                                                "holder_zip": "78702"})
        cl = self.claim(cover, (("FILL", 85, "O"),), kind="preauth")
        fid = E.build_837(self.conn, self.c(), MGR2, [cl])
        body, segs = self.segments(fid)
        hls = [s[1:] for s in segs if s[0] == "HL"]
        self.assertEqual(hls, [["1", "", "20", "1"], ["2", "1", "22", "1"], ["3", "2", "23", "0"]])
        self.assertIn(["PAT", "19"], segs)
        clm = next(s for s in segs if s[0] == "CLM")
        self.assertEqual(clm[19], "PB")
        self.assertIn(["TOO", "JP", "T", "O"], segs)                       # baby tooth 85 is T
        self.assertFalse([s for s in segs if s[0] == "DTP"])               # planned work has no service date
        sbr = next(s for s in segs if s[0] == "SBR")
        self.assertEqual(sbr[2], "")


class ReplyTests(Edi):
    def sent_claim(self):
        cover = self.set_up_all()
        cl = self.claim(cover)
        fid = E.build_837(self.conn, self.c(), MGR2, [cl])
        self.conn.commit()
        return cover, cl, fid

    def test_a_remittance_is_checked_then_posted_once(self):
        cover, cl, _ = self.sent_claim()
        claim = INS.get_claim(self.conn, 2, cl)
        paid = claim["estimate_p"] - 2000
        text = isa(body=(f"BPR*I*{E.amount(paid)}*C*ACH*CCP*01*999999999*DA*123456*1512345678**01*999988880*DA*98765*20261019",
                         "TRN*1*EFT0042*1512345678", "N1*PR*DELTA TEST DENTAL", "REF*2U*DTD01",
                         f"CLP*C{claim['claim_no']}*1*300*{E.amount(paid)}*40*12*PAYERCLM1", "CAS*CO*45*20",
                         f"SVC*AD:D2391*150*{E.amount(paid)}"))
        r = E.read_835(self.conn, self.c(), text)
        self.assertTrue(r["ok"])
        self.assertEqual(r["claims"][0]["writeoff_p"], 2000)
        self.assertEqual(r["paid_on"], "2026-10-19")
        out = E.post_835(self.conn, self.c(), MGR2, text)
        self.conn.commit()
        claim = INS.get_claim(self.conn, 2, cl)
        self.assertEqual((claim["status"], claim["paid_p"], claim["writeoff_p"]), ("paid", paid, 2000))
        pp = self.conn.execute("SELECT * FROM payer_payments WHERE id = ?", (out["payer_payment_id"],)).fetchone()
        self.assertEqual((pp["amount_p"], pp["reference"]), (paid, "EFT0042"))
        with self.assertRaises(E.EdiError):
            E.post_835(self.conn, self.c(), MGR2, text)                       # the same remittance twice
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM edi_files WHERE kind = '835'").fetchone()[0], 1)

    def test_a_denial_marks_the_claim_rejected_and_mismatches_are_refused(self):
        cover, cl, _ = self.sent_claim()
        no = INS.get_claim(self.conn, 2, cl)["claim_no"]
        text = isa(body=("BPR*H*0*C*NON************20261019", "TRN*1*DENY1*1512345678", "N1*PR*DELTA TEST DENTAL",
                         f"CLP*C{no}*4*300*0*0*12*PAYERCLM2", "CAS*CO*29*300"))
        E.post_835(self.conn, self.c(), MGR2, text)
        c = INS.get_claim(self.conn, 2, cl)
        self.assertEqual(c["status"], "rejected")
        self.assertIn("CO-29", c["note"])
        wrong = isa(body=("BPR*I*99*C*ACH", "TRN*1*X1", f"CLP*C{no + 50}*1*300*99*0*12*P"))
        r = E.read_835(self.conn, self.c(), wrong)
        self.assertFalse(r["ok"])
        self.assertIn("No claim", r["claims"][0]["problem"])
        with self.assertRaises(E.EdiError):
            E.post_835(self.conn, self.c(), MGR2, wrong)
        with self.assertRaises(E.EdiError):
            E.read_835(self.conn, self.c(), "hello")

    def test_receipt_and_cover_check(self):
        cover, cl, fid = self.sent_claim()
        ctl = E.get_file(self.conn, 2, fid)["control_no"]
        rec = isa("FA", "999", (f"AK1*HC*{ctl}*005010X224A2", "AK2*837*0001", "IK5*A", "AK9*A*1*1*1"))
        f_id, ok = E.post_999(self.conn, self.c(), MGR2, rec)
        self.assertTrue(ok)
        self.assertEqual(E.get_file(self.conn, 2, fid)["status"], "accepted")
        bad = isa("FA", "999", (f"AK1*HC*{ctl}*005010X224A2", "IK3*CLM*12**8", "AK9*R*1*1*0"))
        E.post_999(self.conn, self.c(), MGR2, bad)
        self.assertEqual(E.get_file(self.conn, 2, fid)["status"], "rejected")
        q = E.build_270(self.conn, self.c(), MGR2, cover)
        segs, _ = E.parse(E.get_file(self.conn, 2, q)["body"])
        self.assertIn(["EQ", "35"], segs)
        trn = next(s for s in segs if s[0] == "TRN")
        ans = isa("HB", "271", (f"TRN*2*{trn[2]}*1512345678", "EB*1**35", "EB*C*IND*35***23*50", "EB*A*IND*35*****0.2"))
        E.post_271(self.conn, self.c(), MGR2, ans)
        note = INS.get_cover(self.conn, 2, cover)["verify_note"]
        self.assertIn("Cover is active.", note)
        self.assertIn("Deductible: 50", note)
        self.assertIn("pays 20%", note)


class ScreenTests(Edi):
    def test_screens_for_a_us_clinic(self):
        cover = self.set_up_all()
        self.claim(cover)
        c = self.login("mgr2")
        page = self.page(c, "/claims/electronic")
        self.assertIn("Make the claim file", page)
        for url in ("/claims/electronic/setup", "/claims/electronic/import", f"/cover/{cover}/us-details", "/claims"):
            self.page(c, url)
        self.assertIn("/claims/electronic", self.page(c, "/claims"))
        cl = self.conn.execute("SELECT id FROM claims WHERE clinic_id = 2").fetchone()[0]
        r = self.post(c, "/claims/electronic", {"claim_id": str(cl)}, follow_redirects=True)
        self.assertIn("Claim file made.", r.get_data(as_text=True))
        fid = self.conn.execute("SELECT id FROM edi_files").fetchone()[0]
        d = c.get(f"/claims/electronic/files/{fid}/download")
        self.assertTrue(d.get_data(as_text=True).startswith("ISA*00*"))
        self.assertIn("attachment", d.headers["Content-Disposition"])
        self.assertEqual(self.login("mgr").get(f"/claims/electronic/files/{fid}").status_code, 404)       # clinic one cannot see it
        self.assertNotIn("/claims/electronic", self.page(self.login("mgr"), "/claims"))                   # not a US clinic
