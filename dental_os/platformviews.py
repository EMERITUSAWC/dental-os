"""Sign-up, the operator console, clinic billing and support access (versions 0.53 and 0.54)."""
import functools
import json

from flask import Blueprint, abort, current_app, flash, g, redirect, render_template, request, session, url_for

from .auth import manager_required
from .db import get_db
from .services import money as M, platform as PF, platformbill as PB, regions, throttle

signup_bp = Blueprint("signup", __name__)
operator_bp = Blueprint("operator", __name__)
billing_bp = Blueprint("billing", __name__)


def _ip():
    return throttle.key(current_app.config["SECRET_KEY"], request.remote_addr or "unknown")


# --------------------------------------------------------------------------------------------------------------- sign-up
@signup_bp.route("/signup", methods=["GET", "POST"])
def start():
    if not current_app.config["ALLOW_SIGNUP"]:
        abort(404)
    db = get_db()
    if request.method == "POST":
        try:
            cid, uid = PF.signup(db, request.form, _ip(), True)
            db.commit()
            session.clear()
            session["uid"] = uid
            flash("Your clinic is ready. Start with Settings: opening hours, dentists, chairs and prices.", "ok")
            return redirect(url_for("views.dashboard"))
        except PF.PlatformError as exc:
            db.rollback()
            flash(str(exc), "error")
    countries = sorted(((code, row[0]) for code, row in regions.COUNTRIES.items()), key=lambda x: x[1])
    return render_template("signup.html", countries=countries, form=request.form, trial_days=PF.TRIAL_DAYS)


# ------------------------------------------------------------------------------------------------------- operator console
def _operator():
    op_id = session.get("op_id")
    if not op_id:
        return None
    return get_db().execute("SELECT * FROM operators WHERE id = ? AND active = 1", (op_id,)).fetchone()


def operator_required(view):
    @functools.wraps(view)
    def wrapped(*a, **kw):
        g.operator = _operator()
        if not g.operator:
            return redirect(url_for("operator.login"))
        return view(*a, **kw)
    return wrapped


@operator_bp.route("/operator/login", methods=["GET", "POST"])
def login():
    db = get_db()
    if request.method == "POST":
        try:
            op = PF.operator_login(db, request.form.get("username"), request.form.get("password"), _ip())
            db.commit()
            session.clear()
            session["op_id"] = op["id"]
            return redirect(url_for("operator.home"))
        except PF.PlatformError as exc:
            db.commit()                                       # keep the record of the failed try
            flash(str(exc), "error")
    return render_template("op_login.html")


@operator_bp.post("/operator/logout")
def logout():
    session.clear()
    return redirect(url_for("operator.login"))


@operator_bp.get("/operator")
@operator_required
def home():
    db = get_db()
    return render_template("op_home.html", rows=PF.clinics_overview(db), states=PF.STATES, plans=PB.plans(db))


@operator_bp.route("/operator/clinics/<int:cid>", methods=["GET", "POST"])
@operator_required
def clinic(cid):
    db = get_db()
    c = db.execute("SELECT * FROM clinics WHERE id = ?", (cid,)).fetchone()
    if not c:
        abort(404)
    if request.method == "POST":
        f = request.form
        try:
            what = f.get("what")
            if what == "state":
                PF.set_state(db, g.operator["id"], cid, f.get("state"), f.get("reason"), f.get("trial_days", type=int))
            elif what == "plan":
                PB.put_on_plan(db, g.operator["id"], cid, f.get("plan_id", type=int), f.get("start") or None)
            elif what == "paid":
                PB.mark_paid(db, g.operator["id"], f.get("bill_id", type=int), f.get("method"), f.get("reference"))
            elif what == "support":
                grant = PF.live_grant(db, cid)
                if not grant:
                    raise PF.PlatformError("This clinic has not granted support access, or it has ended.")
                db.execute("INSERT INTO operator_log(operator_id, clinic_id, action) VALUES (?,?, 'support.open')", (g.operator["id"], cid))
                db.commit()
                session["support"] = {"clinic": cid, "grant": grant["id"]}
                return redirect(url_for("views.dashboard"))
            db.commit()
            flash("Saved.", "ok")
        except (PF.PlatformError, PB.BillError) as exc:
            db.rollback()
            flash(str(exc), "error")
        return redirect(url_for("operator.clinic", cid=cid))
    over = next(x for x in PF.clinics_overview(db) if x["c"]["id"] == cid)
    log = db.execute("SELECT l.*, o.display_name FROM operator_log l LEFT JOIN operators o ON o.id = l.operator_id WHERE l.clinic_id = ? ORDER BY l.id DESC LIMIT 30",
                     (cid,)).fetchall()
    return render_template("op_clinic.html", x=over, states=PF.STATES, plans=PB.plans(db, True), bills=PB.bills(db, cid), log=log,
                           grant=PF.live_grant(db, cid))


