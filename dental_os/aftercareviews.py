"""Aftercare sheet screens (version 0.44)."""
import json

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from .auth import clinical_required
from .db import get_db
from .services import aftercare as AC, i18n

bp = Blueprint("aftercare", __name__)


def _codes(db):
    return db.execute("SELECT id, code, name, category FROM procedure_codes WHERE clinic_id = ? AND active = 1 ORDER BY category, name",
                      (g.clinic["id"],)).fetchall()


@bp.get("/aftercare")
@clinical_required
def home():
    db = get_db()
    rows = AC.sheets(db, g.clinic["id"])
    db.commit()
    return render_template("aftercare_list.html", rows=rows)


@bp.route("/aftercare/new", methods=["GET", "POST"])
@bp.route("/aftercare/<int:sid>", methods=["GET", "POST"])
@clinical_required
def edit(sid=None):
    db = get_db()
    s = AC.get(db, g.clinic["id"], sid) if sid else None
    if sid and not s:
        abort(404)
    if request.method == "POST":
        try:
            if request.form.get("what") == "approve":
                AC.approve(db, g.clinic, g.user, sid)
                flash("Approved. It can now be given to patients.", "ok")
            else:
                if g.user["role"] not in ("manager", "dentist"):
                    abort(403)
                sid = AC.save(db, g.clinic, g.user["id"], request.form, sid)
                flash("Sheet saved. A dentist needs to approve it before it is given out." if not AC.get(db, g.clinic["id"], sid)["approved_at"] else "Sheet saved.", "ok")
            db.commit()
            return redirect(url_for("aftercare.edit", sid=sid))
        except AC.AftercareError as exc:
            db.rollback()
            flash(str(exc), "error")
    picked = set(json.loads(s["codes"])) if s else set()
    approver = db.execute("SELECT display_name FROM users WHERE id = ?", (s["approved_by"],)).fetchone() if s and s["approved_by"] else None
    return render_template("aftercare_edit.html", s=s, codes=_codes(db), picked=picked, approver=approver)


@bp.route("/patients/<int:pid>/aftercare", methods=["GET", "POST"])
@clinical_required
def patient(pid):
    db = get_db()
    cid = g.clinic["id"]
    p = db.execute("SELECT * FROM patients WHERE id = ? AND clinic_id = ?", (pid, cid)).fetchone()
    if not p:
        abort(404)
    if request.method == "POST":
        try:
            picked = AC.give(db, g.clinic, g.user["id"], pid, [int(x) for x in request.form.getlist("sheet") if x.isdigit()])
            db.commit()
            lang = i18n.patient_lang(p, g.clinic)
            g.doc_lang = lang
            return render_template("aftercare_print.html", p=p, sheets=[AC.text_in(s, lang) for s in picked])
        except AC.AftercareError as exc:
            db.rollback()
            flash(str(exc), "error")
    all_ok = [s for s in AC.sheets(db, cid, True) if s["approved_at"]]
    suggested = {s["id"] for s in AC.suggested(db, cid, pid)}
    db.commit()
    return render_template("aftercare_patient.html", p=p, sheets=all_ok, suggested=suggested, given=AC.given(db, cid, pid))
