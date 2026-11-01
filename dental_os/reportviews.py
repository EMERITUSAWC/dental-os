"""The report builder (version 0.28)."""
import csv
import io
import json

from flask import Blueprint, Response, abort, flash, g, redirect, render_template, request, url_for

from .auth import manager_required
from .db import get_db
from .services import dataio, reportbuilder as RB

bp = Blueprint("reports", __name__)


def _spec_from(args):
    filters = [{"col": args.get(f"f{i}_col"), "op": args.get(f"f{i}_op"), "value": args.get(f"f{i}_val")} for i in range(4)]
    return {"dataset": args.get("dataset") or "patients", "columns": args.getlist("col"), "group_by": args.get("group_by"),
            "agg": args.get("agg") or "count", "measure": args.get("measure"), "filters": filters, "sort": args.get("sort"),
            "desc": args.get("desc") == "1"}


def _spec_args(spec):
    out = {"dataset": spec["dataset"], "col": spec["columns"], "group_by": spec["group_by"], "agg": spec["agg"], "measure": spec["measure"],
           "sort": spec["sort"], "desc": "1" if spec["desc"] else ""}
    for i, f in enumerate(spec["filters"]):
        out.update({f"f{i}_col": f["col"], f"f{i}_op": f["op"], f"f{i}_val": f["value"]})
    return out


@bp.get("/reports/builder")
@manager_required
def builder():
    db = get_db()
    spec = _spec_from(request.args)
    result, error = None, None
    if not RB.allowed(g.user, spec["dataset"]):
        spec["dataset"] = "patients"
    if request.args.get("run"):
        try:
            result = RB.run(db, g.user, spec)
        except RB.ReportError as exc:
            error = str(exc)
    report = RB.get(db, g.clinic["id"], request.args.get("report_id", type=int)) if request.args.get("report_id") else None
    datasets = {k: v[0] for k, v in RB.DATASETS.items() if RB.allowed(g.user, k)}
    return render_template("report_builder.html", spec=spec, result=result, error=error, datasets=datasets,
                           cols=RB.DATASETS[spec["dataset"]][3], ops=RB.OPS, aggs=RB.AGGS, saved=RB.visible(db, g.user), report=report,
                           qs=request.query_string.decode())


@bp.post("/reports/builder/save")
@manager_required
def save():
    db = get_db()
    spec = _spec_from(request.form)
    try:
        rid = RB.save(db, g.clinic, g.user, request.form.get("name"), spec, request.form.get("shared") == "1",
                      request.form.get("report_id", type=int))
        flash("Report saved.", "ok")
        return redirect(url_for("reports.saved", report_id=rid))
    except RB.ReportError as exc:
        db.rollback()
        flash(str(exc), "error")
        return redirect(url_for("reports.builder", dataset=spec["dataset"]))


@bp.get("/reports/saved")
def saved_list():
    return render_template("report_saved_list.html", rows=RB.visible(get_db(), g.user))


@bp.get("/reports/saved/<int:report_id>")
def saved(report_id):
    db = get_db()
    r = RB.get(db, g.clinic["id"], report_id)
    if not r or (not r["shared"] and r["created_by"] != g.user["id"]):
        abort(404)
    spec = json.loads(r["spec"])
    if not RB.allowed(g.user, spec["dataset"]):
        abort(403)
    try:
        result = RB.run(db, g.user, spec)
    except RB.ReportError as exc:
        flash(str(exc), "error")
        result = None
    if request.args.get("format") == "csv" and result:
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow([h[0] for h in result["head"]])
        for row in result["rows"]:
            w.writerow([dataio._safe_cell(f"{v / 100:.2f}" if (h[1] == "money" and v is not None) else ("" if v is None else v))
                        for v, h in zip(row, result["head"])])
        name = "".join(ch if ch.isalnum() else "-" for ch in r["name"]).strip("-").lower() or "report"
        return Response(buf.getvalue(), mimetype="text/csv", headers={"Content-Disposition": f'attachment; filename="{name}.csv"'})
    return render_template("report_saved.html", r=r, result=result, edit_args=_spec_args(RB.clean_spec(g.user, spec)))


@bp.post("/reports/saved/<int:report_id>/archive")
@manager_required
def saved_archive(report_id):
    try:
        RB.archive(get_db(), g.clinic, g.user, report_id)
        flash("Report removed from the list.", "ok")
    except RB.ReportError as exc:
        flash(str(exc), "error")
    return redirect(url_for("reports.saved_list"))
