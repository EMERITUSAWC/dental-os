"""Clinical depth (version 0.17): prescriptions, referrals in and out, vital signs, letters, the contact log, and X-ray layouts
with a viewer."""
from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from .safeurl import local_path
from .auth import clinical_required, manager_required
from .db import get_db
from .services import (audit, contactlog as CL, files as F, letters as L, mounts as M, patients as P, prescriptions as RX,
                       referrals as REF, vitals as V)
from .services.timeutil import now_local

bp = Blueprint("care", __name__)


def _patient(pid):
    p = P.get(get_db(), g.clinic["id"], pid)
    if not p:
        abort(404)
    return p


def _providers():
    return get_db().execute("SELECT * FROM providers WHERE clinic_id = ? AND active = 1 ORDER BY id", (g.clinic["id"],)).fetchall()


def _visits(pid):
    return get_db().execute("""SELECT id, start_at, category FROM appointments WHERE clinic_id = ? AND patient_id = ?
                               AND status IN ('scheduled','complete') AND start_at IS NOT NULL ORDER BY start_at DESC LIMIT 10""",
                            (g.clinic["id"], pid)).fetchall()


def _today():
    return now_local(g.clinic["timezone"]).date()


def _doc_lang(p):
    from .services import i18n
    g.doc_lang = i18n.patient_lang(p, g.clinic)


# ------------------------------------------------------------------------------------------------------------ prescriptions
def _rx_items(form):
    out = []
    for i in range(RX.MAX_ITEMS):
        drug_id = form.get(f"drug_{i}", type=int)
        if drug_id:
            out.append({"drug_id": drug_id, "dose": form.get(f"dose_{i}"), "days": form.get(f"days_{i}"), "quantity": form.get(f"qty_{i}")})
    return out


@bp.route("/patients/<int:pid>/prescriptions", methods=["GET", "POST"])
@clinical_required
def prescriptions(pid):
    db, cid = get_db(), g.clinic["id"]
    p = _patient(pid)
    drugs = RX.list_drugs(db, cid, active_only=True)
    warnings, form = [], request.form if request.method == "POST" else {}
    if request.method == "POST":
        items = _rx_items(request.form)
        chosen = [RX.get_drug(db, cid, it["drug_id"]) for it in items]
        warnings = RX.checks(db, cid, pid, [d for d in chosen if d])
        if request.form.get("action") == "issue":
            try:
                rx_id = RX.create(db, g.clinic, g.user, pid, request.form.get("provider_id", type=int), items, request.form.get("notes"),
                                  request.form.get("override_reason"), request.form.get("appointment_id", type=int))
                flash("Prescription written. Print it and sign it in ink.", "ok")
                return redirect(url_for("care.prescription_print", rx_id=rx_id))
            except RX.RxError as exc:
                db.rollback()
                flash(str(exc), "error")
    return render_template("rx_patient.html", p=p, drugs=drugs, rows=RX.list_for_patient(db, cid, pid), providers=_providers(),
                           visits=_visits(pid), warnings=warnings, form=form, max_items=RX.MAX_ITEMS,
                           checked=request.method == "POST", can_write=g.user["role"] in ("manager", "dentist"))


@bp.get("/prescriptions/<int:rx_id>")
@clinical_required
def prescription_print(rx_id):
    db, cid = get_db(), g.clinic["id"]
    rx = RX.get(db, cid, rx_id)
    if not rx:
        abort(404)
    p = _patient(rx["patient_id"])
    _doc_lang(p)
    audit.log(db, cid, g.user["id"], "rx.view", "patient", p["id"])
    return render_template("rx_print.html", rx=rx, p=p, items=RX.items(db, rx_id), age=RX.age_on(p["dob"], rx["issued_on"]))


@bp.post("/prescriptions/<int:rx_id>/cancel")
@clinical_required
def prescription_cancel(rx_id):
    db = get_db()
    try:
        pid = RX.cancel(db, g.clinic, g.user, rx_id, request.form.get("reason"))
        flash("Prescription cancelled. It stays on the record.", "ok")
        return redirect(url_for("care.prescriptions", pid=pid))
    except RX.RxError as exc:
        db.rollback()
        flash(str(exc), "error")
        return redirect(url_for("care.prescription_print", rx_id=rx_id))


