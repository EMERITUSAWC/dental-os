"""Version 0.60: asking an X-ray AI company for a second look, with the dentist deciding.

Dental OS does not read X-rays itself. Software that finds disease on X-rays is a regulated medical device (in the US it needs
FDA clearance; Pearl, Overjet and VideaHealth hold clearances), so the clinic chooses a company that is allowed to do this where
the clinic works, and Dental OS sends the picture there and shows what comes back.

What leaves the server: the picture's pixels only, as a fresh PNG. A DICOM file's tags (which hold the patient's name, number and
date of birth) are dropped, and so are a photo's hidden details. No patient name, number, date of birth or record goes with it.
What comes back are suggestions. Each finding stays "suggested" until a dentist agrees or rejects it, and nothing changes the chart
or a plan by itself. Off until a manager switches it on with the company's address and key; the key is stored encrypted.

The request and answer below are Dental OS's own plain format. Each company has its own; a short adapter per company maps one to
the other once the clinic has an account with it (listed under "Waiting on an outside party"). The network call is passed in
(`transport`) so the tests never touch the internet.

  Request:  POST <address>  Authorization: Bearer <key>
            {"image": "<base64 PNG>", "mime": "image/png", "kind": "xray", "notation": "FDI"}
  Answer:   {"model": "name and version", "findings": [{"tooth": "36", "kind": "caries", "label": "Distal caries",
             "confidence": 0.87, "box": [x, y, width, height]}]}        box values are fractions of the picture, 0 to 1
"""
import base64
import datetime
import hashlib
import json
import urllib.error
import urllib.request

from . import audit, teeth, webhooks

KINDS = {"caries": "Decay", "periapical": "Change at the root tip", "bone_loss": "Bone loss", "calculus": "Calculus",
         "restoration": "Filling or crown", "margin": "Gap at a filling or crown edge", "impacted": "Impacted tooth",
         "missing": "Missing tooth", "other": "Other"}
STATES = {"suggested": "Suggested", "agreed": "Agreed by the dentist", "rejected": "Rejected by the dentist"}
PER_HOUR = 120                 # reads a clinic can ask for in an hour: a guard against runaway cost
MAX_FINDINGS = 200
DECIDERS = ("dentist", "manager")


class XrayAIError(Exception):
    pass


# ------------------------------------------------------------------------------------------------------------- the account
def _fernet(secret_key):
    from cryptography.fernet import Fernet
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(b"dental-os/xray-ai/" + str(secret_key).encode()).digest()))


def _open(secret_key, value):
    from cryptography.fernet import InvalidToken
    try:
        return _fernet(secret_key).decrypt(value.encode()).decode()
    except (InvalidToken, ValueError, AttributeError):
        return None


def account(db, cid):
    return db.execute("SELECT * FROM xray_ai_account WHERE clinic_id = ?", (cid,)).fetchone()


def is_on(db, cid, secret_key):
    a = account(db, cid)
    return bool(a and a["enabled"] and a["endpoint"] and a["api_key"] and _open(secret_key, a["api_key"]))


def save_account(db, clinic, user_id, secret_key, form):
    cid = clinic["id"]
    a = account(db, cid)
    vendor = (form.get("vendor") or "").strip()[:80]
    endpoint = (form.get("endpoint") or "").strip()
    key = (form.get("api_key") or "").strip()
    enabled = form.get("enabled") == "1"
    if endpoint:
        try:
            endpoint = webhooks.check_url(endpoint)
        except webhooks.WebhookError as exc:
            raise XrayAIError(str(exc))
    if key and (len(key) < 16 or any(c.isspace() for c in key)):
        raise XrayAIError("That key does not look right.")
    sealed = _fernet(secret_key).encrypt(key.encode()).decode() if key else (a["api_key"] if a else None)
    if enabled:
        if not (vendor and endpoint and sealed):
            raise XrayAIError("To switch it on, give the company's name, its https address and the clinic's key.")
        if form.get("agree") != "1":
            raise XrayAIError("Tick the box to confirm the clinic may send X-rays to this company and has told its patients.")
    if a:
        db.execute("""UPDATE xray_ai_account SET enabled = ?, vendor = ?, endpoint = ?, api_key = ?, updated_by = ?, updated_at = datetime('now')
                      WHERE clinic_id = ?""", (1 if enabled else 0, vendor or None, endpoint or None, sealed, user_id, cid))
    else:
        db.execute("""INSERT INTO xray_ai_account(clinic_id, enabled, vendor, endpoint, api_key, updated_by) VALUES (?,?,?,?,?,?)""",
                   (cid, 1 if enabled else 0, vendor or None, endpoint or None, sealed, user_id))
    audit.log(db, cid, user_id, "xrayai.account", detail="on" if enabled else "off")              # never the key


# ------------------------------------------------------------------------------------------------------------- one read
def default_transport(url, headers, body, timeout=60):
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:          # nosec: https address checked when it was saved
            return resp.status, resp.read(2_000_000).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(200_000).decode("utf-8", "replace")
    except (urllib.error.URLError, OSError, ValueError):
        raise XrayAIError("Could not reach the X-ray AI company. Try again later.")


def _num(v, lo, hi):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if lo <= f <= hi else None


