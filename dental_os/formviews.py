"""The form builder screens (version 0.34), and the page where a patient fills a form in."""
from flask import Blueprint, abort, current_app, flash, g, redirect, render_template, request, session, url_for

from .auth import manager_required
from .db import get_db
from .services import formbuilder as FB, i18n, messaging, patients as P, qr, throttle

bp = Blueprint("cforms", __name__)
pub = Blueprint("formpub", __name__)


def _base():
    return current_app.config.get("PUBLIC_URL") or request.url_root


@bp.get("/forms/builder")
@manager_required
def builder_list():
    return render_template("cforms_list.html", rows=FB.forms(get_db(), g.clinic["id"]))


@bp.route("/forms/builder/new", methods=["GET", "POST"])
@bp.route("/forms/builder/<int:form_id>", methods=["GET", "POST"])
@manager_required
def builder(form_id=None):
    db = get_db()
    f = FB.get(db, g.clinic["id"], form_id) if form_id else None
    if form_id and not f:
        abort(404)
    if request.method == "POST":
        try:
            fields = FB.parse_fields(request.form)
            fid = FB.save(db, g.clinic, g.user["id"], request.form.get("title"), request.form.get("intro"), fields, form_id,
                          request.form.get("active", "1") == "1")
            flash("Form saved.", "ok")
            return redirect(url_for("cforms.builder", form_id=fid))
        except FB.FormError as exc:
            db.rollback()
            flash(str(exc), "error")
    fields = FB.fields_of(f) if f else []
    rows = fields + [{"key": "", "label": "", "kind": "text", "required": False, "options": []} for _ in range(max(5, 10 - len(fields)))]
    return render_template("cforms_builder.html", f=f, rows=rows[:FB.MAX_FIELDS + 10], kinds=FB.KINDS)


@bp.get("/forms/builder/<int:form_id>/preview")
@manager_required
def preview(form_id):
    f = FB.get(get_db(), g.clinic["id"], form_id)
    if not f:
        abort(404)
    return render_template("cforms_fill.html", title=f["title"], intro=f["intro"], fields=FB.fields_of(f), clinic=g.clinic, preview=True,
                           answers={}, bad=[])


@bp.route("/patients/<int:pid>/forms", methods=["GET", "POST"])
def patient_forms(pid):
    db = get_db()
    p = P.get(db, g.clinic["id"], pid)
    if not p:
        abort(404)
    link = None
    if request.method == "POST":
        try:
            token, rid = FB.send(db, g.clinic, g.user["id"], pid, request.form.get("form_id", type=int), request.form.get("days", 14, type=int))
            db.commit()
            link = _base().rstrip("/") + url_for("formpub.fill", token=token)
            flash("Form link made. It works once.", "ok")
        except FB.FormError as exc:
            db.rollback()
            flash(str(exc), "error")
    wa = qr_svg = None
    if link:
        f = FB.get(db, g.clinic["id"], request.form.get("form_id", type=int))
        text = i18n.t(i18n.patient_lang(p, g.clinic), "Hello %(name)s, please fill in this form from %(clinic)s before your visit: %(url)s",
                      name=p["first_name"], clinic=g.clinic["name"], url=link)
        wa = messaging.wa_link(p["phone"], text) if p["phone"] and p["reminders_ok"] else None
        qr_svg = qr.svg(link, 200, "QR code for the form")
    return render_template("cforms_patient.html", p=p, forms=FB.forms(db, g.clinic["id"], True), rows=FB.for_patient(db, g.clinic["id"], pid),
                           statuses=FB.STATUSES, link=link, wa=wa, qr_svg=qr_svg)


@bp.get("/forms/review")
def review_list():
    return render_template("cforms_review.html", rows=FB.to_review(get_db(), g.clinic["id"]))


@bp.route("/forms/responses/<int:rid>", methods=["GET", "POST"])
def response_view(rid):
    db = get_db()
    r = FB.response(db, g.clinic["id"], rid)
    if not r:
        abort(404)
    if request.method == "POST":
        try:
            if request.form.get("what") == "cancel":
                FB.cancel(db, g.clinic, g.user["id"], rid)
                flash("Form link cancelled.", "ok")
            else:
                FB.file_response(db, g.clinic, g.user["id"], rid, request.form.get("note"))
                flash("Form filed on the record.", "ok")
        except FB.FormError as exc:
            db.rollback()
            flash(str(exc), "error")
        return redirect(url_for("cforms.response_view", rid=rid))
    return render_template("cforms_response.html", r=r, rows=FB.shown(r), statuses=FB.STATUSES)


# ---------------------------------------------------------------------------------------------------------- the public page
@pub.before_request
def _anonymous():
    g.user = None


@pub.after_request
def _headers(resp):
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["X-Robots-Tag"] = "noindex, nofollow"
    return resp


@pub.route("/f/<token>", methods=["GET", "POST"])
def fill(token):
    db = get_db()
    bucket = "form-bad:" + throttle.key(current_app.config["SECRET_KEY"], request.remote_addr or "unknown")
    if throttle.count(db, bucket, 60) >= 30:
        abort(404)
    r = FB.resolve(db, token)
    if not r:
        throttle.record(db, bucket)
        db.commit()
        abort(404)
    clinic = db.execute("SELECT * FROM clinics WHERE id = ?", (r["clinic_id"],)).fetchone()
    patient = db.execute("SELECT * FROM patients WHERE id = ?", (r["patient_id"],)).fetchone()
    g.clinic = clinic
    g.lang = i18n.pick(request.args.get("lang")) or i18n.pick(session.get("lang")) or i18n.patient_lang(patient, clinic)
    g.doc_lang = g.lang
    f = db.execute("SELECT title, intro FROM custom_forms WHERE id = ?", (r["form_id"],)).fetchone()
    fields = __import__("json").loads(r["fields"])
    bad, answers = [], {}
    if request.method == "POST":
        try:
            FB.submit(db, token, request.form)
            db.commit()
            return render_template("cforms_done.html", clinic=clinic)
        except FB.FormError as exc:
            db.rollback()
            bad = exc.labels
            answers = request.form
            flash(str(exc), "error")
    if r["status"] != "sent":
        db.commit()
        return render_template("cforms_done.html", clinic=clinic, closed=True)
    return render_template("cforms_fill.html", title=f["title"], intro=f["intro"], fields=fields, clinic=clinic, preview=False,
                           answers=answers, bad=bad, first_name=patient["first_name"])
