"""Messages (send list, replies, campaigns, templates), online booking requests, health forms to review, and each patient's link."""
from datetime import datetime

from flask import Blueprint, abort, current_app, flash, g, redirect, render_template, request, url_for

from .auth import clinical_required, manager_required
from .db import get_db
from .services import mailer as _mail, sms as _sms
from .services import (booking, campaigns as CAMP, i18n, inbox, links, msgtemplates as MT, outbox, scheduling,
                       timeutil, whatsapp as WA)
from .services.messaging import wa_link
from .views import _patient_or_404, _today

bp = Blueprint("comms", __name__)


def _guard(fn):
    """Run a service call, turn its errors into a flash message, roll back on failure. Returns True on success."""
    db = get_db()
    try:
        fn()
        return True
    except (outbox.MessageError, inbox.InboxError, booking.BookingError, links.LinkError, CAMP.CampaignError, MT.TemplateError,
            scheduling.SchedulingError, WA.WhatsAppError, ValueError) as exc:
        db.rollback()
        flash(str(exc), "error")
    return False


# ----------------------------------------------------------------------------- send list
@bp.get("/messages")
def queue():
    db = get_db()
    built = outbox.build(db, g.clinic)
    outbox.refresh(db, g.clinic)
    return render_template("queue.html", rows=outbox.queue(db, g.clinic), built=built, api_on=WA.is_on(db, g.clinic["id"], current_app.config["SECRET_KEY"]),
                           auto_on=bool((WA.row(db, g.clinic["id"]) or {"auto_send": 0})["auto_send"]), counts=outbox.counts(db, g.clinic["id"]),
                           kinds=outbox.KINDS, attn=outbox.attention(db, g.clinic, g.user), active="queue",
                           mail_on=_mail.is_on(db, g.clinic["id"]), sms_on=_sms.is_on(db, g.clinic["id"]))


@bp.post("/messages/queue/<int:oid>/email")
def queue_email(oid):
    """Send one message by email."""
    db = get_db()
    try:
        _mail.deliver_outbox(db, g.clinic, g.user["id"], current_app.config["SECRET_KEY"], oid)
        flash("Sent by email.", "ok")
    except LookupError:
        abort(404)
    except outbox.MessageError as exc:
        db.rollback()
        flash(str(exc), "error")
    return redirect(url_for("comms.queue"))


@bp.post("/messages/queue/email-all")
def queue_email_all():
    db = get_db()
    if not _mail.is_on(db, g.clinic["id"]):
        flash("Email is not switched on.", "error")
        return redirect(url_for("comms.queue"))
    res = _mail.send_ready(db, g.clinic, g.user["id"], current_app.config["SECRET_KEY"])
    flash(_res_text(res), "ok" if not res["stopped"] else "error")
    return redirect(url_for("comms.queue"))


def _res_text(res):
    if res["stopped"]:
        return res["stopped"]
    return "Emails sent: %d. Left for a person: %d." % (res["sent"], res["left"])


@bp.post("/messages/queue/<int:oid>/open")
def queue_open(oid):
    db = get_db()
    try:
        url = outbox.send(db, g.clinic, g.user["id"], oid)
    except LookupError:
        abort(404)
    except (outbox.MessageError,) as exc:
        db.rollback()
        flash(str(exc), "error")
        return redirect(url_for("comms.queue"))
    db.commit()
    return redirect(url)


@bp.post("/messages/queue/<int:oid>/send")
def queue_send(oid):
    """Send one message through the WhatsApp Business API."""
    db = get_db()
    try:
        WA.deliver_outbox(db, g.clinic, g.user["id"], current_app.config["SECRET_KEY"], oid)
        flash("Sent by WhatsApp. Delivery news will show on the patient's messages.", "ok")
    except LookupError:
        abort(404)
    except outbox.MessageError as exc:
        db.rollback()
        flash(str(exc), "error")
    return redirect(url_for("comms.queue"))


@bp.post("/messages/queue/send-all")
def queue_send_all():
    db = get_db()
    if not WA.is_on(db, g.clinic["id"], current_app.config["SECRET_KEY"]):
        flash("The WhatsApp API is not switched on.", "error")
        return redirect(url_for("comms.queue"))
    res = WA.send_ready(db, g.clinic, g.user["id"], current_app.config["SECRET_KEY"])
    msg = f"{res['sent']} sent by WhatsApp. {res['left']} left in the list for a person."
    flash(msg + (" Stopped: " + res["stopped"] if res["stopped"] else ""), "error" if res["stopped"] else "ok")
    return redirect(url_for("comms.queue"))


