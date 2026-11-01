"""Version 0.63: a live connection to a US dental clearinghouse, so claims, cover checks, acknowledgements and remittances move
without anyone downloading or uploading files.

The files are the ones services/edi.py already makes and reads (837D, 270, 271, 835, 999); this module carries them, and reads one
more kind, the 277CA claim acknowledgement. Each clearinghouse is an adapter with three jobs: send a claim, ask a cover question,
and fetch what has come back. The first adapter is Stedi, built from its public API reference:

  send a claim     POST https://healthcare.us.stedi.com/2024-04-01/dental-claims/raw-x12-submission     {"x12": "<837D>"}
                   the answer carries Stedi's first 277CA in "x12", and claim edit errors in "errors"
  cover question   POST https://healthcare.us.stedi.com/2024-04-01/change/medicalnetwork/eligibility/v3/raw-x12   {"x12": "<270>"}
                   the answer carries the payer's 271 (or a 999 when the question itself was wrong) in "x12"
  what came back   GET  https://core.us.stedi.com/2023-08-01/polling/transactions?pageToken=... (or startDateTime=...)
                   inbound 277 and 835 transactions; the raw X12 is the artifact whose usage is "input"
  every call       Authorization: <the clinic's API key>  (a test key for test files, a live key for live ones)

NOT yet tried against Stedi's own test account: the first pilot run should be with a test key and Stedi's test payer.
The network call is passed in (`transport`) so the tests never touch the internet. The key is stored encrypted.
"""
import base64
import datetime
import hashlib
import json
import urllib.error
import urllib.parse
import urllib.request

from . import audit, edi as E, insurance as INS
from .timeutil import now_local

PROVIDERS = {"stedi": "Stedi"}
STEDI = {
    "claim": "https://healthcare.us.stedi.com/2024-04-01/dental-claims/raw-x12-submission",
    "eligibility": "https://healthcare.us.stedi.com/2024-04-01/change/medicalnetwork/eligibility/v3/raw-x12",
    "poll": "https://core.us.stedi.com/2023-08-01/polling/transactions",
    "receiver": {"T": "STEDITEST", "P": "STEDI"},          # Stedi's receiver ID (ISA08) for test and live files
}
# X12 claim status category codes, the first part of STC01 in a 277CA.
ACCEPTED = {"A0": "Forwarded on by the clearinghouse", "A1": "Received", "A2": "Accepted for processing by the payer", "A5": "Split into more claims"}
RETURNED = {"A3": "Returned: the claim could not be processed", "A4": "Returned: not found", "A6": "Returned: information is missing",
            "A7": "Returned: information is not valid", "A8": "Returned: two fields do not agree"}


class ClearinghouseError(Exception):
    """str(exc) is a whole sentence for the screen; exc.detail holds what the clearinghouse said, for the connection log."""
    def __init__(self, message, detail=None):
        super().__init__(message)
        self.detail = detail


# ------------------------------------------------------------------------------------------------------------- the account
def _fernet(secret_key):
    from cryptography.fernet import Fernet
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(b"dental-os/clearinghouse/" + str(secret_key).encode()).digest()))


def account(db, cid):
    return db.execute("SELECT * FROM clearinghouse_account WHERE clinic_id = ?", (cid,)).fetchone()


def is_on(db, cid):
    a = account(db, cid)
    return bool(a and a["enabled"] and a["api_key"])


def _key(secret_key, a):
    from cryptography.fernet import InvalidToken
    try:
        return _fernet(secret_key).decrypt(a["api_key"].encode()).decode()
    except (InvalidToken, ValueError, AttributeError):
        raise ClearinghouseError("The clearinghouse key cannot be read. A manager should enter it again.")


