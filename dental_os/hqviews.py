"""Version 0.71: Dental OS HQ, the company's own workspace, one part per department. Signed in with the operator login."""
import datetime

from flask import Blueprint, Response, abort, flash, g, redirect, render_template, request, url_for

from .db import get_db
from .platformviews import operator_can, operator_required
from .services import hq as H, hqtech as T, opteam as OT, platform as PF, regions

bp = Blueprint("hq", __name__)
clinic_bp = Blueprint("help", __name__)        # the help library as clinic staff see it


def _int(name):
    v = request.form.get(name) or request.args.get(name)
    return int(v) if str(v or "").isdigit() and len(str(v)) < 12 else None


def _clinic_names(db):
    return db.execute("SELECT id, name FROM clinics ORDER BY name").fetchall()


def _done(endpoint, **kw):
    get_db().commit()
    flash("Saved.", "ok")
    return redirect(url_for(endpoint, **kw))


def _failed(exc, endpoint, **kw):
    get_db().rollback()
    flash(str(exc), "error")
    return redirect(url_for(endpoint, **kw))


# -------------------------------------------------------------------------------------------------------------------- home
@bp.get("/hq")
@operator_required
def home():
    db = get_db()
    return render_template("hq_home.html", work=H.my_work(db, g.operator, g.op_depts), depts=OT.DEPTS, about=OT.DEPT_ABOUT,
                           mine=g.op_depts, owner=OT.is_owner(g.operator), states=H.TICKET_STATES, stages=H.STAGES,
                           server_flags=T.INSTALL_FLAGS)


@bp.get("/hq/leadership")
@operator_required
@operator_can("hq.leadership")
def leadership():
    db = get_db()
    return render_template("hq_leadership.html", x=H.company(db), states=PF.STATES)


@bp.get("/hq/departments/<dept>")
@operator_required
def department(dept):
    """A department head's view of their own team. Owners see every department."""
    if dept not in OT.DEPTS:
        abort(404)
    if dept not in g.op_heads:
        abort(403)
    db = get_db()
    return render_template("hq_department.html", dept=dept, label=OT.DEPTS[dept], about=OT.DEPT_ABOUT[dept], rows=OT.team(db, dept))


# ---------------------------------------------------------------------------------------------------------- customer support
@bp.route("/hq/support", methods=["GET", "POST"])
@operator_required
@operator_can("hq.support")
def support():
    db = get_db()
    which = request.args.get("which", "open")
    if request.method == "POST":
        f = request.form
        try:
            if f.get("what") == "import":
                n = H.import_reports(db, g.operator["id"])
                db.commit()
                flash("Shared problem reports are in the inbox." if n else "No new problem reports to bring in.", "ok")
                return redirect(url_for("hq.support"))
            tid = H.new_ticket(db, g.operator["id"], _int("clinic_id"), f.get("contact"), f.get("channel"), f.get("subject"), f.get("body"),
                               f.get("priority"), _int("assigned_to"))
            db.commit()
            flash("Saved.", "ok")
            return redirect(url_for("hq.ticket_view", tid=tid))
        except H.HQError as exc:
            return _failed(exc, "hq.support")
    return render_template("hq_support.html", rows=H.tickets(db, which, g.operator["id"]), which=which, nums=H.support_numbers(db),
                           clinics=_clinic_names(db), people=H.people_in(db, "support"), priorities=H.PRIORITIES, channels=H.CHANNELS,
                           states=H.TICKET_STATES, sla=H.SLA_HOURS)


@bp.route("/hq/support/<int:tid>", methods=["GET", "POST"])
@operator_required
@operator_can("hq.support")
def ticket_view(tid):
    db = get_db()
    try:
        t = H.ticket(db, tid)
    except H.HQError:
        abort(404)
    if request.method == "POST":
        f = request.form
        try:
            what = f.get("what")
            if what in ("reply", "internal"):
                H.add_note(db, g.operator["id"], tid, what, f.get("body"))
                if f.get("ai_draft"):                                   # 0.75: a suggested answer was used
                    db.execute("INSERT INTO operator_log(operator_id, clinic_id, action, detail) VALUES (?,?, 'ai.used', ?)",
                               (g.operator["id"], t["clinic_id"], "support draft" + ("" if f.get("ai_draft") == f.get("body") else ", edited")))
            elif what == "change":
                H.set_ticket(db, g.operator["id"], tid, status=f.get("status") or None,
                             assigned_to=_int("assigned_to") if f.get("assigned_to") else (0 if "assigned_to" in f else None),
                             priority=f.get("priority") or None)
            else:
                abort(400)
            return _done("hq.ticket_view", tid=tid)
        except H.HQError as exc:
            return _failed(exc, "hq.ticket_view", tid=tid)
    from .services import aidesk
    matches = aidesk.match_articles(db, t["subject"], t["body"])
    draft = None
    use = _int("use")
    if use:
        chosen = next((a for a in matches if a["id"] == use), None) or db.execute("SELECT * FROM hq_articles WHERE id = ? AND published = 1", (use,)).fetchone()
        draft = aidesk.support_draft(chosen, t["contact"]) if chosen else None
    return render_template("hq_ticket.html", t=t, notes=H.ticket_notes(db, tid), clocks=H.clocks(t), people=H.people_in(db, "support"),
                           priorities=H.PRIORITIES, channels=H.CHANNELS, states=H.TICKET_STATES, matches=matches, draft=draft)


