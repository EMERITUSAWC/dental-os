"""The addresses Meta calls: one to prove the address is ours (GET), one to deliver statuses and replies (POST). There is no sign-in
and no form token here, so the only thing that lets a request in is Meta's signature, made with the clinic's app secret.
Each clinic has its own secret address, so one clinic's messages can never be filed under another."""
import json

from flask import Blueprint, abort, current_app, request

from .db import get_db
from .services import whatsapp as WA

bp = Blueprint("wahook", __name__)


@bp.get("/hooks/whatsapp/<key>")
def verify(key):
    acct = WA.hook_for_key(get_db(), key)
    if not acct:
        abort(404)
    import secrets
    given = request.args.get("hub.verify_token", "")
    if request.args.get("hub.mode") == "subscribe" and acct["verify_token"] and secrets.compare_digest(given, acct["verify_token"]):
        return request.args.get("hub.challenge", ""), 200, {"Content-Type": "text/plain; charset=utf-8"}
    abort(403)


@bp.post("/hooks/whatsapp/<key>")
def receive(key):
    db = get_db()
    acct = WA.hook_for_key(db, key)
    if not acct:
        abort(404)
    request.max_content_length = WA.MAX_BODY                 # refuse to read more than this
    raw = request.get_data(cache=False)
    if len(raw) > WA.MAX_BODY:
        abort(413)
    secret = WA.app_secret(db, acct["clinic_id"], current_app.config["SECRET_KEY"])
    if not WA.verify_signature(secret, raw, request.headers.get("X-Hub-Signature-256", "")):
        abort(403)                                           # not from Meta, or the secret is missing or wrong
    try:
        payload = json.loads(raw.decode("utf-8"))
    except ValueError:
        abort(400)
    clinic = db.execute("SELECT * FROM clinics WHERE id = ?", (acct["clinic_id"],)).fetchone()
    WA.handle(db, clinic, payload)
    WA.clean_events(db, 30)                                  # bookkeeping and unknown-number messages are forgotten after 30 days
    return "ok", 200, {"Content-Type": "text/plain; charset=utf-8"}
