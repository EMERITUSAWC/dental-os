"""Version 0.72: Dental OS HQ, engineering and compliance.

Engineering: the health of this server (version, off-site copies, restore drills, pages that broke), clinic servers that report in
(version, last copy, last drill, free disk, never records), features clinics ask for ranked by how many clinics want them, and the
notes for each release, which clinics see under Help once published.

Compliance: incidents in the service itself with a 72-hour clock, requests clinics make of us as the company holding their data,
the outside companies that touch clinic data, the registrations the company must renew, and how HQ's own access is kept in check.
No web framework here."""
import datetime
import hashlib
import secrets

from . import offsite
from .hq import HQError, NEED_SUBJECT, NOT_FOUND, BAD_DATE, _clean, _date, _log, _no_phone, _now
from ..version import VERSION

REQUEST_STATES = {"new": "New", "planned": "Planned", "building": "Being built", "done": "Shipped", "declined": "Not doing"}
SEVERITIES = {"low": "Low", "medium": "Medium", "high": "High"}
DATA_KINDS = {"patient_request": "Help with a patient's data request", "agreement": "Data processing agreement",
              "audit": "Audit or security questions", "end_of_contract": "End of contract: copy and deletion", "other": "Other"}
DATA_STATES = {"open": "Open", "done": "Done", "refused": "Refused, with reasons"}
REPORT_HOURS = 72                  # the strictest common rule (UK and EU GDPR, article 33): tell the regulator within 72 hours
DATA_DAYS = 30                     # answer clinics within a month, so they can meet their own one-month deadline
STALE_HOURS = 26                   # a clinic server that has not reported in a day (with slack)
REVIEW_DAYS = 90

BAD_KEY = "Unknown or switched-off install key."
NEED_VERSION = "Write the version and its notes."
SAME_VERSION = "That version already has notes."
SUB_FIELDS = "Write the company's name, what it does for us, what data reaches it and its country."


def _key_hash(key):
    return hashlib.sha256((key or "").encode()).hexdigest()


# --------------------------------------------------------------------------------------------------------------- engineering
def server_health(conn, now=None):
    now = now or datetime.datetime.now(datetime.timezone.utc)
    day = _now(now - datetime.timedelta(days=1))
    fortnight = _now(now - datetime.timedelta(days=14))
    ok_copy = conn.execute("SELECT MAX(created_at) FROM offsite_log WHERE ok = 1").fetchone()[0]
    bad_copy = conn.execute("SELECT MAX(created_at) FROM offsite_log WHERE ok = 0").fetchone()[0]
    drill = conn.execute("SELECT created_at, ok FROM restore_drills ORDER BY id DESC LIMIT 1").fetchone()
    top = conn.execute("""SELECT endpoint, COUNT(*) AS n FROM error_log WHERE created_at >= ? GROUP BY endpoint ORDER BY n DESC LIMIT 8""",
                       (fortnight,)).fetchall()
    return {"version": VERSION, "copy_ok_at": ok_copy, "copy_failed_at": bad_copy if bad_copy and (not ok_copy or bad_copy > ok_copy) else None,
            "copy_stale": not ok_copy or ok_copy < day, "drill": drill,
            "drill_due": not drill or drill["created_at"] < _now(now - datetime.timedelta(days=offsite.DRILL_EVERY_DAYS)),
            "errors_day": conn.execute("SELECT COUNT(*) FROM error_log WHERE created_at >= ?", (day,)).fetchone()[0],
            "errors_fortnight": conn.execute("SELECT COUNT(*) FROM error_log WHERE created_at >= ?", (fortnight,)).fetchone()[0],
            "top": top}


def add_install(conn, op_id, label, clinic_id=None):
    """A clinic server that will report in. Returns (id, key): the key is shown once and only its hash is kept."""
    label = _clean(label, 80)
    if not label:
        raise HQError(NEED_SUBJECT)
    if clinic_id and not conn.execute("SELECT 1 FROM clinics WHERE id = ?", (clinic_id,)).fetchone():
        raise HQError(NOT_FOUND)
    key = "dosi_" + secrets.token_urlsafe(32)
    iid = conn.execute("INSERT INTO hq_installs(clinic_id, label, key_hash, created_by) VALUES (?,?,?,?)",
                       (clinic_id or None, label, _key_hash(key), op_id)).lastrowid
    _log(conn, op_id, "install.add", f"#{iid} {label}", clinic_id or None)
    return iid, key


