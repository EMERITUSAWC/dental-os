"""Working through internet outages, and a copy kept off site (versions 0.50 and 0.51).

The clinic runs Dental OS on its own computer on the clinic's network, so the diary, records, charting and the till keep working when
the internet is down. Only four things need the internet: WhatsApp, SMS and email messages (they wait on the send list and go out when
it is back), online payments, and the copy kept off site.

The off-site copy is the whole database and every patient file, packed into one file and locked with a passphrase the clinic keeps
(PBKDF2 with 390,000 rounds, then Fernet: AES in CBC mode with an HMAC). Without the passphrase the copy cannot be read, so it can sit
on a shared drive, a USB disk or a cloud folder. It goes to a folder (a USB disk, or a Google Drive, OneDrive or Dropbox folder
that syncs by itself) or to a WebDAV address (Nextcloud and many hosts offer one). Each copy is checked after it is written.

A restore drill unpacks a copy somewhere else and checks it, without touching the live system, so the clinic knows the copies work
before the day it needs one. A second, read-only server can load the newest copy now and then, so an owner abroad can look at the
diary and the numbers; it refuses every change. Two copies that can both be changed are not offered: merging them is how systems
lose data. No web framework here."""
import base64
import datetime
import hashlib
import hmac
import io
import json
import os
import secrets
import shutil
import sqlite3
import tempfile
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

MAGIC = b"DOSCOPY1\n"
ROUNDS = 390_000
KEEP = 14


class OffsiteError(Exception):
    pass


# ----------------------------------------------------------------------------------------------------------------- lock
def _key(passphrase, salt):
    if not passphrase or len(passphrase) < 12:
        raise OffsiteError("The passphrase must be at least 12 characters. Write it down and keep it away from the computer.")
    return base64.urlsafe_b64encode(hashlib.pbkdf2_hmac("sha256", passphrase.encode(), salt, ROUNDS))


def lock(data, passphrase):
    from cryptography.fernet import Fernet
    salt = secrets.token_bytes(16)
    return MAGIC + salt + Fernet(_key(passphrase, salt)).encrypt(data)


def unlock(blob, passphrase):
    from cryptography.fernet import Fernet, InvalidToken
    if not blob.startswith(MAGIC):
        raise OffsiteError("That is not a Dental OS copy.")
    salt, token = blob[len(MAGIC):len(MAGIC) + 16], blob[len(MAGIC) + 16:]
    try:
        return Fernet(_key(passphrase, salt)).decrypt(token)
    except InvalidToken:
        raise OffsiteError("The passphrase is wrong, or the copy is damaged.")


