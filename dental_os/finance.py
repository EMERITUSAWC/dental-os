"""Insurance and other payers, claims, invoices, statements and ageing."""
from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from .auth import manager_required
from .db import get_db
from .services import ageing, benefits as BEN, billing, insurance as INS, invoices as INV, money, payerpay as PP
from .services.timeutil import parse_date
from .views import _document_for, _patient_or_404, _today

bp = Blueprint("finance", __name__)


def _opt_amount(name):
    raw = (request.form.get(name) or "").strip()
    return money.parse_money(raw, g.clinic["currency"]) if raw else None


def _opt_date(name):
    raw = (request.form.get(name) or "").strip()
    if not raw:
        return None
    try:
        return parse_date(raw).isoformat()
    except ValueError:
        raise ValueError("Use a date like 2026-11-30.")


# ----------------------------------------------------------------------------- payers
@bp.route("/payers", methods=["GET", "POST"])
@manager_required
def payers():
    db, cid = get_db(), g.clinic["id"]
    if request.method == "POST":
        try:
            INS.save_payer(db, g.clinic, g.user["id"], request.form.get("payer_id", type=int), request.form.get("name"),
                           request.form.get("kind"), bool(request.form.get("contracted")), request.form.get("phone"),
                           request.form.get("email"), request.form.get("notes"), bool(request.form.get("active")))
            flash("Saved.", "ok")
        except INS.InsuranceError as exc:
            db.rollback()
            flash(str(exc), "error")
        except LookupError:
            abort(404)
        return redirect(url_for("finance.payers"))
    return render_template("payers.html", rows=INS.list_payers(db, cid), kinds=INS.PAYER_KINDS)


@bp.route("/payers/<int:payer_id>/fees", methods=["GET", "POST"])
@manager_required
def payer_fees(payer_id):
    db, cid = get_db(), g.clinic["id"]
    payer = INS.get_payer(db, cid, payer_id)
    if not payer:
        abort(404)
    if request.method == "POST":
        try:
            fees = {}
            for key, raw in request.form.items():
                if key.startswith("fee_"):
                    fees[int(key[4:])] = money.parse_money(raw, g.clinic["currency"], allow_zero=True) if raw.strip() else None
            INS.set_payer_fees(db, g.clinic, g.user["id"], payer_id, fees)
            flash("Prices saved.", "ok")
        except (INS.InsuranceError, ValueError) as exc:
            db.rollback()
            flash(str(exc), "error")
        return redirect(url_for("finance.payer_fees", payer_id=payer_id))
    return render_template("payer_fees.html", payer=payer, rows=INS.payer_fee_table(db, cid, payer_id))


# ----------------------------------------------------------------------------- a patient's cover and claims
@bp.route("/patients/<int:pid>/insurance", methods=["GET", "POST"])
def patient_insurance(pid):
    db, cid = get_db(), g.clinic["id"]
    patient = _patient_or_404(pid)
    if request.method == "POST":
        try:
            pct = request.form.get("coverage_pct", type=int)
            INS.add_cover(db, g.clinic, g.user["id"], pid, request.form.get("payer_id", type=int), request.form.get("member_no"),
                          request.form.get("holder_name"), pct if pct is not None else -1, _opt_amount("annual_max"),
                          _opt_date("valid_from"), _opt_date("valid_to"))
            flash("Cover added.", "ok")
        except (INS.InsuranceError, ValueError) as exc:
            db.rollback()
            flash(str(exc), "error")
        return redirect(url_for("finance.patient_insurance", pid=pid))
    covers = INS.covers_for(db, cid, pid)
    planned = db.execute("""SELECT pr.*, c.name AS code_name FROM procedures pr JOIN procedure_codes c ON c.id = pr.code_id
                            WHERE pr.clinic_id = ? AND pr.patient_id = ? AND pr.status = 'planned' ORDER BY pr.id""", (cid, pid)).fetchall()
    cover, ests = INS.estimate_lines(db, g.clinic, pid, [{"code_id": r["code_id"], "fee_p": r["fee_p"]} for r in planned])
    est_covers = INS.estimate_covers(db, g.clinic, pid)
    cover_status = []
    today = _today()
    for c in covers:
        sm = BEN.summary(db, g.clinic, c, today)
        cover_status.append({"cover": c, "used": sm["max_used"], "left": sm["max_left"], "sum": sm, "in_force": INS._in_force(c, today),
                             "claimable": INS.unclaimed(db, cid, pid, "claim", c["id"]),
                             "preauthable": INS.unclaimed(db, cid, pid, "preauth", c["id"]),
                             "rules": BEN.category_rules(db, cid, c["id"]), "limits": len(BEN.limits(db, cid, c["id"]))})
    return render_template("insurance.html", p=patient, covers=cover_status, payers=INS.list_payers(db, cid, active_only=True),
                           estimates=list(zip(planned, ests)), est_cover=cover, est_covers=est_covers, claims=INS.claims_for_patient(db, cid, pid),
                           claimable=INS.unclaimed(db, cid, pid, "claim"), preauthable=INS.unclaimed(db, cid, pid, "preauth"),
                           statuses=INS.CLAIM_STATUS)


