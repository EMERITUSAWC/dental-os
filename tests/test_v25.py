"""v0.25: DICOM X-rays shown in the browser, and the imaging bridge with its agent. The agent is run here against the app itself,
with real folders and files; only the network is replaced."""
import base64
import io
import json
import struct
import tempfile
import time
from pathlib import Path

from PIL import Image

from tests.test_v04 import Base
from dental_os.services import apikeys, dicom as D, files as F, imaging as IM, mounts as M
from tools.imaging_agent import Agent

PX = [0, 1000, 2000, 4095] * 6          # 4 rows x 6 columns, 12-bit values


def png_pixels(data):
    im = Image.open(io.BytesIO(data))
    return im.size, im.mode, list(im.tobytes())


class DicomTests(Base):
    def test_both_common_syntaxes_with_window_and_spacing(self):
        for explicit in (True, False):
            d = D.make(4, 6, PX, explicit=explicit, spacing=0.035, window=(2047, 4095), stored=12, patient_name="MENSAH^AMA", patient_id="DOS-1")
            s = D.summary(D.parse(d))
            self.assertEqual((s["patient_name"], s["patient_id"], s["study_date"], s["mm_per_px"], s["size"]),
                             ("MENSAH AMA", "DOS-1", "2026-10-20", 0.035, "6 × 4"))
            size, mode, px = png_pixels(D.to_png(d))
            self.assertEqual((size, mode), ((6, 4), "L"))
            self.assertEqual(px[:4], [0, 62, 124, 255])

    def test_no_window_uses_the_full_range_and_monochrome1_is_inverted(self):
        size, mode, px = png_pixels(D.to_png(D.make(2, 2, [100, 200, 300, 400])))
        self.assertEqual(px, [0, 85, 170, 255])
        size, mode, px = png_pixels(D.to_png(D.make(2, 2, [0, 100, 200, 255], bits=8, photometric="MONOCHROME1")))
        self.assertEqual(px, [255, 155, 55, 0])

    def test_a_sequence_before_the_pixels_is_stepped_over(self):
        d = D.make(1, 2, [0, 10])
        at = d.index(b"\xe0\x7f\x10\x00")                                  # the pixel data element
        item = struct.pack("<HHI", 0xFFFE, 0xE000, 0xFFFFFFFF) + struct.pack("<HH", 0x0008, 0x0100) + b"SH" + struct.pack("<H", 2) + b"X1" \
            + struct.pack("<HHI", 0xFFFE, 0xE00D, 0)
        seq = struct.pack("<HH", 0x0008, 0x2218) + b"SQ\x00\x00" + struct.pack("<I", 0xFFFFFFFF) + item + struct.pack("<HHI", 0xFFFE, 0xE0DD, 0)
        d2 = d[:at] + seq + d[at:]
        self.assertEqual(png_pixels(D.to_png(d2))[2], [0, 255])

    def test_compressed_dicom(self):
        buf = io.BytesIO()
        Image.new("L", (8, 4), 128).save(buf, "JPEG")
        jpeg = D.make(4, 8, [], bits=8, transfer_syntax="1.2.840.10008.1.2.4.50", frames=[buf.getvalue()])
        size, mode, px = png_pixels(D.to_png(jpeg))
        self.assertEqual(size, (8, 4))
        self.assertTrue(all(120 <= v <= 136 for v in px))
        lossless = D.make(4, 8, [], bits=8, transfer_syntax="1.2.840.10008.1.2.4.70", frames=[b"\xff\xd8\xff\xc3\x00"])
        with self.assertRaises(D.DicomError) as e:
            D.to_png(lossless)
        self.assertIn("imaging software", str(e.exception))

    def test_bad_files_are_refused_plainly(self):
        for bad in (b"hello", b"\x00" * 128 + b"DICM" + b"\x02\x00"):
            with self.assertRaises(D.DicomError):
                D.to_png(bad)
        d = D.make(4, 6, PX)
        with self.assertRaises(D.DicomError):
            D.to_png(d[:-10])