@bp.route("/tools/medicines", methods=["GET", "POST"])
@manager_required
def drugs():
    db, cid = get_db(), g.clinic["id"]
    if request.method == "POST":
        try:
            action = request.form.get("action")
            if action == "starters":
                n = RX.add_starter_drugs(db, g.clinic, g.user["id"])
                flash("Starter medicines added. Check every dose against your national formulary." if n
                      else "You already have all the starter medicines.", "ok")
            elif action == "toggle":
                RX.toggle_drug(db, g.clinic, g.user["id"], request.form.get("id", type=int))
                flash("Updated.", "ok")
            else:
                RX.save_drug(db, g.clinic, g.user["id"], request.form.get("id", type=int), request.form.get("name"), request.form.get("strength"),
                             request.form.get("form"), request.form.get("dose"), request.form.get("days"), request.form.get("quantity"),
                             request.form.get("drug_class"), request.form.get("controlled") == "1", request.form.get("warnings"))
                flash("Medicine saved.", "ok")
        except RX.RxError as exc:
            db.rollback()
            flash(str(exc), "error")
        return redirect(url_for("care.drugs"))
    return render_template("rx_drugs.html", rows=RX.list_drugs(db, cid), classes=RX.CLASSES,
                           edit=RX.get_drug(db, cid, request.args.get("edit", type=int)) if request.args.get("edit") else None)


# ---------------------------------------------------------------------------------------------------------------- referrals
@bp.route("/patients/<int:pid>/referrals", methods=["GET", "POST"])
@clinical_required
def patient_referrals(pid):
    db, cid = get_db(), g.clinic["id"]
    p = _patient(pid)
    if request.method == "POST":
        try:
            rid = REF.create_out(db, g.clinic, g.user["id"], pid, request.form.get("referrer_id", type=int), request.form.get("reason"),
                                 request.form.get("urgency") or "routine", request.form.get("teeth"), request.form.get("history"),
                                 request.form.get("provider_id", type=int))
            flash("Referral saved as a draft. Print the letter, then mark it as sent.", "ok")
            return redirect(url_for("care.referral", rid=rid))
        except REF.ReferralError as exc:
            db.rollback()
            flash(str(exc), "error")
    return render_template("referrals_patient.html", p=p, rows=REF.for_patient(db, cid, pid),
                           referrers=[r for r in REF.list_referrers(db, cid, True) if r["kind"] in ("dentist", "specialist", "doctor", "hospital", "other")],
                           providers=_providers(), urgency=REF.URGENCY, statuses=REF.STATUSES, form=request.form)


@bp.route("/referrals/<int:rid>", methods=["GET", "POST"])
@clinical_required
def referral(rid):
    db, cid = get_db(), g.clinic["id"]
    r = REF.get_out(db, cid, rid)
    if not r:
        abort(404)
    if request.method == "POST":
        try:
            REF.move(db, g.clinic, g.user["id"], rid, request.form.get("status"), request.form.get("note"), _today().isoformat())
            flash("Referral updated.", "ok")
        except REF.ReferralError as exc:
            db.rollback()
            flash(str(exc), "error")
        return redirect(url_for("care.referral", rid=rid))
    p = _patient(r["patient_id"])
    _doc_lang(p)
    from .services import medical
    hist = medical.get_history(db, cid, p["id"])
    return render_template("referral.html", r=r, p=p, statuses=REF.STATUSES, urgency=REF.URGENCY, next=REF.NEXT[r["status"]],
                           hist=hist, alerts=medical.active_alerts(db, cid, p["id"]), today=_today())


@bp.get("/referrals")
@clinical_required
def referrals_open():
    return render_template("referrals_open.html", rows=REF.open_list(get_db(), g.clinic["id"]), statuses=REF.STATUSES, urgency=REF.URGENCY)