def save_account(db, clinic, user_id, secret_key, form):
    cid = clinic["id"]
    a = account(db, cid)
    provider = form.get("provider") or "stedi"
    if provider not in PROVIDERS:
        raise ClearinghouseError("Pick the clearinghouse.")
    key = (form.get("api_key") or "").strip()
    enabled = form.get("enabled") == "1"
    if key and (len(key) < 16 or any(c.isspace() for c in key)):
        raise ClearinghouseError("That key does not look right.")
    sealed = _fernet(secret_key).encrypt(key.encode()).decode() if key else (a["api_key"] if a else None)
    if enabled and not sealed:
        raise ClearinghouseError("To switch it on you need the clinic's key from the clearinghouse.")
    if a:
        db.execute("UPDATE clearinghouse_account SET enabled = ?, provider = ?, api_key = ?, updated_by = ?, updated_at = datetime('now') WHERE clinic_id = ?",
                   (1 if enabled else 0, provider, sealed, user_id, cid))
    else:
        db.execute("INSERT INTO clearinghouse_account(clinic_id, enabled, provider, api_key, updated_by) VALUES (?,?,?,?,?)",
                   (cid, 1 if enabled else 0, provider, sealed, user_id))
    audit.log(db, cid, user_id, "clearinghouse.account", detail=f"{provider} {'on' if enabled else 'off'}")


def setup_problems(db, clinic):
    """What stops sending, in plain words. The receiver ID has to be the clearinghouse's own, test or live to match the files."""
    s = E.settings(db, clinic["id"])
    out = []
    want = STEDI["receiver"].get(s.get("usage") or "T")
    if (s.get("receiver_id") or "").upper() != want:
        out.append("For test files with Stedi, the receiver ID under Electronic claims, Settings must be STEDITEST." if want == "STEDITEST"
                   else "For live files with Stedi, the receiver ID under Electronic claims, Settings must be STEDI.")
    return out


def log(db, cid, user_id, kind, ok, detail, ref=None):
    db.execute("INSERT INTO clearinghouse_log(clinic_id, kind, ok, detail, ref, user_id) VALUES (?,?,?,?,?,?)",
               (cid, kind, 1 if ok else 0, (detail or "")[:500] or None, (ref or "")[:120] or None, user_id))


def recent_log(db, cid, limit=30):
    return db.execute("""SELECT l.*, u.display_name FROM clearinghouse_log l LEFT JOIN users u ON u.id = l.user_id
                         WHERE l.clinic_id = ? ORDER BY l.id DESC LIMIT ?""", (cid, limit)).fetchall()


# ------------------------------------------------------------------------------------------------------------- the wire
def default_transport(method, url, headers, body=None, timeout=40):
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:          # nosec: fixed Stedi addresses, or ones Stedi gives
            return resp.status, resp.read(5_000_000).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(200_000).decode("utf-8", "replace")
    except (urllib.error.URLError, OSError, ValueError):
        raise ClearinghouseError("Could not reach the clearinghouse. Check the internet and try again.")


def _json(text):
    try:
        data = json.loads(text)
    except ValueError:
        raise ClearinghouseError("The clearinghouse's answer could not be read.")
    if not isinstance(data, dict):
        raise ClearinghouseError("The clearinghouse's answer could not be read.")
    return data


def _errors(data):
    out = []
    for e in data.get("errors") or []:
        if isinstance(e, dict):
            text = " ".join(str(e.get(k)) for k in ("code", "description", "message", "followupAction") if e.get(k))
            out.append(" ".join(text.split())[:200])
        elif e:
            out.append(str(e)[:200])
    return out


def _headers(key, idem=None):
    h = {"Authorization": key, "Content-Type": "application/json", "Accept": "application/json"}
    if idem:
        h["Idempotency-Key"] = idem
    return h


# ------------------------------------------------------------------------------------------------------------- 277CA
def read_277(text):
    """[{'ref': CLM01, 'category', 'code', 'accepted': bool, 'text'}] from a 277CA, one per claim, the claim-level status only."""
    segs, comp = E.parse(text)
    if E.kind_of(segs) != "277":
        raise ClearinghouseError("That is not a claim acknowledgement (277).")
    out, level, ref = [], None, None
    for s in segs:
        if s[0] == "HL" and len(s) > 3:
            level, ref = s[3], None
        elif s[0] == "TRN" and len(s) > 2 and s[1] == "2" and level == "PT":
            ref = s[2]
        elif s[0] == "STC" and level == "PT" and ref and len(s) > 1 and not any(o["ref"] == ref for o in out):
            parts = s[1].split(comp)
            cat = parts[0]
            code = parts[1] if len(parts) > 1 else ""
            free = s[12] if len(s) > 12 else ""
            if cat in ACCEPTED or cat in RETURNED:
                out.append({"ref": ref, "category": cat, "code": code, "accepted": cat in ACCEPTED,
                            "text": (ACCEPTED.get(cat) or RETURNED.get(cat)) + (f" (status {code})" if code else "") + (f": {free}" if free else "")})
    return out


