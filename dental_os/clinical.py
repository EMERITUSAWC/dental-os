"""Treatment, money, recalls and reports screens."""
from datetime import timedelta

from werkzeug.exceptions import HTTPException
from flask import Blueprint, Response, abort, flash, g, redirect, render_template, request, session, url_for

from .safeurl import local_path
from .auth import manager_required
from .db import get_db
from .services import audit, billing, memberships, messaging, money, recalls, reports, treatment as TR
from .services import refunds as RF
from .services import patients as P
from .services.timeutil import now_local, parse_date
from .views import _document_for, _patient_or_404, _today

bp = Blueprint("clinical", __name__)


def _back(default):
    target = request.form.get("back") or ""
    return target if local_path(target) else default


def _providers():
    return get_db().execute("SELECT * FROM providers WHERE clinic_id=? AND active=1 ORDER BY id",
                            (g.clinic["id"],)).fetchall()


# ------------------------------------------------------------ treatment
@bp.get("/patients/<int:pid>/treatment")
def treatment(pid):
    db, cid = get_db(), g.clinic["id"]
    patient = _patient_or_404(pid)
    rows = TR.list_for_patient(db, cid, pid)
    codes = db.execute("SELECT * FROM procedure_codes WHERE clinic_id=? AND active=1 ORDER BY category, name", (cid,)).fetchall()
    visits = db.execute(
        """SELECT id, start_at, category FROM appointments WHERE clinic_id=? AND patient_id=?
           AND status IN ('scheduled','complete') ORDER BY start_at DESC LIMIT 10""", (cid, pid)).fetchall()
    from .services import autocodes as AUTO
    from .services import ghclaims as GC
    return render_template("treatment.html", p=patient, rows=rows, codes=codes, visits=visits, autos=AUTO.groups(db, cid, True), dx=GC.diagnoses(db, cid),
                           providers=_providers(), sums=TR.summary(rows), teeth=TR.PERMANENT + TR.PRIMARY,
                           surfaces=TR.SURFACE_ORDER)


@bp.post("/patients/<int:pid>/treatment")
def treatment_add(pid):
    db = get_db()
    _patient_or_404(pid)
    try:
        fee = request.form.get("fee", "").strip()
        tooth = TR.clean_tooth(request.form.get("tooth"))
        surfaces = TR.clean_surfaces(request.form.getlist("surfaces"))
        raw = request.form.get("code_id") or ""
        if raw.startswith("auto:") and raw[5:].isdigit():
            from .services import autocodes as AUTO
            try:
                code_id = AUTO.resolve(db, g.clinic["id"], int(raw[5:]), tooth, surfaces)
            except AUTO.AutoError as exc:
                raise TR.TreatmentError(str(exc))
        else:
            code_id = int(raw) if raw.isdigit() else None
        TR.add_planned(db, g.clinic, g.user["id"], pid, code_id,
                       tooth=tooth,
                       surfaces=surfaces,
                       fee_p=money.parse_money(fee, g.clinic["currency"], allow_zero=True) if fee else None,
                       provider_id=request.form.get("provider_id", type=int),
                       notes=(request.form.get("notes") or "").strip() or None)
        flash("Added to the plan.", "ok")
    except (TR.TreatmentError, ValueError) as exc:
        db.rollback()
        flash(str(exc), "error")
    return redirect(url_for("clinical.treatment", pid=pid))


def _proc_or_404(proc_id):
    proc = TR.get(get_db(), g.clinic["id"], proc_id)
    if not proc:
        abort(404)
    return proc


