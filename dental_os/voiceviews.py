"""Phone calls (version 0.21): set-up, the call log, the ringing pop-up, click to call, and the addresses Twilio calls."""
from flask import Blueprint, Response, abort, current_app, flash, g, jsonify, redirect, render_template, request, url_for

from .auth import manager_required
from .db import get_db
from .services import i18n, voice as V

bp = Blueprint("voice", __name__)
hooks = Blueprint("voicehook", __name__)


def _secret():
    return current_app.config["SECRET_KEY"]


def _base():
    return current_app.config.get("PUBLIC_URL") or request.url_root


@bp.route("/settings/phone", methods=["GET", "POST"])
@manager_required
def setup():
    db = get_db()
    if request.method == "POST":
        try:
            V.save_account(db, g.clinic, g.user["id"], _secret(), request.form.get("enabled") == "1", request.form.get("account_sid"),
                           request.form.get("auth_token"), request.form.get("twilio_number"), request.form.get("desk_number"),
                           request.form.get("missed_task_to", type=int))
            flash("Saved.", "ok")
        except V.CallError as exc:
            db.rollback()
            flash(str(exc), "error")
        return redirect(url_for("voice.setup"))
    a = V.account(db, g.clinic["id"])
    users = db.execute("SELECT id, display_name FROM users WHERE clinic_id = ? AND active = 1 ORDER BY display_name", (g.clinic["id"],)).fetchall()
    hook = (_base().rstrip("/") + url_for("voicehook.incoming", key=a["hook_key"])) if a else None
    return render_template("voice_setup.html", a=a, users=users, hook=hook)


@bp.get("/calls")
def calls():
    return render_template("calls.html", rows=V.log(get_db(), g.clinic["id"]), statuses=V.STATUSES, on=V.is_on(get_db(), g.clinic["id"]))


@bp.get("/calls/live")
def live():
    db = get_db()
    out = []
    for c in V.ringing_now(db, g.clinic):
        out.append({"id": c["id"], "from": c["from_number"], "name": (f"{c['first_name']} {c['last_name']}" if c["patient_id"] else None),
                    "url": url_for("views.patient_view", pid=c["patient_id"]) if c["patient_id"] else url_for("views.patients", q=c["from_number"])})
    resp = jsonify({"calls": out})
    resp.headers["Cache-Control"] = "no-store"
    return resp


@bp.post("/patients/<int:pid>/call")
def call(pid):
    db = get_db()
    try:
        V.call_patient(db, g.clinic, g.user, _secret(), pid, _base())
        flash("Your desk phone is ringing. Pick it up and the patient's phone will ring next.", "ok")
    except V.CallError as exc:
        db.rollback()
        flash(str(exc), "error")
    return redirect(url_for("views.patient_view", pid=pid))


# ------------------------------------------------------------------------------------------------- the addresses Twilio calls
def _twilio(key):
    db = get_db()
    acct = V.account_for_hook(db, key)
    if not acct:
        abort(404)
    if current_app.config.get("PUBLIC_URL"):
        url = current_app.config["PUBLIC_URL"].rstrip("/") + request.full_path.rstrip("?")
    else:
        url = request.url
    if not V.signature_ok(db, acct, _secret(), url, request.form.to_dict(), request.headers.get("X-Twilio-Signature", "")):
        abort(403)
    clinic = db.execute("SELECT * FROM clinics WHERE id = ?", (acct["clinic_id"],)).fetchone()
    return db, acct, clinic


def _say(clinic, text):
    lang = i18n.clinic_lang(clinic)
    return _xml(V.twiml_missed(i18n.t(lang, text), lang))


def _xml(text):
    return Response(text, mimetype="text/xml")


@hooks.post("/hooks/voice/<key>")
def incoming(key):
    db, acct, clinic = _twilio(key)
    V.incoming(db, clinic, request.form.get("CallSid", ""), request.form.get("From", ""), request.form.get("To", ""))
    if not acct["enabled"]:
        return _say(clinic, "Sorry, the clinic cannot take calls on this line right now.")
    return _xml(V.twiml_ring(acct["desk_number"], _base().rstrip("/") + url_for("voicehook.after", key=key)))


@hooks.post("/hooks/voice/<key>/after")
def after(key):
    db, acct, clinic = _twilio(key)
    status = V.after_dial(db, clinic, request.form.get("CallSid", ""), request.form.get("DialCallStatus", ""), request.form.get("DialCallDuration"))
    if status == "completed":
        return _xml('<?xml version="1.0" encoding="UTF-8"?><Response><Hangup/></Response>')
    return _say(clinic, "Sorry we missed your call. We will call you back as soon as we can.")


@hooks.post("/hooks/voice/<key>/bridge")
def bridge(key):
    db, acct, clinic = _twilio(key)
    xml = V.bridge_for(db, acct, request.args.get("ref", ""))
    if not xml:
        return _say(clinic, "Sorry, this call could not be connected.")
    return _xml(xml)


@hooks.post("/hooks/voice/<key>/status")
def status(key):
    db, acct, clinic = _twilio(key)
    V.status_update(db, clinic, request.form.get("CallSid", ""), request.form.get("CallStatus", ""), request.form.get("CallDuration"))
    return "", 204