# ------------------------------------------------------------------------------------------------------------- the pack
def pack(db_path, files_path=None, pg_url=None):
    """The database and the patient files in one ZIP, as bytes."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        with tempfile.TemporaryDirectory() as tmp:
            if pg_url:
                import subprocess
                if not shutil.which("pg_dump"):
                    raise OffsiteError("pg_dump was not found. Install the PostgreSQL client tools.")
                dump = Path(tmp) / "database.dump"
                r = subprocess.run(["pg_dump", "--format=custom", "--file", str(dump), pg_url], capture_output=True, text=True)
                if r.returncode:
                    raise OffsiteError("pg_dump failed: " + r.stderr.strip()[:300])
                z.write(dump, "database.dump")
            else:
                snap = Path(tmp) / "database.sqlite"
                src, dst = sqlite3.connect(db_path), sqlite3.connect(snap)
                with dst:
                    src.backup(dst)
                src.close()
                dst.close()
                z.write(snap, "database.sqlite")
        if files_path and Path(files_path).is_dir():
            for p in sorted(Path(files_path).rglob("*")):
                if p.is_file():
                    z.write(p, "files/" + p.relative_to(files_path).as_posix())
        z.writestr("about.json", json.dumps({"made_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                                             "kind": "postgresql" if pg_url else "sqlite"}))
    return buf.getvalue()


def check(zip_bytes):
    """Open a pack and check it. Returns what is in it. Raises OffsiteError if it is not sound."""
    try:
        z = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile:
        raise OffsiteError("The copy is damaged.")
    if z.testzip() is not None:
        raise OffsiteError("The copy is damaged.")
    about = json.loads(z.read("about.json"))
    out = {"made_at": about["made_at"], "kind": about["kind"], "files": sum(1 for n in z.namelist() if n.startswith("files/"))}
    if about["kind"] == "sqlite":
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "check.sqlite"
            p.write_bytes(z.read("database.sqlite"))
            conn = sqlite3.connect(p)
            try:
                if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise OffsiteError("The database in the copy failed its check.")
                out.update({t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("clinics", "patients", "appointments", "payments")})
            finally:
                conn.close()
    return out


# ---------------------------------------------------------------------------------------------------------- the target
def _target(cfg):
    t = (cfg.get("OFFSITE_TARGET") or "").strip()
    if not t:
        raise OffsiteError("No off-site place is set. Set OFFSITE_TARGET to a folder, a WebDAV address or s3://bucket/folder.")
    if t.startswith("s3://"):
        return "s3", t
    if t.startswith("https://"):
        return "webdav", t.rstrip("/") + "/"
    if t.startswith("http://"):
        raise OffsiteError("Use an https address for the off-site copy.")
    return "folder", t[7:] if t.startswith("folder:") else t


def _auth(cfg):
    user, pw = cfg.get("OFFSITE_USER") or "", cfg.get("OFFSITE_PASSWORD") or ""
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()} if user else {}


def _http(method, url, headers, body=None, timeout=120):
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except (urllib.error.URLError, OSError) as exc:
        raise OffsiteError("Could not reach the off-site place: %s" % (getattr(exc, "reason", None) or exc.__class__.__name__))


# ------------------------------------------------------------------------------------------------- Amazon S3 (0.68)
# s3://bucket/folder: Amazon S3, Lightsail object storage, or any store that speaks the S3 API (OFFSITE_S3_ENDPOINT for those).
# OFFSITE_USER is the access key id and OFFSITE_PASSWORD the secret. Requests are signed with AWS Signature Version 4, written
# out here so no extra library is needed. Keep at most 14 copies with a lifecycle rule on the bucket (see deploy/README.md).
def _s3_url(cfg, name):
    rest = (cfg.get("OFFSITE_TARGET") or "")[5:].strip("/")
    bucket, _, folder = rest.partition("/")
    if not bucket:
        raise OffsiteError("Write the S3 place as s3://bucket or s3://bucket/folder.")
    key = "/".join(x for x in (folder, name) if x)
    endpoint = (cfg.get("OFFSITE_S3_ENDPOINT") or "").rstrip("/")
    region = cfg.get("OFFSITE_S3_REGION") or "us-east-1"
    if endpoint:
        if not endpoint.startswith("https://"):
            raise OffsiteError("Use an https address for OFFSITE_S3_ENDPOINT.")
        return f"{endpoint}/{bucket}/{urllib.parse.quote(key, safe='/~')}", region
    return f"https://{bucket}.s3.{region}.amazonaws.com/{urllib.parse.quote(key, safe='/~')}", region


def s3_sign(method, url, region, access_key, secret_key, body=b"", headers=None, now=None):
    """AWS Signature Version 4 for one S3 request. Returns the headers to send."""
    u = urllib.parse.urlsplit(url)
    now = now or datetime.datetime.now(datetime.timezone.utc)
    amz = now.strftime("%Y%m%dT%H%M%SZ")
    day = amz[:8]
    payload = hashlib.sha256(body or b"").hexdigest()
    h = {"host": u.netloc, "x-amz-content-sha256": payload, "x-amz-date": amz}
    h.update({k.lower(): str(v).strip() for k, v in (headers or {}).items()})
    names = sorted(h)
    query = "&".join(sorted("=".join(urllib.parse.quote(x, safe="-_.~") for x in (pair.split("=", 1) + [""])[:2])
                            for pair in u.query.split("&") if pair))
    canonical = "\n".join([method, u.path or "/", query, "".join(f"{n}:{h[n]}\n" for n in names), ";".join(names), payload])
    scope = f"{day}/{region}/s3/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amz, scope, hashlib.sha256(canonical.encode()).hexdigest()])
    k = ("AWS4" + secret_key).encode()
    for part in (day, region, "s3", "aws4_request"):
        k = hmac.new(k, part.encode(), hashlib.sha256).digest()
    sig = hmac.new(k, to_sign.encode(), hashlib.sha256).hexdigest()
    out = {n: v for n, v in h.items() if n != "host"}
    out["Authorization"] = f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, SignedHeaders={';'.join(names)}, Signature={sig}"
    return out


def _s3(cfg, method, name, body=None, headers=None, transport=None):
    url, region = _s3_url(cfg, name)
    if not (cfg.get("OFFSITE_USER") and cfg.get("OFFSITE_PASSWORD")):
        raise OffsiteError("Set OFFSITE_USER to the access key id and OFFSITE_PASSWORD to the secret for the S3 place.")
    signed = s3_sign(method, url, region, cfg["OFFSITE_USER"], cfg["OFFSITE_PASSWORD"], body or b"", headers)
    return (transport or _http)(method, url, signed, body)


def send(cfg, blob, name, transport=None):
    kind, where = _target(cfg)
    if kind == "s3":
        status, _ = _s3(cfg, "PUT", name, blob, {"Content-Type": "application/octet-stream"}, transport)
        if status not in (200, 201, 204):
            raise OffsiteError(f"The S3 place refused the copy (HTTP {status}). Check the bucket, the region and the keys.")
        status, head = _s3(cfg, "GET", name, None, {"Range": f"bytes=0-{len(MAGIC) - 1}"}, transport)
        if status not in (200, 206) or not head.startswith(MAGIC):
            raise OffsiteError("The copy could not be read back from the S3 place.")
        return where.rstrip("/") + "/" + name
    if kind == "folder":
        d = Path(where)
        if not d.is_dir():
            raise OffsiteError("The off-site folder is not there. Is the USB disk plugged in, or the cloud folder set up?")
        tmp = d / (name + ".part")
        tmp.write_bytes(blob)
        os.replace(tmp, d / name)
        if (d / name).read_bytes()[:len(MAGIC)] != MAGIC:
            raise OffsiteError("The copy could not be read back from the folder.")
        for stale in sorted(d.glob("dental-os-*.copy"))[:-KEEP]:
            stale.unlink()
        return str(d / name)
    status, _ = (transport or _http)("PUT", where + urllib.parse.quote(name), {**_auth(cfg), "Content-Type": "application/octet-stream"}, blob)
    if status not in (200, 201, 204):
        raise OffsiteError(f"The off-site place refused the copy (HTTP {status}).")
    status, head = (transport or _http)("GET", where + urllib.parse.quote(name), {**_auth(cfg), "Range": f"bytes=0-{len(MAGIC) - 1}"})
    if status not in (200, 206) or not head.startswith(MAGIC):
        raise OffsiteError("The copy could not be read back from the off-site place.")
    return where + name


def latest(cfg, transport=None):
    """The newest copy at the off-site place, as bytes. WebDAV needs OFFSITE_LATEST, the name of the file to fetch."""
    kind, where = _target(cfg)
    if kind == "folder":
        found = sorted(Path(where).glob("dental-os-*.copy"))
        if not found:
            raise OffsiteError("There is no copy in the off-site folder yet.")
        return found[-1].name, found[-1].read_bytes()
    name = cfg.get("OFFSITE_LATEST") or "dental-os-latest.copy"
    if kind == "s3":
        status, body = _s3(cfg, "GET", name, transport=transport)
        if status != 200:
            raise OffsiteError(f"Could not fetch the copy from S3 (HTTP {status}).")
        return name, body
    status, body = (transport or _http)("GET", where + urllib.parse.quote(name), _auth(cfg))
    if status != 200:
        raise OffsiteError(f"Could not fetch the copy (HTTP {status}).")
    return name, body


# ------------------------------------------------------------------------------------------------------------ the jobs
def make_and_send(cfg, db_path, files_path, conn=None, transport=None, now=None):
    """Make a copy, lock it, send it, check it. Logs the result. Returns (where, size)."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    name = f"dental-os-{now:%Y%m%d-%H%M%S}.copy"
    pg = db_path if str(db_path).startswith(("postgres://", "postgresql://")) else None
    try:
        data = pack(None if pg else db_path, files_path, pg)
        if not pg:
            check(data)
        blob = lock(data, cfg.get("OFFSITE_PASSPHRASE") or "")
        where = send(cfg, blob, name, transport)
        if _target(cfg)[0] in ("webdav", "s3"):
            send(cfg, blob, "dental-os-latest.copy", transport)
    except OffsiteError as exc:
        if conn is not None:
            conn.execute("INSERT INTO offsite_log(ok, detail) VALUES (0, ?)", (str(exc)[:300],))
            conn.commit()
        raise
    if conn is not None:
        conn.execute("INSERT INTO offsite_log(ok, detail, size_bytes) VALUES (1, ?, ?)", (where[-300:], len(blob)))
        conn.commit()
    return where, len(blob)