# ----------------------------------------------------------------------------------------------------------------- help library
@bp.get("/hq/help")
@operator_required
@operator_can("hq.articles")
def help_list():
    return render_template("hq_help.html", rows=H.articles(get_db()))


@bp.route("/hq/help/new", methods=["GET", "POST"], defaults={"aid": None})
@bp.route("/hq/help/<int:aid>", methods=["GET", "POST"])
@operator_required
@operator_can("hq.articles")
def help_edit(aid):
    db = get_db()
    row = None
    if aid:
        try:
            row = H.article(db, aid)
        except H.HQError:
            abort(404)
    if request.method == "POST":
        f = request.form
        try:
            new_id = H.save_article(db, g.operator["id"], aid, f.get("title"), f.get("body"), f.get("audience"), f.get("published") == "1")
            return _done("hq.help_edit", aid=new_id)
        except H.HQError as exc:
            db.rollback()
            flash(str(exc), "error")
            return render_template("hq_help_edit.html", row=row, form=f)
    return render_template("hq_help_edit.html", row=row, form=row or {})


@clinic_bp.get("/help")
def clinic_help():
    return render_template("help_list.html", rows=H.articles(get_db(), "clinic", True))


@clinic_bp.get("/help/<int:aid>")
def clinic_help_article(aid):
    try:
        row = H.article(get_db(), aid, clinic_side=True)
    except H.HQError:
        abort(404)
    return render_template("help_article.html", row=row, paras=H.paragraphs(row["body"]))


# ---------------------------------------------------------------------------------------------------------------------- finance
@bp.get("/hq/finance")
@operator_required
@operator_can("hq.finance")
def finance():
    db = get_db()
    status = request.args.get("status")
    return render_template("hq_finance.html", subs=H.subscriptions(db), income=H.monthly_income(db), owed=H.owed(db), late=H.overdue(db),
                           bills=H.invoices(db, status), status=status, states=PF.STATES)


@bp.get("/hq/finance/vat")
@operator_required
@operator_can("hq.finance")
def vat():
    db = get_db()
    a = request.args
    today = datetime.date.today()
    first = today.replace(day=1)
    last_month_end = first - datetime.timedelta(days=1)
    start, end = a.get("start") or last_month_end.replace(day=1).isoformat(), a.get("end") or last_month_end.isoformat()
    report = None
    if a.get("rate") is not None and a.get("rate") != "":
        try:
            report = H.vat_report(db, start, end, a.get("rate"), a.get("included", "1") == "1")
        except H.HQError as exc:
            flash(str(exc), "error")
            return redirect(url_for("hq.vat"))
        if a.get("csv") == "1":
            return Response(H.vat_csv(report), mimetype="text/csv",
                            headers={"Content-Disposition": f"attachment; filename=vat-{report['start']}-to-{report['end']}.csv"})
    return render_template("hq_vat.html", report=report, start=start, end=end, rate=a.get("rate", ""), included=a.get("included", "1"))


# ------------------------------------------------------------------------------------------------------------------------ sales
@bp.route("/hq/sales", methods=["GET", "POST"])
@operator_required
@operator_can("hq.sales")
def sales():
    db = get_db()
    if request.method == "POST":
        try:
            lid = H.save_lead(db, g.operator["id"], None, request.form)
            return _done("hq.lead_view", lid=lid)
        except H.HQError as exc:
            return _failed(exc, "hq.sales")
    stage = request.args.get("stage", "open")
    mine = request.args.get("mine") == "1"
    return render_template("hq_sales.html", rows=H.leads(db, stage, g.operator["id"] if mine else None), stage=stage, mine=mine,
                           pipe=H.pipeline(db), figures=H.sales_figures(db), stages=H.STAGES, people=H.people_in(db, "sales"),
                           currencies=sorted(regions.CURRENCIES))


