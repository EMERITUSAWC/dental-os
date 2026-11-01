"""Duplicate patient records (version 0.40). Managers only."""
import json

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from .auth import manager_required
from .db import get_db
from .services import merge as MG

bp = Blueprint("merge", __name__)

LABELS = MG.LABELS


@bp.get("/patients/duplicates")
@manager_required
def duplicates():
    db = get_db()
    return render_template("merge_list.html", rows=MG.candidates(db, g.clinic["id"]), merges=MG.merges(db, g.clinic["id"])[:30])


@bp.post("/patients/duplicates/not")
@manager_required
def not_duplicate():
    db = get_db()
    try:
        MG.not_duplicate(db, g.clinic, g.user["id"], request.form.get("a", type=int), request.form.get("b", type=int))
        db.commit()
        flash("Marked as different people. They will not be listed again.", "ok")
    except MG.MergeError as exc:
        db.rollback()
        flash(str(exc), "error")
    return redirect(url_for("merge.duplicates"))


@bp.route("/patients/merge", methods=["GET", "POST"])
@manager_required
def compare():
    db = get_db()
    cid = g.clinic["id"]
    keep_id, dup_id = request.values.get("keep", type=int), request.values.get("dup", type=int)
    if request.method == "POST":
        try:
            mid = MG.merge(db, g.clinic, g.user["id"], keep_id, dup_id, request.form.get("confirm") == "1")
            db.commit()
            flash("Records merged.", "ok")
            return redirect(url_for("merge.done", mid=mid))
        except MG.MergeError as exc:
            db.rollback()
            flash(str(exc), "error")
    keep = db.execute("SELECT * FROM patients WHERE id = ? AND clinic_id = ?", (keep_id, cid)).fetchone()
    dup = db.execute("SELECT * FROM patients WHERE id = ? AND clinic_id = ?", (dup_id, cid)).fetchone()
    if not keep or not dup:
        abort(404)
    return render_template("merge_compare.html", keep=keep, dup=dup, kc=MG.counts(db, cid, keep_id), dc=MG.counts(db, cid, dup_id), labels=LABELS)


@bp.get("/patients/merges/<int:mid>")
@manager_required
def done(mid):
    db = get_db()
    m = MG.get_merge(db, g.clinic["id"], mid)
    if not m:
        abort(404)
    keep = db.execute("SELECT * FROM patients WHERE id = ?", (m["keep_id"],)).fetchone()
    return render_template("merge_done.html", m=m, keep=keep, moved=json.loads(m["moved"]), left=json.loads(m["left_behind"]),
                           filled=json.loads(m["filled"]), labels=LABELS, left_labels=MG.LEFT)
