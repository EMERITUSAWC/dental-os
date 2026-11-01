"""Treatment plans and quotes, memberships, and instalment plans."""
from datetime import timedelta

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from .safeurl import local_path
from .auth import manager_required
from .db import get_db
from .services import billing, instalments as INS, memberships as MEM, money, plans as PL, treatment as TR
from .services.timeutil import now_local, parse_date
from .views import _document_for, _patient_or_404, _today

bp = Blueprint("commercial", __name__)


def _amount(name, allow_zero=False):
    return money.parse_money(request.form.get(name, ""), g.clinic["currency"], allow_zero=allow_zero)


def _opt_amount(name):
    raw = (request.form.get(name) or "").strip()
    return money.parse_money(raw, g.clinic["currency"]) if raw else None


def _date(name, default):
    raw = (request.form.get(name) or "").strip()
    if not raw:
        return default
    try:
        return parse_date(raw)
    except ValueError:
        raise ValueError("Use a date like 2026-11-30.")


def _int(name, default=None):
    raw = (request.form.get(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise ValueError("Use a whole number.")


def _methods():
    return billing.enabled_methods(g.clinic)


def _plan_or_404(plan_id):
    plan = PL.get(get_db(), g.clinic["id"], plan_id)
    if not plan:
        abort(404)
    return plan


# ----------------------------------------------------------------------------- treatment plans
@bp.route("/patients/<int:pid>/plans", methods=["GET", "POST"])
def patient_plans(pid):
    db, cid = get_db(), g.clinic["id"]
    patient = _patient_or_404(pid)
    if request.method == "POST":
        try:
            plan_id = PL.create(db, g.clinic, g.user["id"], pid, request.form.get("title"), request.form.get("note"))
            return redirect(url_for("commercial.plan_show", plan_id=plan_id))
        except PL.PlanError as exc:
            db.rollback()
            flash(str(exc), "error")
        return redirect(url_for("commercial.patient_plans", pid=pid))
    return render_template("plans_patient.html", p=patient, rows=PL.for_patient(db, cid, pid), statuses=PL.STATUSES)


@bp.get("/plans/<int:plan_id>")
def plan_show(plan_id):
    db, cid = get_db(), g.clinic["id"]
    plan = _plan_or_404(plan_id)
    patient = _patient_or_404(plan["patient_id"])
    rows = PL.items(db, cid, plan_id)
    codes = db.execute("SELECT * FROM procedure_codes WHERE clinic_id=? AND active=1 ORDER BY category, name", (cid,)).fetchall()
    users = db.execute("SELECT id, display_name FROM users WHERE clinic_id=? AND active=1 ORDER BY display_name", (cid,)).fetchall()
    return render_template("plan_show.html", p=patient, plan=plan, rows=rows, phases=PL.phases(rows), total=PL.total(rows),
                           deposit=PL.deposits(db, cid, plan_id), events=PL.events(db, cid, plan_id), codes=codes, users=users,
                           statuses=PL.STATUSES, progress=PL.progress(rows), methods=_methods(), teeth=TR.PERMANENT + TR.PRIMARY,
                           surfaces=TR.SURFACE_ORDER, instalment_plans=INS.for_patient(db, cid, patient["id"]))


@bp.get("/plans/<int:plan_id>/quote")
def plan_quote(plan_id):
    db, cid = get_db(), g.clinic["id"]
    plan = _plan_or_404(plan_id)
    rows = PL.items(db, cid, plan_id)
    return render_template("plan_quote.html", p=_document_for(plan["patient_id"]), plan=plan, phases=PL.phases(rows),
                           total=PL.total(rows), deposit=PL.deposits(db, cid, plan_id), today=_today())


def _plan_action(plan_id, fn, ok):
    """Run one plan change, show the result, and go back to the plan."""
    db = get_db()
    _plan_or_404(plan_id)
    try:
        fn()
        flash(ok, "ok")
    except (PL.PlanError, TR.TreatmentError, ValueError) as exc:
        db.rollback()
        flash(str(exc), "error")
    return redirect(url_for("commercial.plan_show", plan_id=plan_id))


@bp.post("/plans/<int:plan_id>/items")
def plan_item_add(plan_id):
    def go():
        PL.add_item(get_db(), g.clinic, g.user["id"], plan_id, request.form.get("code_id", type=int),
                    tooth=TR.clean_tooth(request.form.get("tooth")), surfaces=TR.clean_surfaces(request.form.getlist("surfaces")),
                    fee_p=_opt_amount("fee") if (request.form.get("fee") or "").strip() else None,
                    phase=_int("phase", 1), note=request.form.get("note"))
    return _plan_action(plan_id, go, "Added to the plan.")


def _item_action(item_id, fn, ok):
    db = get_db()
    row = db.execute("SELECT plan_id FROM plan_items WHERE id = ? AND clinic_id = ?", (item_id, g.clinic["id"])).fetchone()
    if not row:
        abort(404)
    try:
        fn()
        flash(ok, "ok")
    except LookupError:
        abort(404)
    except (PL.PlanError, ValueError) as exc:
        db.rollback()
        flash(str(exc), "error")
    return redirect(url_for("commercial.plan_show", plan_id=row["plan_id"]))


@bp.post("/plan-items/<int:item_id>/update")
def plan_item_update(item_id):
    return _item_action(item_id, lambda: PL.update_item(
        get_db(), g.clinic, g.user["id"], item_id,
        fee_p=_opt_amount("fee") if (request.form.get("fee") or "").strip() else None, phase=_int("phase")), "Updated.")


@bp.post("/plan-items/<int:item_id>/remove")
def plan_item_remove(item_id):
    return _item_action(item_id, lambda: PL.remove_item(get_db(), g.clinic, g.user["id"], item_id), "Removed from the plan.")


@bp.post("/plans/<int:plan_id>/present")
def plan_present(plan_id):
    return _plan_action(plan_id, lambda: PL.present(get_db(), g.clinic, g.user, plan_id, _int("follow_up_days", 7),
                                                    request.form.get("owner_id", type=int)),
                        "Marked as presented. It now shows on the follow-up list.")


@bp.post("/plans/<int:plan_id>/followup")
def plan_followup(plan_id):
    return _plan_action(plan_id, lambda: PL.log_followup(get_db(), g.clinic, g.user, plan_id, request.form.get("note"),
                                                         _int("next_in_days", 7)), "Follow-up noted.")


@bp.post("/plans/<int:plan_id>/accept")
def plan_accept(plan_id):
    def go():
        deposit = _opt_amount("deposit")
        PL.accept(get_db(), g.clinic, g.user, plan_id, request.form.get("signer_name"), request.form.get("relation"),
                  request.form.get("method"), request.form.get("agreed"), deposit_p=deposit,
                  deposit_method=request.form.get("deposit_method"), deposit_reference=request.form.get("deposit_reference"))
    return _plan_action(plan_id, go, "Plan accepted. The work is now on the treatment list.")


@bp.post("/plans/<int:plan_id>/decline")
def plan_decline(plan_id):
    return _plan_action(plan_id, lambda: PL.decline(get_db(), g.clinic, g.user, plan_id, request.form.get("reason")),
                        "Marked as declined. It stays on record.")


@bp.post("/plans/<int:plan_id>/reopen")
def plan_reopen(plan_id):
    return _plan_action(plan_id, lambda: PL.reopen(get_db(), g.clinic, g.user, plan_id), "Reopened.")


@bp.get("/plans")
def plans_board():
    db = get_db()
    PL_rows = PL.waiting(db, g.clinic)
    MEM.run_renewals(db, g.clinic)
    db.commit()
    return render_template("plans_board.html", waiting=PL_rows, value=sum(r["value"] for r in PL_rows),
                           overdue=INS.overdue(db, g.clinic), fees=MEM.attention(db, g.clinic), today=_today())


# ----------------------------------------------------------------------------- memberships
@bp.get("/memberships")
def memberships_home():
    db, cid = get_db(), g.clinic["id"]
    MEM.run_renewals(db, g.clinic)
    db.commit()
    rows = MEM.members(db, cid)
    return render_template("memberships.html", rows=rows, attention=MEM.attention(db, g.clinic), monthly=MEM.monthly_value(rows),
                           plans=MEM.list_plans(db, cid, active_only=True), cycles=MEM.CYCLES, methods=_methods())


@bp.route("/membership-plans", methods=["GET", "POST"])
@manager_required
def membership_plans():
    db, cid = get_db(), g.clinic["id"]
    if request.method == "POST":
        try:
            if request.form.get("action") == "benefit":
                MEM.set_benefit(db, g.clinic, g.user["id"], request.form.get("plan_id", type=int),
                                request.form.get("code_id", type=int), _int("qty", 0))
                flash("Saved.", "ok")
            else:
                MEM.save_plan(db, g.clinic, g.user["id"], request.form.get("plan_id", type=int), request.form.get("name"),
                              request.form.get("description"), _amount("fee"), request.form.get("cycle"),
                              _int("discount_pct", 0), bool(request.form.get("active")))
                flash("Saved.", "ok")
        except (MEM.MembershipError, ValueError) as exc:
            db.rollback()
            flash(str(exc), "error")
        except LookupError:
            abort(404)
        return redirect(url_for("commercial.membership_plans"))
    codes = db.execute("SELECT * FROM procedure_codes WHERE clinic_id=? AND active=1 ORDER BY category, name", (cid,)).fetchall()
    return render_template("membership_plans.html", plans=MEM.list_plans(db, cid), codes=codes, cycles=MEM.CYCLES)


@bp.route("/patients/<int:pid>/membership", methods=["GET", "POST"])
def patient_membership(pid):
    db, cid = get_db(), g.clinic["id"]
    patient = _patient_or_404(pid)
    if request.method == "POST":
        try:
            MEM.enrol(db, g.clinic, g.user["id"], pid, request.form.get("plan_id", type=int),
                      start_on=_date("start_on", None), pay_method=request.form.get("pay_method") or None,
                      pay_reference=request.form.get("pay_reference"))
            flash("Membership started.", "ok")
        except (MEM.MembershipError, ValueError) as exc:
            db.rollback()
            flash(str(exc), "error")
        return redirect(url_for("commercial.patient_membership", pid=pid))
    MEM.run_renewals(db, g.clinic)
    db.commit()
    current = MEM.current_for(db, cid, pid)
    return render_template("membership_patient.html", p=patient, m=current, history=MEM.history_for(db, cid, pid),
                           plans=MEM.list_plans(db, cid, active_only=True), methods=_methods(), cycles=MEM.CYCLES, today=_today(),
                           charges=MEM.charges(db, cid, current["id"]) if current else [],
                           benefits=MEM.benefit_status(db, g.clinic, current) if current else None,
                           uses=MEM.uses(db, cid, current["id"]) if current else [], grace=MEM.GRACE_DAYS)


def _charge_action(charge_id, fn, ok):
    db = get_db()
    row = db.execute("SELECT m.patient_id FROM membership_charges mc JOIN memberships m ON m.id = mc.membership_id "
                     "WHERE mc.id = ? AND mc.clinic_id = ?", (charge_id, g.clinic["id"])).fetchone()
    if not row:
        abort(404)
    try:
        fn()
        flash(ok, "ok")
    except (MEM.MembershipError, ValueError) as exc:
        db.rollback()
        flash(str(exc), "error")
    target = request.form.get("back") or ""
    if local_path(target):
        return redirect(target)
    return redirect(url_for("commercial.patient_membership", pid=row["patient_id"]))


@bp.post("/membership-charges/<int:charge_id>/pay")
def membership_charge_pay(charge_id):
    return _charge_action(charge_id, lambda: MEM.pay_charge(get_db(), g.clinic, g.user["id"], charge_id, request.form.get("method"),
                                                            request.form.get("reference")), "Payment recorded.")


@bp.post("/membership-charges/<int:charge_id>/fail")
def membership_charge_fail(charge_id):
    return _charge_action(charge_id, lambda: MEM.mark_failed(get_db(), g.clinic, g.user["id"], charge_id, request.form.get("note")),
                          "Marked as failed. It stays on the list until it is paid.")


@bp.post("/membership-charges/<int:charge_id>/waive")
@manager_required
def membership_charge_waive(charge_id):
    return _charge_action(charge_id, lambda: MEM.waive_charge(get_db(), g.clinic, g.user["id"], charge_id, request.form.get("note")),
                          "Fee waived.")


@bp.post("/memberships/<int:membership_id>/cancel")
@manager_required
def membership_cancel(membership_id):
    db = get_db()
    m = MEM.get(db, g.clinic["id"], membership_id)
    if not m:
        abort(404)
    try:
        MEM.cancel(db, g.clinic, g.user["id"], membership_id, request.form.get("reason"))
        flash("Membership cancelled.", "ok")
    except MEM.MembershipError as exc:
        db.rollback()
        flash(str(exc), "error")
    return redirect(url_for("commercial.patient_membership", pid=m["patient_id"]))


# ----------------------------------------------------------------------------- instalment plans
@bp.route("/patients/<int:pid>/instalments", methods=["GET", "POST"])
def patient_instalments(pid):
    db, cid = get_db(), g.clinic["id"]
    patient = _patient_or_404(pid)
    if request.method == "POST":
        try:
            INS.create(db, g.clinic, g.user["id"], pid, _amount("total"), _int("count", 0), request.form.get("frequency"),
                       _date("start_on", _today()), treatment_plan_id=request.form.get("treatment_plan_id", type=int),
                       note=request.form.get("note"))
            flash("Instalment plan created.", "ok")
        except (INS.InstalmentError, ValueError) as exc:
            db.rollback()
            flash(str(exc), "error")
        return redirect(url_for("commercial.patient_instalments", pid=pid))
    return render_template("instalments.html", p=patient, rows=INS.for_patient(db, cid, pid), methods=_methods(),
                           frequencies=INS.FREQUENCIES, today=_today(), balance=billing.totals(db, cid, pid)["balance"],
                           plans=[r["plan"] for r in PL.for_patient(db, cid, pid) if r["plan"]["status"] == "accepted"])


@bp.post("/instalments/<int:inst_id>/pay")
def instalment_pay(inst_id):
    db = get_db()
    row = db.execute("SELECT p.patient_id FROM instalments i JOIN payment_plans p ON p.id = i.payment_plan_id "
                     "WHERE i.id = ? AND i.clinic_id = ?", (inst_id, g.clinic["id"])).fetchone()
    if not row:
        abort(404)
    try:
        pay_id = INS.pay(db, g.clinic, g.user["id"], inst_id, request.form.get("method"), request.form.get("reference"))
        flash("Payment recorded.", "ok")
        return redirect(url_for("clinical.receipt", pay_id=pay_id))
    except INS.InstalmentError as exc:
        db.rollback()
        flash(str(exc), "error")
    return redirect(url_for("commercial.patient_instalments", pid=row["patient_id"]))


@bp.post("/payment-plans/<int:plan_id>/cancel")
@manager_required
def instalment_cancel(plan_id):
    db = get_db()
    plan = INS.get(db, g.clinic["id"], plan_id)
    if not plan:
        abort(404)
    try:
        INS.cancel(db, g.clinic, g.user["id"], plan_id, request.form.get("reason"))
        flash("Instalment plan cancelled.", "ok")
    except INS.InstalmentError as exc:
        db.rollback()
        flash(str(exc), "error")
    return redirect(url_for("commercial.patient_instalments", pid=plan["patient_id"]))
