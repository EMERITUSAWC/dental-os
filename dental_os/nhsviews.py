"""NHS England screens (version 0.24): the contract, courses of treatment and FP17s."""
import csv
import io

from flask import Blueprint, Response, abort, flash, g, redirect, render_template, request, url_for

from .auth import manager_required
from .db import get_db
from .services import dataio, nhs as N

bp = Blueprint("nhs", __name__)


def _fail(exc):
    get_db().rollback()
    flash(str(exc), "error")


def _need_on():
    if not N.is_on(get_db(), g.clinic["id"]) and g.user["role"] != "manager":
        abort(404)


@bp.get("/nhs")
def home():
    _need_on()
    db = get_db()
    status = request.args.get("status") if request.args.get("status") in N.STATUSES else "closed"
    rows = N.listing(db, g.clinic["id"], status)
    return render_template("nhs_home.html", pr=N.progress(db, g.clinic), rows=rows, status=status, statuses=N.STATUSES, courses=N.COURSES,
                           on=N.is_on(db, g.clinic["id"]), problems={r["id"]: N.fp17_problems(db, g.clinic, r) for r in rows if r["status"] in ("closed", "rejected")})


@bp.route("/nhs/setup", methods=["GET", "POST"])
@manager_required
def setup():
    db = get_db()
    cid = g.clinic["id"]
    if request.method == "POST":
        f = request.form
        try:
            what = f.get("what")
            if what == "year":
                N.save_year(db, g.clinic, g.user["id"], f.get("year"), f.get("udas"), f.get("uda_value"))
            elif what == "people":
                for k, v in f.items():
                    if k.startswith("perf_") and k[5:].isdigit():
                        N.save_performer(db, g.clinic, g.user["id"], int(k[5:]), v)
                    elif k.startswith("cls_") and k[4:].isdigit():
                        N.save_class(db, g.clinic, g.user["id"], int(k[4:]), v)
            else:
                charges = {k: f.get(k) for k in ("band1", "band2", "band3", "from", "source")} if f.get("band1") else None
                N.save_settings(db, g.clinic, g.user["id"], f.get("enabled") == "1", f.get("contract_no"), charges)
            flash("Saved.", "ok")
        except N.NhsError as exc:
            _fail(exc)
        return redirect(url_for("nhs.setup"))
    providers = db.execute("SELECT id, name, nhs_performer_no FROM providers WHERE clinic_id = ? AND active = 1 ORDER BY name", (cid,)).fetchall()
    codes = db.execute("SELECT id, code, name, nhs_class FROM procedure_codes WHERE clinic_id = ? AND active = 1 AND category != 'NHS charges' "
                       "ORDER BY category, code", (cid,)).fetchall()
    years = db.execute("SELECT * FROM nhs_years WHERE clinic_id = ? ORDER BY year DESC", (cid,)).fetchall()
    return render_template("nhs_setup.html", s=N.settings(db, cid), providers=providers, codes=codes, classes=N.NHS_CLASSES, years=years,
                           this_year=N.year_of(N._today(g.clinic)))


@bp.route("/patients/<int:pid>/nhs", methods=["GET", "POST"])
def patient(pid):
    _need_on()
    db = get_db()
    cid = g.clinic["id"]
    p = db.execute("SELECT * FROM patients WHERE id = ? AND clinic_id = ?", (pid, cid)).fetchone()
    if not p:
        abort(404)
    if request.method == "POST":
        f = request.form
        what = f.get("what")
        course_id = f.get("course_id", type=int)
        try:
            if course_id:
                c = db.execute("SELECT * FROM nhs_courses WHERE id = ? AND clinic_id = ? AND patient_id = ?", (course_id, cid, pid)).fetchone()
                if not c:
                    abort(404)
            if what == "open":
                N.open_course(db, g.clinic, g.user["id"], pid, f.get("provider_id", type=int), f.get("opened_on"), f.get("exemption"),
                              f.get("evidence"), f.get("hc3_amount"))
                flash("Course of treatment started.", "ok")
            elif what == "update":
                N.update_course(db, g.clinic, g.user["id"], course_id, f.get("exemption"), f.get("evidence"), f.get("hc3_amount"), f.get("notes"))
                flash("Saved.", "ok")
            elif what == "declare":
                N.record_declaration(db, g.clinic, g.user["id"], course_id, f.get("signed_on"))
                flash("Declaration recorded.", "ok")
            elif what == "close":
                N.close_course(db, g.clinic, g.user["id"], course_id, f.get("course_type"), f.get("closed_on"))
                flash("Course finished. The patient's charge is on their account, and the FP17 is due within 62 days.", "ok")
            elif what == "sent":
                N.mark_sent(db, g.clinic, g.user["id"], course_id, f.get("reference"), f.get("sent_on"))
                flash("FP17 marked as sent.", "ok")
            elif what == "outcome":
                N.mark_outcome(db, g.clinic, g.user["id"], course_id, f.get("outcome"), f.get("note"))
                flash("Saved.", "ok")
            elif what == "withdraw":
                N.withdraw(db, g.clinic, g.user["id"], course_id, f.get("reason"))
                flash("Course withdrawn.", "ok")
        except N.NhsError as exc:
            _fail(exc)
        return redirect(url_for("nhs.patient", pid=pid))
    courses = []
    for c in N.for_patient(db, cid, pid):
        its = N.items(db, cid, c)
        courses.append({"c": c, "work": its, "suggest": N.suggest(its), "problems": N.fp17_problems(db, g.clinic, c) if c["status"] != "open" else []})
    providers = db.execute("SELECT id, name, nhs_performer_no FROM providers WHERE clinic_id = ? AND active = 1 ORDER BY name", (cid,)).fetchall()
    return render_template("nhs_patient.html", p=p, courses=courses, providers=providers, kinds=N.COURSES, exemptions=N.EXEMPTIONS,
                           evidence=N.EVIDENCE, statuses=N.STATUSES, s=N.settings(db, cid), today=N._today(g.clinic).isoformat())


@bp.get("/nhs/courses/<int:course_id>/fp17")
def fp17(course_id):
    _need_on()
    db = get_db()
    c = N.get(db, g.clinic["id"], course_id)
    if not c:
        abort(404)
    return render_template("nhs_fp17.html", c=c, items=N.items(db, g.clinic["id"], c), s=N.settings(db, g.clinic["id"]), kinds=N.COURSES,
                           exemptions=N.EXEMPTIONS, evidence=N.EVIDENCE, problems=N.fp17_problems(db, g.clinic, c))


@bp.get("/nhs/fp17.csv")
@manager_required
def fp17_csv():
    status = request.args.get("status") if request.args.get("status") in N.STATUSES else "closed"
    buf = io.StringIO()
    csv.writer(buf).writerows([[dataio._safe_cell(x) for x in row] for row in N.fp17_rows(get_db(), g.clinic, status)])
    return Response(buf.getvalue(), mimetype="text/csv", headers={"Content-Disposition": 'attachment; filename="fp17.csv"'})
