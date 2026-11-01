"""v0.50 and v0.51: the locked off-site copy, restore drills, the internet check and the read-only viewing copy."""
import datetime
import os
import tempfile
from pathlib import Path

import unittest

from tests.dbmeta import ON_PG
from tests.test_v04 import Base
from dental_os import create_app
from dental_os.services import offsite as OS

PASS = "correct horse battery staple"


class WebDav:
    """Stands in for a WebDAV server: PUT stores, GET returns (with Range)."""
    def __init__(self):
        self.store, self.calls = {}, []

    def __call__(self, method, url, headers, body=None, timeout=120):
        self.calls.append((method, url, headers.get("Authorization")))
        if method == "PUT":
            self.store[url] = body
            return 201, b""
        if url not in self.store:
            return 404, b""
        data = self.store[url]
        if "Range" in headers:
            a, b = headers["Range"][6:].split("-")
            return 206, data[int(a):int(b) + 1]
        return 200, data


class Copies(Base):
    def setUp(self):
        super().setUp()
        self.files = Path(self.app.config["FILES_PATH"])
        (self.files / "1").mkdir(parents=True, exist_ok=True)
        (self.files / "1" / "xray.png").write_bytes(b"\x89PNG fake")
        self.conn = self.db()

    def tearDown(self):
        self.conn.close()


@unittest.skipIf(ON_PG, "copies the SQLite file; a PostgreSQL server's copy is made with pg_dump and checked by hand")
class CopyTests(Copies):
    def test_folder_copy_is_locked_checked_and_restores(self):
        with tempfile.TemporaryDirectory() as off:
            cfg = {"OFFSITE_TARGET": off, "OFFSITE_PASSPHRASE": PASS}
            where, size = OS.make_and_send(cfg, self.app.config["DATABASE"], str(self.files), self.conn)
            blob = Path(where).read_bytes()
            self.assertTrue(blob.startswith(OS.MAGIC))
            self.assertNotIn(b"Mensah", blob)                                      # the copy is locked: no names in it
            with self.assertRaises(OS.OffsiteError):
                OS.drill(blob, "the wrong passphrase!")
            info = OS.drill(blob, PASS)
            self.assertEqual((info["patients"], info["files"]), (3, 1))
            with tempfile.TemporaryDirectory() as out:
                db_path, _ = OS.restore_to(blob, PASS, out)
                self.assertTrue(db_path.exists())
                self.assertEqual((Path(out) / "files" / "1" / "xray.png").read_bytes(), b"\x89PNG fake")
            self.assertFalse(OS.stale(self.conn))
            self.assertTrue(OS.stale(self.conn, now=datetime.datetime.utcnow() + datetime.timedelta(days=2)))

    def test_missing_folder_short_passphrase_and_webdav(self):
        with self.assertRaises(OS.OffsiteError):
            OS.make_and_send({"OFFSITE_TARGET": "/no/such/usb", "OFFSITE_PASSPHRASE": PASS}, self.app.config["DATABASE"], None, self.conn)
        self.assertEqual(OS.status(self.conn)["last"]["ok"], 0)
        with tempfile.TemporaryDirectory() as off:
            with self.assertRaises(OS.OffsiteError):
                OS.make_and_send({"OFFSITE_TARGET": off, "OFFSITE_PASSPHRASE": "short"}, self.app.config["DATABASE"], None, self.conn)
        with self.assertRaises(OS.OffsiteError):
            OS.make_and_send({"OFFSITE_TARGET": "http://plain.example", "OFFSITE_PASSPHRASE": PASS}, self.app.config["DATABASE"], None, self.conn)
        dav = WebDav()
        cfg = {"OFFSITE_TARGET": "https://dav.example/copies", "OFFSITE_USER": "clinic", "OFFSITE_PASSWORD": "pw", "OFFSITE_PASSPHRASE": PASS}
        OS.make_and_send(cfg, self.app.config["DATABASE"], str(self.files), self.conn, transport=dav)
        self.assertIn("https://dav.example/copies/dental-os-latest.copy", dav.store)
        self.assertTrue(all(c[2] and c[2].startswith("Basic ") for c in dav.calls))
        name, blob = OS.latest(cfg, transport=dav)
        self.assertEqual(OS.drill(blob, PASS)["patients"], 3)

    def test_internet_check(self):
        def down(*a, **k):
            raise OS.OffsiteError("no route")
        self.assertFalse(OS.net_check(self.conn, transport=down))
        first = OS.offline_since(self.conn)
        self.assertIsNotNone(first)
        OS.net_check(self.conn, transport=down)
        self.assertEqual(OS.offline_since(self.conn), first)                      # still the time it first went
        self.assertTrue(OS.net_check(self.conn, transport=lambda *a, **k: (204, b"")))
        self.assertIsNone(OS.offline_since(self.conn))


class ScreenTests(Copies):
    def test_banners_and_the_copies_page(self):
        OS.net_check(self.conn, transport=lambda *a, **k: (_ for _ in ()).throw(OS.OffsiteError("x")))
        desk = self.login("desk")
        self.assertIn("No internet at the moment", self.page(desk, "/"))
        mgr = self.login("mgr")
        self.assertIn("No off-site place is set yet", self.page(mgr, "/settings/copies"))
        self.assertEqual(desk.get("/settings/copies").status_code, 403)

    @unittest.skipIf(ON_PG, "the viewing copy is SQLite")
    def test_read_only_copy_refuses_changes(self):
        with tempfile.TemporaryDirectory() as off:
            cfg = {"OFFSITE_TARGET": off, "OFFSITE_PASSPHRASE": PASS}
            OS.make_and_send(cfg, self.app.config["DATABASE"], str(self.files), self.conn)
            with tempfile.TemporaryDirectory() as view:
                viewer = create_app({"TESTING": True, "DATABASE": os.path.join(view, "v.sqlite"), "SECRET_KEY": "v", "FILES_PATH": os.path.join(view, "files"),
                                     "READ_ONLY": True, **cfg})
                out = viewer.test_cli_runner().invoke(args=["offsite-load"]).output
                self.assertIn("Loaded", out)
                self.assertTrue(Path(view, "files", "1", "xray.png").exists())
                c = viewer.test_client()
                import re
                tok = re.search(r'name="_csrf" value="([^"]+)"', c.get("/login").get_data(as_text=True)).group(1)
                from tests.test_v04 import PW
                c.post("/login", data={"_csrf": tok, "username": "mgr", "password": PW})
                page = c.get("/patients").get_data(as_text=True)
                self.assertIn("Mensah", page)
                self.assertIn("read-only copy", page)
                tok = re.search(r'name="_csrf" value="([^"]+)"', page).group(1)
                self.assertEqual(c.post("/patients/new", data={"_csrf": tok, "first_name": "X"}).status_code, 403)
        out = self.app.test_cli_runner().invoke(args=["offsite-load"]).output
        self.assertIn("only runs on a server set to READ_ONLY=1", out)