@bp.route("/hq/sales/<int:lid>", methods=["GET", "POST"])
@operator_required
@operator_can("hq.sales")
def lead_view(lid):
    db = get_db()
    try:
        row = H.lead(db, lid)
    except H.HQError:
        abort(404)
    if request.method == "POST":
        f = request.form
        try:
            what = f.get("what")
            if what == "save":
                H.save_lead(db, g.operator["id"], lid, f)
            elif what == "note":
                H.add_lead_note(db, g.operator["id"], lid, f.get("body"))
            elif what == "handover":
                pid = H.handover(db, g.operator["id"], lid)
                db.commit()
                flash("Handed to onboarding.", "ok")
                return redirect(url_for("hq.project_view", pid=pid) if g.op_can["hq.onboarding"] else url_for("hq.lead_view", lid=lid))
            else:
                abort(400)
            return _done("hq.lead_view", lid=lid)
        except H.HQError as exc:
            return _failed(exc, "hq.lead_view", lid=lid)
    handed = db.execute("SELECT id FROM hq_projects WHERE lead_id = ?", (lid,)).fetchone()
    return render_template("hq_lead.html", l=row, notes=H.lead_notes(db, lid), stages=H.STAGES, people=H.people_in(db, "sales"),
                           currencies=sorted(regions.CURRENCIES), clinics=_clinic_names(db), handed=handed)


# ------------------------------------------------------------------------------------------------------------------- onboarding
@bp.route("/hq/onboarding", methods=["GET", "POST"])
@operator_required
@operator_can("hq.onboarding")
def onboarding():
    db = get_db()
    if request.method == "POST":
        try:
            pid = H.save_project(db, g.operator["id"], None, request.form)
            return _done("hq.project_view", pid=pid)
        except H.HQError as exc:
            return _failed(exc, "hq.onboarding")
    which = request.args.get("which", "open")
    return render_template("hq_onboarding.html", rows=H.projects(db, which), which=which, states=H.PROJECT_STATES, clinics=_clinic_names(db),
                           people=H.people_in(db, "onboarding"))


@bp.route("/hq/onboarding/<int:pid>", methods=["GET", "POST"])
@operator_required
@operator_can("hq.onboarding")
def project_view(pid):
    db = get_db()
    try:
        row = H.project(db, pid)
    except H.HQError:
        abort(404)
    if request.method == "POST":
        f = request.form
        try:
            what = f.get("what")
            if what == "save":
                H.save_project(db, g.operator["id"], pid, f)
            elif what == "tick":
                H.tick(db, g.operator["id"], pid, f.get("step"), f.get("done") == "1", f.get("note"))
            elif what == "training":
                H.add_training(db, g.operator["id"], pid, f.get("on_date"), f.get("topic"), f.get("people"))
            elif what == "trained":
                if H.training_done(db, g.operator["id"], _int("training_id")) != pid:
                    abort(404)
            else:
                abort(400)
            return _done("hq.project_view", pid=pid)
        except H.HQError as exc:
            return _failed(exc, "hq.project_view", pid=pid)
    app_ticks = db.execute("SELECT COUNT(*) FROM golive_ticks WHERE clinic_id = ?", (row["clinic_id"],)).fetchone()[0] if row["clinic_id"] else None
    return render_template("hq_project.html", p=row, steps=H.STEPS, done=H.steps_done(db, pid), trainings=H.trainings(db, pid),
                           states=H.PROJECT_STATES, clinics=_clinic_names(db), people=H.people_in(db, "onboarding"), app_ticks=app_ticks)


# ------------------------------------------------------------------------------------------------------- 0.72: engineering
from flask import current_app, jsonify  # noqa: E402

check_bp = Blueprint("hqcheck", __name__)


@bp.route("/hq/engineering", methods=["GET", "POST"])
@operator_required
@operator_can("hq.engineering")
def engineering():
    db = get_db()
    shown = None
    if request.method == "POST":
        f = request.form
        try:
            if f.get("what") == "install":
                iid, key = T.add_install(db, g.operator["id"], f.get("label"), _int("clinic_id"))
                db.commit()
                shown = {"label": (f.get("label") or "").strip(), "key": key}
            elif f.get("what") in ("on", "off"):
                T.switch_install(db, g.operator["id"], _int("id"), f.get("what") == "on")
                return _done("hq.engineering")
            elif f.get("what") == "release":
                T.save_release(db, g.operator["id"], f.get("version"), f.get("released_on"), f.get("notes"), f.get("published") == "1")
                return _done("hq.engineering")
            else:
                abort(400)
        except H.HQError as exc:
            return _failed(exc, "hq.engineering")
    return render_template("hq_engineering.html", h=T.server_health(db), installs=T.installs(db), flags=T.INSTALL_FLAGS, shown=shown,
                           clinics=_clinic_names(db), releases=T.releases(db), version=T.VERSION, drill_days=T.offsite.DRILL_EVERY_DAYS)


