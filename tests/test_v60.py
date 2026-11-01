"""v0.60: the picture check on every X-ray, and the X-ray AI connection with the dentist deciding."""
import base64
import io
import json
import random
import re

from PIL import Image, ImageDraw, ImageFilter

from tests.test_v04 import Base
from dental_os.services import xrayai as XA, xrayqc as QC

KEY = "sk-test-0123456789abcdef"


def picture(blur=0, scale=None, size=(1200, 900), exif_text=None, fmt="PNG"):
    rnd = random.Random(60)
    im = Image.new("L", size, 60)
    d = ImageDraw.Draw(im)
    for i in range(8):
        x = 80 + i * 140
        d.rounded_rectangle([x, 150, x + 110, 750], 40, fill=200)
        d.rectangle([x + 40, 300, x + 70, 700], fill=120)
    for _ in range(4000):
        d.point((rnd.randrange(size[0]), rnd.randrange(size[1])), fill=rnd.randrange(40, 220))
    if blur:
        im = im.filter(ImageFilter.GaussianBlur(blur))
    if scale:
        im = im.point(scale)
    out = io.BytesIO()
    if exif_text:
        im = im.convert("RGB")
        exif = Image.Exif()
        exif[0x013B] = exif_text                                  # Artist
        im.save(out, "JPEG", exif=exif)
    else:
        im.save(out, fmt)
    return out.getvalue()


class Fake:
    def __init__(self, code=200, answer=None):
        self.code, self.calls = code, []
        self.answer = answer if answer is not None else {"model": "TestRead 2.1", "findings": [
            {"tooth": "36", "kind": "caries", "label": "Distal caries", "confidence": 0.87, "box": [0.4, 0.3, 0.1, 0.2]},
            {"tooth": "99", "kind": "periapical", "confidence": 1.7, "box": [0.9, 0.9, 0.5, 0.5]},
            {"tooth": "", "kind": "made_up", "label": "  odd   spacing  ", "box": "nonsense"},
            "not a finding"]}

    def __call__(self, url, headers, body, timeout=60):
        self.calls.append((url, headers, body))
        return self.code, self.answer if isinstance(self.answer, str) else json.dumps(self.answer)


class Xray(Base):
    def setUp(self):
        super().setUp()
        self.conn = self.db()

    def tearDown(self):
        self.conn.close()

    def upload(self, c, data, name="bw.png", kind="xray", pid=1):
        return c.post(f"/patients/{pid}/files", data={"_csrf": self.tok(c), "kind": kind, "file": (io.BytesIO(data), name)},
                      content_type="multipart/form-data")

    def file_id(self, name=None):
        return self.conn.execute("SELECT id FROM patient_files ORDER BY id DESC").fetchone()[0]

    def switch_on(self, transport=None):
        c = self.login("mgr")
        r = self.post(c, "/settings/xray-ai", {"vendor": "Test AI Ltd", "endpoint": "https://ai.example.com/v1/read", "api_key": KEY,
                                              "enabled": "1", "agree": "1"})
        self.assertEqual(r.status_code, 302, r.get_data(as_text=True)[:400])
        self.app.config["XRAY_AI_TRANSPORT"] = transport
        return c


