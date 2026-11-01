"""Version 0.60: the X-ray AI settings, sending an X-ray for a second look, and the dentist agreeing or rejecting each finding."""
from flask import Blueprint, abort, current_app, flash, g, redirect, render_template, request, url_for

from .auth import clinical_required, manager_required
from .db import get_db
from .services import xrayai as XA

bp = Blueprint("xrayai", __name__)


def _root():
    from .record import _files_root
    return _files_root()


@bp.route("/settings/xray-ai", methods=["GET", "POST"])
@manager_required
def setup():
    db = get_db()
    if request.method == "POST":
        try:
            XA.save_account(db, g.clinic, g.user["id"], current_app.config["SECRET_KEY"], request.form)
            flash("Saved.", "ok")
            return redirect(url_for("xrayai.setup"))
        except XA.XrayAIError as exc:
            flash(str(exc), "error")
    a = XA.account(db, g.clinic["id"])
    return render_template("xrayai_setup.html", a=a, on=XA.is_on(db, g.clinic["id"], current_app.config["SECRET_KEY"]),
                           has_key=bool(a and a["api_key"]))


@bp.post("/files/<int:file_id>/ai-read")
@clinical_required
def send(file_id):
    db = get_db()
    try:
        rid = XA.read(db, g.clinic, g.user, current_app.config["SECRET_KEY"], _root(), file_id,
                      transport=current_app.config.get("XRAY_AI_TRANSPORT"))
        r = db.execute("SELECT status, detail FROM xray_ai_reads WHERE id = ?", (rid,)).fetchone()
        if r["status"] == "done":
            flash("The X-ray AI's suggestions are below. A dentist agrees or rejects each one.", "ok")
        else:
            flash("The X-ray could not be read this time. The reason is below.", "error")
    except XA.XrayAIError as exc:
        flash(str(exc), "error")
    except LookupError:
        abort(404)
    return redirect(url_for("care.viewer", file_id=file_id) + "#ai")


@bp.post("/ai-findings/<int:finding_id>")
@clinical_required
def decide(finding_id):
    db = get_db()
    try:
        rid = XA.decide(db, g.clinic, g.user, finding_id, request.form.get("status"), request.form.get("note"))
    except PermissionError:
        abort(403)
    except LookupError:
        abort(404)
    except XA.XrayAIError as exc:
        flash(str(exc), "error")
        return redirect(url_for("views.dashboard"))
    fid = db.execute("SELECT file_id FROM xray_ai_reads WHERE id = ? AND clinic_id = ?", (rid, g.clinic["id"])).fetchone()[0]
    flash("Saved.", "ok")
    return redirect(url_for("care.viewer", file_id=fid) + "#ai")