@bp.post("/procedures/<int:proc_id>/<action>")
def procedure_action(proc_id, action):
    db, cid, uid = get_db(), g.clinic["id"], g.user["id"]
    proc = _proc_or_404(proc_id)
    try:
        if action == "done":
            short = TR.mark_done(db, g.clinic, uid, proc_id, request.form.get("appointment_id", type=int),
                                 request.form.get("provider_id", type=int))
            if short:
                flash("The stock list shows too little for this treatment's kit. What was there was taken; the manager will see the rest under Stock, Kits.", "error")
            applied = memberships.apply_benefits(db, g.clinic, uid, proc_id)
            if applied:
                flash(f"Membership applied: {applied}.", "ok")
        elif action == "cancel":
            TR.cancel(db, cid, uid, proc_id)
        elif action == "fee":
            TR.set_fee(db, cid, uid, proc_id, money.parse_money(request.form.get("fee", ""), g.clinic["currency"], allow_zero=True))
        elif action == "void":
            if g.user["role"] != "manager":
                abort(403)
            TR.void(db, cid, uid, proc_id, request.form.get("reason"))
            memberships.reverse_for_procedure(db, g.clinic, uid, proc_id)
        else:
            abort(404)
        flash("Updated.", "ok")
    except (TR.TreatmentError, ValueError) as exc:
        db.rollback()
        flash(str(exc), "error")
    return redirect(url_for("clinical.treatment", pid=proc["patient_id"]))


# ------------------------------------------------------------ account and payments
@bp.get("/patients/<int:pid>/account")
def account(pid):
    db, cid = get_db(), g.clinic["id"]
    patient = _patient_or_404(pid)
    audit.log(db, cid, g.user["id"], "account.view", "patient", pid)
    return render_template("account.html", p=patient, t=billing.totals(db, cid, pid),
                           family=billing.family_balance(db, cid, pid),
                           events=list(reversed(billing.statement(db, cid, pid, g.lang))),
                           methods=billing.enabled_methods(g.clinic), reasons=billing.ADJUSTMENT_REASONS, today=_today(),
                           all_methods=billing.METHODS, refund_reasons=RF.REASONS)


@bp.post("/patients/<int:pid>/payments")
def payment_add(pid):
    db = get_db()
    _patient_or_404(pid)
    try:
        pay_id = billing.record_payment(db, g.clinic, g.user["id"], pid, money.parse_money(request.form.get("amount", ""), g.clinic["currency"]),
                                        request.form.get("method", ""), request.form.get("reference"),
                                        request.form.get("note"), desk=True)
        flash("Payment recorded.", "ok")
        return redirect(url_for("clinical.receipt", pay_id=pay_id))
    except (billing.BillingError, ValueError) as exc:
        db.rollback()
        flash(str(exc), "error")
        return redirect(url_for("clinical.account", pid=pid))


@bp.post("/patients/<int:pid>/adjustments")
@manager_required
def adjustment_add(pid):
    db = get_db()
    _patient_or_404(pid)
    try:
        amount = money.parse_money(request.form.get("amount", ""), g.clinic["currency"])
        sign = -1 if request.form.get("direction", "discount") == "discount" else 1
        billing.add_adjustment(db, g.clinic, g.user["id"], pid, sign * amount, request.form.get("reason", ""),
                               request.form.get("note"))
        flash("Adjustment saved.", "ok")
    except (billing.BillingError, ValueError) as exc:
        db.rollback()
        flash(str(exc), "error")
    return redirect(url_for("clinical.account", pid=pid))


@bp.get("/payments/<int:pay_id>/receipt")
def receipt(pay_id):
    db, cid = get_db(), g.clinic["id"]
    pay = billing.get_receipt(db, cid, pay_id)
    if not pay:
        abort(404)
    _document_for(pay["patient_id"])
    return render_template("receipt.html", pay=pay, t=billing.totals(db, cid, pay["patient_id"]),
                           methods=billing.METHODS)


@bp.post("/payments/<int:pay_id>/void")
@manager_required
def payment_void(pay_id):
    db = get_db()
    pay = billing.get_receipt(db, g.clinic["id"], pay_id)
    if not pay:
        abort(404)
    try:
        billing.void_payment(db, g.clinic["id"], g.user["id"], pay_id, request.form.get("reason"))
        flash("Payment voided. It stays on record.", "ok")
    except billing.BillingError as exc:
        db.rollback()
        flash(str(exc), "error")
    return redirect(url_for("clinical.account", pid=pay["patient_id"]))