def parse_answer(text):
    """(model, [finding]) from the company's answer. Strict: a finding that cannot be read is dropped, never guessed."""
    try:
        data = json.loads(text)
    except ValueError:
        raise XrayAIError("The X-ray AI company's answer could not be read.")
    if not isinstance(data, dict) or not isinstance(data.get("findings"), list):
        raise XrayAIError("The X-ray AI company's answer could not be read.")
    out = []
    for f in data["findings"][:MAX_FINDINGS]:
        if not isinstance(f, dict):
            continue
        tooth = None
        raw = f.get("tooth")
        if raw not in (None, ""):
            try:
                t = int(str(raw).strip())
            except ValueError:
                t = None
            tooth = t if t in teeth.VALID else None
        kind = f.get("kind") if f.get("kind") in KINDS else "other"
        label = " ".join(str(f.get("label") or "").split())[:120] or None
        conf = _num(f.get("confidence"), 0, 1)
        box = f.get("box")
        if isinstance(box, list) and len(box) == 4 and all(_num(v, 0, 1) is not None for v in box):
            x, y, w, h = (float(v) for v in box)
            box = [round(x, 4), round(y, 4), round(min(w, 1 - x), 4), round(min(h, 1 - y), 4)] if w > 0 and h > 0 else None
        else:
            box = None
        out.append({"tooth": tooth, "kind": kind, "label": label, "confidence": conf, "box": box})
    model = " ".join(str(data.get("model") or "").split())[:80] or None
    return model, out


def read(db, clinic, user, secret_key, root, file_id, transport=None):
    """Send one X-ray, keep what comes back. Returns the read's id. A failed try is kept too, with a plain reason."""
    from . import files, xrayqc
    cid = clinic["id"]
    f = files.get(db, cid, file_id)
    if not f or f["archived"]:
        raise LookupError("file")
    if not is_on(db, cid, secret_key):
        raise XrayAIError("The X-ray AI is not switched on. A manager sets it up under Settings.")
    if f["kind"] != "xray":
        raise XrayAIError("Only X-rays can be sent.")
    since = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    if db.execute("SELECT COUNT(*) FROM xray_ai_reads WHERE clinic_id = ? AND created_at >= ?", (cid, since)).fetchone()[0] >= PER_HOUR:
        raise XrayAIError("The clinic has sent a lot of X-rays in the last hour. Wait a little and try again.")
    try:
        png = xrayqc.pixels_png(files.path_for(root, f).read_bytes(), f["mime"])
    except (xrayqc.QualityError, OSError):
        raise XrayAIError("This X-ray could not be opened, so it was not sent.")
    a = account(db, cid)
    body = json.dumps({"image": base64.b64encode(png).decode(), "mime": "image/png", "kind": "xray", "notation": "FDI"}).encode()
    headers = {"Authorization": "Bearer " + _open(secret_key, a["api_key"]), "Content-Type": "application/json", "Accept": "application/json"}
    status, model, found, detail = "failed", None, [], None
    try:
        code, text = (transport or default_transport)(a["endpoint"], headers, body)
        if code == 200:
            model, found = parse_answer(text)
            status = "done"
        elif code in (401, 403):
            detail = "The company refused the key. A manager should check it under Settings."
        else:
            detail = "The company could not read it this time. Try again later."
    except XrayAIError as exc:
        detail = str(exc)
    rid = db.execute("""INSERT INTO xray_ai_reads(clinic_id, patient_id, file_id, vendor, model, status, detail, findings, created_by)
                        VALUES (?,?,?,?,?,?,?,?,?)""", (cid, f["patient_id"], f["id"], a["vendor"], model, status, detail, len(found), user["id"])).lastrowid
    for x in found:
        db.execute("""INSERT INTO xray_ai_findings(clinic_id, read_id, tooth, kind, label, confidence, box) VALUES (?,?,?,?,?,?,?)""",
                   (cid, rid, x["tooth"], x["kind"], x["label"], x["confidence"], json.dumps(x["box"]) if x["box"] else None))
    audit.log(db, cid, user["id"], "xrayai.read", "patient", f["patient_id"], status)
    return rid


# ------------------------------------------------------------------------------------------------------------- the dentist decides
def decide(db, clinic, user, finding_id, status, note=None):
    if user["role"] not in DECIDERS:
        raise PermissionError("Only a dentist can agree or reject a finding.")
    if status not in ("agreed", "rejected", "suggested"):
        raise XrayAIError("Pick agree or reject.")
    cid = clinic["id"]
    row = db.execute("""SELECT x.*, r.patient_id FROM xray_ai_findings x JOIN xray_ai_reads r ON r.id = x.read_id
                        WHERE x.id = ? AND x.clinic_id = ?""", (finding_id, cid)).fetchone()
    if not row:
        raise LookupError("finding")
    db.execute("""UPDATE xray_ai_findings SET status = ?, note = ?, decided_by = ?, decided_at = ? WHERE id = ? AND clinic_id = ?""",
               (status, (note or "").strip()[:300] or row["note"], None if status == "suggested" else user["id"],
                None if status == "suggested" else datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S"), finding_id, cid))
    audit.log(db, cid, user["id"], "xrayai.decide", "patient", row["patient_id"], status)
    return row["read_id"]


def for_file(db, cid, file_id):
    """The reads of one X-ray, newest first, each with its findings."""
    out = []
    for r in db.execute("""SELECT r.*, u.display_name FROM xray_ai_reads r LEFT JOIN users u ON u.id = r.created_by
                           WHERE r.clinic_id = ? AND r.file_id = ? ORDER BY r.id DESC""", (cid, file_id)).fetchall():
        found = []
        for x in db.execute("""SELECT x.*, u.display_name AS decider FROM xray_ai_findings x LEFT JOIN users u ON u.id = x.decided_by
                               WHERE x.clinic_id = ? AND x.read_id = ? ORDER BY x.tooth IS NULL, x.tooth, x.id""", (cid, r["id"])).fetchall():
            d = dict(x)
            d["box_list"] = json.loads(x["box"]) if x["box"] else None
            found.append(d)
        out.append({"read": r, "findings": found})
    return out


def waiting_count(db, cid):
    return db.execute("SELECT COUNT(*) FROM xray_ai_findings WHERE clinic_id = ? AND status = 'suggested'", (cid,)).fetchone()[0]