@bp.route("/hq/requests", methods=["GET", "POST"])
@operator_required
@operator_can("hq.requests")
def requests_list():
    db = get_db()
    if request.method == "POST":
        try:
            rid = T.save_request(db, g.operator["id"], None, request.form.get("title"), request.form.get("detail"))
            if _int("clinic_id"):
                T.want(db, g.operator["id"], rid, _int("clinic_id"), _int("ticket_id"))
            return _done("hq.request_view", rid=rid)
        except H.HQError as exc:
            return _failed(exc, "hq.requests_list")
    which = request.args.get("which", "open")
    return render_template("hq_requests.html", rows=T.requests(db, which), which=which, states=T.REQUEST_STATES, clinics=_clinic_names(db))


@bp.route("/hq/requests/<int:rid>", methods=["GET", "POST"])
@operator_required
@operator_can("hq.requests")
def request_view(rid):
    db = get_db()
    row = db.execute("SELECT * FROM hq_requests WHERE id = ?", (rid,)).fetchone()
    if not row:
        abort(404)
    if request.method == "POST":
        f = request.form
        try:
            if f.get("what") == "want":
                added = T.want(db, g.operator["id"], rid, _int("clinic_id"), _int("ticket_id"))
                db.commit()
                flash("Saved." if added else "That clinic is already counted.", "ok")
                return redirect(url_for("hq.request_view", rid=rid))
            if f.get("what") == "save":
                if not g.op_can["hq.engineering"]:
                    abort(403)                                 # support and sales add clinics; engineering decides
                T.save_request(db, g.operator["id"], rid, f.get("title"), f.get("detail"), f.get("status"), f.get("version"))
                return _done("hq.request_view", rid=rid)
            abort(400)
        except H.HQError as exc:
            return _failed(exc, "hq.request_view", rid=rid)
    return render_template("hq_request.html", r=row, wanted=T.request_clinics(db, rid), states=T.REQUEST_STATES, clinics=_clinic_names(db),
                           can_decide=g.op_can["hq.engineering"])


@check_bp.post("/hq/checkin")
def checkin():
    """A clinic server reports its version, copies and free disk. Bearer install key; JSON body; never records."""
    db = get_db()
    auth = request.headers.get("Authorization", "")
    key = auth[7:].strip() if auth.startswith("Bearer ") else ""
    try:
        T.checkin(db, key, request.get_json(silent=True) or {})
        db.commit()
    except H.HQError:
        db.rollback()
        return jsonify({"ok": False}), 401
    return jsonify({"ok": True, "latest": current_app.config.get("HQ_LATEST_VERSION") or T.VERSION})


@clinic_bp.get("/help/whats-new")
def clinic_whats_new():
    return render_template("help_whats_new.html", rows=T.releases(get_db(), True), paras=H.paragraphs)


# -------------------------------------------------------------------------------------------------------- 0.72: compliance
EDIT = {
    "incident": ("hq_incidents", T.save_incident),
    "datareq": ("hq_data_requests", T.save_data_request),
    "outside": ("hq_subprocessors", T.save_subprocessor),
    "registration": ("hq_registrations", T.save_registration),
}


@bp.route("/hq/compliance", methods=["GET", "POST"])
@operator_required
@operator_can("hq.compliance")
def compliance():
    db = get_db()
    if request.method == "POST":
        kind = request.form.get("register")
        if kind not in EDIT:
            abort(400)
        try:
            EDIT[kind][1](db, g.operator["id"], None, request.form)
            return _done("hq.compliance")
        except H.HQError as exc:
            return _failed(exc, "hq.compliance")
    return render_template("hq_compliance.html", access=T.access_check(db), issues=T.ACCESS_ISSUES, incidents=T.incidents(db),
                           datareqs=T.data_requests(db), subs=T.subprocessors(db), regs=T.registrations(db), sev=T.SEVERITIES,
                           kinds=T.DATA_KINDS, dstates=T.DATA_STATES, clinics=_clinic_names(db), report_hours=T.REPORT_HOURS,
                           data_days=T.DATA_DAYS, review_days=T.REVIEW_DAYS, item=None)


@bp.route("/hq/compliance/<kind>/<int:item_id>", methods=["GET", "POST"])
@operator_required
@operator_can("hq.compliance")
def compliance_item(kind, item_id):
    if kind not in EDIT:
        abort(404)
    db = get_db()
    row = db.execute(f"SELECT * FROM {EDIT[kind][0]} WHERE id = ?", (item_id,)).fetchone()
    if not row:
        abort(404)
    if request.method == "POST":
        try:
            EDIT[kind][1](db, g.operator["id"], item_id, request.form)
            return _done("hq.compliance_item", kind=kind, item_id=item_id)
        except H.HQError as exc:
            return _failed(exc, "hq.compliance_item", kind=kind, item_id=item_id)
    return render_template("hq_compliance_item.html", kind=kind, item=row, sev=T.SEVERITIES, kinds=T.DATA_KINDS, dstates=T.DATA_STATES,
                           clinics=_clinic_names(db))