def switch_install(conn, op_id, iid, on):
    if not conn.execute("SELECT 1 FROM hq_installs WHERE id = ?", (iid,)).fetchone():
        raise HQError(NOT_FOUND)
    conn.execute("UPDATE hq_installs SET active = ? WHERE id = ?", (1 if on else 0, iid))
    _log(conn, op_id, "install.on" if on else "install.off", f"#{iid}")


def _stamp(v):
    """A time the clinic server sent: accept 'YYYY-MM-DD HH:MM:SS' or ISO with T and Z, keep the first 19 characters."""
    v = str(v or "").strip().replace("T", " ").replace("Z", "")[:19]
    try:
        datetime.datetime.strptime(v, "%Y-%m-%d %H:%M:%S")
        return v
    except ValueError:
        return None


def checkin(conn, key, report, now=None):
    """A clinic server reports in. Only these fields are kept; anything else in the report is ignored."""
    row = conn.execute("SELECT * FROM hq_installs WHERE key_hash = ? AND active = 1", (_key_hash(key),)).fetchone()
    if not row or not key:
        raise HQError(BAD_KEY)
    report = report if isinstance(report, dict) else {}
    disk = report.get("disk_free_pct")
    disk = int(disk) if isinstance(disk, (int, float)) and 0 <= disk <= 100 else None
    version = _clean(str(report.get("version") or ""), 20) or None
    conn.execute("""UPDATE hq_installs SET version = ?, checked_in_at = ?, last_copy_at = ?, last_copy_failed_at = ?, last_drill_at = ?,
                    disk_free_pct = ? WHERE id = ?""",
                 (version, _now(now), _stamp(report.get("last_copy_at")), _stamp(report.get("last_copy_failed_at")),
                  _stamp(report.get("last_drill_at")), disk, row["id"]))
    return row["id"]


def my_report(conn):
    """What this server sends when it reports in."""
    import shutil
    ok = conn.execute("SELECT MAX(created_at) FROM offsite_log WHERE ok = 1").fetchone()[0]
    bad = conn.execute("SELECT MAX(created_at) FROM offsite_log WHERE ok = 0").fetchone()[0]
    drill = conn.execute("SELECT MAX(created_at) FROM restore_drills WHERE ok = 1").fetchone()[0]
    try:
        u = shutil.disk_usage(".")
        disk = int(u.free * 100 / u.total)
    except OSError:
        disk = None
    return {"version": VERSION, "last_copy_at": ok, "last_copy_failed_at": bad, "last_drill_at": drill, "disk_free_pct": disk}


def send_report(cfg, conn, transport=None):
    """Post this server's report to HQ (HQ_URL with HQ_INSTALL_KEY). Returns the HTTP status."""
    import json
    import urllib.request
    url, key = (cfg.get("HQ_URL") or "").rstrip("/"), cfg.get("HQ_INSTALL_KEY") or ""
    if not url.startswith("https://") or not key:
        raise HQError("Set HQ_URL (https) and HQ_INSTALL_KEY on this server first.")
    body = json.dumps(my_report(conn)).encode()
    headers = {"Authorization": "Bearer " + key, "Content-Type": "application/json"}
    if transport:
        return transport(url + "/hq/checkin", headers, body)
    req = urllib.request.Request(url + "/hq/checkin", data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=20) as resp:          # noqa: S310 - https only, checked above
        return resp.status


def installs(conn, now=None):
    now = now or datetime.datetime.now(datetime.timezone.utc)
    stale = _now(now - datetime.timedelta(hours=STALE_HOURS))
    day = _now(now - datetime.timedelta(days=1))
    drill_due = _now(now - datetime.timedelta(days=offsite.DRILL_EVERY_DAYS))
    out = []
    for r in conn.execute("SELECT i.*, c.name AS clinic_name FROM hq_installs i LEFT JOIN clinics c ON c.id = i.clinic_id ORDER BY i.active DESC, i.label").fetchall():
        flags = []
        if r["active"]:
            if not r["checked_in_at"] or r["checked_in_at"] < stale:
                flags.append("silent")
            if r["version"] and r["version"] != VERSION:
                flags.append("old_version")
            if not r["last_copy_at"] or r["last_copy_at"] < day:
                flags.append("no_copy")
            if not r["last_drill_at"] or r["last_drill_at"] < drill_due:
                flags.append("drill_due")
            if r["disk_free_pct"] is not None and r["disk_free_pct"] < 15:
                flags.append("disk")
        out.append({"i": r, "flags": flags})
    return out


INSTALL_FLAGS = {"silent": "Not heard from in a day", "old_version": "Older version", "no_copy": "No off-site copy in a day",
                 "drill_due": "Restore drill due", "disk": "Disk nearly full"}