class ViewerTests(Base):
    def upload(self, c, data, name="a.dcm"):
        return self.post(c, "/patients/1/files", {"kind": "xray", "file": (io.BytesIO(data), name)}, content_type="multipart/form-data",
                         follow_redirects=True)

    def test_a_dicom_upload_is_shown_measured_and_checked_against_the_patient(self):
        dent = self.login("dent")
        self.upload(dent, D.make(4, 6, PX, spacing=0.05, patient_name="SOMEONE^ELSE"))
        fid = self.db().execute("SELECT id FROM patient_files").fetchone()[0]
        r = dent.get(f"/files/{fid}/preview.png")
        self.assertEqual((r.status_code, r.mimetype), (200, "image/png"))
        self.assertEqual(png_pixels(r.data)[0], (6, 4))
        self.assertEqual(dent.get(f"/files/{fid}/preview.png").data, r.data)          # made once, kept
        page = self.page(dent, f"/files/{fid}/viewer")
        self.assertIn(f"/files/{fid}/preview.png", page)
        self.assertIn('data-mmpx="0.05"', page)
        self.assertIn("does not match this patient", page)
        self.assertIn(f"/files/{fid}/preview.png", self.page(dent, "/patients/1/files"))
        self.upload(dent, D.make(4, 6, PX, patient_name="MENSAH^AMA"), "b.dcm")
        fid2 = self.db().execute("SELECT MAX(id) FROM patient_files").fetchone()[0]
        self.assertNotIn("does not match this patient", self.page(dent, f"/files/{fid2}/viewer"))
        self.assertEqual(self.login("mgr2").get(f"/files/{fid}/preview.png").status_code, 404)
        self.assertNotEqual(self.login("desk").get(f"/files/{fid}/preview.png").status_code, 200)
        conn = self.db()
        clinic = conn.execute("SELECT * FROM clinics WHERE id = 1").fetchone()
        mid = M.create(conn, clinic, 2, 1, "fmx18")
        M.place(conn, clinic, 2, mid, 1, fid)                                           # DICOM can go in an X-ray layout
        conn.commit()
        self.assertIn(f"/files/{fid}/preview.png", self.page(dent, f"/mounts/{mid}"))

    def test_a_dicom_that_cannot_be_shown_says_why(self):
        dent = self.login("dent")
        bad = D.make(4, 6, PX, transfer_syntax=D.EXPLICIT_BE)
        self.upload(dent, bad)
        fid = self.db().execute("SELECT id FROM patient_files").fetchone()[0]
        r = dent.get(f"/files/{fid}/preview.png")
        self.assertEqual(r.status_code, 415)
        self.assertIn("big-endian", r.get_data(as_text=True))


