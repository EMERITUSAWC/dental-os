"""Version 0.62: prescriptions a pharmacy can check, a standard electronic copy, and a connection to an e-prescribing network.

1. The pharmacy check. Every issued prescription can carry a QR code and a short link. A pharmacist opens it on any phone and sees
   whether it is genuine, still valid, cancelled or already dispensed, then records the dispensing so the same paper cannot be used
   twice. The link is made from the server's secret key, so it can be printed again at any time, and a copy of the database alone
   cannot be used to make links. The page shows only what a pharmacist needs: the medicines, the prescriber, and the patient's
   initials and year of birth to match against the paper and the person. It works today in any country, with any pharmacy.
2. The electronic copy: a FHIR R4 Bundle (Patient, Practitioner, Organization, one MedicationRequest per medicine), the standard
   format national health systems and e-prescribing networks read.
3. The network connection: a clinic that has an e-prescribing network account sends the FHIR copy to the network's https address
   with its key, and Dental OS keeps the network's answer. Each network has its own rules and certification (DoseSpot and Surescripts
   in the US, the NHS Electronic Prescription Service in England), so a short adapter per network is written once the clinic or
   Dental OS has a partner agreement. Off until a manager switches it on; the key is stored encrypted.
"""
import base64
import datetime
import hashlib
import hmac
import json
import secrets
import urllib.error
import urllib.request

from . import audit, webhooks
from .timeutil import now_local

VALID_DAYS = 30                 # a starting point; set what the law says where the clinic works
CONTROLLED_VALID_DAYS = 28      # the UK limit for Schedule 2 to 4 controlled drugs is 28 days; check the local rule
BAD_PER_HOUR = 30
BLOCKED = {"dispensed": "This prescription was already dispensed.", "cancelled": "This prescription was cancelled by the dentist.",
           "expired": "This prescription is out of date."}
STATES = {"valid": "Valid", "dispensed": "Already dispensed", "cancelled": "Cancelled by the dentist", "expired": "Out of date"}


class RxLinkError(Exception):
    pass


# ------------------------------------------------------------------------------------------------------------ the link
def _sig(secret_key, rx_id, salt):
    mac = hmac.new(str(secret_key).encode(), f"rx-link:{rx_id}:{salt}".encode(), hashlib.sha256).digest()
    return base64.b32encode(mac[:15]).decode().rstrip("=").lower()


def token_for(db, clinic, secret_key, rx_id, user_id=None):
    """The link token for a prescription, made once and the same every time after. Only an issued prescription gets one."""
    cid = clinic["id"]
    rx = db.execute("SELECT id, status FROM prescriptions WHERE id = ? AND clinic_id = ?", (rx_id, cid)).fetchone()
    if not rx:
        raise LookupError("prescription")
    row = db.execute("SELECT * FROM rx_links WHERE clinic_id = ? AND prescription_id = ?", (cid, rx_id)).fetchone()
    if not row:
        if rx["status"] != "issued":
            raise RxLinkError("A cancelled prescription has no pharmacy link.")
        salt = secrets.token_hex(8)
        db.execute("INSERT INTO rx_links(clinic_id, prescription_id, salt, created_by) VALUES (?,?,?,?)", (cid, rx_id, salt, user_id))
        audit.log(db, cid, user_id, "rx.link", "prescription", rx_id)
    else:
        salt = row["salt"]
    return f"{rx_id}-{_sig(secret_key, rx_id, salt)}"


def resolve(db, secret_key, token):
    """(link row, prescription row) for a token, or (None, None). Constant-time comparison."""
    try:
        rid_text, sig = (token or "").split("-", 1)
        rx_id = int(rid_text)
    except ValueError:
        return None, None
    link = db.execute("SELECT * FROM rx_links WHERE prescription_id = ?", (rx_id,)).fetchone()
    if not link or not hmac.compare_digest(_sig(secret_key, rx_id, link["salt"]), sig.lower()):
        return None, None
    rx = db.execute("""SELECT r.*, p.name AS provider_name, p.reg_body, p.reg_number, p.qualifications FROM prescriptions r
                       JOIN providers p ON p.id = r.provider_id WHERE r.id = ? AND r.clinic_id = ?""", (rx_id, link["clinic_id"])).fetchone()
    return link, rx


def state(link, rx, items, today):
    """valid, dispensed, cancelled or expired."""
    if rx["status"] == "cancelled":
        return "cancelled"
    if link["dispensed_at"]:
        return "dispensed"
    days = CONTROLLED_VALID_DAYS if any(i["controlled"] for i in items) else VALID_DAYS
    if (today - datetime.date.fromisoformat(rx["issued_on"])).days > days:
        return "expired"
    return "valid"


def last_until(rx, items):
    days = CONTROLLED_VALID_DAYS if any(i["controlled"] for i in items) else VALID_DAYS
    return datetime.date.fromisoformat(rx["issued_on"]) + datetime.timedelta(days=days)