def drill(blob, passphrase):
    """A restore drill: unlock and check a copy, touching nothing live."""
    return check(unlock(blob, passphrase))


DRILL_EVERY_DAYS = 35          # "once a month", with a few days' slack
SHORT = "The copy opened, but it has far fewer patients than the live system. Check that copies are being made from the right database."


def record_drill(conn, name=None, info=None, error=None):
    """Write down a drill. A copy that opens but holds far fewer patients than the live system does not pass."""
    live = conn.execute("SELECT COUNT(*) FROM patients").fetchone()[0]
    ok, detail = (0, str(error)[:300]) if error else (1, None)
    got = info.get("patients") if info else None
    if ok and isinstance(got, int) and live >= 20 and got < live * 0.9:
        ok, detail = 0, SHORT
    conn.execute("""INSERT INTO restore_drills(ok, copy_name, made_at, patients, live_patients, files, detail)
                    VALUES (?,?,?,?,?,?,?)""", (ok, name, (info or {}).get("made_at"), got if isinstance(got, int) else None, live,
                                                (info or {}).get("files"), detail))
    conn.commit()
    return bool(ok), detail


def drill_status(conn, now=None, days=DRILL_EVERY_DAYS):
    """The last drill, the last good one, and whether one is due."""
    last = conn.execute("SELECT * FROM restore_drills ORDER BY id DESC LIMIT 1").fetchone()
    good = conn.execute("SELECT * FROM restore_drills WHERE ok = 1 ORDER BY id DESC LIMIT 1").fetchone()
    now = now or datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    due = not good or datetime.datetime.strptime(good["created_at"][:19], "%Y-%m-%d %H:%M:%S") < now - datetime.timedelta(days=days)
    return {"last": last, "last_ok": good, "due": due}


