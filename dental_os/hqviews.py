"""Version 0.71: Dental OS HQ, the company's own workspace, one part per department. Signed in with the operator login."""
import datetime

from flask import Blueprint, Response, abort, flash, g, redirect, render_template, request, url_for

from .db import get_db
from .platformviews import operator_can, operator_required
from .services import hq as H, opteam as OT, platform as PF, regions

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
                           mine=g.op_depts, owner=OT.is_owner(g.operator), states=H.TICKET_STATES, stages=H.STAGES)


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
            elif what == "change":
                H.set_ticket(db, g.operator["id"], tid, status=f.get("status") or None,
                             assigned_to=_int("assigned_to") if f.get("assigned_to") else (0 if "assigned_to" in f else None),
                             priority=f.get("priority") or None)
            else:
                abort(400)
            return _done("hq.ticket_view", tid=tid)
        except H.HQError as exc:
            return _failed(exc, "hq.ticket_view", tid=tid)
    return render_template("hq_ticket.html", t=t, notes=H.ticket_notes(db, tid), clocks=H.clocks(t), people=H.people_in(db, "support"),
                           priorities=H.PRIORITIES, channels=H.CHANNELS, states=H.TICKET_STATES)


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
