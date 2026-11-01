"""v0.63: the live clearinghouse connection. A fake Stedi answers the way its API reference describes."""
import datetime
import json
import re

from tests.test_v23 import Edi, MGR2, isa
from dental_os.services import clearinghouse as CH, edi as E, insurance as INS

KEY = "test_stedi_key_0123456789"


def ack_277(ref, cat="A1", code="20"):
    return isa("HN", "277", ("BHT*0085*08*ACK1*20261031*1200*TH", "HL*1**20*1", "NM1*PR*2*STEDI*****PI*STEDI", "HL*2*1*21*1",
                             "NM1*41*2*SMILE DENTAL*****46*SUBMIT01", "HL*3*2*19*1", "NM1*85*2*SMILE DENTAL*****XX*1234567893",
                             "HL*4*3*PT", "NM1*QC*1*OTHER*ZED****MI*W123456789", f"TRN*2*{ref}",
                             f"STC*{cat}:{code}:PR*20261031*WQ*300********Checked"))


class FakeStedi:
    def __init__(self):
        self.calls, self.claim_answer, self.inbound = [], None, []
        self.eligibility_x12 = None

    def __call__(self, method, url, headers, body=None, timeout=40):
        self.calls.append((method, url, headers, body))
        if url == CH.STEDI["claim"]:
            x12 = json.loads(body)["x12"]
            ref = re.search(r"CLM\*(C\d+)\*", x12).group(1)
            return 200, json.dumps(self.claim_answer or {"status": "SUCCESS", "errors": [], "controlNumber": "000000001",
                                                         "claimReference": {"correlationId": "CORR-1", "patientControlNumber": ref},
                                                         "x12": ack_277(ref)})
        if url == CH.STEDI["eligibility"]:
            x12 = json.loads(body)["x12"]
            trn = re.search(r"TRN\*1\*([^*~]+)", x12).group(1)
            return 200, json.dumps({"id": "ec_1", "x12": self.eligibility_x12 or isa("HB", "271", (f"TRN*2*{trn}*1512345678", "EB*1**35"))})
        if url.startswith(CH.STEDI["poll"]):
            items = [{"transactionId": tid, "direction": "INBOUND", "x12": {"metadata": {"transaction": {"transactionSetIdentifier": kind}}},
                      "artifacts": [{"usage": "input", "artifactType": "application/edi-x12", "url": f"https://files.stedi.example/{tid}"}]}
                     for tid, kind, _raw in self.inbound]
            items.append({"transactionId": "out-1", "direction": "OUTBOUND", "x12": {"metadata": {"transaction": {"transactionSetIdentifier": "837"}}}})
            return 200, json.dumps({"items": items if "startDateTime" in url else [], "nextPageToken": "TOKEN-2"})
        if url.startswith("https://files.stedi.example/"):
            tid = url.rsplit("/", 1)[1]
            return 200, next(raw for t, _k, raw in self.inbound if t == tid)
        raise AssertionError(url)


class Live(Edi):
    def connect(self, fake):
        cover = self.set_up_all()
        self.app.config["CLEARINGHOUSE_TRANSPORT"] = fake
        CH.save_account(self.conn, self.c(), MGR2, "test", {"provider": "stedi", "api_key": KEY, "enabled": "1"})
        self.conn.commit()
        return cover

    def receiver(self, rid="STEDITEST"):
        self.conn.execute("UPDATE edi_settings SET receiver_id = ? WHERE clinic_id = 2", (rid,))
        self.conn.commit()


class AccountTests(Live):
    def test_the_key_is_kept_safe_and_the_receiver_must_match(self):
        fake = FakeStedi()
        self.connect(fake)
        self.assertTrue(CH.is_on(self.conn, 2))
        self.assertNotIn(KEY, self.conn.execute("SELECT api_key FROM clearinghouse_account").fetchone()[0])
        self.assertEqual(CH.setup_problems(self.conn, self.c()), ["For test files with Stedi, the receiver ID under Electronic claims, Settings must be STEDITEST."])
        self.receiver()
        self.assertEqual(CH.setup_problems(self.conn, self.c()), [])
        self.conn.execute("UPDATE edi_settings SET usage = 'P' WHERE clinic_id = 2")
        self.assertIn("must be STEDI.", CH.setup_problems(self.conn, self.c())[0])
        self.conn.commit()
        self.assertEqual(self.login("dent").get("/claims/electronic/connection").status_code, 403)
        page = self.page(self.login("mgr2"), "/claims/electronic/connection")
        self.assertNotIn(KEY, page)


