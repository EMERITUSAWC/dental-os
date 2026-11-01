"""Version 0.64: the clinic's records as HL7 FHIR R4 (4.0.1), read-only, for hospitals, national health systems, insurers' portals
and other software. The keyed REST API (/api/v1) stays free of clinical data; FHIR needs a key a manager has marked for clinical
records, and every read of a patient's record is written in that patient's access log ("who looked").

Resources: Patient, Practitioner, Organization, Appointment, Procedure (treatment done or planned, with the tooth), AllergyIntolerance
(allergy alerts), MedicationRequest (prescriptions), Coverage (insurance). Search by _id, patient, identifier, name, birthdate and
date, as the FHIR search page describes, with paging by _count and a next link. Patient/{id}/$everything returns the whole record.

Codes: teeth use FDI numbers with the FHIR oral site code system; a treatment carries its CDT code (US clinics enter their own,
licensed from the ADA) when it has one, and the clinic's own code and name as text always. Allergies and medicines are text: Dental
OS keeps the clinic's wording, not a national coded dictionary. No web framework here."""
import datetime

FHIR_VERSION = "4.0.1"
MIME = "application/fhir+json"
TOOTH_SYSTEM = "http://terminology.hl7.org/CodeSystem/ex-tooth"
CDT_SYSTEM = "http://www.ada.org/cdt"
TYPES = ("Patient", "Practitioner", "Organization", "Appointment", "Procedure", "AllergyIntolerance", "MedicationRequest", "Coverage")
BY_PATIENT = ("Appointment", "Procedure", "AllergyIntolerance", "MedicationRequest", "Coverage")
MAX_COUNT = 200


class FhirError(Exception):
    def __init__(self, status, code, text):
        super().__init__(text)
        self.status, self.code, self.text = status, code, text


def outcome(code, text, severity="error"):
    return {"resourceType": "OperationOutcome", "issue": [{"severity": severity, "code": code, "diagnostics": text}]}


def _gender(v):
    v = (v or "").strip().lower()
    return {"female": "female", "f": "female", "male": "male", "m": "male"}.get(v, "other" if v else "unknown")


def _ref(kind, rid):
    return {"reference": f"{kind}/{rid}"}


def _instant(text):
    """'2026-10-31 14:05' or '2026-10-31 14:05:09' (UTC in the database) to a FHIR instant."""
    t = (text or "").strip()
    if len(t) >= 16:
        return t[:10] + "T" + (t[11:19] if len(t) >= 19 else t[11:16] + ":00") + "Z"
    return None


def _local(clinic, text):
    """A diary time in the clinic's own zone ('2026-11-03 09:30') to a FHIR dateTime with the zone's offset."""
    from zoneinfo import ZoneInfo
    try:
        dt = datetime.datetime.strptime(text[:16], "%Y-%m-%d %H:%M").replace(tzinfo=ZoneInfo(clinic["timezone"] or "UTC"))
    except (ValueError, TypeError):
        return None
    return dt.isoformat(timespec="seconds")


# ---------------------------------------------------------------------------------------------------- one resource each
def patient(clinic, p):
    r = {"resourceType": "Patient", "id": str(p["id"]), "active": p["status"] == "active",
         "identifier": [{"system": f"urn:dental-os:clinic:{clinic['id']}:patient", "value": str(p["id"])}],
         "name": [{"use": "official", "family": p["last_name"] or "", "given": [p["first_name"]] if p["first_name"] else []}],
         "gender": _gender(p["gender"]), "managingOrganization": _ref("Organization", clinic["id"])}
    if p["dob"]:
        r["birthDate"] = p["dob"]
    telecom = [{"system": "phone", "value": p["phone"], "use": "mobile"}] if p["phone"] else []
    if p["email"]:
        telecom.append({"system": "email", "value": p["email"]})
    if telecom:
        r["telecom"] = telecom
    if p["address"]:
        r["address"] = [{"text": p["address"]}]
    if p["merged_into"]:
        r["link"] = [{"other": _ref("Patient", p["merged_into"]), "type": "replaced-by"}]
    return r


def practitioner(clinic, d):
    r = {"resourceType": "Practitioner", "id": str(d["id"]), "active": bool(d["active"]), "name": [{"text": d["name"]}]}
    ids = []
    if d["npi"]:
        ids.append({"system": "http://hl7.org/fhir/sid/us-npi", "value": d["npi"]})
    if d["reg_number"]:
        ids.append({"system": f"urn:dental-os:register:{(d['reg_body'] or 'register').lower().replace(' ', '-')}", "value": d["reg_number"]})
    if ids:
        r["identifier"] = ids
    if d["qualifications"]:
        r["qualification"] = [{"code": {"text": d["qualifications"]}}]
    return r


