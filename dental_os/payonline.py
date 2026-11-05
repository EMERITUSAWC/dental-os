"""Online payments (version 0.20). Staff screens to set up the clinic's payment company and send pay links, and the public pages a
patient opens: the pay page, the page they come back to, and the address the payment company calls."""
from flask import Blueprint, abort, current_app, flash, g, redirect, render_template, request, url_for

from .auth import manager_required
from .db import get_db
from .services import i18n, links as LK, onlinepay as OP, patients as P, qr, throttle

bp = Blueprint("payments", __name__)
pub = Blueprint("paypub", __name__)


def _base():
    return current_app.config.get("PUBLIC_URL") or request.url_root


def _secret():
    return current_app.config["SECRET_KEY"]


# --------------------------------------------------------------------------------------------------------- staff screens
@bp.route("/settings/online-payments", methods=["GET", "POST"])
@manager_required
def setup():
    db = get_db()
    if request.method == "POST":
        try:
            if request.form.get("action") == "check":
                flash(OP.check(db, g.clinic, _secret()), "ok")
            elif request.form.get("action") == "card_fee":            # 0.74: optional fee on credit cards at the desk
                from .services import cardfee
                try:
                    cardfee.set_fee(db, g.clinic, g.user["id"], cardfee.parse_pct(request.form.get("card_fee")))
                except cardfee.CardFeeError as exc:
                    db.rollback()
                    flash(str(exc), "error")
                    return redirect(url_for("payments.setup"))
                db.commit()
                flash("Saved.", "ok")
            elif request.form.get("action") == "allow_save":
                OP.set_allow_save(db, g.clinic, g.user["id"], request.form.get("allow_save") == "1")
                flash("Saved.", "ok")
            else:
                OP.save_account(db, g.clinic, g.user["id"], _secret(), request.form.get("provider"), request.form.get("enabled") == "1",
                                request.form.get("api_key"), request.form.get("webhook_secret"), request.form.get("new_hook") == "1")
                flash("Saved.", "ok")
        except OP.PayError as exc:
            if request.form.get("action") != "check":
                db.rollback()
            flash(str(exc), "error")
        return redirect(url_for("payments.setup"))
    a = OP.account(db, g.clinic["id"])
    hook = (_base().rstrip("/") + url_for("paypub.hook", key=a["hook_key"])) if a else None
    return render_template("pay_setup.html", a=a, providers=OP.PROVIDERS, hook=hook, test=OP.test_mode(a),
                           paystack_ok=g.clinic["currency"] in OP.PAYSTACK_CURRENCIES,
                           card_fee_allowed=_cardfee_ok())


@bp.route("/patients/<int:pid>/pay-links", methods=["GET", "POST"])
def patient_links(pid):
    db = get_db()
    p = P.get(db, g.clinic["id"], pid)
    if not p:
        abort(404)
    if request.method == "POST":
        from .services import money
        try:
            raw = (request.form.get("amount") or "").strip()
            amount = money.parse_money(raw, g.clinic["currency"]) if raw else 0
            ref = OP.create_link(db, g.clinic, g.user["id"], _secret(), pid, amount, request.form.get("description"), _base(),
                                 "membership" if request.form.get("purpose") == "membership" else "treatment")
            flash("Pay link made. Send it to the patient.", "ok")
            return redirect(url_for("payments.link_show", ref=ref))
        except (OP.PayError, ValueError) as exc:
            db.rollback()
            flash(str(exc), "error")
        return redirect(url_for("payments.patient_links", pid=pid))
    from .services import billing, savedcards as SC
    OP._expire(db, g.clinic)
    return render_template("pay_patient.html", p=p, rows=OP.links_for(db, g.clinic["id"], pid), on=OP.is_on(db, g.clinic),
                           balance=billing.totals(db, g.clinic["id"], pid)["balance"], statuses=OP.STATUSES,
                           cards=SC.cards_for(db, g.clinic["id"], pid), attempts=SC.attempts(db, g.clinic["id"], pid),
                           cards_on=SC.allowed(db, g.clinic), today=SC._today(g.clinic))