def restore_to(blob, passphrase, folder):
    """Unpack a copy into a folder of its own (database file and files folder). Returns the database path."""
    data = unlock(blob, passphrase)
    info = check(data)
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for n in z.namelist():
            target = (folder / n).resolve()
            if not str(target).startswith(str(folder.resolve()) + os.sep):
                raise OffsiteError("The copy holds a file outside its own folder.")
            if n.endswith("/"):
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(z.read(n))
    return folder / ("database.sqlite" if info["kind"] == "sqlite" else "database.dump"), info


def load_read_only(blob, passphrase, db_path, files_path):
    """For the read-only server: swap in the newest copy. The old database is replaced in one step, so a viewer never sees half."""
    with tempfile.TemporaryDirectory(dir=Path(db_path).parent) as tmp:
        path, info = restore_to(blob, passphrase, tmp)
        if info["kind"] != "sqlite":
            raise OffsiteError("A read-only copy from PostgreSQL is loaded with pg_restore by the person who runs the server.")
        os.replace(path, db_path)
        src = Path(tmp) / "files"
        if files_path and src.is_dir():
            dest = Path(files_path)
            old = dest.with_name(dest.name + ".old")
            if old.exists():
                shutil.rmtree(old)
            if dest.exists():
                dest.rename(old)
            shutil.move(str(src), str(dest))
            if old.exists():
                shutil.rmtree(old)
    stamp = Path(db_path).with_name("read_only_copy.json")
    stamp.write_text(json.dumps({"made_at": info["made_at"], "loaded_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")}))
    return info


def status(conn):
    last_ok = conn.execute("SELECT * FROM offsite_log WHERE ok = 1 ORDER BY id DESC LIMIT 1").fetchone()
    last = conn.execute("SELECT * FROM offsite_log ORDER BY id DESC LIMIT 1").fetchone()
    return {"last_ok": last_ok, "last": last, "recent": conn.execute("SELECT * FROM offsite_log ORDER BY id DESC LIMIT 20").fetchall()}


def stale(conn, hours=26, now=None):
    """True when no good copy has been made in the last `hours`."""
    row = conn.execute("SELECT created_at FROM offsite_log WHERE ok = 1 ORDER BY id DESC LIMIT 1").fetchone()
    if not row:
        return True
    now = now or datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    return datetime.datetime.strptime(row["created_at"][:19], "%Y-%m-%d %H:%M:%S") < now - datetime.timedelta(hours=hours)


# ------------------------------------------------------------------------------------------------------- the internet
def net_check(conn, url="https://www.gstatic.com/generate_204", transport=None, now=None):
    """Is the internet there? The answer is kept so every screen can say so."""
    now = (now or datetime.datetime.now(datetime.timezone.utc)).strftime("%Y-%m-%d %H:%M:%S")
    try:
        code, _ = (transport or _http)("GET", url, {}, None, 8)
        ok = code < 500
    except OffsiteError:
        ok = False
    row = conn.execute("SELECT * FROM net_status WHERE id = 1").fetchone()
    since = None if ok else ((row["failing_since"] if row and not row["ok"] and row["failing_since"] else now))
    conn.execute("""INSERT INTO net_status(id, ok, checked_at, failing_since) VALUES (1, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET ok = excluded.ok, checked_at = excluded.checked_at, failing_since = excluded.failing_since""",
                 (1 if ok else 0, now, since))
    conn.commit()
    return ok


def offline_since(conn, now=None, fresh_minutes=30):
    """When the internet went, if the last check (within the last half hour) found it gone. Otherwise None."""
    row = conn.execute("SELECT * FROM net_status WHERE id = 1").fetchone()
    if not row or row["ok"]:
        return None
    now = now or datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    if datetime.datetime.strptime(row["checked_at"], "%Y-%m-%d %H:%M:%S") < now - datetime.timedelta(minutes=fresh_minutes):
        return None
    return row["failing_since"]
