"""Pages that need no staff login: the online booking page and a patient's private link. They share no session with the staff
screens, show only what is meant for the public or for that one patient, and are rate limited."""
from flask import Blueprint, abort, current_app, flash, g, redirect, render_template, request, session, url_for

from .db import get_db
from .services import booking, i18n, links, outbox, scheduling, throttle

bp = Blueprint("public", __name__)
BAD_LINKS_PER_HOUR = 20


@bp.before_request
def _anonymous():
    g.user = None          # a signed-in staff member opening a patient's link is treated as a visitor


@bp.after_request
def _headers(resp):
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["X-Robots-Tag"] = "noindex, nofollow"
    return resp


def _speak(clinic, patient=None):
    """Pick the language of this page: the visitor's own choice (?lang=fr, kept in their browser session), else the patient's, else the clinic's."""
    g.lang = i18n.pick(session.get("lang")) or (i18n.patient_lang(patient, clinic) if patient else i18n.clinic_lang(clinic))


def say(text):
    return i18n.t(g.lang, text)


def _key():
    return throttle.key(current_app.config["SECRET_KEY"], request.remote_addr or "unknown")


# ----------------------------------------------------------------------------- online booking
@bp.route("/book/<token>", methods=["GET", "POST"])
def book(token):
    db = get_db()
    clinic, c = booking.clinic_for_token(db, token)
    if not clinic:
        abort(404)
    g.clinic = clinic
    _speak(clinic)
    if request.method == "POST":
        try:
            booking.submit(db, clinic, c, request.form, _key())
            db.commit()
            return render_template("public_book_done.html")
        except booking.BookingError as exc:
            db.rollback()
            flash(" ".join(say(p) for p in exc.parts) if exc.parts else say(str(exc)), "error")
    chosen = request.values.get("provider_id", type=int)
    return render_template("public_book.html", days=booking.offered(db, clinic, c, chosen, lang=g.lang), c=c, providers=booking.providers(db, clinic["id"]),
                           categories=scheduling.CATEGORIES, chosen=chosen, token=token, form=request.form if request.method == "POST" else {})


# ----------------------------------------------------------------------------- a patient's link
def _resolve(token):
    db = get_db()
    bucket = f"link-bad:{_key()}"
    if throttle.count(db, bucket, 60) >= BAD_LINKS_PER_HOUR:
        abort(404)
    found = links.resolve(db, token)
    if not found:
        throttle.record(db, bucket)
        db.commit()
        abort(404)
    link, patient, clinic = found
    g.clinic = clinic
    _speak(clinic, patient)
    return db, link, patient, clinic


@bp.get("/p/<token>")
def portal(token):
    db, link, patient, clinic = _resolve(token)
    return render_template("portal.html", p=patient, d=links.portal(db, clinic, patient, g.lang), token=token)


@bp.post("/p/<token>/visit/<int:appt_id>")
def portal_visit(token, appt_id):
    db, link, patient, clinic = _resolve(token)
    try:
        links.confirm_visit(db, clinic, patient, appt_id, request.form.get("answer"))
        flash(say("Thank you. We have your answer."), "ok")
    except (links.LinkError, scheduling.SchedulingError) as exc:
        db.rollback()
        flash(say(str(exc)), "error")
    return redirect(url_for("public.portal", token=token))


@bp.post("/p/<token>/quote/<int:plan_id>/accept")
def portal_quote(token, plan_id):
    db, link, patient, clinic = _resolve(token)
    relation = request.form.get("relation", "self")
    try:
        links.accept_quote(db, clinic, patient, plan_id, request.form.get("signer_name"), relation, bool(request.form.get("agreed")))
        flash(say("Thank you. The clinic has your agreement and will contact you to book."), "ok")
    except links.LinkError as exc:
        db.rollback()
        flash(say(str(exc)), "error")
    return redirect(url_for("public.portal", token=token))


@bp.route("/p/<token>/health", methods=["GET", "POST"])
def portal_health(token):
    db, link, patient, clinic = _resolve(token)
    if request.method == "POST":
        try:
            links.submit_form(db, clinic, patient, link["id"], request.form)
            flash(say("Thank you. The clinic will check your answers."), "ok")
            return redirect(url_for("public.portal", token=token))
        except links.LinkError as exc:
            db.rollback()
            flash(say(str(exc)), "error")
    return render_template("portal_health.html", p=patient, token=token, form=request.form if request.method == "POST" else {})