@bp.post("/patients/<int:pid>/cards/<int:card_id>/<action>")
def card_action(pid, card_id, action):
    from .services import money, savedcards as SC
    db = get_db()
    card = db.execute("SELECT * FROM saved_cards WHERE id = ? AND clinic_id = ? AND patient_id = ?", (card_id, g.clinic["id"], pid)).fetchone()
    if not card:
        abort(404)
    try:
        if action == "charge":
            amount = money.parse_money((request.form.get("amount") or "").strip(), g.clinic["currency"])
            if request.form.get("told") != "1":
                raise SC.CardError("Tick that the patient was told about this charge first.")
            SC.charge(db, g.clinic, g.user["id"], _secret(), card_id, amount, "treatment", (request.form.get("what") or "").strip() or None)
            flash("Card charged. The payment is on the patient's account.", "ok")
        elif action == "remove":
            SC.remove(db, g.clinic, g.user["id"], card_id, request.form.get("reason"))
            flash("Card removed. It can no longer be charged.", "ok")
        elif action == "auto_off":
            SC.set_auto(db, g.clinic, g.user["id"], card_id, False)
            flash("Automatic charges stopped for this card.", "ok")
        else:
            abort(404)
    except (SC.CardError, OP.PayError, ValueError) as exc:
        db.commit() if action == "charge" else db.rollback()     # a failed charge attempt is kept on record
        flash(str(exc), "error")
    return redirect(url_for("payments.patient_links", pid=pid))


@bp.get("/pay-links/<ref>")
def link_show(ref):
    db = get_db()
    link = OP.get_link(db, ref)
    if not link or link["clinic_id"] != g.clinic["id"]:
        abort(404)
    url = _base().rstrip("/") + url_for("paypub.pay_page", ref=ref)
    from .services import messaging, money
    patient = P.get(db, g.clinic["id"], link["patient_id"])
    text = i18n.t(i18n.patient_lang(patient, g.clinic), "Hello %(name)s, here is the link to pay %(amount)s to %(clinic)s: %(url)s",
                  name=link["first_name"], amount=money.fmt(link["amount_p"], link["currency"]), clinic=g.clinic["name"], url=url)
    wa = messaging.wa_link(patient["phone"], text) if patient["phone"] else None
    return render_template("pay_link.html", link=link, url=url, text=text, wa=wa, qr_svg=qr.svg(url, 200, "QR code for the pay link"),
                           statuses=OP.STATUSES)


@bp.post("/pay-links/<ref>/<action>")
def link_action(ref, action):
    db = get_db()
    link = OP.get_link(db, ref)
    if not link or link["clinic_id"] != g.clinic["id"]:
        abort(404)
    try:
        if action == "cancel":
            OP.cancel_link(db, g.clinic, g.user["id"], ref)
            flash("Pay link cancelled.", "ok")
        elif action == "check":
            status = OP.confirm(db, g.clinic, _secret(), ref)
            flash("Paid. The payment is on the patient's account." if status == "paid" else "Not paid yet.", "ok")
        else:
            abort(404)
    except OP.PayError as exc:
        db.rollback()
        flash(str(exc), "error")
    return redirect(url_for("payments.link_show", ref=ref))


@bp.get("/pay-links")
def links_list():
    db = get_db()
    OP._expire(db, g.clinic)
    status = request.args.get("status") or None
    return render_template("pay_links.html", rows=OP.links_for(db, g.clinic["id"], status=status if status in OP.STATUSES else None),
                           statuses=OP.STATUSES, status=status)


# ---------------------------------------------------------------------------------------------------------- public pages
@pub.before_request
def _anonymous():
    g.user = None


@pub.after_request
def _headers(resp):
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["X-Robots-Tag"] = "noindex, nofollow"
    return resp