def post_277(db, clinic, user_id, text):
    """Put each acknowledgement on its claim. A returned claim is marked rejected, so it can be fixed and sent again."""
    cid = clinic["id"]
    done = []
    for r in read_277(text):
        if not r["ref"].startswith("C") or not r["ref"][1:].isdigit():
            continue
        c = db.execute("SELECT * FROM claims WHERE clinic_id = ? AND claim_no = ?", (cid, int(r["ref"][1:]))).fetchone()
        if not c:
            continue
        day = now_local(clinic["timezone"]).date().isoformat()
        db.execute("UPDATE claims SET edi_ack = ? WHERE id = ? AND clinic_id = ?", (f"{day}: {r['text']}"[:300], c["id"], cid))
        if not r["accepted"] and c["status"] in ("submitted", "partly_paid"):
            INS.reject(db, clinic, user_id, c["id"], ("Returned before processing. " + r["text"])[:200])
        done.append((c["id"], r["accepted"]))
    E._save_file(db, clinic, user_id, "277", "in", None, text, [x[0] for x in done], status="posted",
                 summary=f"{sum(1 for x in done if x[1])} accepted, {sum(1 for x in done if not x[1])} returned")
    return done


# ------------------------------------------------------------------------------------------------------------- sending
def send_claim(db, clinic, user, secret_key, claim_id, transport=None):
    """Build the 837D for one claim and send it. On any failure nothing is kept (the caller rolls back) and the reason is raised."""
    cid = clinic["id"]
    a = account(db, cid)
    if not is_on(db, cid):
        raise ClearinghouseError("No clearinghouse is connected. A manager sets it up under Electronic claims.")
    problems = setup_problems(db, clinic)
    if problems:
        raise ClearinghouseError(problems[0])
    fid = E.build_837(db, clinic, user["id"], [claim_id])
    f = E.get_file(db, cid, fid)
    code, text = (transport or default_transport)("POST", STEDI["claim"], _headers(_key(secret_key, a), f"dos-{cid}-claim-{fid}"),
                                                   json.dumps({"x12": f["body"]}).encode())
    data = _json(text) if text.strip().startswith("{") else {}
    errs = _errors(data)
    if code in (401, 403):
        raise ClearinghouseError("The clearinghouse refused the clinic's key. A manager should check it.")
    if code != 200 or errs or (data.get("status") and data.get("status") != "SUCCESS"):
        raise ClearinghouseError("The clearinghouse did not take the claim. The reason is in the connection log below.",
                                 "; ".join(errs) if errs else f"Answer {code}")
    ref = (data.get("claimReference") or {}).get("correlationId") or data.get("controlNumber")
    db.execute("UPDATE edi_files SET status = 'accepted', summary = ? WHERE id = ?", ("Sent to Stedi" + (f", reference {ref}" if ref else ""), fid))
    if data.get("x12"):
        try:
            post_277(db, clinic, user["id"], data["x12"])
        except (E.EdiError, ClearinghouseError):
            pass                                                              # the claim went; the receipt is read again when polled
    log(db, cid, user["id"], "claim", True, f"Claim sent, file {fid}", ref)
    audit.log(db, cid, user["id"], "clearinghouse.claim", "edi_file", fid)
    return fid


def check_cover(db, clinic, user, secret_key, cover_id, transport=None):
    """Ask the payer about one cover, now, and save the answer on the cover. Returns the cover row."""
    cid = clinic["id"]
    a = account(db, cid)
    if not is_on(db, cid):
        raise ClearinghouseError("No clearinghouse is connected. A manager sets it up under Electronic claims.")
    fid = E.build_270(db, clinic, user["id"], cover_id)
    f = E.get_file(db, cid, fid)
    code, text = (transport or default_transport)("POST", STEDI["eligibility"], _headers(_key(secret_key, a)), json.dumps({"x12": f["body"]}).encode())
    if code in (401, 403):
        raise ClearinghouseError("The clearinghouse refused the clinic's key. A manager should check it.")
    data = _json(text) if text.strip().startswith("{") else {}
    x12 = data.get("x12") or ""
    if code != 200 or not x12:
        raise ClearinghouseError("The payer could not answer this time. The reason is in the connection log.", "; ".join(_errors(data)) or f"Answer {code}")
    kind = E.kind_of(E.parse(x12)[0])
    if kind == "999":
        r = E.read_999(x12)
        raise ClearinghouseError("The cover question was not accepted. Check the patient's and the cover's details.", "; ".join(r["errors"]) or None)
    db.execute("UPDATE edi_files SET status = 'accepted' WHERE id = ?", (fid,))
    cover = E.post_271(db, clinic, user["id"], x12)
    log(db, cid, user["id"], "cover", True, "Cover checked", data.get("id"))
    return cover


