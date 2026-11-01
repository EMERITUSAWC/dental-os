"""The imaging bridge (version 0.25): X-rays go from the X-ray computer straight onto the right patient's record.

How it works:
1. At the chair, staff press "Take X-rays now" on the patient and pick the X-ray computer (a "station").
2. The imaging agent on that computer (tools/imaging_agent.py) asks every few seconds whether a patient is waiting. When one is, it can
   open the sensor software for that patient, if the clinic set up the command line the sensor software accepts.
3. Every new image the sensor software saves into the watched folder is sent here and filed on that patient as an X-ray.
4. A DICOM file that carries this system's patient number (DOS-123) in its Patient ID goes to that patient whatever is waiting,
   so files exported later, or in a batch, still land on the right record.

Any sensor that saves PNG, JPEG or DICOM files into a folder works, which is almost all of them. The agent talks to this system only
through the clinic's API key. No web framework here."""
import base64
import binascii
import datetime
import re

from . import audit, dicom, files as F

STATION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,39}$")
WAIT_MINUTES = 30
CHART = re.compile(r"^DOS-(\d{1,9})$")


class ImagingError(Exception):
    pass


def _now(db, minutes=0):
    """UTC, written the way SQLite writes datetime('now'), so it sorts and compares as text on both databases."""
    t = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=minutes)
    return t.strftime("%Y-%m-%d %H:%M:%S")


def _later(db, minutes):
    return _now(db, minutes)


def chart_id(pid):
    """What to put in the sensor software's patient ID box, so the DICOM files carry it."""
    return f"DOS-{pid}"


def stations(db, cid):
    return db.execute("SELECT * FROM imaging_stations WHERE clinic_id = ? ORDER BY name", (cid,)).fetchall()


def add_station(db, clinic, user_id, name):
    name = (name or "").strip()
    if not STATION.match(name):
        raise ImagingError("Name the X-ray computer with letters and digits, for example Surgery 1.")
    db.execute("INSERT INTO imaging_stations(clinic_id, name) VALUES (?,?) ON CONFLICT(clinic_id, name) DO NOTHING", (clinic["id"], name))
    audit.log(db, clinic["id"], user_id, "imaging.station", None, None, name)


def request_images(db, clinic, user_id, pid, station):
    cid = clinic["id"]
    if not db.execute("SELECT 1 FROM patients WHERE id = ? AND clinic_id = ? AND status = 'active'", (pid, cid)).fetchone():
        raise ImagingError("That patient was not found.")
    if not db.execute("SELECT 1 FROM imaging_stations WHERE clinic_id = ? AND name = ?", (cid, station or "")).fetchone():
        raise ImagingError("Pick one of the clinic's X-ray computers.")
    db.execute("UPDATE imaging_requests SET done_at = ? WHERE clinic_id = ? AND station = ? AND done_at IS NULL", (_now(db), cid, station))
    rid = db.execute("""INSERT INTO imaging_requests(clinic_id, patient_id, station, requested_by, requested_at, expires_at)
                        VALUES (?,?,?,?,?,?)""", (cid, pid, station, user_id, _now(db), _later(db, WAIT_MINUTES))).lastrowid
    audit.log(db, cid, user_id, "imaging.request", "patient", pid, station)
    return rid


def finish(db, clinic, user_id, rid):
    r = db.execute("SELECT * FROM imaging_requests WHERE id = ? AND clinic_id = ?", (rid, clinic["id"])).fetchone()
    if not r:
        raise ImagingError("That X-ray session was not found.")
    db.execute("UPDATE imaging_requests SET done_at = ? WHERE id = ? AND done_at IS NULL", (_now(db), rid))
    return r["patient_id"]


def waiting(db, cid, station):
    return db.execute("""SELECT r.*, p.first_name, p.last_name, p.dob FROM imaging_requests r JOIN patients p ON p.id = r.patient_id
                         WHERE r.clinic_id = ? AND r.station = ? AND r.done_at IS NULL AND r.expires_at > ? ORDER BY r.id DESC LIMIT 1""",
                      (cid, station, _now(db))).fetchone()


def for_patient(db, cid, pid):
    return db.execute("""SELECT * FROM imaging_requests WHERE clinic_id = ? AND patient_id = ? AND done_at IS NULL AND expires_at > ?
                         ORDER BY id DESC""", (cid, pid, _now(db))).fetchall()


def next_for(db, clinic, station):
    """What the agent asks every few seconds. Also marks the station as seen, so staff can tell the agent is running."""
    if not STATION.match(station or ""):
        raise ImagingError("Name the X-ray computer with letters and digits, for example Surgery 1.")
    cid = clinic["id"]
    db.execute("""INSERT INTO imaging_stations(clinic_id, name, last_seen) VALUES (?,?,?)
                  ON CONFLICT(clinic_id, name) DO UPDATE SET last_seen = excluded.last_seen""", (cid, station, _now(db)))
    r = waiting(db, cid, station)
    if not r:
        return None
    return {"id": r["id"], "patient": {"id": r["patient_id"], "first_name": r["first_name"], "last_name": r["last_name"],
                                       "dob": r["dob"], "chart": chart_id(r["patient_id"])}, "expires_at": r["expires_at"]}


def receive(db, clinic, user_id, station, file_name, data_b64, root, request_id=None, kind="xray", taken_on=None):
    """One image from the agent. Returns (file id, patient id)."""
    cid = clinic["id"]
    try:
        data = base64.b64decode(data_b64 or "", validate=True)
    except (binascii.Error, ValueError):
        raise ImagingError("The image data is not valid base64.")
    pid = None
    if data[128:132] == b"DICM":
        try:
            m = CHART.match(dicom.summary(dicom.parse(data)).get("patient_id", "").strip())
        except dicom.DicomError:
            m = None
        if m and db.execute("SELECT 1 FROM patients WHERE id = ? AND clinic_id = ?", (int(m.group(1)), cid)).fetchone():
            pid = int(m.group(1))
    req = None
    if request_id:
        req = db.execute("SELECT * FROM imaging_requests WHERE id = ? AND clinic_id = ?", (request_id, cid)).fetchone()
    elif station:
        req = waiting(db, cid, station)
    if pid is None:
        if not req or req["done_at"] or req["expires_at"] <= _now(db):
            raise ImagingError("No patient is waiting for X-rays on this computer. Press Take X-rays now on the patient first.")
        pid = req["patient_id"]
    if not taken_on:
        taken_on = datetime.date.today().isoformat()
    try:
        fid = F.save_bytes(db, clinic, user_id, pid, data, file_name, kind, "From the imaging bridge" + (f" ({station})" if station else ""),
                           taken_on, root)
    except F.FileError as exc:
        raise ImagingError(str(exc))
    if req and req["patient_id"] == pid:
        db.execute("UPDATE imaging_requests SET files = files + 1, expires_at = ? WHERE id = ?", (_later(db, 10) if req["expires_at"] < _later(db, 10) else req["expires_at"], req["id"]))
    return fid, pid
