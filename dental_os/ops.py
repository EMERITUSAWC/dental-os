"""v0.8 screens: KPI dashboard, group view, lab cases, stock, and the API and webhook settings."""
import csv
import io
from datetime import timedelta

from flask import Blueprint, Response, abort, current_app, flash, g, redirect, render_template, request, url_for

from .auth import clinical_required, manager_required
from .db import get_db
from .services import dataio, apikeys, kpi, labs as LAB, orgs, scheduling, stock as STK, webhooks as WH
from .services import money
from .services.timeutil import parse_date
from .views import _patient_or_404, _today

bp = Blueprint("ops", __name__)


def _amount(name):
    raw = (request.form.get(name) or "").strip()
    return money.parse_money(raw, g.clinic["currency"], allow_zero=True) if raw else 0


def _int(name, default=0):
    raw = (request.form.get(name) or "").strip()
    if not raw:
        return default
    if not raw.lstrip("-").isdigit():
        raise ValueError("Use whole numbers only.")
    return int(raw)


def _range(today):
    try:
        end = parse_date(request.args["end"]) if request.args.get("end") else today
        start = parse_date(request.args["start"]) if request.args.get("start") else today.replace(day=1)
    except ValueError:
        end, start = today, today.replace(day=1)
    return kpi.clamp_range(start, end)


# ----------------------------------------------------------------------------- KPI dashboard
@bp.get("/reports/kpi")
@manager_required
def kpi_view():
    today = _today()
    start, end = _range(today)
    return render_template("kpi.html", d=kpi.dashboard(get_db(), g.clinic, start, end, today), presets=kpi.presets(today),
                           org=orgs.for_user(get_db(), g.user))


@bp.get("/reports/kpi.csv")
@manager_required
def kpi_csv():
    today = _today()
    start, end = _range(today)
    buf = io.StringIO()
    csv.writer(buf).writerows([[dataio._safe_cell(c) for c in row]
                               for row in kpi.csv_rows(get_db(), g.clinic, start, end)])
    return Response(buf.getvalue(), mimetype="text/csv", headers={"Content-Disposition": f'attachment; filename="kpi-{start}-to-{end}.csv"'})


# ----------------------------------------------------------------------------- group view
@bp.get("/group")
@manager_required
def group_view():
    db = get_db()
    org = orgs.for_user(db, g.user)
    if not org:
        abort(404)
    today = _today()
    start, end = _range(today)
    return render_template("group.html", org=org, d=orgs.compare(db, org, start, end, viewer=g.user), presets=kpi.presets(today))


# ----------------------------------------------------------------------------- lab cases
def _lab_form_values():
    return dict(lab_id=request.form.get("lab_id", type=int), kind=request.form.get("kind"), tooth=request.form.get("tooth"),
                shade=request.form.get("shade"), instructions=request.form.get("instructions"), due_on=request.form.get("due_on"),
                cost_p=_amount("cost"))


@bp.get("/lab")
def lab_board():
    return render_template("lab_board.html", b=LAB.board(get_db(), g.clinic), labels=LAB.LABELS)


@bp.route("/lab/new", methods=["GET", "POST"])
def lab_new():
    db, cid = get_db(), g.clinic["id"]
    pid = request.values.get("patient_id", type=int)
    patient = _patient_or_404(pid) if pid else None
    if not patient:
        flash("Open a patient first, then choose New lab case on their Lab tab.", "error")
        return redirect(url_for("views.patients"))
    if request.method == "POST":
        try:
            vals = _lab_form_values()
            case_id = LAB.create(db, g.clinic, g.user["id"], pid, procedure_id=request.form.get("procedure_id", type=int),
                                 provider_id=request.form.get("provider_id", type=int), **vals)
            flash("Lab case saved. Send it when it is ready to go.", "ok")
            return redirect(url_for("ops.lab_case", case_id=case_id))
        except (LAB.LabError, ValueError) as exc:
            db.rollback()
            flash(str(exc), "error")
    procs = db.execute("""SELECT pr.id, pr.tooth, c.name FROM procedures pr JOIN procedure_codes c ON c.id = pr.code_id
                          WHERE pr.clinic_id = ? AND pr.patient_id = ? AND pr.status IN ('planned','done') ORDER BY pr.id DESC LIMIT 40""",
                       (cid, pid)).fetchall()
    return render_template("lab_new.html", p=patient, labs=LAB.list_labs(db, cid, True), kinds=LAB.KINDS, procs=procs,
                           providers=db.execute("SELECT * FROM providers WHERE clinic_id = ? AND active = 1", (cid,)).fetchall(),
                           f=request.form)