class BridgeTests(Base):
    def setUp(self):
        super().setUp()
        conn = self.db()
        self.clinic = dict(conn.execute("SELECT * FROM clinics WHERE id = 1").fetchone())
        IM.add_station(conn, self.clinic, 1, "Surgery 1")
        _, self.key = apikeys.create(conn, self.clinic, 1, "X-ray agent", "write")
        _, self.read_key = apikeys.create(conn, self.clinic, 1, "Read only", "read")
        conn.commit()
        conn.close()
        self.folder = tempfile.TemporaryDirectory()
        self.client = self.app.test_client()

    def tearDown(self):
        self.folder.cleanup()
        super().tearDown() if hasattr(super(), "tearDown") else None

    def transport(self, method, url, headers, body=None):
        path = url.replace("http://dental.test", "")
        r = self.client.open(path, method=method, headers=headers, data=body)
        return r.status_code, r.get_data(as_text=True)

    def agent(self, key=None, launcher=None):
        return Agent({"server": "http://dental.test", "api_key": key or self.key, "station": "Surgery 1", "watch": [self.folder.name]},
                     transport=self.transport, launcher=launcher)

    def drop(self, name, data):
        p = Path(self.folder.name) / name
        p.write_bytes(data)
        return p

    def settle(self, a):
        a.ready_files(now=time.time() + 5)                     # first look notes the sizes
        return a.run_once(now=time.time() + 5)

    def test_staff_pick_the_patient_and_the_agent_files_new_images_on_them(self):
        launched = []
        a = self.agent(launcher=launched.append)
        a.cfg["launch"] = ["imaging.exe", "/ID={chart}", "/NAME={last_name},{first_name}"]
        self.drop("img1.jpg", b"\xff\xd8\xff" + b"0" * 300)
        self.assertEqual(self.settle(a), {"img1.jpg": "wait"})              # nobody waiting yet: left where it is
        self.assertTrue((Path(self.folder.name) / "img1.jpg").exists())
        desk = self.login("dent")
        r = self.post(desk, "/patients/1/xray-now", {"station": "Surgery 1"}, follow_redirects=True)
        self.assertIn("ready for this patient", r.get_data(as_text=True))
        self.assertEqual(self.settle(a), {"img1.jpg": "sent"})
        self.assertEqual(launched, [["imaging.exe", "/ID=DOS-1", "/NAME=Mensah,Ama"]])
        self.drop("img2.png", b"\x89PNG\r\n\x1a\n" + b"0" * 300)
        self.assertEqual(self.settle(a), {"img2.png": "sent"})
        self.assertEqual(len(launched), 1)                                    # opened once per patient
        conn = self.db()
        rows = conn.execute("SELECT patient_id, kind, original_name FROM patient_files ORDER BY id").fetchall()
        self.assertEqual([tuple(r) for r in rows], [(1, "xray", "img1.jpg"), (1, "xray", "img2.png")])
        self.assertEqual(conn.execute("SELECT files FROM imaging_requests").fetchone()[0], 2)
        sent = list((Path(self.folder.name) / "sent").rglob("*"))
        self.assertEqual(sorted(p.name for p in sent if p.is_file()), ["img1.jpg", "img2.png"])
        self.assertIn("is sending its new images", self.page(desk, "/patients/1/files"))
        rid = conn.execute("SELECT id FROM imaging_requests").fetchone()[0]
        self.post(desk, f"/imaging/{rid}/done")
        self.drop("img3.jpg", b"\xff\xd8\xff" + b"1" * 300)
        self.assertEqual(self.settle(a), {"img3.jpg": "wait"})                # session finished: waits again

    def test_a_dicom_with_our_patient_number_needs_no_waiting_patient(self):
        a = self.agent()
        self.drop("pano.dcm", D.make(4, 6, PX, patient_id="DOS-2"))
        self.assertEqual(self.settle(a), {"pano.dcm": "sent"})
        self.assertEqual(self.db().execute("SELECT patient_id FROM patient_files").fetchone()[0], 2)
        self.drop("other.dcm", D.make(4, 6, PX, patient_id="DOS-3"))            # clinic two's patient: not filed here
        self.assertEqual(self.settle(a), {"other.dcm": "wait"})

    def test_files_that_cannot_be_filed_go_to_failed_with_the_reason(self):
        a = self.agent()
        conn = self.db()
        IM.request_images(conn, self.clinic, 2, 1, "Surgery 1")
        conn.commit()
        self.drop("scan.jpg", b"not really a jpeg" * 10)
        self.assertEqual(self.settle(a), {"scan.jpg": "failed"})
        note = next((Path(self.folder.name) / "failed").rglob("*.txt"))
        self.assertIn("Only PNG, JPEG", note.read_text())
        self.drop("big.dcm", b"\x00" * (10 * 1024 * 1024 + 1))
        self.assertEqual(self.settle(a), {"big.dcm": "failed"})
        self.drop("notes.txt", b"hello")
        self.assertEqual(self.settle(a), {})                                  # not an image type: left alone

    def test_the_api_checks_keys_and_stations(self):
        r = self.client.get("/api/v1/imaging/next?station=Surgery%201", headers={"Authorization": "Bearer " + self.read_key})
        self.assertEqual((r.status_code, r.get_json()["data"]), (200, None))
        r = self.client.get("/api/v1/imaging/next?station=%3Cbad%3E", headers={"Authorization": "Bearer " + self.key})
        self.assertEqual(r.status_code, 400)
        r = self.client.post("/api/v1/imaging/files", headers={"Authorization": "Bearer " + self.read_key},
                             json={"station": "Surgery 1", "data_base64": base64.b64encode(b"x").decode()})
        self.assertEqual(r.status_code, 403)
        r = self.client.post("/api/v1/imaging/files", headers={"Authorization": "Bearer " + self.key}, json={"station": "Surgery 1", "data_base64": "%%%"})
        self.assertEqual(r.status_code, 422)
        self.assertEqual(self.client.get("/api/v1/imaging/next?station=x").status_code, 401)
        mgr = self.login("mgr")
        self.assertIn("Agent running", self.page(mgr, "/imaging"))
        self.post(mgr, "/imaging", {"name": "Surgery 2"})
        self.assertIn("Surgery 2", self.page(mgr, "/imaging"))
        self.assertEqual(self.login("dent").get("/imaging").status_code, 403)
        with self.assertRaises(IM.ImagingError):
            IM.request_images(self.db(), self.clinic, 1, 1, "No such room")

    def test_the_agent_settings_file(self):
        from tools import imaging_agent as IA
        cfg = Path(self.folder.name) / "agent.json"
        self.assertEqual(IA.main(["--config", str(cfg), "--setup"]), 0)
        self.assertEqual(json.loads(cfg.read_text())["station"], "Surgery 1")
        self.assertEqual(IA.main(["--config", str(cfg), "--setup"]), 1)          # never overwrites