def save_request(conn, op_id, request_id, title, detail, status=None, version=None):
    title, detail = _clean(title, 160), _clean(detail, 4000)
    if not title:
        raise HQError(NEED_SUBJECT)
    _no_phone(title, detail)
    status = status if status in REQUEST_STATES else "new"
    if request_id:
        if not conn.execute("SELECT 1 FROM hq_requests WHERE id = ?", (request_id,)).fetchone():
            raise HQError(NOT_FOUND)
        conn.execute("UPDATE hq_requests SET title = ?, detail = ?, status = ?, version = ? WHERE id = ?",
                     (title, detail or None, status, _clean(version, 20) or None, request_id))
    else:
        request_id = conn.execute("INSERT INTO hq_requests(title, detail, status, created_by) VALUES (?,?,?,?)",
                                  (title, detail or None, status, op_id)).lastrowid
    _log(conn, op_id, "request.save", f"#{request_id} {status}")
    return request_id


def want(conn, op_id, request_id, clinic_id, ticket_id=None):
    """A clinic wants this feature. Once per clinic; returns False when it was already counted."""
    if not conn.execute("SELECT 1 FROM hq_requests WHERE id = ?", (request_id,)).fetchone():
        raise HQError(NOT_FOUND)
    if not conn.execute("SELECT 1 FROM clinics WHERE id = ?", (clinic_id,)).fetchone():
        raise HQError(NOT_FOUND)
    if conn.execute("SELECT 1 FROM hq_request_clinics WHERE request_id = ? AND clinic_id = ?", (request_id, clinic_id)).fetchone():
        return False
    conn.execute("INSERT INTO hq_request_clinics(request_id, clinic_id, ticket_id, added_by) VALUES (?,?,?,?)",
                 (request_id, clinic_id, ticket_id, op_id))
    return True


def requests(conn, which="open"):
    sql = "r.status IN ('new','planned','building')" if which == "open" else "1 = 1"
    return conn.execute(f"""SELECT r.*, (SELECT COUNT(*) FROM hq_request_clinics x WHERE x.request_id = r.id) AS clinics,
                            (SELECT COUNT(*) FROM hq_request_clinics x JOIN clinics c ON c.id = x.clinic_id
                             WHERE x.request_id = r.id AND c.plan_state IN ('active','past_due')) AS paying
                            FROM hq_requests r WHERE {sql} ORDER BY clinics DESC, r.id""").fetchall()


def request_clinics(conn, request_id):
    return conn.execute("""SELECT x.*, c.name AS clinic_name FROM hq_request_clinics x JOIN clinics c ON c.id = x.clinic_id
                           WHERE x.request_id = ? ORDER BY c.name""", (request_id,)).fetchall()


def save_release(conn, op_id, version, released_on, notes, published):
    version, notes = _clean(version, 20), _clean(notes, 8000)
    if not version or not notes:
        raise HQError(NEED_VERSION)
    on = _date(released_on) or datetime.date.today().isoformat()
    row = conn.execute("SELECT id FROM hq_releases WHERE version = ?", (version,)).fetchone()
    if row:
        conn.execute("UPDATE hq_releases SET released_on = ?, notes = ?, published = ? WHERE id = ?", (on, notes, 1 if published else 0, row["id"]))
        rid = row["id"]
    else:
        rid = conn.execute("INSERT INTO hq_releases(version, released_on, notes, published, created_by) VALUES (?,?,?,?,?)",
                           (version, on, notes, 1 if published else 0, op_id)).lastrowid
    _log(conn, op_id, "release.save", version)
    return rid


def releases(conn, published_only=False):
    return conn.execute("SELECT * FROM hq_releases" + (" WHERE published = 1" if published_only else "") + " ORDER BY released_on DESC, id DESC").fetchall()


# ---------------------------------------------------------------------------------------------------------------- compliance
def _dt(v):
    v = (v or "").strip().replace("T", " ")
    if not v:
        return None
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.datetime.strptime(v[:19], fmt).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
    raise HQError(BAD_DATE)


