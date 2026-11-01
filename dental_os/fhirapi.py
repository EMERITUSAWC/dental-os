"""Version 0.64: the FHIR R4 read-only API at /api/fhir/R4. A key must be marked for clinical records. Every patient whose record
is read is written in that patient's access log, with the key's name, so the clinic can see which program looked."""
import json
from urllib.parse import urlencode

from flask import Blueprint, Response, g, request

from .db import get_db
from .services import apikeys, audit, fhir as F, throttle

bp = Blueprint("fhir", __name__, url_prefix="/api/fhir/R4")
from .version import VERSION  # noqa: E402,F401
RATE_PER_MINUTE = 300


def _send(body, status=200):
    return Response(json.dumps(body, ensure_ascii=False), status=status, mimetype=F.MIME)


def _fail(status, code, text):
    return _send(F.outcome(code, text), status)


def _base():
    return request.url_root.rstrip("/") + bp.url_prefix


@bp.before_request
def authenticate():
    db = get_db()
    g.user = g.clinic = g.api_key = None
    key = apikeys.authenticate(db, request.headers.get("Authorization"))
    if request.endpoint == "fhir.metadata":
        return None                                          # the capability statement is public, as FHIR expects
    if not key:
        resp = _fail(401, "login", "Send a valid key as 'Authorization: Bearer <key>'.")
        resp.headers["WWW-Authenticate"] = "Bearer"
        return resp
    if not key["fhir"]:
        return _fail(403, "forbidden", "This key cannot read clinical records. A manager makes a key for FHIR under Tools, API and webhooks.")
    if request.method not in ("GET", "HEAD"):
        return _fail(405, "not-supported", "This FHIR server is read-only.")
    if not throttle.allow(db, f"api:{key['id']}", RATE_PER_MINUTE, 1):
        resp = _fail(429, "throttled", f"Too many requests. The limit is {RATE_PER_MINUTE} a minute for each key.")
        resp.headers["Retry-After"] = "60"
        return resp
    g.api_key = key
    g.clinic = db.execute("SELECT * FROM clinics WHERE id = ?", (key["clinic_id"],)).fetchone()


def _log(pids, what):
    db = get_db()
    for pid in pids:
        audit.log(db, g.clinic["id"], None, "fhir.read", "patient", pid, f"{g.api_key['name'][:40]}: {what}")


@bp.get("/metadata")
def metadata():
    return _send(F.capability(_base(), VERSION))


@bp.get("/<kind>")
def search(kind):
    if kind not in F.TYPES:
        return _fail(404, "not-supported", f"{kind} is not offered here.")
    params = {k: v for k, v in request.args.items() if k not in ("_count", "_page_after", "_format")}
    count = min(max(request.args.get("_count", 50, type=int) or 50, 1), F.MAX_COUNT)
    after = max(request.args.get("_page_after", 0, type=int) or 0, 0)
    try:
        res, nxt, pids = F.fetch(get_db(), g.clinic, kind, params, after=after, count=count)
    except F.FhirError as exc:
        return _fail(exc.status, exc.code, exc.text)
    _log(pids, kind)
    self_url = f"{_base()}/{kind}" + (f"?{request.query_string.decode()}" if request.query_string else "")
    next_url = f"{_base()}/{kind}?" + urlencode({**params, "_count": count, "_page_after": nxt}) if nxt else None
    return _send(F.bundle(kind, res, _base(), self_url, next_url))


@bp.get("/<kind>/<rid>")
def read(kind, rid):
    if kind not in F.TYPES:
        return _fail(404, "not-supported", f"{kind} is not offered here.")
    try:
        res, _nxt, pids = F.fetch(get_db(), g.clinic, kind, {"_id": rid}, count=F.MAX_COUNT)
    except F.FhirError as exc:
        return _fail(exc.status, exc.code, exc.text)
    match = [r for r in res if r["id"] == rid]
    if not match:
        return _fail(404, "not-found", f"No {kind} with that id.")
    _log(pids, kind)
    return _send(match[0])


@bp.get("/Patient/<int:pid>/$everything")
def everything(pid):
    try:
        res = F.everything(get_db(), g.clinic, pid)
    except F.FhirError as exc:
        return _fail(exc.status, exc.code, exc.text)
    _log([pid], "everything")
    return _send(F.bundle("Patient", res, _base(), f"{_base()}/Patient/{pid}/$everything", btype="searchset"))