def organization(clinic):
    r = {"resourceType": "Organization", "id": str(clinic["id"]), "active": True, "name": clinic["name"],
         "type": [{"text": "Dental practice"}]}
    if clinic["phone"]:
        r["telecom"] = [{"system": "phone", "value": clinic["phone"]}]
    if clinic["address"]:
        r["address"] = [{"text": clinic["address"], "country": clinic["country"]}]
    return r


APPT_STATUS = {"planned": "proposed", "unscheduled": "proposed", "asap": "waitlist", "complete": "fulfilled", "broken": "noshow"}


def appointment(clinic, a):
    st = APPT_STATUS.get(a["status"])
    if a["status"] == "broken" and a["break_reason"] == "cancelled":
        st = "cancelled"
    if a["status"] == "scheduled":
        st = "checked-in" if a["seated_at"] and not a["dismissed_at"] else ("arrived" if a["arrived_at"] and not a["dismissed_at"] else "booked")
    r = {"resourceType": "Appointment", "id": str(a["id"]), "status": st or "booked",
         "participant": [{"actor": _ref("Patient", a["patient_id"]), "status": "accepted"}]}
    if a["provider_id"]:
        r["participant"].append({"actor": _ref("Practitioner", a["provider_id"]), "status": "accepted"})
    if a["start_at"]:
        start = _local(clinic, a["start_at"])
        if start:
            r["start"] = start
            if a["duration_min"]:
                r["minutesDuration"] = int(a["duration_min"])
                end = datetime.datetime.fromisoformat(start) + datetime.timedelta(minutes=int(a["duration_min"]))
                r["end"] = end.isoformat(timespec="seconds")
    if a["category"]:
        r["serviceType"] = [{"text": a["category"]}]
    if st == "cancelled":
        r["cancelationReason"] = {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/appointment-cancellation-reason", "code": "pat"}],
                                  "text": "Cancelled by the patient"}
    return r


PROC_STATUS = {"done": "completed", "planned": "preparation", "cancelled": "not-done", "voided": "entered-in-error"}


def procedure(clinic, x):
    coding = [{"system": CDT_SYSTEM, "code": x["cdt_code"]}] if x["cdt_code"] else []
    coding.append({"system": f"urn:dental-os:clinic:{clinic['id']}:code", "code": x["code"], "display": x["name"]})
    r = {"resourceType": "Procedure", "id": str(x["id"]), "status": PROC_STATUS.get(x["status"], "unknown"),
         "code": {"coding": coding, "text": x["name"]}, "subject": _ref("Patient", x["patient_id"])}
    if x["status"] == "done" and x["done_on"]:
        r["performedDateTime"] = x["done_on"]
    if x["provider_id"]:
        r["performer"] = [{"actor": _ref("Practitioner", x["provider_id"])}]
    if x["tooth"]:
        site = {"coding": [{"system": TOOTH_SYSTEM, "code": str(x["tooth"])}], "text": f"Tooth {x['tooth']} (FDI)"}
        if x["surfaces"]:
            site["text"] += f", surfaces {x['surfaces']}"
        r["bodySite"] = [site]
    notes = [n for n in (x["notes"], x["void_reason"]) if n]
    if notes:
        r["note"] = [{"text": n} for n in notes]
    return r


ALLERGY_SEVERITY = {"critical": "high", "warning": "low", "info": "low"}


def allergy(clinic, a):
    return {"resourceType": "AllergyIntolerance", "id": str(a["id"]),
            "clinicalStatus": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/allergyintolerance-clinical",
                                           "code": "active" if a["active"] else "inactive"}]},
            "verificationStatus": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/allergyintolerance-verification", "code": "unconfirmed"}]},
            "criticality": ALLERGY_SEVERITY.get(a["severity"], "unable-to-assess"),
            "code": {"text": a["text"]}, "patient": _ref("Patient", a["patient_id"]),
            "recordedDate": (a["created_at"] or "")[:10] or None}


def medication_requests(clinic, rx, items):
    out = []
    for it in items:
        r = {"resourceType": "MedicationRequest", "id": f"{rx['id']}-{it['id']}", "status": "active" if rx["status"] == "issued" else "cancelled",
             "intent": "order", "medicationCodeableConcept": {"text": " ".join(v for v in (it["name"], it["strength"], it["form"]) if v)},
             "subject": _ref("Patient", rx["patient_id"]), "authoredOn": rx["issued_on"], "requester": _ref("Practitioner", rx["provider_id"]),
             "groupIdentifier": {"system": f"urn:dental-os:clinic:{clinic['id']}:prescription", "value": str(rx["id"])}}
        dose = ", ".join(v for v in (it["dose"], f"for {it['days']} days" if it["days"] else None) if v)
        if dose:
            r["dosageInstruction"] = [{"text": dose}]
        if it["quantity"]:
            r["dispenseRequest"] = {"quantity": {"unit": str(it["quantity"])}}
        out.append(r)
    return out