def save_incident(conn, op_id, incident_id, f):
    title, what = _clean(f.get("title"), 160), _clean(f.get("what"), 4000)
    if not title or not what:
        raise HQError(NEED_SUBJECT)
    _no_phone(title, what, f.get("lessons"))
    found = _dt(f.get("found_at")) or _now()
    sev = f.get("severity") if f.get("severity") in SEVERITIES else "medium"
    n = f.get("clinics_affected")
    n = int(n) if str(n or "").isdigit() and int(n) < 100000 else None
    vals = (title, found, what, sev, n, _dt(f.get("regulator_told_at")), _dt(f.get("clinics_told_at")), _dt(f.get("closed_at")),
            _clean(f.get("lessons"), 4000) or None)
    if incident_id:
        if not conn.execute("SELECT 1 FROM hq_incidents WHERE id = ?", (incident_id,)).fetchone():
            raise HQError(NOT_FOUND)
        conn.execute("""UPDATE hq_incidents SET title = ?, found_at = ?, what = ?, severity = ?, clinics_affected = ?, regulator_told_at = ?,
                        clinics_told_at = ?, closed_at = ?, lessons = ? WHERE id = ?""", vals + (incident_id,))
    else:
        incident_id = conn.execute("""INSERT INTO hq_incidents(title, found_at, what, severity, clinics_affected, regulator_told_at, clinics_told_at,
                                      closed_at, lessons, owner_id, created_by) VALUES (?,?,?,?,?,?,?,?,?,?,?)""", vals + (op_id, op_id)).lastrowid
    _log(conn, op_id, "incident.save", f"#{incident_id} {sev}")
    return incident_id


def incident_clock(r, now=None):
    """Hours left to tell the regulator (negative when late); None once told or closed."""
    if r["regulator_told_at"] or r["closed_at"]:
        return None
    now = now or datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    found = datetime.datetime.strptime(r["found_at"], "%Y-%m-%d %H:%M:%S")
    return round(REPORT_HOURS - (now.replace(tzinfo=None) - found).total_seconds() / 3600, 1)


def incidents(conn, now=None):
    return [{"r": r, "hours_left": incident_clock(r, now)}
            for r in conn.execute("SELECT * FROM hq_incidents ORDER BY CASE WHEN closed_at IS NULL THEN 0 ELSE 1 END, found_at DESC").fetchall()]


def save_data_request(conn, op_id, req_id, f):
    summary = _clean(f.get("summary"), 400)
    if not summary:
        raise HQError(NEED_SUBJECT)
    _no_phone(summary, f.get("outcome"))
    kind = f.get("kind") if f.get("kind") in DATA_KINDS else "other"
    received = _date(f.get("received_on")) or datetime.date.today().isoformat()
    due = _date(f.get("due_on")) or (datetime.date.fromisoformat(received) + datetime.timedelta(days=DATA_DAYS)).isoformat()
    status = f.get("status") if f.get("status") in DATA_STATES else "open"
    cid = f.get("clinic_id")
    cid = int(cid) if str(cid or "").isdigit() else None
    if cid and not conn.execute("SELECT 1 FROM clinics WHERE id = ?", (cid,)).fetchone():
        raise HQError(NOT_FOUND)
    closed = datetime.date.today().isoformat() if status != "open" else None
    vals = (cid, kind, summary, received, due, status, _clean(f.get("outcome"), 2000) or None)
    if req_id:
        if not conn.execute("SELECT 1 FROM hq_data_requests WHERE id = ?", (req_id,)).fetchone():
            raise HQError(NOT_FOUND)
        conn.execute("""UPDATE hq_data_requests SET clinic_id = ?, kind = ?, summary = ?, received_on = ?, due_on = ?, status = ?, outcome = ?
                        WHERE id = ?""", vals + (req_id,))
        if closed:
            conn.execute("UPDATE hq_data_requests SET closed_on = COALESCE(closed_on, ?) WHERE id = ?", (closed, req_id))
        else:
            conn.execute("UPDATE hq_data_requests SET closed_on = NULL WHERE id = ?", (req_id,))
    else:
        req_id = conn.execute("""INSERT INTO hq_data_requests(clinic_id, kind, summary, received_on, due_on, status, outcome, closed_on, owner_id, created_by)
                                 VALUES (?,?,?,?,?,?,?,?,?,?)""", vals + (closed, op_id, op_id)).lastrowid
    _log(conn, op_id, "datareq.save", f"#{req_id} {status}", cid)
    return req_id


def data_requests(conn, today=None):
    today = (today or datetime.date.today()).isoformat()
    rows = conn.execute("""SELECT d.*, c.name AS clinic_name FROM hq_data_requests d LEFT JOIN clinics c ON c.id = d.clinic_id
                           ORDER BY CASE d.status WHEN 'open' THEN 0 ELSE 1 END, d.due_on""").fetchall()
    return [{"r": r, "late": r["status"] == "open" and r["due_on"] < today} for r in rows]