@bp.route("/tools/referrers", methods=["GET", "POST"])
def referrers():
    db, cid = get_db(), g.clinic["id"]
    if g.user["role"] not in ("manager", "front_desk", "dentist"):
        abort(403)
    if request.method == "POST":
        try:
            if request.form.get("action") == "toggle":
                REF.toggle_referrer(db, g.clinic, g.user["id"], request.form.get("id", type=int))
                flash("Updated.", "ok")
            else:
                REF.save_referrer(db, g.clinic, g.user["id"], request.form.get("id", type=int), request.form.get("name"),
                                  request.form.get("kind"), request.form.get("specialty"), request.form.get("practice"),
                                  request.form.get("phone"), request.form.get("email"), request.form.get("address"))
                flash("Referrer saved.", "ok")
        except REF.ReferralError as exc:
            db.rollback()
            flash(str(exc), "error")
        return redirect(url_for("care.referrers"))
    today = _today()
    start = request.args.get("from") or today.replace(day=1).isoformat()
    by_ref, by_source = REF.sources_report(db, cid, start, (today.isoformat() + " 99"))
    return render_template("referrers.html", rows=REF.list_referrers(db, cid), kinds=REF.KINDS, sources=REF.SOURCES,
                           edit=REF.get_referrer(db, cid, request.args.get("edit", type=int)) if request.args.get("edit") else None,
                           by_ref=by_ref, by_source=by_source, start=start)


@bp.post("/patients/<int:pid>/referral-source")
def referral_source(pid):
    db = get_db()
    _patient(pid)
    try:
        REF.set_source(db, g.clinic, g.user["id"], pid, request.form.get("referrer_id", type=int), request.form.get("source") or "")
        flash("Saved.", "ok")
    except REF.ReferralError as exc:
        db.rollback()
        flash(str(exc), "error")
    return redirect(url_for("views.patient_view", pid=pid))


# ------------------------------------------------------------------------------------------------------------------- vitals
@bp.route("/patients/<int:pid>/vitals", methods=["GET", "POST"])
@clinical_required
def vitals(pid):
    db, cid = get_db(), g.clinic["id"]
    p = _patient(pid)
    if request.method == "POST":
        try:
            _vid, grade = V.record(db, g.clinic, g.user["id"], pid, request.form, request.form.get("appointment_id", type=int))
            if grade in ("amber", "red"):
                flash(V.GRADES[grade], "error")
            else:
                flash("Readings saved.", "ok")
        except V.VitalsError as exc:
            db.rollback()
            flash(str(exc), "error")
        return redirect(url_for("care.vitals", pid=pid))
    rows = V.history(db, cid, pid)
    graded = [{"row": r, "grade": V.grade(g.clinic, r["bp_systolic"], r["bp_diastolic"])} for r in rows]
    return render_template("vitals.html", p=p, rows=graded, grades=V.GRADES, visits=_visits(pid))


@bp.post("/settings/blood-pressure")
@manager_required
def bp_levels():
    db = get_db()
    try:
        V.save_levels(db, g.clinic, g.user["id"], request.form.get("amber"), request.form.get("red"))
        flash("Saved.", "ok")
    except V.VitalsError as exc:
        flash(str(exc), "error")
    return redirect(url_for("views.settings"))


# ------------------------------------------------------------------------------------------------------------------ letters
@bp.route("/patients/<int:pid>/letters", methods=["GET", "POST"])
def patient_letters(pid):
    db, cid = get_db(), g.clinic["id"]
    p = _patient(pid)
    if not L.list_templates(db, cid):
        L.ensure_starters(db, cid)
    templates = L.list_templates(db, cid, active_only=True)
    if request.method == "POST":
        try:
            lid = L.save_letter(db, g.clinic, g.user["id"], pid, request.form.get("title"), request.form.get("body"),
                                request.form.get("addressee"), request.form.get("template_id", type=int), request.form.get("provider_id", type=int))
            flash("Letter saved. Print it from here.", "ok")
            return redirect(url_for("care.letter_print", lid=lid))
        except L.LetterError as exc:
            db.rollback()
            flash(str(exc), "error")
    draft = None
    tid = request.args.get("template_id", type=int)
    if tid:
        t = L.get_template(db, cid, tid)
        if not t:
            abort(404)
        provider = None
        pid_ = request.args.get("provider_id", type=int)
        if pid_:
            provider = db.execute("SELECT * FROM providers WHERE id = ? AND clinic_id = ?", (pid_, cid)).fetchone()
        from .services import i18n
        draft = {"template": t, "title": t["name"], "body": L.fill(t["body"], L.context(db, g.clinic, p, provider, i18n.patient_lang(p, g.clinic))),
                 "provider_id": pid_}
    return render_template("letters_patient.html", p=p, templates=templates, rows=L.for_patient(db, cid, pid), draft=draft,
                           providers=_providers(), form=request.form)


