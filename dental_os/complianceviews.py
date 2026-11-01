"""Version 0.67: the security and compliance page, the access review and the evidence pack. Managers only."""
from flask import Blueprint, Response, abort, current_app, flash, g, redirect, render_template, request, url_for

from .auth import manager_required
from .db import get_db
from .services import compliance as C

bp = Blueprint("compliance", __name__)


def _page(check=None):
    db = get_db()
    cfg = current_app.config
    rows = C.controls(db, g.clinic, cfg)
    return render_template("compliance.html", rows=rows, titles=C.CONTROLS, refs=C.REFS, states=C.STATES, seal=C.latest_seal(db),
                           stale=C.seal_stale(db), review=C.review_status(db, g.clinic["id"]),
                           services=C.outside_services(db, g.clinic, cfg), check=check,
                           counts={s: sum(1 for r in rows if r[1] == s) for s in C.STATES})


@bp.get("/settings/compliance")
@manager_required
def home():
    return _page()


@bp.post("/settings/compliance/check")
@manager_required
def check():
    db = get_db()
    C.seal(db, current_app.config["SECRET_KEY"], g.user["id"])
    result = C.verify(db, current_app.config["SECRET_KEY"])
    from .services import audit
    audit.log(db, g.clinic["id"], g.user["id"], "compliance.check", detail="intact" if result["ok"] else "broken")
    db.commit()
    return _page(result)


@bp.post("/settings/compliance/pack")
@manager_required
def pack():
    db = get_db()
    data = C.evidence_pack(db, g.clinic, current_app.config, g.user, current_app.config["SECRET_KEY"])
    stamp = C._utc().strftime("%Y%m%d-%H%M")
    return Response(data, mimetype="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="dental-os-evidence-{g.clinic["id"]}-{stamp}.zip"'})


@bp.route("/settings/access-review", methods=["GET", "POST"])
@manager_required
def review():
    db = get_db()
    rows = C.access_list(db, g.clinic["id"])
    if request.method == "POST":
        decisions, notes = {}, {}
        for r in rows:
            uid = r["u"]["id"]
            decisions[uid] = request.form.get(f"d{uid}")
            notes[uid] = request.form.get(f"n{uid}")
        try:
            C.record_review(db, g.clinic, g.user["id"], decisions, notes, request.form.get("note"))
            flash("Access review saved.", "ok")
            return redirect(url_for("compliance.review"))
        except C.ComplianceError as exc:
            flash(str(exc), "error")
    return render_template("access_review.html", rows=[r for r in rows if r["u"]["active"]], off=[r for r in rows if not r["u"]["active"]],
                           flags=C.FLAGS, decisions=C.DECISIONS, status=C.review_status(db, g.clinic["id"]),
                           history=C.reviews(db, g.clinic["id"]), every=C.REVIEW_EVERY_DAYS, form=request.form)


@bp.get("/settings/access-review/<int:review_id>")
@manager_required
def review_view(review_id):
    db = get_db()
    head = db.execute("""SELECT r.*, u.display_name FROM access_reviews r LEFT JOIN users u ON u.id = r.reviewed_by
                         WHERE r.id = ? AND r.clinic_id = ?""", (review_id, g.clinic["id"])).fetchone()
    if not head:
        abort(404)
    return render_template("access_review_view.html", head=head, items=C.review_items(db, g.clinic["id"], review_id), decisions=C.DECISIONS)