@bp.post("/messages/queue/<int:oid>/skip")
def queue_skip(oid):
    db = get_db()
    try:
        outbox.skip(db, g.clinic, g.user["id"], oid, request.form.get("reason"))
        flash("Skipped.", "ok")
    except LookupError:
        abort(404)
    except outbox.MessageError as exc:
        db.rollback()
        flash(str(exc), "error")
    return redirect(url_for("comms.queue"))


# ----------------------------------------------------------------------------- replies
@bp.get("/messages/replies")
def replies():
    db = get_db()
    from .services import aidesk
    rows = inbox.unread(db, g.clinic["id"])
    return render_template("replies.html", rows=rows, attn=outbox.attention(db, g.clinic, g.user), active="replies",
                           sorted_as={r["patient_id"]: aidesk.classify(r["last_body"])[0] for r in rows}, intents=aidesk.INTENTS)


@bp.route("/patients/<int:pid>/messages", methods=["GET", "POST"])
def patient_messages(pid):
    db, cid = get_db(), g.clinic["id"]
    patient = _patient_or_404(pid)
    new_link = None
    if request.method == "POST":
        action = request.form.get("action")
        if action == "log_reply":
            ok = _guard(lambda: inbox.log_inbound(db, g.clinic, g.user["id"], pid, request.form.get("body"), bool(request.form.get("confirms"))))
            if ok:
                flash("Reply saved.", "ok")
        elif action == "read":
            inbox.mark_read(db, cid, pid)
        elif action == "send":
            try:
                url = inbox.reply(db, g.clinic, g.user["id"], pid, request.form.get("text"))
                if request.form.get("ai_draft") and request.form.get("ai_draft") == request.form.get("text"):
                    from .services import aidesk
                    aidesk.used(db, g.clinic, g.user["id"], "reply draft, sent as written", pid)
                elif request.form.get("ai_draft"):
                    from .services import aidesk
                    aidesk.used(db, g.clinic, g.user["id"], "reply draft, edited", pid)
                db.commit()
                return redirect(url)
            except inbox.InboxError as exc:
                db.rollback()
                flash(str(exc), "error")
        elif action == "link_create":
            try:
                token = links.create(db, g.clinic, g.user["id"], pid)
                db.commit()
                new_link = url_for("public.portal", token=token, _external=True)
            except links.LinkError as exc:
                db.rollback()
                flash(str(exc), "error")
        elif action == "link_send":
            token = request.form.get("token", "")
            check = links.resolve(db, token)
            if not check or check[1]["id"] != pid:
                flash("That link is not valid. Make a new one.", "error")
            elif not patient["reminders_ok"]:
                flash("This patient has not agreed to messages. Give them the link in person instead.", "error")
            else:
                url = url_for("public.portal", token=token, _external=True)
                body = MT.render(MT.body_for(db, cid, "portal_link", i18n.patient_lang(patient, g.clinic)), {"first_name": patient["first_name"], "clinic": g.clinic["name"], "link": url})
                logged = body.replace(url, "[private link]")
                db.execute("INSERT INTO messages(clinic_id, patient_id, kind, body, status, created_by) VALUES (?,?, 'portal_link', ?, 'opened', ?)",
                           (cid, pid, logged, g.user["id"]))
                db.commit()
                return redirect(wa_link(patient["phone"], body))
        elif action == "link_revoke":
            links.revoke(db, g.clinic, g.user["id"], pid)
            flash("The link no longer works.", "ok")
        else:
            abort(400)
        if new_link is None:
            return redirect(url_for("comms.patient_messages", pid=pid))
    from .services import aidesk
    ai = aidesk.read_replies(db, g.clinic, patient) if request.method == "GET" else None   # 0.75: read before marking as read
    inbox.mark_read(db, cid, pid) if request.method == "GET" else None
    cur = links.current(db, g.clinic, pid)
    return render_template("patient_messages.html", p=patient, thread=inbox.thread(db, cid, pid), link=cur, new_link=new_link,
                           token=(new_link.rsplit("/", 1)[1] if new_link else None), yes=inbox.looks_like_yes, ai=ai, intents=aidesk.INTENTS)


# ----------------------------------------------------------------------------- online booking requests
@bp.get("/messages/requests")
def requests_list():
    db = get_db()
    status = request.args.get("status", "new")
    return render_template("requests.html", rows=booking.requests(db, g.clinic["id"], status), status=status,
                           attn=outbox.attention(db, g.clinic, g.user), active="requests")


@bp.get("/messages/requests/<int:rid>")
def request_show(rid):
    db, cid = get_db(), g.clinic["id"]
    req = booking.get(db, cid, rid)
    if not req:
        abort(404)
    start = timeutil.parse_dt(req["wanted_start"])
    return render_template("request.html", r=req, matches=booking.matches(db, cid, req), providers=booking.providers(db, cid),
                           start=start, active="requests")