@bp.route("/cover/<int:cover_id>/benefits", methods=["GET", "POST"])
def cover_benefits(cover_id):
    db, cid = get_db(), g.clinic["id"]
    cover = INS.get_cover(db, cid, cover_id)
    if not cover or not cover["active"]:
        abort(404)
    p = _patient_or_404(cover["patient_id"])
    categories = [r[0] for r in db.execute("SELECT DISTINCT category FROM procedure_codes WHERE clinic_id = ? ORDER BY category", (cid,))]
    codes = db.execute("SELECT * FROM procedure_codes WHERE clinic_id = ? AND active = 1 ORDER BY category, name", (cid,)).fetchall()
    if request.method == "POST":
        try:
            cats = {}
            for i, cat in enumerate(categories):
                raw = (request.form.get(f"pct_{i}") or "").strip()
                wait = (request.form.get(f"wait_{i}") or "").strip()
                try:
                    cats[cat] = (int(raw) if raw else None, int(wait) if wait else 0)
                except ValueError:
                    raise INS.InsuranceError("Use whole numbers for shares and waiting periods.")
            lims = []
            for i in range(6):
                code_id = request.form.get(f"lim_code_{i}", type=int)
                if not code_id:
                    continue
                times, months = request.form.get(f"lim_times_{i}", type=int), request.form.get(f"lim_months_{i}", type=int)
                if not times or not months:
                    raise INS.InsuranceError("A limit is 1 to 50 times in 1 to 120 months.")
                lims.append((code_id, times, months))
            ortho = _opt_amount("ortho_max")
            INS.save_benefit_terms(db, g.clinic, g.user["id"], cover_id, _opt_amount("deductible") or 0, request.form.get("on_preventive") == "1",
                                   request.form.get("year_start_month", type=int) or 1, ortho, _opt_date("waiting_from"), cats, lims)
            flash("Cover terms saved.", "ok")
            return redirect(url_for("finance.patient_insurance", pid=p["id"]))
        except (INS.InsuranceError, ValueError) as exc:
            db.rollback()
            flash(str(exc), "error")
        return redirect(url_for("finance.cover_benefits", cover_id=cover_id))
    rules = BEN.category_rules(db, cid, cover_id)
    lims = list(BEN.limits(db, cid, cover_id).values())
    return render_template("cover_benefits.html", p=p, cover=cover, categories=categories, rules=rules, codes=codes, lims=lims,
                           months=[(m, _month_name(m)) for m in range(1, 13)])


def _month_name(m):
    return ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December")[m - 1]


@bp.post("/cover/<int:cover_id>/main")
def cover_main(cover_id):
    db = get_db()
    try:
        pid = INS.make_main(db, g.clinic, g.user["id"], cover_id)
    except LookupError:
        abort(404)
    flash("That cover now pays first.", "ok")
    return redirect(url_for("finance.patient_insurance", pid=pid))


@bp.post("/cover/<int:cover_id>/verify")
def cover_verify(cover_id):
    db = get_db()
    cover = INS.get_cover(db, g.clinic["id"], cover_id)
    if not cover:
        abort(404)
    try:
        INS.verify(db, g.clinic, g.user["id"], cover_id, request.form.get("note"))
        flash("Cover check recorded.", "ok")
    except INS.InsuranceError as exc:
        db.rollback()
        flash(str(exc), "error")
    return redirect(url_for("finance.patient_insurance", pid=cover["patient_id"]))


# ----------------------------------------------------------------------------- one payment from a payer across many claims
@bp.get("/payer-payments")
def payer_payments():
    db = get_db()
    return render_template("payer_payments.html", rows=PP.recent(db, g.clinic["id"]), payers=INS.list_payers(db, g.clinic["id"]))


