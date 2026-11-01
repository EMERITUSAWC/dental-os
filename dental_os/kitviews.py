"""Treatment kit screens (version 0.45)."""
import datetime

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from .auth import manager_required
from .db import get_db
from .services import kits as K

bp = Blueprint("kits", __name__)


@bp.get("/stock/kits")
def home():
    if g.user["role"] == "front_desk":
        abort(403)
    db = get_db()
    today = datetime.date.today()
    start = request.args.get("start") or today.replace(day=1).isoformat()
    end = request.args.get("end") or today.isoformat()
    return render_template("kits_list.html", rows=K.kits(db, g.clinic["id"]), short=K.shortfalls(db, g.clinic["id"]),
                           costs=K.material_costs(db, g.clinic["id"], start, end), start=start, end=end)


@bp.route("/stock/kits/<int:code_id>", methods=["GET", "POST"])
@manager_required
def edit(code_id):
    db = get_db()
    cid = g.clinic["id"]
    code = db.execute("SELECT * FROM procedure_codes WHERE id = ? AND clinic_id = ?", (code_id, cid)).fetchone()
    if not code:
        abort(404)
    if request.method == "POST":
        try:
            wanted = {}
            for key, raw in request.form.items():
                if key.startswith("qty_"):
                    raw = raw.strip() or "0"
                    if not raw.isdigit():
                        raise K.KitError("Use whole numbers from 0 to 1000.")
                    wanted[int(key[4:])] = int(raw)
            K.save(db, g.clinic, g.user["id"], code_id, wanted)
            db.commit()
            flash("Kit saved.", "ok")
            return redirect(url_for("kits.home"))
        except K.KitError as exc:
            db.rollback()
            flash(str(exc), "error")
    have = {r["item_id"]: r["qty"] for r in K.lines(db, cid, code_id)}
    items = db.execute("SELECT * FROM stock_items WHERE clinic_id = ? AND active = 1 ORDER BY category, name", (cid,)).fetchall()
    return render_template("kits_edit.html", code=code, items=items, have=have)