@bp.post("/messages/requests/<int:rid>/accept")
def request_accept(rid):
    db, cid = get_db(), g.clinic["id"]
    req = booking.get(db, cid, rid)
    if not req:
        abort(404)
    choice = request.form.get("patient", "new")
    try:
        start = datetime.strptime(f"{request.form.get('date', '')} {request.form.get('time', '')}", "%Y-%m-%d %H:%M")
    except ValueError:
        flash("Pick a date and a time.", "error")
        return redirect(url_for("comms.request_show", rid=rid))
    holder = {}

    def go():
        holder["appt"] = booking.accept(db, g.clinic, g.user["id"], rid, None if choice == "new" else int(choice),
                                        request.form.get("provider_id", type=int), start)
    if choice != "new" and not choice.isdigit():
        abort(400)
    if _guard(go):
        flash("Booked. Open the visit to send them the confirmation.", "ok")
        return redirect(url_for("views.appointment_view", aid=holder["appt"]))
    return redirect(url_for("comms.request_show", rid=rid))


@bp.post("/messages/requests/<int:rid>/decline")
def request_decline(rid):
    db = get_db()
    if not booking.get(db, g.clinic["id"], rid):
        abort(404)
    if _guard(lambda: booking.decline(db, g.clinic, g.user["id"], rid, request.form.get("reason"))):
        flash("Declined. Let them know by phone or WhatsApp.", "ok")
        return redirect(url_for("comms.requests_list"))
    return redirect(url_for("comms.request_show", rid=rid))


# ----------------------------------------------------------------------------- health forms to review (clinical staff)
@bp.get("/messages/forms")
@clinical_required
def forms():
    db = get_db()
    return render_template("forms.html", rows=links.pending_forms(db, g.clinic["id"]), attn=outbox.attention(db, g.clinic, g.user), active="forms")


@bp.route("/messages/forms/<int:sid>", methods=["GET", "POST"])
@clinical_required
def form_review(sid):
    db, cid = get_db(), g.clinic["id"]
    row, answers = links.get_form(db, cid, sid)
    if not row:
        abort(404)
    if request.method == "POST":
        action = request.form.get("action")
        if action == "accept":
            if _guard(lambda: links.accept_form(db, g.clinic, g.user["id"], sid)):
                flash("Added to the medical history.", "ok")
                return redirect(url_for("comms.forms"))
        elif action == "dismiss":
            if _guard(lambda: links.dismiss_form(db, g.clinic, g.user["id"], sid)):
                flash("Dismissed. The medical history was not changed.", "ok")
                return redirect(url_for("comms.forms"))
        else:
            abort(400)
        return redirect(url_for("comms.form_review", sid=sid))
    from .services import medical
    return render_template("form_review.html", s=row, a=answers, old=medical.get_history(db, cid, row["patient_id"]), active="forms")


# ----------------------------------------------------------------------------- campaigns (manager)
@bp.get("/messages/campaigns")
@manager_required
def campaigns():
    db = get_db()
    return render_template("campaigns.html", rows=CAMP.list_for(db, g.clinic["id"]), kinds=CAMP.KINDS,
                           attn=outbox.attention(db, g.clinic, g.user), active="campaigns")


@bp.route("/messages/campaigns/new", methods=["GET", "POST"])
@manager_required
def campaign_new():
    db = get_db()
    kind = request.values.get("kind", "overdue_recall")
    if kind not in CAMP.KINDS:
        kind = "overdue_recall"
    if request.method == "POST":
        holder = {}

        def go():
            holder["id"] = CAMP.create(db, g.clinic, g.user["id"], request.form.get("name"), request.form.get("kind"),
                                       request.form.get("param_days", type=int), request.form.get("purpose"), request.form.get("body"),
                                       request.form.get("min_gap_days", type=int))
        if _guard(go):
            flash("Campaign saved. Check who it would reach, then add them to the send list.", "ok")
            return redirect(url_for("comms.campaign_show", cid_=holder["id"]))
    return render_template("campaign_form.html", kind=kind, kinds=CAMP.KINDS, purposes=CAMP.PURPOSES, active="campaigns",
                           form=request.form if request.method == "POST" else {})


