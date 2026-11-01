"""Moving in from another system (versions 0.48 and 0.49). Managers only."""
import json

from flask import Blueprint, abort, current_app, flash, g, redirect, render_template, request, url_for

from .auth import manager_required
from .db import get_db
from .services import migrate as MI, teeth

bp = Blueprint("migrate", __name__)


@bp.route("/tools/move-in", methods=["GET", "POST"])
@manager_required
def home():
    db = get_db()
    if request.method == "POST":
        up = request.files.get("file")
        if not up or not up.filename:
            flash("Choose a file first.", "error")
            return redirect(url_for("migrate.home"))
        try:
            rid = MI.stage(db, g.clinic, g.user["id"], current_app.instance_path, up.filename, up.read(MI.MAX_BYTES + 1))
            db.commit()
            return redirect(url_for("migrate.preview", rid=rid))
        except MI.MigrateError as exc:
            db.rollback()
            flash(str(exc), "error")
    return render_template("migrate_home.html", runs=MI.runs(db, g.clinic["id"]), kinds=MI.KINDS)


def _overrides(form, files):
    out = {}
    for f in files:
        i = str(f["index"])
        o = {}
        kind = form.get(f"kind_{i}")
        if kind is not None:
            o["kind"] = kind if kind in MI.KINDS else None
        m = {}
        for c in range(len(f["header"])):
            v = form.get(f"map_{i}_{c}")
            if v is not None:
                m[str(c)] = v
        if m:
            o["map"] = m
        out[i] = o
    return out


@bp.route("/tools/move-in/<int:rid>", methods=["GET", "POST"])
@manager_required
def preview(rid):
    db = get_db()
    cid = g.clinic["id"]
    r = MI.get_run(db, cid, rid)
    if not r:
        abort(404)
    if r["status"] != "preview":
        return render_template("migrate_done.html", r=r, s=json.loads(r["summary"] or "{}"), kinds=MI.KINDS)
    try:
        files = MI.load(current_app.instance_path, cid, r)
    except MI.MigrateError as exc:
        flash(str(exc), "error")
        return redirect(url_for("migrate.home"))
    overrides = json.loads(r["overrides"] or "{}")
    notation = r["notation"]
    if request.method == "POST":
        what = request.form.get("what")
        if what == "cancel":
            MI.finish(db, current_app.instance_path, cid, rid, "cancelled")
            db.commit()
            flash("Cancelled. The uploaded file has been deleted.", "ok")
            return redirect(url_for("migrate.home"))
        overrides = _overrides(request.form, MI.describe(files, overrides))
        notation = request.form.get("notation") if request.form.get("notation") in teeth.NOTATIONS else notation
        db.execute("UPDATE migration_runs SET overrides = ?, notation = ? WHERE id = ?", (json.dumps(overrides), notation, rid))
        db.commit()
        if what == "import":
            try:
                result = MI.run(db, g.clinic, g.user["id"], files, overrides, notation, keep=True)
                MI.finish(db, current_app.instance_path, cid, rid, "done", result)
                db.commit()
                flash("Imported.", "ok")
            except MI.MigrateError as exc:
                db.rollback()
                flash(str(exc), "error")
            return redirect(url_for("migrate.preview", rid=rid))
        return redirect(url_for("migrate.preview", rid=rid))
    try:
        result = MI.run(db, g.clinic, g.user["id"], files, overrides, notation, keep=False)
    except MI.MigrateError as exc:
        flash(str(exc), "error")
        result = None
    return render_template("migrate_preview.html", r=r, result=result, files=MI.describe(files, overrides), kinds=MI.KINDS, fields=MI.FIELDS,
                           needed=MI.NEEDED, notation=notation, notations=teeth.NOTATIONS)