@bp.route("/payer-payments/new", methods=["GET", "POST"])
def payer_payment_new():
    db, cid = get_db(), g.clinic["id"]
    payer_id = request.values.get("payer_id", type=int)
    payer = db.execute("SELECT * FROM payers WHERE id = ? AND clinic_id = ?", (payer_id, cid)).fetchone() if payer_id else None
    if not payer:
        flash("Pick a payer from the list.", "error")
        return redirect(url_for("finance.payer_payments"))
    claims = PP.open_claims(db, cid, payer_id)
    if request.method == "POST":
        try:
            allocations = []
            for x in claims:
                k = x["claim"]["id"]
                raw, wo = (request.form.get(f"amt_{k}") or "").strip(), (request.form.get(f"wo_{k}") or "").strip()
                allocations.append({"claim_id": k, "amount_p": money.parse_money(raw, g.clinic["currency"], allow_zero=True) if raw else 0,
                                    "writeoff_p": money.parse_money(wo, g.clinic["currency"], allow_zero=True) if wo else 0,
                                    "final": request.form.get(f"final_{k}") == "1"})
            total_raw = (request.form.get("amount") or "").strip()
            pp_id = PP.post(db, g.clinic, g.user["id"], payer_id, request.form.get("received_on"),
                            money.parse_money(total_raw, g.clinic["currency"], allow_zero=True) if total_raw else None,
                            request.form.get("reference"), allocations, request.form.get("note"))
            flash("Payment from the payer recorded on each patient's account.", "ok")
            return redirect(url_for("finance.payer_payment_show", pp_id=pp_id))
        except (PP.PayerPayError, ValueError) as exc:
            db.rollback()
            flash(str(exc), "error")
    return render_template("payer_payment_new.html", payer=payer, claims=claims, form=request.form, today=_today())


@bp.get("/payer-payments/<int:pp_id>")
def payer_payment_show(pp_id):
    db, cid = get_db(), g.clinic["id"]
    pp = PP.get(db, cid, pp_id)
    if not pp:
        abort(404)
    return render_template("payer_payment.html", pp=pp, lines=PP.lines(db, cid, pp_id), statuses=INS.CLAIM_STATUS)


@bp.post("/cover/<int:cover_id>/end")
def cover_end(cover_id):
    db = get_db()
    try:
        pid = INS.end_cover(db, g.clinic, g.user["id"], cover_id)
    except LookupError:
        abort(404)
    flash("Cover ended. Old claims are kept.", "ok")
    return redirect(url_for("finance.patient_insurance", pid=pid))


@bp.post("/patients/<int:pid>/claims")
def claim_create(pid):
    db = get_db()
    _patient_or_404(pid)
    try:
        claim_id = INS.create_claim(db, g.clinic, g.user["id"], pid, request.form.get("cover_id", type=int),
                                    [x for x in request.form.getlist("procedure_id") if x.isdigit()],
                                    kind=request.form.get("kind", "claim"))
        flash("Claim made. Check it, then mark it as sent.", "ok")
        return redirect(url_for("finance.claim_show", claim_id=claim_id))
    except INS.InsuranceError as exc:
        db.rollback()
        flash(str(exc), "error")
    return redirect(url_for("finance.patient_insurance", pid=pid))


# ----------------------------------------------------------------------------- claims
@bp.get("/claims")
def claims_list():
    db = get_db()
    status = request.args.get("status", "open")
    return render_template("claims.html", rows=INS.claim_list(db, g.clinic, status), status=status, statuses=INS.CLAIM_STATUS,
                           awaiting=INS.awaiting_total(db, g.clinic["id"]), by_payer=PP.ageing_by_payer(db, g.clinic))


@bp.get("/claims/<int:claim_id>")
def claim_show(claim_id):
    db, cid = get_db(), g.clinic["id"]
    claim = INS.get_claim(db, cid, claim_id)
    if not claim:
        abort(404)
    return render_template("claim.html", c=claim, items=INS.claim_items(db, cid, claim_id), statuses=INS.CLAIM_STATUS,
                           methods=billing.enabled_methods(g.clinic), today=_today(),
                           payments=db.execute("SELECT * FROM payments WHERE clinic_id = ? AND claim_id = ? ORDER BY id", (cid, claim_id)).fetchall())


def _claim_action(claim_id, fn, ok):
    db = get_db()
    if not INS.get_claim(db, g.clinic["id"], claim_id):
        abort(404)
    try:
        fn()
        flash(ok, "ok")
    except (INS.InsuranceError, ValueError) as exc:
        db.rollback()
        flash(str(exc), "error")
    return redirect(url_for("finance.claim_show", claim_id=claim_id))