@bp.get("/patients/<int:pid>/lab")
def patient_lab(pid):
    patient = _patient_or_404(pid)
    return render_template("lab_patient.html", p=patient, rows=LAB.for_patient(get_db(), g.clinic["id"], pid), labels=LAB.LABELS,
                           today=_today().isoformat())


@bp.get("/lab/<int:case_id>")
def lab_case(case_id):
    db, cid = get_db(), g.clinic["id"]
    case = LAB.get(db, cid, case_id)
    if not case:
        abort(404)
    visits = db.execute("""SELECT id, start_at FROM appointments WHERE clinic_id = ? AND patient_id = ? AND status = 'scheduled' ORDER BY start_at""",
                        (cid, case["patient_id"])).fetchall()
    return render_template("lab_case.html", c=case, events=LAB.events(db, cid, case_id), labels=LAB.LABELS, today=_today(),
                           labs=LAB.list_labs(db, cid, True), kinds=LAB.KINDS, visits=visits,
                           providers=db.execute("SELECT * FROM providers WHERE clinic_id = ? AND active = 1", (cid,)).fetchall(),
                           remade=db.execute("SELECT id FROM lab_cases WHERE clinic_id = ? AND remake_of = ?", (cid, case_id)).fetchall())


@bp.get("/lab/<int:case_id>/sheet")
def lab_sheet(case_id):
    case = LAB.get(get_db(), g.clinic["id"], case_id)
    if not case:
        abort(404)
    return render_template("lab_sheet.html", c=case, today=_today())


@bp.post("/lab/<int:case_id>/<action>")
def lab_action(case_id, action):
    db, cid, uid = get_db(), g.clinic["id"], g.user["id"]
    if not LAB.get(db, cid, case_id):
        abort(404)
    try:
        if action == "edit":
            LAB.update(db, g.clinic, uid, case_id, provider_id=request.form.get("provider_id", type=int), **_lab_form_values())
            flash("Saved.", "ok")
        elif action == "send":
            LAB.send(db, g.clinic, uid, case_id, request.form.get("sent_on"), request.form.get("due_on"))
            flash("Marked as sent to the lab.", "ok")
        elif action == "receive":
            LAB.receive(db, g.clinic, uid, case_id, request.form.get("received_on"))
            flash("Marked as back from the lab.", "ok")
        elif action == "fit":
            LAB.fit(db, g.clinic, uid, case_id, request.form.get("fitted_on"))
            flash("Marked as fitted.", "ok")
        elif action == "cancel":
            LAB.cancel(db, g.clinic, uid, case_id, request.form.get("reason"))
            flash("Case cancelled. The record is kept.", "ok")
        elif action == "remake":
            new_id = LAB.remake(db, g.clinic, uid, case_id, request.form.get("reason"), _amount("cost"))
            flash("A new case was made for the remake.", "ok")
            return redirect(url_for("ops.lab_case", case_id=new_id))
        elif action == "visit":
            LAB.link_fit_visit(db, g.clinic, uid, case_id, request.form.get("appointment_id", type=int))
            flash("Saved.", "ok")
        else:
            abort(404)
    except (LAB.LabError, ValueError) as exc:
        db.rollback()
        flash(str(exc), "error")
    return redirect(url_for("ops.lab_case", case_id=case_id))