@bp.get("/letters/<int:lid>")
def letter_print(lid):
    db, cid = get_db(), g.clinic["id"]
    letter = L.get_letter(db, cid, lid)
    if not letter:
        abort(404)
    p = _patient(letter["patient_id"])
    _doc_lang(p)
    return render_template("letter_print.html", letter=letter, p=p)


@bp.route("/tools/letters", methods=["GET", "POST"])
@manager_required
def letter_templates():
    db, cid = get_db(), g.clinic["id"]
    if not L.list_templates(db, cid):
        L.ensure_starters(db, cid)
    if request.method == "POST":
        try:
            if request.form.get("action") == "toggle":
                L.toggle_template(db, g.clinic, g.user["id"], request.form.get("id", type=int))
                flash("Updated.", "ok")
            else:
                L.save_template(db, g.clinic, g.user["id"], request.form.get("id", type=int), request.form.get("name"), request.form.get("body"))
                flash("Letter saved.", "ok")
        except L.LetterError as exc:
            db.rollback()
            flash(str(exc), "error")
        return redirect(url_for("care.letter_templates"))
    return render_template("letter_templates.html", rows=L.list_templates(db, cid), fields=L.FIELDS,
                           edit=L.get_template(db, cid, request.args.get("edit", type=int)) if request.args.get("edit") else None)


# -------------------------------------------------------------------------------------------------------------- contact log
@bp.route("/patients/<int:pid>/contacts", methods=["GET", "POST"])
def contacts(pid):
    db, cid = get_db(), g.clinic["id"]
    p = _patient(pid)
    if request.method == "POST":
        try:
            CL.add(db, g.clinic, g.user["id"], pid, request.form.get("kind"), request.form.get("summary"), request.form.get("outcome"),
                   request.form.get("follow_up_on"))
            flash("Saved to the contact log.", "ok")
        except CL.ContactError as exc:
            db.rollback()
            flash(str(exc), "error")
        return redirect(url_for("care.contacts", pid=pid))
    return render_template("contacts.html", p=p, rows=CL.for_patient(db, cid, pid), kinds=CL.KINDS, today=_today())


@bp.post("/contacts/<int:log_id>/done")
def contact_done(log_id):
    db = get_db()
    try:
        pid = CL.done(db, g.clinic, g.user["id"], log_id)
    except CL.ContactError:
        abort(404)
    flash("Follow-up marked as done.", "ok")
    back = request.form.get("back") or ""
    if local_path(back):
        return redirect(back)
    return redirect(url_for("care.contacts", pid=pid))


# ------------------------------------------------------------------------------------------------------- X-ray layouts, viewer
@bp.route("/patients/<int:pid>/images", methods=["GET", "POST"])
@clinical_required
def images(pid):
    db, cid = get_db(), g.clinic["id"]
    p = _patient(pid)
    if request.method == "POST":
        try:
            mid = M.create(db, g.clinic, g.user["id"], pid, request.form.get("layout"), request.form.get("title"), request.form.get("taken_on"))
            return redirect(url_for("care.mount", mid=mid))
        except M.MountError as exc:
            db.rollback()
            flash(str(exc), "error")
    return render_template("images.html", p=p, rows=M.for_patient(db, cid, pid), layouts=M.LAYOUTS, today=_today())


