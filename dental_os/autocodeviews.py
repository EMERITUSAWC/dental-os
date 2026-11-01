"""Auto code setup (version 0.46). Managers only."""
from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from .auth import manager_required
from .db import get_db
from .services import autocodes as AUTO

bp = Blueprint("autocodes", __name__)


@bp.route("/autocodes", methods=["GET", "POST"])
@manager_required
def home():
    db = get_db()
    if request.method == "POST":
        try:
            gid = AUTO.save_group(db, g.clinic, g.user["id"], request.form.get("name"))
            db.commit()
            return redirect(url_for("autocodes.group", gid=gid))
        except AUTO.AutoError as exc:
            db.rollback()
            flash(str(exc), "error")
    return render_template("autocodes_list.html", rows=AUTO.groups(db, g.clinic["id"]))


@bp.route("/autocodes/<int:gid>", methods=["GET", "POST"])
@manager_required
def group(gid):
    db = get_db()
    cid = g.clinic["id"]
    grp = AUTO.get(db, cid, gid)
    if not grp:
        abort(404)
    if request.method == "POST":
        f = request.form
        try:
            if f.get("what") == "remove":
                AUTO.remove_rule(db, g.clinic, g.user["id"], f.get("rule_id", type=int))
            elif f.get("what") == "group":
                AUTO.save_group(db, g.clinic, g.user["id"], f.get("name"), gid, f.get("active", "1") == "1")
            else:
                raw = f.get("surfaces") or "0"
                AUTO.add_rule(db, g.clinic, g.user["id"], gid, int(raw) if raw.isdigit() else -1, f.get("region"), f.get("code_id", type=int))
            db.commit()
            flash("Saved.", "ok")
        except AUTO.AutoError as exc:
            db.rollback()
            flash(str(exc), "error")
        return redirect(url_for("autocodes.group", gid=gid))
    codes = db.execute("SELECT id, code, name FROM procedure_codes WHERE clinic_id = ? AND active = 1 ORDER BY category, name", (cid,)).fetchall()
    return render_template("autocodes_group.html", grp=grp, rules=AUTO.rules(db, cid, gid), codes=codes, regions=AUTO.REGIONS)