def initials(patient):
    return "".join((part[:1].upper() + ".") for part in ((patient["first_name"] or ""), (patient["last_name"] or "")) if part)


def dispense(db, clinic, link, rx, items, form):
    """A pharmacist records that it was given out. Once only."""
    pharmacy = " ".join((form.get("pharmacy") or "").split())[:120]
    who = " ".join((form.get("pharmacist") or "").split())[:120]
    licence = " ".join((form.get("licence") or "").split())[:40]
    if len(pharmacy) < 2 or len(who) < 2:
        raise RxLinkError("Give the pharmacy's name and the pharmacist's name.")
    if form.get("checked") != "1":
        raise RxLinkError("Tick the box to confirm you checked the patient against the prescription.")
    today = now_local(clinic["timezone"]).date()
    st = state(link, rx, items, today)
    if st != "valid":
        raise RxLinkError(BLOCKED[st])
    at = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    cur = db.execute("""UPDATE rx_links SET dispensed_at = ?, pharmacy = ?, pharmacist = ?, licence = ? WHERE id = ? AND dispensed_at IS NULL""",
                     (at, pharmacy, who, licence or None, link["id"]))
    if cur.rowcount != 1:
        raise RxLinkError(BLOCKED["dispensed"])
    audit.log(db, clinic["id"], None, "rx.dispensed", "patient", rx["patient_id"], pharmacy[:60])


def link_row(db, cid, rx_id):
    return db.execute("SELECT * FROM rx_links WHERE clinic_id = ? AND prescription_id = ?", (cid, rx_id)).fetchone()


# ------------------------------------------------------------------------------------------------------------ FHIR R4
def fhir_bundle(db, clinic, rx, items, patient):
    """A FHIR R4 Bundle of type 'collection' for one prescription. Medicines are written as text: Dental OS keeps the clinic's own
    list, not a national coded dictionary (dm+d in the UK, RxNorm in the US); a network adapter adds codes where the network needs them."""
    base = f"urn:dental-os:clinic:{clinic['id']}"
    pat_ref, prac_ref, org_ref = f"Patient/p{patient['id']}", f"Practitioner/d{rx['provider_id']}", f"Organization/c{clinic['id']}"
    name = {"family": patient["last_name"] or "", "given": [patient["first_name"]] if patient["first_name"] else []}
    pat = {"resourceType": "Patient", "id": f"p{patient['id']}", "identifier": [{"system": f"{base}:patient", "value": str(patient["id"])}],
           "name": [name]}
    if patient["dob"]:
        pat["birthDate"] = patient["dob"]
    if patient["phone"]:
        pat["telecom"] = [{"system": "phone", "value": patient["phone"]}]
    prac = {"resourceType": "Practitioner", "id": f"d{rx['provider_id']}", "name": [{"text": rx["provider_name"]}]}
    if rx["reg_number"]:
        prac["identifier"] = [{"system": f"{base}:registration:{(rx['reg_body'] or 'register').lower().replace(' ', '-')}", "value": rx["reg_number"]}]
    org = {"resourceType": "Organization", "id": f"c{clinic['id']}", "name": clinic["name"]}
    entries = [{"fullUrl": f"{base}/{pat_ref}", "resource": pat}, {"fullUrl": f"{base}/{prac_ref}", "resource": prac},
               {"fullUrl": f"{base}/{org_ref}", "resource": org}]
    for it in items:
        mr = {"resourceType": "MedicationRequest", "id": f"rx{rx['id']}-{it['id']}", "status": "active" if rx["status"] == "issued" else "cancelled",
              "intent": "order", "medicationCodeableConcept": {"text": " ".join(x for x in (it["name"], it["strength"], it["form"]) if x)},
              "subject": {"reference": pat_ref}, "authoredOn": rx["issued_on"], "requester": {"reference": prac_ref},
              "identifier": [{"system": f"{base}:prescription", "value": str(rx["id"])}]}
        dose = ", ".join(x for x in (it["dose"], f"for {it['days']} days" if it["days"] else None) if x)
        if dose:
            mr["dosageInstruction"] = [{"text": dose}]
        if it["quantity"]:
            mr["dispenseRequest"] = {"quantity": {"value": _qty(it["quantity"])} if _qty(it["quantity"]) is not None else {"unit": str(it["quantity"])}}
        if rx["notes"]:
            mr["note"] = [{"text": rx["notes"]}]
        entries.append({"fullUrl": f"{base}/MedicationRequest/{mr['id']}", "resource": mr})
    return {"resourceType": "Bundle", "type": "collection", "timestamp": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "identifier": {"system": f"{base}:prescription", "value": str(rx["id"])}, "entry": entries}


def _qty(v):
    try:
        f = float(str(v).split()[0])
        return int(f) if f.is_integer() else f
    except (ValueError, IndexError):
        return None


# ------------------------------------------------------------------------------------------------------------ the network
def _fernet(secret_key):
    from cryptography.fernet import Fernet
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(b"dental-os/erx/" + str(secret_key).encode()).digest()))


