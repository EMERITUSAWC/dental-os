"""Version 0.62: the pharmacy check link, the FHIR copy, sending to an e-prescribing network, and the public page a pharmacist opens."""
import json

from flask import Blueprint, Response, abort, current_app, flash, g, redirect, render_template, request, session, url_for

from .auth import clinical_required, manager_required
from .db import get_db
from .services import audit, i18n, prescriptions as RX, qr, rxlink as RL, throttle
from .services.timeutil import now_local

bp = Blueprint("rxnet", __name__)
pub = Blueprint("rxpub", __name__)


def _base():
    return (current_app.config.get("PUBLIC_URL") or request.url_root).rstrip("/")


def _rx(rx_id):
    rx = RX.get(get_db(), g.clinic["id"], rx_id)
    if not rx:
        abort(404)
    return rx


def pharmacy_link(rx_id):
    """(url, svg) for the print page, or (None, None) when the prescription has no link yet."""
    db = get_db()
    if not RL.link_row(db, g.clinic["id"], rx_id):
        return None, None
    url = _base() + url_for("rxpub.check", token=RL.token_for(db, g.clinic, current_app.config["SECRET_KEY"], rx_id))
    return url, qr.svg(url, pixels=150, label=i18n.t(getattr(g, "doc_lang", None) or getattr(g, "lang", None), "Pharmacy check"))


@bp.post("/prescriptions/<int:rx_id>/link")
@clinical_required
def make_link(rx_id):
    _rx(rx_id)
    try:
        RL.token_for(get_db(), g.clinic, current_app.config["SECRET_KEY"], rx_id, g.user["id"])
        flash("The pharmacy check is on the prescription. Print it again to include the code.", "ok")
    except RL.RxLinkError as exc:
        flash(str(exc), "error")
    return redirect(url_for("care.prescription_print", rx_id=rx_id))


@bp.get("/prescriptions/<int:rx_id>/fhir.json")
@clinical_required
def fhir(rx_id):
    db = get_db()
    rx = _rx(rx_id)
    p = db.execute("SELECT * FROM patients WHERE id = ? AND clinic_id = ?", (rx["patient_id"], g.clinic["id"])).fetchone()
    audit.log(db, g.clinic["id"], g.user["id"], "rx.fhir", "patient", p["id"])
    body = json.dumps(RL.fhir_bundle(db, g.clinic, rx, RX.items(db, rx_id), p), indent=2, ensure_ascii=False)
    return Response(body, mimetype="application/fhir+json", headers={"Content-Disposition": f'attachment; filename="prescription-{rx_id}.fhir.json"'})


@bp.post("/prescriptions/<int:rx_id>/send")
@clinical_required
def send(rx_id):
    db = get_db()
    rx = _rx(rx_id)
    p = db.execute("SELECT * FROM patients WHERE id = ? AND clinic_id = ?", (rx["patient_id"], g.clinic["id"])).fetchone()
    try:
        sid = RL.send(db, g.clinic, g.user, current_app.config["SECRET_KEY"], rx, RX.items(db, rx_id), p,
                      transport=current_app.config.get("ERX_TRANSPORT"))
        ok = db.execute("SELECT status FROM erx_sends WHERE id = ?", (sid,)).fetchone()["status"] == "accepted"
        flash("Sent. The network accepted the prescription." if ok else "The network did not take it. The reason is below.", "ok" if ok else "error")
    except PermissionError:
        abort(403)
    except RL.RxLinkError as exc:
        flash(str(exc), "error")
    return redirect(url_for("care.prescription_print", rx_id=rx_id))


@bp.route("/settings/e-prescribing", methods=["GET", "POST"])
@manager_required
def setup():
    db = get_db()
    if request.method == "POST":
        try:
            RL.save_network(db, g.clinic, g.user["id"], current_app.config["SECRET_KEY"], request.form)
            flash("Saved.", "ok")
            return redirect(url_for("rxnet.setup"))
        except RL.RxLinkError as exc:
            flash(str(exc), "error")
    n = RL.network(db, g.clinic["id"])
    return render_template("erx_setup.html", n=n, on=RL.network_on(db, g.clinic["id"]), has_key=bool(n and n["api_key"]),
                           valid=RL.VALID_DAYS, controlled=RL.CONTROLLED_VALID_DAYS)


# ---------------------------------------------------------------------------------------------------------- the pharmacy's page
@pub.before_request
def _anonymous():
    g.user = None


@pub.after_request
def _headers(resp):
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["X-Robots-Tag"] = "noindex, nofollow"
    return resp


@pub.route("/rx/<token>", methods=["GET", "POST"])
def check(token):
    db = get_db()
    bucket = "rx-bad:" + throttle.key(current_app.config["SECRET_KEY"], request.remote_addr or "unknown")
    if throttle.count(db, bucket, 60) >= RL.BAD_PER_HOUR:
        abort(404)
    link, rx = RL.resolve(db, current_app.config["SECRET_KEY"], token)
    if not link:
        throttle.record(db, bucket)
        db.commit()
        abort(404)
    clinic = db.execute("SELECT * FROM clinics WHERE id = ?", (link["clinic_id"],)).fetchone()
    patient = db.execute("SELECT first_name, last_name, dob FROM patients WHERE id = ?", (rx["patient_id"],)).fetchone()
    g.clinic = clinic
    g.lang = i18n.pick(request.args.get("lang")) or i18n.pick(session.get("lang")) or i18n.clinic_lang(clinic)
    items = RX.items(db, rx["id"])
    if request.method == "POST":
        try:
            RL.dispense(db, clinic, link, rx, items, request.form)
            db.commit()
            flash("Recorded as dispensed. Thank you.", "ok")
        except RL.RxLinkError as exc:
            db.rollback()
            flash(str(exc), "error")
        return redirect(url_for("rxpub.check", token=token))
    today = now_local(clinic["timezone"]).date()
    return render_template("rx_check.html", clinic=clinic, rx=rx, items=items, link=link, state=RL.state(link, rx, items, today),
                           states=RL.STATES, initials=RL.initials(patient), born=(patient["dob"] or "")[:4], until=RL.last_until(rx, items))
