"""Version 0.73: the morning huddle, goals and notes waiting to be signed."""
from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from .auth import manager_required, signer_required
from .db import get_db
from .services import huddle as HU, money as M

bp = Blueprint("huddle", __name__)


@bp.get("/huddle")
def view():
    """Everyone can open the huddle; the money parts are for managers."""
    db = get_db()
    data = HU.huddle(db, g.clinic, g.user)
    db.commit()
    return render_template("huddle.html", h=data, money=g.user["role"] == "manager", fields=FIELD_LABELS)


FIELD_LABELS = HU.FIELD_LABELS


def _goal_values(prefix):
    out = {}
    for f in HU.FIELDS:
        raw = (request.form.get(prefix + f) or "").strip()
        if not raw:
            out[f] = None
        elif f.endswith("_p"):
            out[f] = M.parse_money(raw, g.clinic["currency"], allow_zero=True)
        else:
            if not raw.isdigit() or len(raw) > 6:
                raise HU.GoalError("Goals are whole numbers of zero or more; acceptance is 0 to 100.")
            out[f] = int(raw)
    return out


@bp.route("/reports/goals", methods=["GET", "POST"])
@manager_required
def goals():
    db = get_db()
    today = HU._today(g.clinic)
    month = request.values.get("month") or today.isoformat()[:7]
    try:
        HU.check_month(month)
    except HU.GoalError:
        abort(404)
    if request.method == "POST":
        try:
            if request.form.get("what") == "copy":
                y, m = (int(x) for x in month.split("-"))
                prev = f"{y - 1}-12" if m == 1 else f"{y}-{m - 1:02d}"
                n = HU.copy_goals(db, g.clinic, g.user["id"], prev, month)
                db.commit()
                if n:
                    flash("Copied last month's goals.", "ok")
                else:
                    flash("Nothing to copy: last month has no goals, or this month already has them.", "ok")
                return redirect(url_for("huddle.goals", month=month))
            HU.save_goal(db, g.clinic, g.user["id"], month, None, _goal_values("clinic_"))
            for p in db.execute("SELECT id FROM providers WHERE clinic_id = ? AND active = 1", (g.clinic["id"],)).fetchall():
                raw = (request.form.get(f"dentist_{p['id']}") or "").strip()
                HU.save_goal(db, g.clinic, g.user["id"], month, p["id"],
                             {"production_p": M.parse_money(raw, g.clinic["currency"], allow_zero=True) if raw else None})
            db.commit()
            flash("Saved.", "ok")
        except (HU.GoalError, ValueError) as exc:
            db.rollback()
            flash(str(exc), "error")
        return redirect(url_for("huddle.goals", month=month))
    y, m = (int(x) for x in month.split("-"))
    nav = {"prev": f"{y - 1}-12" if m == 1 else f"{y}-{m - 1:02d}", "next": f"{y + 1}-01" if m == 12 else f"{y}-{m + 1:02d}"}
    providers = db.execute("SELECT id, name FROM providers WHERE clinic_id = ? AND active = 1 ORDER BY name", (g.clinic["id"],)).fetchall()
    start, end = HU._month_bounds(month)
    from .services import i18n
    label = i18n.fmt_date(start, g.get("lang") or "en").split(" ", 1)[-1]
    return render_template("goals.html", month=month, month_label=label, nav=nav, g_=HU.goals(db, g.clinic["id"], month), providers=providers,
                           fields=FIELD_LABELS, days=HU.working_days(db, g.clinic, start, end))


@bp.get("/notes/unsigned")
@signer_required
def unsigned():
    return render_template("unsigned.html", rows=HU.unsigned(get_db(), g.clinic, g.user), days=HU.UNSIGNED_DAYS,
                           everyone=g.user["role"] == "manager")