@bp.route("/lab/labs", methods=["GET", "POST"])
@manager_required
def lab_labs():
    db, cid = get_db(), g.clinic["id"]
    if request.method == "POST":
        try:
            LAB.save_lab(db, g.clinic, g.user["id"], request.form.get("lab_id", type=int), request.form.get("name"),
                         request.form.get("phone"), request.form.get("email"), request.form.get("notes"), bool(request.form.get("active")))
            flash("Saved.", "ok")
        except LAB.LabError as exc:
            db.rollback()
            flash(str(exc), "error")
        except LookupError:
            abort(404)
        return redirect(url_for("ops.lab_labs"))
    today = _today()
    start, end = today - timedelta(days=179), today
    return render_template("lab_labs.html", rows=LAB.list_labs(db, cid), perf=LAB.performance(db, cid, start, end), start=start, end=end)


# ----------------------------------------------------------------------------- stock
@bp.get("/stock")
@clinical_required
def stock_list():
    db = get_db()
    q = (request.args.get("q") or "").strip()[:60]
    cat = request.args.get("category") or None
    return render_template("stock.html", rows=STK.items(db, g.clinic, q or None, include_off=request.args.get("off") == "1", category=cat),
                           q=q, category=cat, categories=STK.CATEGORIES, summary=STK.summary(db, g.clinic), off=request.args.get("off") == "1")


def _item_or_404(item_id):
    item = STK.get_item(get_db(), g.clinic["id"], item_id)
    if not item:
        abort(404)
    return item


@bp.get("/stock/item/<int:item_id>")
@clinical_required
def stock_item(item_id):
    db, cid = get_db(), g.clinic["id"]
    item = _item_or_404(item_id)
    bs = STK.batches(db, cid, item_id)
    return render_template("stock_item.html", i=item, level=sum(b["qty"] for b in bs), batches=bs, history=STK.history(db, cid, item_id),
                           kinds=STK.KINDS, today=_today().isoformat())


@bp.post("/stock/item/<int:item_id>/move")
@clinical_required
def stock_move(item_id):
    db = get_db()
    _item_or_404(item_id)
    try:
        kind = request.form.get("kind")
        qty = _int("qty", None)
        if qty is None:
            raise STK.StockError("Enter a number.")
        cost = _amount("unit_cost") if (request.form.get("unit_cost") or "").strip() else None
        made = STK.move(db, g.clinic, g.user["id"], item_id, kind, qty, cost, (request.form.get("expires_on") or "").strip() or None,
                        request.form.get("note"), (request.form.get("moved_on") or "").strip() or None)
        flash("Recorded." if made else "The count matched. Nothing changed.", "ok")
    except (STK.StockError, ValueError) as exc:
        db.rollback()
        flash(str(exc), "error")
    return redirect(url_for("ops.stock_item", item_id=item_id))


@bp.route("/stock/items/new", methods=["GET", "POST"])
@manager_required
def stock_new():
    return _stock_form(None)


@bp.route("/stock/item/<int:item_id>/edit", methods=["GET", "POST"])
@manager_required
def stock_edit(item_id):
    return _stock_form(_item_or_404(item_id))


def _stock_form(item):
    db, cid = get_db(), g.clinic["id"]
    if request.method == "POST":
        try:
            item_id = STK.save_item(db, g.clinic, g.user["id"], item["id"] if item else None, request.form.get("name"), request.form.get("sku"),
                                    request.form.get("unit"), request.form.get("category"), request.form.get("supplier_id", type=int),
                                    _int("reorder_level"), _int("reorder_qty"), _amount("cost"), request.form.get("location"),
                                    bool(request.form.get("active")) if item else True)
            flash("Saved.", "ok")
            return redirect(url_for("ops.stock_item", item_id=item_id))
        except (STK.StockError, ValueError) as exc:
            db.rollback()
            flash(str(exc), "error")
    return render_template("stock_form.html", i=item, f=request.form, suppliers=STK.suppliers(db, cid, True), units=STK.UNITS,
                           categories=STK.CATEGORIES)