class SendTests(Live):
    def test_a_claim_goes_and_its_first_acknowledgement_comes_back(self):
        fake = FakeStedi()
        cover = self.connect(fake)
        self.receiver()
        cl = self.claim(cover)
        mgr = self.login("mgr2")
        self.assertIn("Send to the clearinghouse", self.page(mgr, "/claims/electronic"))
        r = self.post(mgr, "/claims/electronic/send", {"claim_id": str(cl)})
        self.assertEqual(r.status_code, 302)
        method, url, headers, body = fake.calls[0]
        self.assertEqual((method, headers["Authorization"]), ("POST", KEY))
        self.assertTrue(headers["Idempotency-Key"].startswith("dos-2-claim-"))
        self.assertTrue(json.loads(body)["x12"].startswith("ISA"))
        c = INS.get_claim(self.conn, 2, cl)
        self.assertEqual(c["status"], "submitted")
        self.assertIn("Received", c["edi_ack"])
        f = self.conn.execute("SELECT status, summary FROM edi_files WHERE kind = '837D'").fetchone()
        self.assertEqual(f["status"], "accepted")
        self.assertIn("CORR-1", f["summary"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM edi_files WHERE kind = '277'").fetchone()[0], 1)
        self.assertIn("Claim acknowledgement", self.page(mgr, "/claims/electronic"))

    def test_a_refused_claim_is_not_marked_sent(self):
        fake = FakeStedi()
        fake.claim_answer = {"status": "ERROR", "errors": [{"code": "33", "description": "Subscriber date of birth is required"}], "httpStatusCode": "400 BAD_REQUEST"}
        cover = self.connect(fake)
        self.receiver()
        cl = self.claim(cover)
        mgr = self.login("mgr2")
        r = self.post(mgr, "/claims/electronic/send", {"claim_id": str(cl)}, follow_redirects=True)
        self.assertIn("No claim was sent", r.get_data(as_text=True))
        self.assertEqual(INS.get_claim(self.conn, 2, cl)["status"], "draft")                       # undone
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM edi_files WHERE kind = '837D'").fetchone()[0], 0)
        log = self.conn.execute("SELECT ok, detail FROM clearinghouse_log ORDER BY id DESC").fetchone()
        self.assertEqual(log["ok"], 0)
        self.assertIn("Subscriber date of birth is required", log["detail"])
        self.assertIn("Subscriber date of birth is required", self.page(mgr, "/claims/electronic"))

    def test_a_wrong_receiver_stops_sending(self):
        fake = FakeStedi()
        cover = self.connect(fake)
        cl = self.claim(cover)
        self.post(self.login("mgr2"), "/claims/electronic/send", {"claim_id": str(cl)})
        self.assertEqual(fake.calls, [])
        self.assertEqual(INS.get_claim(self.conn, 2, cl)["status"], "draft")
        self.assertIn("STEDITEST", self.conn.execute("SELECT detail FROM clearinghouse_log").fetchone()[0])

    def test_another_clinic_cannot_send_these_claims(self):
        fake = FakeStedi()
        cover = self.connect(fake)
        self.receiver()
        cl = self.claim(cover)
        self.post(self.login("mgr"), "/claims/electronic/send", {"claim_id": str(cl)})          # clinic one's manager
        self.assertEqual(fake.calls, [])
        self.assertEqual(INS.get_claim(self.conn, 2, cl)["status"], "draft")


class AckTests(Live):
    def test_reading_acknowledgements(self):
        acks = CH.read_277(ack_277("C7", "A7", "21"))
        self.assertEqual(len(acks), 1)
        self.assertEqual((acks[0]["ref"], acks[0]["accepted"], acks[0]["category"]), ("C7", False, "A7"))
        self.assertIn("not valid", acks[0]["text"])
        self.assertTrue(CH.read_277(ack_277("C8", "A2"))[0]["accepted"])
        with self.assertRaises(CH.ClearinghouseError):
            CH.read_277(isa("HB", "271", ("EB*1**35",)))

    def test_a_returned_claim_can_be_fixed_and_sent_again(self):
        fake = FakeStedi()
        cover = self.connect(fake)
        self.receiver()
        cl = self.claim(cover)
        self.post(self.login("mgr2"), "/claims/electronic/send", {"claim_id": str(cl)})
        no = INS.get_claim(self.conn, 2, cl)["claim_no"]
        fake.inbound = [("t-277", "277", ack_277(f"C{no}", "A6", "35"))]
        self.post(self.login("mgr2"), "/claims/electronic/fetch")
        c = INS.get_claim(self.conn, 2, cl)
        self.assertEqual(c["status"], "rejected")
        self.assertIn("information is missing", c["edi_ack"])
        self.assertIn(c["id"], [x["c"]["id"] for x in E.ready_claims(self.conn, self.c())])           # listed to fix and send again
        self.assertIn("Returned: fix and send again", self.page(self.login("mgr2"), "/claims/electronic"))
        self.post(self.login("mgr2"), "/claims/electronic/send", {"claim_id": str(cl)})
        self.assertEqual(INS.get_claim(self.conn, 2, cl)["status"], "submitted")


class FetchTests(Live):
    def test_remittances_wait_for_a_person_and_nothing_is_read_twice(self):
        fake = FakeStedi()
        cover = self.connect(fake)
        self.receiver()
        cl = self.claim(cover)
        mgr = self.login("mgr2")
        self.post(mgr, "/claims/electronic/send", {"claim_id": str(cl)})
        claim = INS.get_claim(self.conn, 2, cl)
        paid = claim["estimate_p"]
        remit = isa(body=(f"BPR*I*{E.amount(paid)}*C*ACH*CCP*01*999999999*DA*123456*1512345678**01*999988880*DA*98765*20261030",
                          "TRN*1*EFT0099*1512345678", "N1*PR*DELTA TEST DENTAL", f"CLP*C{claim['claim_no']}*1*300*{E.amount(paid)}*0*12*PAYERCLM9",
                          f"SVC*AD:D2391*150*{E.amount(paid)}"))
        fake.inbound = [("t-835", "835", remit), ("t-277b", "277", ack_277(f"C{claim['claim_no']}", "A2"))]
        r = self.post(mgr, "/claims/electronic/fetch", follow_redirects=True)
        self.assertIn("Remittances to check are listed below", r.get_data(as_text=True))
        self.assertIn("Accepted for processing", INS.get_claim(self.conn, 2, cl)["edi_ack"])
        waiting = CH.waiting_remits(self.conn, 2)
        self.assertEqual(len(waiting), 1)
        self.assertEqual(INS.get_claim(self.conn, 2, cl)["status"], "submitted")                   # not posted by itself
        self.assertEqual(self.conn.execute("SELECT poll_token FROM clearinghouse_account").fetchone()[0], "TOKEN-2")
        page = self.page(mgr, f"/claims/electronic/files/{waiting[0]['id']}/check")
        self.assertIn("Post this remittance", page)
        self.post(mgr, f"/claims/electronic/files/{waiting[0]['id']}/check", {"confirm": "1"})
        self.assertEqual(INS.get_claim(self.conn, 2, cl)["status"], "paid")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM edi_files WHERE kind = '835'").fetchone()[0], 1)    # posted in place, not copied
        self.assertEqual(CH.waiting_remits(self.conn, 2), [])
        # The next fetch starts from the saved place, and the same transactions are never read twice.
        self.conn.execute("UPDATE clearinghouse_account SET poll_token = NULL")
        self.conn.commit()
        CH.fetch(self.conn, self.c(), MGR2, "test", transport=fake)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM edi_files WHERE kind = '835'").fetchone()[0], 1)
        polls = [u for m, u, h, b in fake.calls if u.startswith(CH.STEDI["poll"])]
        self.assertIn("startDateTime=", polls[0])

    def test_the_scheduled_job(self):
        fake = FakeStedi()
        self.connect(fake)
        out = self.app.test_cli_runner().invoke(args=["fetch-claims"])
        self.assertIn("Clinic 2: 0 acknowledgements, 0 remittances", out.output)


class CoverTests(Live):
    def test_a_cover_check_is_answered_straight_away(self):
        fake = FakeStedi()
        cover = self.connect(fake)
        mgr = self.login("mgr2")
        r = self.post(mgr, f"/cover/{cover}/us-details", {"what": "check"})
        self.assertIn("/insurance", r.headers["Location"])
        self.assertIn("Cover is active.", INS.get_cover(self.conn, 2, cover)["verify_note"])
        self.assertEqual(fake.calls[0][1], CH.STEDI["eligibility"])

    def test_a_wrong_question_is_explained(self):
        fake = FakeStedi()
        fake.eligibility_x12 = isa("FA", "999", ("AK1*HS*5*005010X279A1", "IK3*NM1*8**8", "AK9*R*1*1*0"))
        cover = self.connect(fake)
        r = self.post(self.login("mgr2"), f"/cover/{cover}/us-details", {"what": "check"}, follow_redirects=True)
        self.assertIn("cover question was not accepted", r.get_data(as_text=True))
        self.assertIsNone(INS.get_cover(self.conn, 2, cover)["verify_note"])
        self.assertIn("NM1", self.conn.execute("SELECT detail FROM clearinghouse_log ORDER BY id DESC").fetchone()[0])
