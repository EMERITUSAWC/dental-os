#!/usr/bin/env python3
"""Dental OS imaging agent (version 0.25).

Runs on the computer the X-ray sensor is plugged into. It watches the folder (or folders) where the sensor software saves images,
and sends each new image to Dental OS, which files it on the patient staff picked with "Take X-rays now". A DICOM file that carries
the patient's Dental OS number (DOS-123) in its Patient ID is filed on that patient whenever it arrives.

Needs Python 3.9 or later and nothing else.

    python imaging_agent.py --setup            writes imaging_agent.json next to this file, to fill in
    python imaging_agent.py                    runs until stopped (Ctrl+C)
    python imaging_agent.py --once             one look at the folders, then stops (for testing)

Sent images move into a "sent" folder beside them, by date. Images that cannot be filed move into "failed", with a note saying why.
Nothing is ever deleted. The API key needs read and write access.

Opening the sensor software for the waiting patient (optional): many imaging programs accept a patient on their command line. Put
that command in "launch", as a list, with any of {chart} {patient_id} {first_name} {last_name} {dob} where the patient's details go.
Check your imaging program's own guide for the exact form it accepts."""
import argparse
import base64
import datetime
import json
import os
import shutil
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_CONFIG = HERE / "imaging_agent.json"
EXAMPLE = {
    "server": "https://your-clinic.example.com",
    "api_key": "paste the read and write key here",
    "station": "Surgery 1",
    "watch": ["C:\\\\XrayExport"],
    "extensions": [".dcm", ".jpg", ".jpeg", ".png"],
    "poll_seconds": 3,
    "launch": None,
}
MAX_BYTES = 10 * 1024 * 1024
SETTLE_SECONDS = 2


def log(msg):
    print(datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True)


def http_transport(verify=True):
    ctx = ssl.create_default_context() if verify else ssl._create_unverified_context()   # noqa: S323  only when the clinic says so

    def call(method, url, headers, body=None):
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=60, context=ctx) as resp:
                return resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", "replace")
    return call