def network(db, cid):
    return db.execute("SELECT * FROM erx_network WHERE clinic_id = ?", (cid,)).fetchone()


def network_on(db, cid):
    n = network(db, cid)
    return bool(n and n["enabled"] and n["endpoint"] and n["api_key"])


def save_network(db, clinic, user_id, secret_key, form):
    cid = clinic["id"]
    n = network(db, cid)
    name = (form.get("name") or "").strip()[:80]
    endpoint = (form.get("endpoint") or "").strip()
    key = (form.get("api_key") or "").strip()
    enabled = form.get("enabled") == "1"
    if endpoint:
        try:
            endpoint = webhooks.check_url(endpoint)
        except webhooks.WebhookError as exc:
            raise RxLinkError(str(exc))
    if key and (len(key) < 16 or any(c.isspace() for c in key)):
        raise RxLinkError("That key does not look right.")
    sealed = _fernet(secret_key).encrypt(key.encode()).decode() if key else (n["api_key"] if n else None)
    if enabled and not (name and endpoint and sealed):
        raise RxLinkError("To switch it on, give the network's name, its https address and the clinic's key.")
    if n:
        db.execute("UPDATE erx_network SET enabled = ?, name = ?, endpoint = ?, api_key = ?, updated_by = ?, updated_at = datetime('now') WHERE clinic_id = ?",
                   (1 if enabled else 0, name or None, endpoint or None, sealed, user_id, cid))
    else:
        db.execute("INSERT INTO erx_network(clinic_id, enabled, name, endpoint, api_key, updated_by) VALUES (?,?,?,?,?,?)",
                   (cid, 1 if enabled else 0, name or None, endpoint or None, sealed, user_id))
    audit.log(db, cid, user_id, "erx.network", detail="on" if enabled else "off")


def default_transport(url, headers, body, timeout=30):
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:          # nosec: https address checked when it was saved
            return resp.status, resp.read(200_000).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(50_000).decode("utf-8", "replace")
    except (urllib.error.URLError, OSError, ValueError):
        raise RxLinkError("Could not reach the e-prescribing network. Print the prescription instead, or try again later.")


def send(db, clinic, user, secret_key, rx, items, patient, transport=None):
    """Send one prescription to the clinic's network. Returns the send row's id; a refused send is kept with a plain reason."""
    cid = clinic["id"]
    if not network_on(db, cid):
        raise RxLinkError("No e-prescribing network is switched on. A manager sets it up under Settings.")
    if rx["status"] != "issued":
        raise RxLinkError("A cancelled prescription cannot be sent.")
    if user["role"] not in ("dentist", "manager"):
        raise PermissionError("Only a dentist can send a prescription.")
    if db.execute("SELECT 1 FROM erx_sends WHERE clinic_id = ? AND prescription_id = ? AND status = 'accepted'", (cid, rx["id"])).fetchone():
        raise RxLinkError("This prescription has already been accepted by the network.")
    n = network(db, cid)
    from cryptography.fernet import InvalidToken
    try:
        key = _fernet(secret_key).decrypt(n["api_key"].encode()).decode()
    except (InvalidToken, ValueError):
        raise RxLinkError("The network key cannot be read. A manager should enter it again under Settings.")
    body = json.dumps(fhir_bundle(db, clinic, rx, items, patient)).encode()
    headers = {"Authorization": "Bearer " + key, "Content-Type": "application/fhir+json", "Accept": "application/fhir+json, application/json"}
    status, ref, detail = "refused", None, None
    try:
        code, text = (transport or default_transport)(n["endpoint"], headers, body)
        if code in (200, 201, 202):
            status = "accepted"
            try:
                data = json.loads(text)
                ref = str(data.get("id") or (data.get("identifier") or {}).get("value") or "")[:80] or None
            except (ValueError, AttributeError):
                ref = None
        elif code in (401, 403):
            detail = "The network refused the clinic's key. A manager should check it under Settings."
        elif code in (400, 422):
            detail = "The network did not accept this prescription. Print it instead, and tell Dental OS support."
        else:
            detail = "The network could not take it this time. Try again later, or print it."
    except RxLinkError as exc:
        detail = str(exc)
    sid = db.execute("""INSERT INTO erx_sends(clinic_id, patient_id, prescription_id, network, status, network_ref, detail, sent_by)
                        VALUES (?,?,?,?,?,?,?,?)""", (cid, rx["patient_id"], rx["id"], n["name"], status, ref, detail, user["id"])).lastrowid
    audit.log(db, cid, user["id"], "erx.send", "patient", rx["patient_id"], status)
    return sid


def sends(db, cid, rx_id):
    return db.execute("""SELECT s.*, u.display_name FROM erx_sends s LEFT JOIN users u ON u.id = s.sent_by
                         WHERE s.clinic_id = ? AND s.prescription_id = ? ORDER BY s.id DESC""", (cid, rx_id)).fetchall()