def _public_link(ref):
    db = get_db()
    bucket = "pay-bad:" + throttle.key(_secret(), request.remote_addr or "unknown")
    if throttle.count(db, bucket, 60) >= 30:
        abort(404)
    link = OP.get_link(db, ref) if ref.startswith("dos_") else None
    if not link:
        throttle.record(db, bucket)
        db.commit()
        abort(404)
    clinic = db.execute("SELECT * FROM clinics WHERE id = ?", (link["clinic_id"],)).fetchone()
    g.clinic = clinic
    g.lang = i18n.pick(request.args.get("lang")) or i18n.patient_lang({"language": link["patient_language"]}, clinic)
    g.doc_lang = g.lang
    return db, link, clinic


@pub.get("/pay/<ref>")
def pay_page(ref):
    db, link, clinic = _public_link(ref)
    OP._expire(db, clinic)
    link = OP.get_link(db, ref)
    from .services import savedcards as SC
    keep = SC.allowed(db, clinic)
    return render_template("pay_public.html", link=link, statuses=OP.STATUSES, keep=keep,
                           consent_save=SC.CONSENT_SAVE, consent_auto=SC.CONSENT_AUTO)


@pub.post("/pay/<ref>/keep")
def pay_keep(ref):
    """The patient chose to keep their card. Their own consent, then on to the payment company."""
    from .services import savedcards as SC
    db, link, clinic = _public_link(ref)
    if link["status"] != "open":
        return redirect(url_for("paypub.pay_page", ref=ref))
    if request.form.get("save") != "1":
        return redirect(link["checkout_url"])
    try:
        url = SC.prepare(db, clinic, _secret(), ref, request.form.get("auto") == "1", _base(), lang=g.lang)
        db.commit()
        return redirect(url)
    except (SC.CardError, OP.PayError):
        db.rollback()
        return redirect(link["checkout_url"])


@pub.get("/pay/<ref>/go")
def pay_go(ref):
    db, link, clinic = _public_link(ref)
    if link["status"] != "open":
        return redirect(url_for("paypub.pay_page", ref=ref))
    return redirect(link["checkout_url"])


@pub.get("/pay/<ref>/done")
def pay_done(ref):
    db, link, clinic = _public_link(ref)
    status = link["status"]
    if status == "open":
        try:
            status = OP.confirm(db, clinic, _secret(), ref)
            db.commit()
        except OP.PayError:
            db.rollback()
            status = "unknown"
    return render_template("pay_public.html", link=OP.get_link(db, ref), statuses=OP.STATUSES, came_back=True, result=status)


@pub.get("/p/<token>/pay")
def portal_pay(token):
    """From the patient's own link: pay the whole balance."""
    db = get_db()
    found = LK.resolve(db, token)
    if not found:
        abort(404)
    _link, patient, clinic = found
    g.clinic = clinic
    from .services import billing
    bal = billing.totals(db, clinic["id"], patient["id"])["balance"]
    bucket = "portal-pay:" + throttle.key(_secret(), token)
    if bal <= 0 or not OP.is_on(db, clinic) or not throttle.allow(db, bucket, 5, 60):
        db.commit()
        return redirect(url_for("public.portal", token=token))
    try:
        ref = OP.create_link(db, clinic, None, _secret(), patient["id"], bal, f"{clinic['name']}: balance", _base())
        db.commit()
    except OP.PayError:
        db.rollback()
        return redirect(url_for("public.portal", token=token))
    return redirect(OP.get_link(db, ref)["checkout_url"])


@pub.post("/hooks/pay/<key>")
def hook(key):
    db = get_db()
    acct = OP.account_for_hook(db, key)
    if not acct:
        abort(404)
    request.max_content_length = OP.MAX_BODY
    raw = request.get_data(cache=False)
    if len(raw) > OP.MAX_BODY:
        abort(413)
    try:
        result = OP.handle_hook(db, acct, raw, request.headers, _secret())
    except OP.PayError as exc:
        db.rollback()
        if str(exc) == "bad signature":
            abort(401)
        return "noted", 200, {"Content-Type": "text/plain; charset=utf-8"}         # a payment that does not match is flagged on the link
    except ValueError:
        abort(400)
    return result, 200, {"Content-Type": "text/plain; charset=utf-8"}


def _cardfee_ok():
    from .services import cardfee
    return cardfee.allowed(g.clinic)