@bp.post("/claims/<int:claim_id>/submit")
def claim_submit(claim_id):
    return _claim_action(claim_id, lambda: INS.submit(get_db(), g.clinic, g.user["id"], claim_id, _opt_date("sent_on"),
                                                      request.form.get("payer_ref")), "Marked as sent.")


@bp.post("/claims/<int:claim_id>/reject")
def claim_reject(claim_id):
    return _claim_action(claim_id, lambda: INS.reject(get_db(), g.clinic, g.user["id"], claim_id, request.form.get("reason")),
                         "Marked as rejected. You can send it again after fixing it.")


@bp.post("/claims/<int:claim_id>/approve")
def claim_approve(claim_id):
    return _claim_action(claim_id, lambda: INS.approve(get_db(), g.clinic, g.user["id"], claim_id,
                                                       money.parse_money(request.form.get("approved", ""), g.clinic["currency"], allow_zero=True),
                                                       request.form.get("payer_ref")), "Approval recorded.")


@bp.post("/claims/<int:claim_id>/cancel")
def claim_cancel(claim_id):
    return _claim_action(claim_id, lambda: INS.cancel(get_db(), g.clinic, g.user["id"], claim_id), "Claim cancelled.")


@bp.post("/claims/<int:claim_id>/payment")
def claim_payment(claim_id):
    def go():
        wo = _opt_amount("writeoff")
        INS.record_payer_payment(get_db(), g.clinic, g.user["id"], claim_id,
                                 money.parse_money(request.form.get("amount", ""), g.clinic["currency"]),
                                 request.form.get("reference"), wo or 0, request.form.get("writeoff_note"),
                                 final=bool(request.form.get("final")))
    return _claim_action(claim_id, go, "Payment from the payer recorded on the patient's account.")


# ----------------------------------------------------------------------------- invoices and statements
@bp.route("/patients/<int:pid>/invoices", methods=["GET", "POST"])
def patient_invoices(pid):
    db, cid = get_db(), g.clinic["id"]
    patient = _patient_or_404(pid)
    if request.method == "POST":
        try:
            inv = INV.create(db, g.clinic, g.user["id"], pid, [x for x in request.form.getlist("procedure_id") if x.isdigit()],
                             request.form.get("note"))
            flash("Invoice issued.", "ok")
            return redirect(url_for("finance.invoice_show", invoice_id=inv))
        except INV.InvoiceError as exc:
            db.rollback()
            flash(str(exc), "error")
        return redirect(url_for("finance.patient_invoices", pid=pid))
    return render_template("invoices.html", p=patient, rows=INV.for_patient(db, cid, pid), open_work=INV.uninvoiced(db, cid, pid))


@bp.get("/invoices/<int:invoice_id>")
def invoice_show(invoice_id):
    db, cid = get_db(), g.clinic["id"]
    inv = INV.get(db, cid, invoice_id)
    if not inv:
        abort(404)
    _document_for(inv["patient_id"])
    return render_template("invoice.html", inv=inv, lines=INV.lines(db, cid, invoice_id), tax_rows=INV.tax_lines(db, cid, invoice_id))


@bp.post("/invoices/<int:invoice_id>/void")
@manager_required
def invoice_void(invoice_id):
    db = get_db()
    try:
        INV.void(db, g.clinic, g.user["id"], invoice_id, request.form.get("reason"))
        flash("Invoice voided. The number stays used and the record is kept.", "ok")
    except INV.InvoiceError as exc:
        db.rollback()
        flash(str(exc), "error")
    except LookupError:
        abort(404)
    return redirect(url_for("finance.invoice_show", invoice_id=invoice_id))


@bp.get("/patients/<int:pid>/statement")
def statement(pid):
    db, cid = get_db(), g.clinic["id"]
    patient = _document_for(pid)
    return render_template("statement.html", p=patient, events=billing.statement(db, cid, pid, g.doc_lang), t=billing.totals(db, cid, pid),
                           ageing=ageing.patient_ageing(db, cid, pid, _today()), insurers=ageing.insurers_owe(db, cid, pid),
                           today=_today())


@bp.get("/reports/ageing")
@manager_required
def ageing_report():
    return render_template("ageing.html", r=ageing.report(get_db(), g.clinic), awaiting=INS.awaiting_total(get_db(), g.clinic["id"]))