@bp.route("/mounts/<int:mid>", methods=["GET", "POST"])
@clinical_required
def mount(mid):
    db, cid = get_db(), g.clinic["id"]
    m = M.get(db, cid, mid)
    if not m:
        abort(404)
    if request.method == "POST":
        try:
            M.place(db, g.clinic, g.user["id"], mid, request.form.get("slot"), request.form.get("file_id", type=int))
        except M.MountError as exc:
            db.rollback()
            flash(str(exc), "error")
        return redirect(url_for("care.mount", mid=mid))
    p = _patient(m["patient_id"])
    pictures = [f for f in F.list_for_patient(db, cid, p["id"]) if f["mime"] in F.VIEWABLE]
    return render_template("mount.html", p=p, m=m, grid=M.slots(m["layout"]), placed=M.placed(db, mid), pictures=pictures,
                           layout_name=M.LAYOUTS[m["layout"]][0])


@bp.get("/files/<int:file_id>/viewer")
@clinical_required
def viewer(file_id):
    db, cid = get_db(), g.clinic["id"]
    f = F.get(db, cid, file_id)
    if not f or f["mime"] not in F.VIEWABLE:
        abort(404)
    p = _patient(f["patient_id"])
    audit.log(db, cid, g.user["id"], "file.view", "patient", p["id"])
    info = None
    if f["mime"] == "application/dicom":
        from .record import _files_root
        info = F.dicom_summary(_files_root(), f)
    from flask import current_app
    from .services import xrayai as XA, xrayqc as QC
    return render_template("viewer.html", f=f, p=p, kinds=F.KINDS, info=info, mismatch=_name_mismatch(info, p),
                           qc=QC.for_files(db, cid, [f["id"]]).get(f["id"]), qc_text=QC.ISSUES,
                           ai_on=XA.is_on(db, cid, current_app.config["SECRET_KEY"]), ai_reads=XA.for_file(db, cid, f["id"]),
                           ai_kinds=XA.KINDS, ai_states=XA.STATES, can_decide=g.user["role"] in XA.DECIDERS)


def _name_mismatch(info, p):
    """True when the name inside the DICOM file plainly is not this patient's: a sign it was filed on the wrong record."""
    if not info or not info.get("patient_name"):
        return False
    inside = {w.lower() for w in info["patient_name"].replace(",", " ").split()}
    ours = {w.lower() for w in f"{p['first_name']} {p['last_name']}".split()}
    return not (inside & ours)


# ---------------------------------------------------------------------------------------------- the imaging bridge (0.25)
@bp.post("/patients/<int:pid>/xray-now")
@clinical_required
def xray_now(pid):
    from .services import imaging as IM
    db = get_db()
    _patient(pid)
    try:
        IM.request_images(db, g.clinic, g.user["id"], pid, request.form.get("station"))
        flash("The X-ray computer is ready for this patient. New images from it are filed here for the next 30 minutes.", "ok")
    except IM.ImagingError as exc:
        db.rollback()
        flash(str(exc), "error")
    back = request.form.get("back") or ""
    return redirect(back if local_path(back) else url_for("record.files_view", pid=pid))


@bp.post("/imaging/<int:rid>/done")
@clinical_required
def xray_done(rid):
    from .services import imaging as IM
    try:
        pid = IM.finish(get_db(), g.clinic, g.user["id"], rid)
        flash("X-ray session finished.", "ok")
    except IM.ImagingError as exc:
        flash(str(exc), "error")
        return redirect(url_for("views.dashboard"))
    return redirect(url_for("record.files_view", pid=pid))


@bp.route("/imaging", methods=["GET", "POST"])
@manager_required
def imaging_setup():
    from .services import imaging as IM
    db = get_db()
    if request.method == "POST":
        try:
            IM.add_station(db, g.clinic, g.user["id"], request.form.get("name"))
            flash("Saved.", "ok")
        except IM.ImagingError as exc:
            db.rollback()
            flash(str(exc), "error")
        return redirect(url_for("care.imaging_setup"))
    now = IM._now(db)
    recent = IM._now(db, -2)
    return render_template("imaging_setup.html", rows=IM.stations(db, g.clinic["id"]), recent=recent, now=now)
