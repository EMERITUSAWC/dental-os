"""Version 0.73: the morning huddle, goals and notes waiting to be signed."""
from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from .auth import clinical_required, manager_required, signer_required
from .db import get_db
from .services import huddle as HU, money as M

bp = Blueprint("huddle", __name__)


@bp.get("/huddle")
def view():
    """Everyone can open the huddle; the money parts are for managers."""
    db = get_db()
    data = HU.huddle(db, g.clinic, g.user)
    from .services import aidesk
    manager = g.user["role"] == "manager"
    ai = aidesk.summary(db, g.clinic, g.user) if manager else {"risky": aidesk.risky_visits(db, g.clinic, data["today"]), "lines": [], "patterns": []}
    db.commit()
    return render_template("huddle.html", h=data, money=manager, fields=FIELD_LABELS, ai=ai, sw=aidesk.SUMMARY_WORDS, pw=aidesk.PATTERN_WORDS,
                           rw=aidesk.RISK_WORDS)


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


# ------------------------------------------------------------------------------------- 0.74: notifications, tours, X-rays
from flask import jsonify  # noqa: E402

from .services import files as F, notify, tours  # noqa: E402


@bp.get("/notifications")
def notifications():
    rows = notify.items(get_db(), g.clinic, g.user)
    get_db().commit()
    return render_template("notifications.html", rows=rows, total=notify.total(rows))


@bp.post("/tours/<key>/done")
def tour_done(key):
    db = get_db()
    try:
        tours.done(db, g.user, key)
    except LookupError:
        abort(404)
    db.commit()
    return jsonify({"ok": True})


@bp.post("/tours/again")
def tours_again():
    db = get_db()
    tours.reset(db, g.user)
    db.commit()
    flash("The tours will show again on each main screen.", "ok")
    return redirect(url_for("views.dashboard"))


@bp.get("/patients/<int:pid>/compare")
@clinical_required
def compare(pid):
    """X-rays and photos side by side, oldest first: chosen ones, or every picture of one tooth."""
    from .services import teeth as TT
    db, cid = get_db(), g.clinic["id"]
    from .views import _patient_or_404
    p = _patient_or_404(pid)
    tooth_raw = (request.args.get("tooth") or "").strip()
    tooth = None
    if tooth_raw:
        try:
            tooth = TT.parse(tooth_raw)
        except TT.ToothError:
            flash("Write the teeth the clinic's usual way, separated by commas.", "error")
    rows = F.for_tooth(db, cid, pid, tooth) if tooth else F.pick(db, cid, pid, request.args.getlist("ids"))
    from .services import audit
    if rows:
        audit.log(db, cid, g.user["id"], "file.compare", "patient", pid)
        db.commit()
    return render_template("compare.html", p=p, rows=rows, gaps=F.gaps(rows), all=F.pictures(db, cid, pid), tooth=tooth, tooth_raw=tooth_raw,
                           chosen={r["id"] for r in rows}, kinds=F.KINDS, most=F.COMPARE_MAX)


@bp.post("/files/<int:file_id>/details")
@clinical_required
def file_details(file_id):
    db = get_db()
    try:
        pid = F.set_details(db, g.clinic, g.user, file_id, request.form.get("teeth"), request.form.get("taken_on"))
        db.commit()
        flash("Saved.", "ok")
    except F.FileError as exc:
        db.rollback()
        flash(str(exc), "error")
        row = F.get(db, g.clinic["id"], file_id)
        if not row:
            abort(404)
        pid = row["patient_id"]
    return redirect(url_for("record.files_view", pid=pid))