@operator_bp.route("/operator/plans", methods=["GET", "POST"])
@operator_required
def plans():
    db = get_db()
    if request.method == "POST":
        f = request.form
        try:
            prices = {}
            for cur in regions.CURRENCIES:
                raw = (f.get("price_" + cur) or "").strip()
                if raw:
                    prices[cur] = M.parse_money(raw, cur)
            maxd = f.get("max_dentists", "").strip()
            PB.save_plan(db, f.get("name"), prices, int(maxd) if maxd.isdigit() else None, f.get("plan_id", type=int), f.get("active", "1") == "1")
            db.commit()
            flash("Saved.", "ok")
        except (PB.BillError, ValueError) as exc:
            db.rollback()
            flash(str(exc), "error")
        return redirect(url_for("operator.plans"))
    return render_template("op_plans.html", rows=PB.plans(db), currencies=sorted(regions.CURRENCIES))


@operator_bp.post("/operator/support/end")
def support_end():
    session.pop("support", None)
    return redirect(url_for("operator.home") if session.get("op_id") else url_for("auth.login"))


# ------------------------------------------------------------------------------------------------- a clinic's own billing
@billing_bp.get("/billing")
@manager_required
def home():
    db = get_db()
    plan = db.execute("SELECT * FROM plans WHERE id = ?", (g.clinic["plan_id"],)).fetchone() if g.clinic["plan_id"] else None
    return render_template("billing.html", state=PF.state(g.clinic), states=PF.STATES, plan=plan, bills=PB.bills(db, g.clinic["id"]),
                           online=bool(PB.provider_for(current_app.config, g.clinic["currency"])), grace=PF.GRACE_DAYS)


@billing_bp.post("/billing/<int:bill_id>/pay")
@manager_required
def pay(bill_id):
    db = get_db()
    try:
        url = PB.start_payment(db, current_app.config, g.clinic, bill_id, request.form.get("email"),
                               url_for("billing.back", bill_id=bill_id, _external=True))
        db.commit()
        return redirect(url)
    except PB.BillError as exc:
        db.rollback()
        flash(str(exc), "error")
        return redirect(url_for("billing.home"))


@billing_bp.get("/billing/<int:bill_id>/back")
@manager_required
def back(bill_id):
    db = get_db()
    try:
        ok = PB.confirm(db, current_app.config, g.clinic, bill_id)
        db.commit()
        flash("Paid. Thank you." if ok else "The payment company has not confirmed the payment yet. If you paid, check again in a few minutes.",
              "ok" if ok else "error")
    except PB.BillError as exc:
        flash(str(exc), "error")
    return redirect(url_for("billing.home"))


@billing_bp.get("/billing/paused")
def closed():
    return render_template("billing_closed.html", state=PF.state(g.clinic), states=PF.STATES)


@billing_bp.route("/settings/support", methods=["GET", "POST"])
@manager_required
def support():
    db = get_db()
    if g.support:
        abort(403)
    if request.method == "POST":
        try:
            if request.form.get("what") == "end":
                PF.end_support(db, g.clinic, g.user["id"])
                flash("Support access ended.", "ok")
            else:
                PF.grant_support(db, g.clinic, g.user["id"], request.form.get("hours", type=int), request.form.get("why"))
                flash("Support can look at the clinic until the time shown.", "ok")
            db.commit()
        except PF.PlatformError as exc:
            db.rollback()
            flash(str(exc), "error")
        return redirect(url_for("billing.support"))
    views = db.execute("SELECT COUNT(*) FROM audit_log WHERE clinic_id = ? AND action = 'support.view'", (g.clinic["id"],)).fetchone()[0]
    return render_template("support_access.html", live=PF.live_grant(db, g.clinic["id"]), rows=PF.grants(db, g.clinic["id"]), hours=PF.SUPPORT_HOURS, views=views)