class Agent:
    def __init__(self, config, transport=None, launcher=None):
        self.cfg = dict(EXAMPLE, **config)
        self.http = transport or http_transport(self.cfg.get("verify_tls", True))
        self.launcher = launcher or (lambda args: subprocess.Popen(args))         # noqa: S603  a command the clinic wrote
        self.sizes = {}
        self.current = None

    # ---------------------------------------------------------------------------------------------------- talking to Dental OS
    def _url(self, path, **q):
        base = self.cfg["server"].rstrip("/") + "/api/v1" + path
        return base + ("?" + urllib.parse.urlencode(q) if q else "")

    def _headers(self):
        return {"Authorization": "Bearer " + self.cfg["api_key"], "Content-Type": "application/json", "Accept": "application/json"}

    def check_waiting(self):
        """Ask which patient is waiting on this computer. Opens the sensor software for a new one, if set up."""
        status, text = self.http("GET", self._url("/imaging/next", station=self.cfg["station"]), self._headers())
        if status != 200:
            log(f"Dental OS said {status}: {text[:200]}")
            return None
        req = json.loads(text).get("data")
        if req and (not self.current or self.current["id"] != req["id"]):
            p = req["patient"]
            log(f"Waiting for X-rays: {p['first_name']} {p['last_name']} ({p['chart']})")
            self._launch(p)
        self.current = req
        return req

    def _launch(self, p):
        cmd = self.cfg.get("launch")
        if not cmd:
            return
        values = {"chart": p["chart"], "patient_id": str(p["id"]), "first_name": p["first_name"], "last_name": p["last_name"],
                  "dob": (p.get("dob") or "").replace("-", "")}
        try:
            self.launcher([str(part).format(**values) for part in cmd])
        except (OSError, KeyError, ValueError) as exc:
            log(f"Could not open the imaging program: {exc}")

    def send(self, path):
        """Send one file. Returns 'sent', 'wait' (no patient yet: try again later) or 'failed'."""
        data = path.read_bytes()
        if len(data) > MAX_BYTES:
            return self._failed(path, "The file is bigger than 10 MB.")
        body = json.dumps({"station": self.cfg["station"], "file_name": path.name, "data_base64": base64.b64encode(data).decode(),
                           "kind": "xray", "taken_on": datetime.date.fromtimestamp(path.stat().st_mtime).isoformat()}).encode()
        try:
            status, text = self.http("POST", self._url("/imaging/files"), self._headers(), body)
        except (urllib.error.URLError, OSError) as exc:
            log(f"Could not reach Dental OS, will try again: {exc}")
            return "wait"
        if status == 201:
            info = json.loads(text)["data"]
            self._move(path, "sent")
            log(f"Sent {path.name} to patient {info['patient_id']}")
            return "sent"
        try:
            message = json.loads(text)["error"]["message"]
        except (ValueError, KeyError, TypeError):
            message = text[:200]
        if status == 422 and "No patient is waiting" in message:
            return "wait"
        if status in (401, 403, 429) or status >= 500:
            log(f"Dental OS said {status}: {message}. Will try again.")
            return "wait"
        return self._failed(path, message)

    # ------------------------------------------------------------------------------------------------------------- folders
    def _move(self, path, where):
        day = datetime.date.today().isoformat()
        dest = path.parent / where / day
        dest.mkdir(parents=True, exist_ok=True)
        target = dest / path.name
        n = 1
        while target.exists():
            target = dest / f"{path.stem}-{n}{path.suffix}"
            n += 1
        shutil.move(str(path), str(target))
        return target

    def _failed(self, path, why):
        target = self._move(path, "failed")
        target.with_suffix(target.suffix + ".txt").write_text(why + "\n", encoding="utf-8")
        log(f"Could not file {path.name}: {why}")
        return "failed"

    def ready_files(self, now=None):
        """New files whose size has stopped changing, so the sensor software has finished writing them."""
        now = now or time.time()
        out = []
        exts = {e.lower() for e in self.cfg["extensions"]}
        for folder in self.cfg["watch"]:
            d = Path(folder)
            if not d.is_dir():
                continue
            for p in sorted(d.iterdir()):
                if not p.is_file() or p.suffix.lower() not in exts or p.name.startswith("."):
                    continue
                st = p.stat()
                key = str(p)
                if self.sizes.get(key) == st.st_size and now - st.st_mtime >= SETTLE_SECONDS:
                    out.append(p)
                self.sizes[key] = st.st_size
        return out

    def run_once(self, now=None):
        self.check_waiting()
        results = {}
        for p in self.ready_files(now):
            results[p.name] = self.send(p)
            if results[p.name] != "wait":
                self.sizes.pop(str(p), None)
        return results

    def run(self):
        log(f"Imaging agent for {self.cfg['station']} started. Watching: {', '.join(self.cfg['watch'])}")
        while True:
            try:
                self.run_once()
            except (urllib.error.URLError, OSError, ValueError) as exc:
                log(f"Problem, will try again: {exc}")
            time.sleep(max(1, int(self.cfg.get("poll_seconds") or 3)))


def main(argv=None):
    ap = argparse.ArgumentParser(description="Send X-rays from this computer to Dental OS.")
    ap.add_argument("--config", default=str(DEFAULT_CONFIG))
    ap.add_argument("--setup", action="store_true", help="write a settings file to fill in")
    ap.add_argument("--once", action="store_true", help="look once, then stop")
    a = ap.parse_args(argv)
    path = Path(a.config)
    if a.setup:
        if path.exists():
            print(f"{path} already exists. Edit it, or delete it first.")
            return 1
        path.write_text(json.dumps(EXAMPLE, indent=2), encoding="utf-8")
        print(f"Wrote {path}. Fill in the server address, the API key, this computer's name and the folder to watch.")
        return 0
    if not path.exists():
        print(f"No settings file at {path}. Run with --setup first.")
        return 1
    cfg = json.loads(path.read_text(encoding="utf-8"))
    for k in ("server", "api_key", "station", "watch"):
        if not cfg.get(k):
            print(f"The settings file needs a value for {k}.")
            return 1
    agent = Agent(cfg)
    if a.once:
        agent.run_once(now=time.time() + SETTLE_SECONDS)
        print(json.dumps(agent.run_once(now=time.time() + SETTLE_SECONDS), indent=2))
        return 0
    try:
        agent.run()
    except KeyboardInterrupt:
        log("Stopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
