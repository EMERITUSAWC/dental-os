"""Staff messages and the equipment register (version 0.29)."""
from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from .safeurl import local_path
from .auth import manager_required
from .db import get_db
from .services import team as T

bp = Blueprint("team", __name__)


def _users():
    return get_db().execute("SELECT id, display_name FROM users WHERE clinic_id = ? AND active = 1 AND id != ? ORDER BY display_name",
                            (g.clinic["id"], g.user["id"])).fetchall()


@bp.route("/team", methods=["GET", "POST"])
def inbox():
    db = get_db()
    if request.method == "POST":
        try:
            T.send(db, g.clinic, g.user, request.form.get("to_user", type=int), request.form.get("body"),
                   request.form.get("patient_id", type=int), request.form.get("reply_to", type=int))
            flash("Message sent.", "ok")
        except T.TeamError as exc:
            db.rollback()
            flash(str(exc), "error")
        back = request.form.get("back") or ""
        return redirect(back if local_path(back) else url_for("team.inbox"))
    box = request.args.get("box") if request.args.get("box") in ("in", "sent") else "in"
    rows = T.inbox(db, g.user) if box == "in" else T.sent(db, g.user)
    page = render_template("team_inbox.html", rows=rows, box=box, users=_users(), reply_to=request.args.get("reply", type=int))
    if box == "in":
        T.mark_read(db, g.user, [r["id"] for r in rows if not r["seen"]])
    return page


@bp.route("/equipment", methods=["GET", "POST"])
def equipment():
    db = get_db()
    if request.method == "POST":
        if g.user["role"] != "manager":
            abort(403)
        try:
            iid = T.save_item(db, g.clinic, g.user["id"], request.form)
            flash("Saved.", "ok")
            return redirect(url_for("team.equipment_item", item_id=iid))
        except T.TeamError as exc:
            db.rollback()
            flash(str(exc), "error")
    return render_template("equipment.html", rows=T.register(db, g.clinic, request.args.get("all") == "1"), kinds=T.EQUIPMENT_KINDS)


@bp.route("/equipment/<int:item_id>", methods=["GET", "POST"])
def equipment_item(item_id):
    db = get_db()
    item = T.get_item(db, g.clinic["id"], item_id)
    if not item:
        abort(404)
    if request.method == "POST":
        f = request.form
        try:
            if f.get("what") == "edit":
                if g.user["role"] != "manager":
                    abort(403)
                T.save_item(db, g.clinic, g.user["id"], f, item_id)
            else:
                T.log_event(db, g.clinic, g.user["id"], item_id, f.get("kind"), f.get("done_on"), f.get("done_by"), f.get("cost"), f.get("notes"))
            flash("Saved.", "ok")
        except T.TeamError as exc:
            db.rollback()
            flash(str(exc), "error")
        return redirect(url_for("team.equipment_item", item_id=item_id))
    return render_template("equipment_item.html", item=item, events=T.events(db, g.clinic["id"], item_id), kinds=T.EQUIPMENT_KINDS,
                           event_kinds=T.EVENT_KINDS, next=T.next_service(item), today=T._today(g.clinic).isoformat())


@bp.route("/staff/groups", methods=["GET", "POST"])
@manager_required
def groups():
    from .services import permgroups as PG
    db = get_db()
    if request.method == "POST":
        f = request.form
        try:
            if f.get("what") == "assign":
                PG.assign(db, g.clinic, g.user["id"], f.get("user_id", type=int), f.get("group_id", type=int))
            else:
                PG.save(db, g.clinic, g.user["id"], f.get("name"), f.getlist("area"), f.get("group_id", type=int))
            flash("Saved.", "ok")
        except PG.GroupError as exc:
            db.rollback()
            flash(str(exc), "error")
        return redirect(url_for("team.groups"))
    people = db.execute("SELECT id, display_name, role, perm_group_id FROM users WHERE clinic_id = ? AND active = 1 ORDER BY display_name",
                        (g.clinic["id"],)).fetchall()
    return render_template("perm_groups.html", groups=PG.groups(db, g.clinic["id"]), areas=PG.AREAS, people=people)