# ------------------------------------------------------------ recalls
@bp.get("/recalls")
def recall_list():
    today = _today()
    rows = recalls.upcoming(get_db(), g.clinic["id"], today)
    # 0.57: a long list comes in pages. Drawing 9,000 rows at once took a second and was no use to anyone.
    page = max(request.args.get("page", 1, type=int), 1)
    pages = max((len(rows) + RECALLS_PER_PAGE - 1) // RECALLS_PER_PAGE, 1)
    page = min(page, pages)
    first = (page - 1) * RECALLS_PER_PAGE
    return render_template("recalls.html", rows=rows[first:first + RECALLS_PER_PAGE], today=today.isoformat(), total=len(rows),
                           page=page, pages=pages, first=first + 1, last=min(first + RECALLS_PER_PAGE, len(rows)))


RECALLS_PER_PAGE = 200


@bp.post("/recalls/<int:rid>/whatsapp")
def recall_whatsapp(rid):
    db = get_db()
    r = recalls.get(db, g.clinic["id"], rid)
    if not r:
        abort(404)
    patient = _patient_or_404(r["patient_id"])
    try:
        return redirect(messaging.prepare_recall(db, g.clinic, g.user["id"], patient, r))
    except PermissionError as exc:
        flash(str(exc), "error")
        return redirect(url_for("clinical.recall_list"))


@bp.post("/recalls/<int:rid>/snooze")
def recall_snooze(rid):
    try:
        recalls.snooze(get_db(), g.clinic, g.user["id"], rid, min(max(request.form.get("days", 30, type=int), 1), 365))
        flash("Hidden for now.", "ok")
    except LookupError:
        abort(404)
    return redirect(url_for("clinical.recall_list"))


@bp.post("/recalls/<int:rid>/disable")
def recall_disable(rid):
    try:
        recalls.disable(get_db(), g.clinic["id"], g.user["id"], rid)
        flash("Recall switched off for this patient.", "ok")
    except LookupError:
        abort(404)
    return redirect(url_for("clinical.recall_list"))


# ------------------------------------------------------------ reports
def _range():
    today = _today()
    try:
        end = parse_date(request.args["end"]) if request.args.get("end") else today
        start = parse_date(request.args["start"]) if request.args.get("start") else end - timedelta(days=29)
    except ValueError:
        end, start = today, today - timedelta(days=29)
    if start > end or (end - start).days > 366:
        start = end - timedelta(days=29)
    return start, end


@bp.get("/reports")
@manager_required
def report_view():
    db, cid = get_db(), g.clinic["id"]
    start, end = _range()
    return render_template("reports.html", start=start, end=end, pilot=reports.pilot(db, cid, start, end),
                           mny=reports.money(db, cid, start, end), methods=billing.methods_in_use(db, g.clinic),
                           owing=billing.outstanding(db, cid, 10))


@bp.get("/reports/takings")
@manager_required
def takings():
    raw = request.args.get("date")
    try:
        day = parse_date(raw) if raw else _today()
    except ValueError:
        day = _today()
    data = billing.daily_takings(get_db(), g.clinic["id"], day.isoformat())
    return render_template("takings.html", day=day, data=data, methods=billing.methods_in_use(get_db(), g.clinic),
                           prev=day - timedelta(days=1), next=day + timedelta(days=1))


# ------------------------------------------------------------ catalogue
@bp.route("/catalogue", methods=["GET", "POST"])
@manager_required
def catalogue():
    db, cid = get_db(), g.clinic["id"]
    if request.method == "POST":
        action = request.form.get("action")
        try:
            if action == "starter":
                n = TR.ensure_catalogue(db, cid)
                recalls.ensure_default_type(db, cid)
                flash(f"Added {n} procedures. Now set the fees.", "ok")
            elif action == "pack":
                from .services import catalogue as _cat
                if request.form.get("pack") not in _cat.PACKS:
                    abort(404)
                n = _cat.add_pack(db, cid, g.user["id"], request.form.get("pack"))
                flash(f"Added {n} procedures. They are switched off until you set a price and tick In use." if n else
                      "You already have every code in that pack.", "ok")
            elif action in ("pricelist_check", "pricelist_apply"):
                from .services import catalogue as _cat
                upload = request.files.get("file")
                if not upload or not upload.filename:
                    raise ValueError("Choose a CSV file.")
                raw = upload.read(256 * 1024 + 1)
                try:
                    text = raw.decode("utf-8-sig")
                except UnicodeDecodeError:
                    text = raw.decode("latin-1")
                try:
                    res = _cat.import_csv(db, g.clinic, g.user["id"], text, update_existing=bool(request.form.get("update_existing")),
                                          apply=action == "pricelist_apply")
                except _cat.CatalogueError as exc:
                    raise ValueError(str(exc))
                if res["errors"]:
                    db.rollback()
                    session["price_errors"] = [[n, m] for n, m in res["errors"][:30]]
                    flash(f"Nothing was changed. {len(res['errors'])} row(s) need fixing.", "error")
                else:
                    verb = "Done." if res["applied"] else "The file is fine. Nothing has been changed yet."
                    flash(f"{verb} {res['added']} new, {res['updated']} changed, {res['unchanged']} left as they were.", "ok")
            elif action == "add":
                code = (request.form.get("code") or "").strip().upper()
                name = (request.form.get("name") or "").strip()
                if not code or not name:
                    raise ValueError("Enter a code and a name.")
                db.execute("INSERT INTO procedure_codes(clinic_id, code, name, category, fee_p) VALUES (?,?,?,?,?)",
                           (cid, code, name, (request.form.get("category") or "General").strip(),
                            money.parse_money(request.form.get("fee") or "0", g.clinic["currency"], allow_zero=True)))
                audit.log(db, cid, g.user["id"], "catalogue.add", detail=code)
                flash("Added.", "ok")
            elif action == "update":
                db.execute("UPDATE procedure_codes SET fee_p=?, active=? WHERE id=? AND clinic_id=?",
                           (money.parse_money(request.form.get("fee") or "0", g.clinic["currency"], allow_zero=True),
                            1 if request.form.get("active") else 0, request.form.get("id", type=int), cid))
                if "tax_rate_id" in request.form:
                    from .services import taxes as _tx
                    _tx.set_code_rate(db, cid, g.user["id"], request.form.get("id", type=int), request.form.get("tax_rate_id", type=int))
                audit.log(db, cid, g.user["id"], "catalogue.update", "procedure_code", request.form.get("id", type=int))
                flash("Saved.", "ok")
        except HTTPException:
            raise
        except Exception as exc:
            db.rollback()
            flash("That code already exists." if "UNIQUE" in str(exc) else str(exc), "error")
        return redirect(url_for("clinical.catalogue"))
    from .services import catalogue as _cat, taxes as _tx
    rows = db.execute("SELECT * FROM procedure_codes WHERE clinic_id=? ORDER BY active DESC, category, name", (cid,)).fetchall()
    return render_template("catalogue.html", rows=rows, tax_rates=_tx.list_rates(db, cid), packs=_cat.packs_status(db, cid),
                           price_errors=session.pop("price_errors", None))


@bp.get("/catalogue/price-list.csv")
@manager_required
def catalogue_csv():
    from .services import catalogue as _cat
    db = get_db()
    audit.log(db, g.clinic["id"], g.user["id"], "catalogue.export")
    return Response(_cat.export_csv(db, g.clinic), mimetype="text/csv",
                    headers={"Content-Disposition": 'attachment; filename="price-list.csv"'})