class PictureCheckTests(Xray):
    def test_the_check_tells_good_from_bad(self):
        self.assertEqual(QC.judge(QC.measure(picture())), [])
        self.assertIn("blurred", QC.judge(QC.measure(picture(blur=2))))
        self.assertIn("blurred", QC.judge(QC.measure(picture(blur=5))))
        self.assertEqual(QC.judge(QC.measure(picture(scale=lambda v: v // 8))), ["too_dark"])
        self.assertEqual(QC.judge(QC.measure(picture(scale=lambda v: min(255, v * 3)))), ["too_bright"])
        self.assertIn("too_small", QC.judge(QC.measure(picture(size=(200, 150)))))
        self.assertIn("low_contrast", QC.judge(QC.measure(picture(scale=lambda v: 120 + v // 12))))
        self.assertEqual(QC.judge(QC.measure(picture(fmt="JPEG"))), [])          # ordinary compression is not blur
        with self.assertRaises(QC.QualityError):
            QC.measure(b"not a picture")

    def test_each_xray_is_checked_as_it_comes_in(self):
        c = self.login("dent")
        self.upload(c, picture(blur=4), "shaken.png")
        bad = self.file_id()
        self.upload(c, picture(), "good.png")
        good = self.file_id()
        self.upload(c, picture(blur=4), "photo.png", kind="photo")
        rows = {r["file_id"]: r for r in self.conn.execute("SELECT * FROM xray_checks")}
        self.assertEqual(set(rows), {bad, good})                                    # photos are not checked
        self.assertEqual((rows[bad]["ok"], rows[bad]["issues"]), (0, "blurred"))
        self.assertEqual(rows[good]["ok"], 1)
        page = self.page(c, "/patients/1/files")
        self.assertIn("Picture check: fine", page)
        self.assertIn("Looks blurred", page)
        self.assertIn("Looks blurred", self.page(c, f"/files/{bad}/viewer"))

    def test_only_the_pixels_leave(self):
        data = picture(exif_text="Ama Mensah 1990-05-01")
        self.assertIn(b"Ama Mensah", data)
        clean = QC.pixels_png(data, "image/jpeg")
        self.assertTrue(clean.startswith(b"\x89PNG"))
        self.assertNotIn(b"Ama Mensah", clean)


class SetupTests(Xray):
    def test_only_managers_and_only_with_care(self):
        self.assertEqual(self.login("dent").get("/settings/xray-ai").status_code, 403)
        c = self.login("mgr")
        base = {"vendor": "Test AI Ltd", "endpoint": "https://ai.example.com/v1/read", "api_key": KEY, "enabled": "1"}
        self.assertIn("Tick the box", self.post(c, "/settings/xray-ai", base).get_data(as_text=True))
        for bad in ("http://ai.example.com/read", "https://10.0.0.4/read", "https://ai.local/read"):
            self.post(c, "/settings/xray-ai", {**base, "endpoint": bad, "agree": "1"})
            self.assertFalse(XA.is_on(self.conn, 1, "test"), bad)
        self.post(c, "/settings/xray-ai", {**base, "agree": "1"})
        self.assertTrue(XA.is_on(self.conn, 1, "test"))
        stored = self.conn.execute("SELECT api_key FROM xray_ai_account WHERE clinic_id = 1").fetchone()[0]
        self.assertNotIn(KEY, stored)
        self.assertNotIn(KEY, self.page(c, "/settings/xray-ai"))
        log = " ".join(str(r[0]) for r in self.conn.execute("SELECT detail FROM audit_log"))
        self.assertNotIn(KEY, log)
        self.post(c, "/settings/xray-ai", {"vendor": "Test AI Ltd", "endpoint": "https://ai.example.com/v1/read", "enabled": "1", "agree": "1"})
        self.assertTrue(XA.is_on(self.conn, 1, "test"))                               # an empty key box keeps the saved key
        self.assertFalse(XA.is_on(self.conn, 2, "test"))


class ReadTests(Xray):
    def test_a_read_sends_only_the_picture_and_keeps_suggestions(self):
        fake = Fake()
        self.switch_on(fake)
        dent = self.login("dent")
        self.upload(dent, picture(exif_text="Ama Mensah +233201112223"), "bw.jpg")
        fid = self.file_id()
        self.assertIn("Ask the X-ray AI for a second look", self.page(dent, f"/files/{fid}/viewer"))
        r = self.post(dent, f"/files/{fid}/ai-read")
        self.assertTrue(r.headers["Location"].endswith(f"/files/{fid}/viewer#ai"))
        url, headers, body = fake.calls[0]
        self.assertEqual((url, headers["Authorization"]), ("https://ai.example.com/v1/read", "Bearer " + KEY))
        sent = json.loads(body)
        self.assertEqual(set(sent), {"image", "mime", "kind", "notation"})
        image = base64.b64decode(sent["image"])
        others = json.dumps({k: v for k, v in sent.items() if k != "image"}).encode()
        for secret in (b"Ama", b"Mensah", b"233201112223", b"1990-05-01"):
            self.assertNotIn(secret, others)
            self.assertNotIn(secret, image)
        self.assertTrue(image.startswith(b"\x89PNG"))
        read = self.conn.execute("SELECT * FROM xray_ai_reads").fetchone()
        self.assertEqual((read["status"], read["model"], read["findings"], read["patient_id"]), ("done", "TestRead 2.1", 3, 1))
        f = self.conn.execute("SELECT * FROM xray_ai_findings ORDER BY id").fetchall()
        self.assertEqual([(x["tooth"], x["kind"], x["status"]) for x in f], [(36, "caries", "suggested"), (None, "periapical", "suggested"), (None, "other", "suggested")])
        self.assertEqual(json.loads(f[0]["box"]), [0.4, 0.3, 0.1, 0.2])
        self.assertIsNone(f[1]["confidence"])                                         # 1.7 is not a confidence
        self.assertEqual(json.loads(f[1]["box"]), [0.9, 0.9, 0.1, 0.1])               # kept inside the picture
        self.assertEqual(f[2]["label"], "odd spacing")
        self.assertIsNone(f[2]["box"])
        page = self.page(dent, f"/files/{fid}/viewer")
        self.assertIn("Distal caries", page)
        self.assertIn("87%", page)
        self.assertIn('class="ai-box suggested"', page)
        self.assertIn("not a diagnosis", page)

    def test_failures_are_kept_with_a_plain_reason(self):
        dent = self.login("dent")
        self.upload(dent, picture())
        fid = self.file_id()
        for code, answer, words in ((401, {}, "refused the key"), (200, "{not json", "could not be read"), (500, {}, "could not read it")):
            self.switch_on(Fake(code, answer))
            self.post(dent, f"/files/{fid}/ai-read")
            last = self.conn.execute("SELECT status, detail FROM xray_ai_reads ORDER BY id DESC").fetchone()
            self.assertEqual(last["status"], "failed")
            self.assertIn(words, last["detail"])
        self.assertIn("refused the key", self.page(dent, f"/files/{fid}/viewer"))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM xray_ai_findings").fetchone()[0], 0)

    def test_limits(self):
        fake = Fake()
        dent = self.login("dent")
        self.upload(dent, picture(), kind="photo")
        photo = self.file_id()
        self.assertIn("not switched on", self.post(dent, f"/files/{photo}/ai-read", follow_redirects=True).get_data(as_text=True))
        self.switch_on(fake)
        self.assertIn("Only X-rays can be sent", self.post(dent, f"/files/{photo}/ai-read", follow_redirects=True).get_data(as_text=True))
        self.upload(dent, picture())
        fid = self.file_id()
        self.assertEqual(self.post(self.login("mgr2"), f"/files/{fid}/ai-read").status_code, 404)    # another clinic
        self.assertEqual(self.post(self.login("desk"), f"/files/{fid}/ai-read").status_code, 403)    # front desk: no clinical record
        self.conn.executemany("INSERT INTO xray_ai_reads(clinic_id, patient_id, file_id, status) VALUES (1, 1, ?, 'done')", [(fid,)] * XA.PER_HOUR)
        self.conn.commit()
        self.assertIn("a lot of X-rays", self.post(dent, f"/files/{fid}/ai-read", follow_redirects=True).get_data(as_text=True))
        self.assertEqual(fake.calls, [])


class DecideTests(Xray):
    def test_only_a_dentist_decides_and_it_is_kept(self):
        self.switch_on(Fake())
        dent = self.login("dent")
        self.upload(dent, picture())
        fid = self.file_id()
        self.post(dent, f"/files/{fid}/ai-read")
        first = self.conn.execute("SELECT id FROM xray_ai_findings ORDER BY id").fetchone()[0]
        self.assertEqual(self.post(self.login("asst"), f"/ai-findings/{first}", {"status": "agreed"}).status_code, 403)
        self.assertEqual(self.post(self.login("mgr2"), f"/ai-findings/{first}", {"status": "agreed"}).status_code, 404)
        r = self.post(dent, f"/ai-findings/{first}", {"status": "agreed"})
        self.assertTrue(r.headers["Location"].endswith(f"/files/{fid}/viewer#ai"))
        row = self.conn.execute("SELECT * FROM xray_ai_findings WHERE id = ?", (first,)).fetchone()
        self.assertEqual((row["status"], row["decided_by"]), ("agreed", 2))
        self.assertIn("Agreed by the dentist", self.page(dent, f"/files/{fid}/viewer"))
        self.assertIn('class="ai-box agreed"', self.page(dent, f"/files/{fid}/viewer"))
        self.post(dent, f"/ai-findings/{first}", {"status": "suggested"})
        row = self.conn.execute("SELECT * FROM xray_ai_findings WHERE id = ?", (first,)).fetchone()
        self.assertEqual((row["status"], row["decided_by"]), ("suggested", None))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM tooth_conditions").fetchone()[0], 0)   # nothing on the chart by itself
        self.assertEqual(XA.waiting_count(self.conn, 1), 3)