def save_subprocessor(conn, op_id, sub_id, f):
    name, purpose, data, country = (_clean(f.get(k), 160) for k in ("name", "purpose", "data", "country"))
    if not (name and purpose and data and country):
        raise HQError(SUB_FIELDS)
    vals = (name, purpose, data, country, _clean(f.get("safeguard"), 300) or None, _date(f.get("agreement_on")), _date(f.get("review_on")),
            0 if f.get("active") == "0" else 1, op_id, _now())
    if sub_id:
        if not conn.execute("SELECT 1 FROM hq_subprocessors WHERE id = ?", (sub_id,)).fetchone():
            raise HQError(NOT_FOUND)
        conn.execute("""UPDATE hq_subprocessors SET name = ?, purpose = ?, data = ?, country = ?, safeguard = ?, agreement_on = ?, review_on = ?,
                        active = ?, updated_by = ?, updated_at = ? WHERE id = ?""", vals + (sub_id,))
    else:
        sub_id = conn.execute("""INSERT INTO hq_subprocessors(name, purpose, data, country, safeguard, agreement_on, review_on, active, updated_by, updated_at)
                                 VALUES (?,?,?,?,?,?,?,?,?,?)""", vals).lastrowid
    _log(conn, op_id, "subprocessor.save", name)
    return sub_id


def subprocessors(conn, today=None):
    today = (today or datetime.date.today()).isoformat()
    return [{"s": r, "no_agreement": not r["agreement_on"], "review_due": bool(r["review_on"] and r["review_on"] <= today)}
            for r in conn.execute("SELECT * FROM hq_subprocessors ORDER BY active DESC, name").fetchall()]


def save_registration(conn, op_id, reg_id, f):
    name = _clean(f.get("name"), 160)
    if not name:
        raise HQError(NEED_SUBJECT)
    vals = (name, _clean(f.get("body"), 160) or None, _clean(f.get("reference"), 80) or None, _date(f.get("renews_on")),
            _clean(f.get("note"), 400) or None, op_id, _now())
    if reg_id:
        if not conn.execute("SELECT 1 FROM hq_registrations WHERE id = ?", (reg_id,)).fetchone():
            raise HQError(NOT_FOUND)
        conn.execute("UPDATE hq_registrations SET name = ?, body = ?, reference = ?, renews_on = ?, note = ?, updated_by = ?, updated_at = ? WHERE id = ?",
                     vals + (reg_id,))
    else:
        reg_id = conn.execute("INSERT INTO hq_registrations(name, body, reference, renews_on, note, updated_by, updated_at) VALUES (?,?,?,?,?,?,?)",
                              vals).lastrowid
    _log(conn, op_id, "registration.save", name)
    return reg_id


def registrations(conn, today=None):
    today = today or datetime.date.today()
    soon = (today + datetime.timedelta(days=60)).isoformat()
    return [{"r": r, "late": bool(r["renews_on"] and r["renews_on"] < today.isoformat()), "soon": bool(r["renews_on"] and r["renews_on"] <= soon)}
            for r in conn.execute("SELECT * FROM hq_registrations ORDER BY COALESCE(renews_on, '9999'), name").fetchall()]


def access_check(conn, today=None):
    """How HQ's own access stands: the last team review, people without two-step login, logins not used in 60 days,
    logins past their end day still switched on, and how often support opened each clinic in the last 90 days."""
    from . import opteam
    today = today or datetime.date.today()
    last = opteam.last_review(conn)
    review_due = not last or last["at"][:10] < (today - datetime.timedelta(days=REVIEW_DAYS)).isoformat()
    unused_since = (today - datetime.timedelta(days=60)).isoformat()
    people = []
    for x in opteam.team(conn):
        o = x["o"]
        if not o["active"]:
            continue
        issues = []
        if not x["two_step"]:
            issues.append("no_2fa")
        if not x["last"] or x["last"][:10] < unused_since:
            issues.append("unused")
        if not x["in_dates"]:
            issues.append("past_end")
        if issues:
            people.append({"o": o, "issues": issues})
    support = conn.execute("""SELECT c.name, COUNT(*) AS n FROM operator_log l JOIN clinics c ON c.id = l.clinic_id
                              WHERE l.action = 'support.open' AND l.at >= ? GROUP BY c.name ORDER BY n DESC""",
                           ((today - datetime.timedelta(days=90)).isoformat(),)).fetchall()
    return {"last": last, "review_due": review_due, "people": people, "support": support}


ACCESS_ISSUES = {"no_2fa": "No two-step login", "unused": "Not used in 60 days", "past_end": "Past its end day"}