# ------------------------------------------------------------------------------------------------------------- fetching
def fetch(db, clinic, user_id, secret_key, transport=None, now=None):
    """Collect new acknowledgements (277) and remittances (835). Acknowledgements go straight on their claims; a remittance waits on
    the Electronic claims screen for a person to check and post, as an uploaded one does. Returns {'acks': n, 'remits': n}."""
    cid = clinic["id"]
    a = account(db, cid)
    if not is_on(db, cid):
        raise ClearinghouseError("No clearinghouse is connected. A manager sets it up under Electronic claims.")
    send = transport or default_transport
    key = _key(secret_key, a)
    now = now or datetime.datetime.now(datetime.timezone.utc)
    token, got = a["poll_token"], {"acks": 0, "remits": 0}
    for _page in range(20):
        if token:
            url = f"{STEDI['poll']}?pageSize=200&pageToken={urllib.parse.quote(token, safe='')}"
        else:
            start = (now - datetime.timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
            url = f"{STEDI['poll']}?pageSize=200&startDateTime={start}"
        code, text = send("GET", url, _headers(key))
        if code in (401, 403):
            raise ClearinghouseError("The clearinghouse refused the clinic's key. A manager should check it.")
        if code != 200:
            raise ClearinghouseError("The clearinghouse could not be read this time. Try again later.", f"Answer {code}")
        data = _json(text)
        for item in data.get("items") or []:
            tid = str(item.get("transactionId") or "")
            setid = str(((item.get("x12") or {}).get("metadata") or {}).get("transaction", {}).get("transactionSetIdentifier")
                        or (item.get("x12") or {}).get("transactionSetIdentifier") or "")
            if item.get("direction") != "INBOUND" or setid not in ("277", "835") or not tid:
                continue
            if db.execute("SELECT 1 FROM clearinghouse_seen WHERE clinic_id = ? AND transaction_id = ?", (cid, tid)).fetchone():
                continue
            art = next((x for x in item.get("artifacts") or [] if x.get("usage") == "input" and x.get("url")), None)
            if not art:
                continue
            c2, raw = send("GET", art["url"], {"Authorization": key, "Accept": "application/edi-x12, text/plain"})
            if c2 != 200 or not raw.lstrip().startswith("ISA"):
                continue                                                      # tried again next time
            try:
                if setid == "277":
                    post_277(db, clinic, user_id, raw)
                    got["acks"] += 1
                else:
                    E._save_file(db, clinic, user_id, "835", "in", None, raw, [], status="waiting", summary="From Stedi: check and post")
                    got["remits"] += 1
            except (E.EdiError, ClearinghouseError, INS.InsuranceError) as exc:
                log(db, cid, user_id, "fetch", False, f"A {setid} from the clearinghouse could not be read: {exc}", tid)
            db.execute("INSERT INTO clearinghouse_seen(clinic_id, transaction_id, kind) VALUES (?,?,?)", (cid, tid, setid))
        nxt = data.get("nextPageToken")
        if nxt:
            token = nxt
        if not data.get("items") or not nxt:
            break
    db.execute("UPDATE clearinghouse_account SET poll_token = ?, last_poll_at = datetime('now') WHERE clinic_id = ?", (token, cid))
    log(db, cid, user_id, "fetch", True, f"{got['acks']} acknowledgements, {got['remits']} remittances")
    return got


def waiting_remits(db, cid):
    return db.execute("SELECT * FROM edi_files WHERE clinic_id = ? AND kind = '835' AND direction = 'in' AND status = 'waiting' ORDER BY id",
                      (cid,)).fetchall()