@bp.get("/stock/order")
@clinical_required
def stock_order():
    return render_template("stock_order.html", groups=STK.to_order(get_db(), g.clinic), today=_today())


@bp.get("/stock/expiry")
@clinical_required
def stock_expiry():
    return render_template("stock_expiry.html", rows=STK.expiring(get_db(), g.clinic, 90))


@bp.route("/stock/suppliers", methods=["GET", "POST"])
@manager_required
def stock_suppliers():
    db, cid = get_db(), g.clinic["id"]
    if request.method == "POST":
        try:
            STK.save_supplier(db, g.clinic, g.user["id"], request.form.get("supplier_id", type=int), request.form.get("name"),
                              request.form.get("phone"), request.form.get("email"), request.form.get("notes"), bool(request.form.get("active")))
            flash("Saved.", "ok")
        except STK.StockError as exc:
            db.rollback()
            flash(str(exc), "error")
        except LookupError:
            abort(404)
        return redirect(url_for("ops.stock_suppliers"))
    return render_template("stock_suppliers.html", rows=STK.suppliers(db, cid))


# ----------------------------------------------------------------------------- API keys and webhooks
def _integrations_page(**extra):
    db, cid = get_db(), g.clinic["id"]
    return render_template("integrations.html", keys=apikeys.list_keys(db, cid), hooks=WH.endpoints(db, cid), events=WH.EVENTS,
                           log=WH.deliveries(db, cid), base=request.url_root.rstrip("/") + "/api/v1", **extra)


@bp.get("/integrations")
@manager_required
def integrations():
    return _integrations_page()


@bp.post("/integrations")
@manager_required
def integrations_post():
    db, uid, action = get_db(), g.user["id"], request.form.get("action")
    allow_private = bool(current_app.config.get("WEBHOOKS_ALLOW_PRIVATE"))
    try:
        if action == "key_create":
            _, key = apikeys.create(db, g.clinic, uid, request.form.get("name"), request.form.get("access"))
            return _integrations_page(new_key=key)
        if action == "key_revoke":
            apikeys.revoke(db, g.clinic, uid, request.form.get("key_id", type=int))
            flash("Key switched off. Programs using it stop working at once.", "ok")
        elif action == "hook_add":
            _, secret = WH.add_endpoint(db, g.clinic, uid, request.form.get("url"), request.form.getlist("events"), request.form.get("note"),
                                        allow_private)
            return _integrations_page(new_secret=secret)
        elif action in ("hook_on", "hook_off"):
            WH.set_active(db, g.clinic, uid, request.form.get("hook_id", type=int), action == "hook_on")
            flash("Saved.", "ok")
        elif action == "hook_secret":
            secret = WH.rotate_secret(db, g.clinic, uid, request.form.get("hook_id", type=int))
            return _integrations_page(new_secret=secret)
        elif action == "hook_test":
            WH.send_test(db, g.clinic, uid, request.form.get("hook_id", type=int))
            flash("A test message is queued. It goes out within a minute or two.", "ok")
        elif action == "hook_retry":
            WH.retry(db, g.clinic, uid, request.form.get("delivery_id", type=int))
            flash("Queued to try again.", "ok")
        else:
            abort(400)
    except (apikeys.ApiKeyError, WH.WebhookError) as exc:
        db.rollback()
        flash(str(exc), "error")
    except LookupError:
        abort(404)
    return redirect(url_for("ops.integrations"))


@bp.get("/integrations/docs")
@manager_required
def api_docs():
    return render_template("api_docs.html", base=request.url_root.rstrip("/") + "/api/v1", events=WH.EVENTS,
                           categories=scheduling.CATEGORIES)