@bp.route("/messages/campaigns/<int:cid_>", methods=["GET", "POST"])
@manager_required
def campaign_show(cid_):
    db, cid = get_db(), g.clinic["id"]
    camp = CAMP.get(db, cid, cid_)
    if not camp:
        abort(404)
    if request.method == "POST":
        action = request.form.get("action")
        if action == "build":
            holder = {}
            if _guard(lambda: holder.update(n=CAMP.build(db, g.clinic, g.user["id"], cid_))):
                flash(f"{holder['n']} added to the send list.", "ok")
        elif action == "close":
            if _guard(lambda: CAMP.close(db, g.clinic, g.user["id"], cid_)):
                flash("Campaign closed. Anything not yet sent was dropped.", "ok")
        else:
            abort(400)
        return redirect(url_for("comms.campaign_show", cid_=cid_))
    aud = CAMP.audience(db, g.clinic, camp) if camp["status"] != "closed" else None
    return render_template("campaign.html", c=camp, kinds=CAMP.KINDS, aud=aud, res=CAMP.results(db, cid, cid_), active="campaigns")


# ----------------------------------------------------------------------------- setup (manager)
@bp.route("/messages/setup", methods=["GET", "POST"])
@manager_required
def setup():
    db, cid = get_db(), g.clinic["id"]
    if request.method == "POST":
        a = request.form.get("action")
        f = request.form
        if a == "auto":
            ok = _guard(lambda: outbox.save_settings(db, g.clinic, g.user["id"], f.get("auto_recall"), f.get("auto_review"),
                                                     f.get("auto_birthday"), f.get("auto_plan"), f.get("review_link")))
        elif a == "rule":
            ok = _guard(lambda: outbox.save_rule(db, g.clinic, g.user["id"], f.get("days_before", type=int), f.get("skip_confirmed"), f.get("active")))
        elif a == "template":
            try:
                MT.save(db, cid, g.user["id"], f.get("kind"), f.get("body"))
                ok = True
            except MT.TemplateError as exc:
                db.rollback()
                flash(str(exc), "error")
                ok = False
            except LookupError:
                abort(404)
        elif a == "template_reset":
            try:
                MT.reset(db, cid, g.user["id"], f.get("kind"))
                ok = True
            except LookupError:
                abort(404)
        elif a == "wa_account":
            ok = _guard(lambda: WA.save_account(db, g.clinic, g.user["id"], current_app.config["SECRET_KEY"], f.get("enabled"),
                                                f.get("phone_number_id"), f.get("access_token"), f.get("app_secret"), f.get("api_version"),
                                                f.get("auto_send"), bool(f.get("new_hook_key"))))
        elif a == "wa_check":
            try:
                flash(WA.check_connection(db, g.clinic, current_app.config["SECRET_KEY"]), "ok")
            except WA.WhatsAppError as exc:
                flash(str(exc), "error")
            return redirect(url_for("comms.setup"))
        elif a == "wa_template":
            try:
                ok = _guard(lambda: WA.save_mapping(db, g.clinic, g.user["id"], f.get("kind"), f.get("lang"), f.get("template_name"),
                                                    f.get("language_code"), f.get("params")))
            except LookupError:
                abort(404)
        elif a == "booking":
            ok = _guard(lambda: booking.save_settings(db, g.clinic, g.user["id"], f.get("booking_on"), f.get("days_ahead", type=int),
                                                      f.get("notice_hours", type=int), f.get("minutes", type=int), f.get("note"),
                                                      bool(f.get("regenerate"))))
        else:
            abort(400)
        if ok:
            flash("Saved.", "ok")
        return redirect(url_for("comms.setup"))
    c = outbox.comms(db, cid)
    link = url_for("public.book", token=c["booking_token"], _external=True) if c["booking_token"] else None
    acct = WA.ensure(db, cid)
    maps = WA.mappings(db, cid)
    wa_rows = []
    for kind in WA.KINDS:
        for lang in i18n.LANGS:
            text, order = WA.suggestion(db, cid, kind, lang)
            wa_rows.append({"kind": kind, "lang": lang, "lang_name": i18n.LANGS[lang], "map": maps.get((kind, lang)), "text": text, "order": order,
                            "label": MT.KINDS[kind][0] if kind in MT.KINDS else "Campaign"})
    wa = {"acct": acct, "url": url_for("wahook.verify", key=acct["hook_key"], _external=True), "rows": wa_rows,
          "has_token": bool(acct["access_token"]), "has_secret": bool(acct["app_secret"]), "unmatched": WA.unmatched(db, cid), "fields": WA.PARAM_FIELDS}
    return render_template("msg_setup.html", c=c, wa=wa, rules=outbox.rules(db, cid), templates=MT.list_for(db, cid), fields=MT.FIELDS,
                           booking_link=link, durations=scheduling.DURATIONS, active="setup", attn=outbox.attention(db, g.clinic, g.user))