def coverage(clinic, c):
    r = {"resourceType": "Coverage", "id": str(c["id"]), "status": "active" if c["active"] else "cancelled",
         "beneficiary": _ref("Patient", c["patient_id"]), "payor": [{"display": c["payer_name"]}],
         "relationship": {"text": c["relationship"] or "self"}}
    if c["member_no"]:
        r["subscriberId"] = c["member_no"]
    if c["group_no"]:
        r["class"] = [{"type": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/coverage-class", "code": "group"}]}, "value": c["group_no"]}]
    period = {k: v for k, v in (("start", c["valid_from"]), ("end", c["valid_to"])) if v}
    if period:
        r["period"] = period
    return r


# ---------------------------------------------------------------------------------------------------- reading the database
def _patient_rows(db, cid, where="", args=()):
    return db.execute(f"SELECT * FROM patients WHERE clinic_id = ? {where} ORDER BY id", (cid, *args)).fetchall()


def fetch(db, clinic, kind, params, after=0, count=50):
    """(resources, more) for a search. params: {name: value}. Unknown parameters are refused, as FHIR allows."""
    cid = clinic["id"]
    allowed = {"_id", "_count", "_page_after"}
    where, args = [], []
    pid = params.get("patient") or params.get("subject") or params.get("beneficiary")
    if pid is not None:
        pid = pid.split("/")[-1]
        if not pid.isdigit():
            raise FhirError(400, "invalid", "patient must be a Patient id, like Patient/12.")
    rid = params.get("_id")
    if rid is not None and not rid.replace("-", "").isdigit():
        raise FhirError(400, "invalid", "_id is a number.")
    if kind == "Patient":
        allowed |= {"name", "family", "given", "birthdate", "identifier", "phone"}
        if rid:
            where.append("AND id = ?")
            args.append(int(rid))
        if params.get("identifier"):
            value = params["identifier"].split("|")[-1]
            where.append("AND id = ?")
            args.append(int(value) if value.isdigit() else -1)
        for p, cols in (("name", ("first_name", "last_name")), ("family", ("last_name",)), ("given", ("first_name",))):
            if params.get(p):
                where.append("AND (" + " OR ".join(f"LOWER({c}) LIKE ?" for c in cols) + ")")
                args += [params[p].lower().strip() + "%"] * len(cols)
        if params.get("birthdate"):
            where.append("AND dob = ?")
            args.append(params["birthdate"][:10])
        if params.get("phone"):
            digits = "".join(ch for ch in params["phone"] if ch.isdigit())[-9:]
            where.append("AND phone LIKE ?")
            args.append("%" + digits)
        rows = db.execute(f"SELECT * FROM patients WHERE clinic_id = ? AND id > ? {' '.join(where)} ORDER BY id LIMIT ?",
                          (cid, after, *args, count + 1)).fetchall()
        shaped = [patient(clinic, r) for r in rows]
    elif kind == "Practitioner":
        rows = db.execute(f"SELECT * FROM providers WHERE clinic_id = ? AND id > ? {'AND id = ?' if rid else ''} ORDER BY id LIMIT ?",
                          (cid, after, *([int(rid)] if rid else []), count + 1)).fetchall()
        shaped = [practitioner(clinic, r) for r in rows]
    elif kind == "Organization":
        rows = [clinic] if (not rid or int(rid) == cid) and after < cid else []
        shaped = [organization(clinic)] if rows else []
    elif kind in BY_PATIENT:
        allowed |= {"patient", "subject", "beneficiary", "date", "status"}
        table, shape, date_col = {
            "Appointment": ("appointments a", appointment, "a.start_at"),
            "Procedure": ("procedures x JOIN procedure_codes pc ON pc.id = x.code_id", procedure, "x.done_on"),
            "AllergyIntolerance": ("patient_alerts a", allergy, "a.created_at"),
            "MedicationRequest": ("prescriptions r", None, "r.issued_on"),
            "Coverage": ("patient_cover c JOIN payers py ON py.id = c.payer_id", coverage, "c.valid_from"),
        }[kind]
        alias = table.split()[1]
        cols = {"Procedure": "x.*, pc.code, pc.name, pc.cdt_code", "Coverage": "c.*, py.name AS payer_name"}.get(kind, f"{alias}.*")
        extra = " AND a.kind = 'allergy'" if kind == "AllergyIntolerance" else ""
        if pid:
            where.append(f"AND {alias}.patient_id = ?")
            args.append(int(pid))
        if rid:
            where.append(f"AND {alias}.id = ?")
            args.append(int(rid.split("-")[0]))
        if params.get("date"):
            d = params["date"]
            op = d[:2] if d[:2] in ("ge", "le", "gt", "lt", "eq") else "eq"
            day = d[2:] if op != "eq" or d[:2] == "eq" else d
            sql_op = {"ge": ">=", "le": "<=", "gt": ">", "lt": "<"}.get(op)
            if sql_op:
                where.append(f"AND substr({date_col}, 1, 10) {sql_op} ?")
            else:
                where.append(f"AND substr({date_col}, 1, 10) = ?")
            args.append(day[:10])
        rows = db.execute(f"SELECT {cols} FROM {table} WHERE {alias}.clinic_id = ? AND {alias}.id > ?{extra} {' '.join(where)} "
                          f"ORDER BY {alias}.id LIMIT ?", (cid, after, *args, count + 1)).fetchall()
        if kind == "MedicationRequest":
            shaped = []
            for r in rows:
                items = db.execute("SELECT * FROM prescription_items WHERE prescription_id = ? ORDER BY id", (r["id"],)).fetchall()
                shaped.append(medication_requests(clinic, r, items))
        else:
            shaped = [shape(clinic, r) for r in rows]
    else:
        raise FhirError(404, "not-supported", f"{kind} is not offered here.")
    bad = sorted(set(params) - allowed)
    if bad:
        raise FhirError(400, "not-supported", "Search parameter not supported: " + ", ".join(bad) + ".")
    more = len(rows) > count
    rows, shaped = rows[:count], shaped[:count]
    flat = []
    for s in shaped:
        flat += s if isinstance(s, list) else [s]
    return flat, (rows[-1]["id"] if more else None), sorted({r["patient_id"] if "patient_id" in r.keys() else r["id"] for r in rows}) if kind in BY_PATIENT + ("Patient",) else []


def bundle(kind, resources, base, self_url, next_url=None, btype="searchset"):
    b = {"resourceType": "Bundle", "type": btype, "timestamp": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
         "link": [{"relation": "self", "url": self_url}],
         "entry": [{"fullUrl": f"{base}/{r['resourceType']}/{r['id']}", "resource": r, **({"search": {"mode": "match"}} if btype == "searchset" else {})}
                   for r in resources]}
    if btype == "searchset":
        b["total"] = len(resources) if not next_url else None
        if b["total"] is None:
            del b["total"]
    if next_url:
        b["link"].append({"relation": "next", "url": next_url})
    return b


def everything(db, clinic, pid):
    """Patient/$everything: the patient, then everything about them, then who treated them and the clinic."""
    p = db.execute("SELECT * FROM patients WHERE id = ? AND clinic_id = ?", (pid, clinic["id"])).fetchone()
    if not p:
        raise FhirError(404, "not-found", "No patient with that id.")
    out = [patient(clinic, p)]
    providers = set()
    for kind in BY_PATIENT:
        after = 0
        while True:
            res, nxt, _ids = fetch(db, clinic, kind, {"patient": str(pid)}, after=after, count=MAX_COUNT)
            out += res
            for r in res:
                for part in r.get("participant", []) + r.get("performer", []) + ([{"actor": r["requester"]}] if "requester" in r else []):
                    ref = (part.get("actor") or {}).get("reference", "")
                    if ref.startswith("Practitioner/"):
                        providers.add(int(ref.split("/")[1]))
            if not nxt:
                break
            after = nxt
    for d in sorted(providers):
        row = db.execute("SELECT * FROM providers WHERE id = ? AND clinic_id = ?", (d, clinic["id"])).fetchone()
        if row:
            out.append(practitioner(clinic, row))
    out.append(organization(clinic))
    return out


def capability(base, software_version):
    search = {"Patient": ["_id", "identifier", "name", "family", "given", "birthdate", "phone"],
              "Practitioner": ["_id"], "Organization": ["_id"]}
    for k in BY_PATIENT:
        search[k] = ["_id", "patient", "date"]
    return {"resourceType": "CapabilityStatement", "status": "active", "kind": "instance",
            "date": datetime.date.today().isoformat(), "fhirVersion": FHIR_VERSION, "format": ["json"],
            "software": {"name": "Dental OS", "version": software_version}, "implementation": {"description": "Dental OS FHIR R4, read-only", "url": base},
            "rest": [{"mode": "server", "security": {"description": "Authorization: Bearer <key>. The key must be marked for clinical records by a manager."},
                      "resource": [{"type": k, "interaction": [{"code": "read"}, {"code": "search-type"}],
                                    "searchParam": [{"name": n, "type": "token" if n in ("_id", "identifier", "patient") else ("date" if n in ("date", "birthdate") else "string")}
                                                    for n in search[k]],
                                    **({"operation": [{"name": "everything", "definition": "http://hl7.org/fhir/OperationDefinition/Patient-everything"}]} if k == "Patient" else {})}
                                   for k in TYPES]}]}
