from datetime import datetime, timedelta

from flask import (Blueprint, abort, flash, g, redirect, render_template, request, url_for)
from werkzeug.security import generate_password_hash

from .auth import manager_required
from .db import get_db
from .services import audit, billing, messaging, patients as P, regions, scheduling as S, taxes as _taxes, passwords as _passwords, twofactor as _tf, privacy as _privacy, i18n as _i18n
from .services.phone import normalise_phone
from .services.timeutil import now_local, parse_date, parse_dt, valid_timezone

bp = Blueprint("views", __name__)


def _today():
    return now_local(g.clinic["timezone"]).date()


def _date_arg(name="date", default=None):
    raw = request.values.get(name)
    if raw:
        try:
            return parse_date(raw)
        except ValueError:
            pass
    return default or _today()


def _patient_or_404(pid):
    p = P.get(get_db(), g.clinic["id"], pid)
    if not p:
        abort(404)
    return p


def _document_for(patient_or_id):
    """A document is printed in the patient's language, whatever language the member of staff uses. Returns the patient."""
    p = patient_or_id if not isinstance(patient_or_id, int) else _patient_or_404(patient_or_id)
    g.doc_lang = _i18n.patient_lang(p, g.clinic)
    return p


# ------------------------------------------------------------------ dashboard
@bp.get("/")
def dashboard():
    db, cid = get_db(), g.clinic["id"]
    today = _today()
    tomorrow = today + timedelta(days=1)

    def day_rows(day, extra=""):
        return db.execute(
            f"""SELECT a.*, p.first_name, p.last_name, p.reminders_ok, pr.name AS provider_name
                FROM appointments a JOIN patients p ON p.id = a.patient_id
                LEFT JOIN providers pr ON pr.id = a.provider_id
                WHERE a.clinic_id = ? AND a.start_at >= ? AND a.start_at < ? AND a.status != 'broken' {extra}
                ORDER BY a.start_at""",
            (cid, f"{day} 00:00", f"{day + timedelta(days=1)} 00:00")).fetchall()

    from .services import labs as _labs, outbox, stock as _stock
    outbox.build(db, g.clinic)
    outbox.refresh(db, g.clinic)
    todays = day_rows(today)
    to_chase = day_rows(tomorrow, "AND a.status = 'scheduled' AND a.confirmation IN ('none','sent')")
    waiting = db.execute(
        """SELECT a.*, p.first_name, p.last_name FROM appointments a JOIN patients p ON p.id = a.patient_id
           WHERE a.clinic_id = ? AND a.status IN ('asap','unscheduled')
           ORDER BY a.status = 'asap' DESC, a.created_at LIMIT 8""", (cid,)).fetchall()
    stats = {
        "booked": len(todays),
        "confirmed": sum(1 for a in todays if a["confirmation"] == "confirmed"),
        "done": sum(1 for a in todays if a["status"] == "complete"),
    }
    return render_template("dashboard.html", today=today, tomorrow=tomorrow, todays=todays, to_chase=to_chase,
                           waiting=waiting, stats=stats, birthdays=P.birthdays(db, cid, today),
                           attn=outbox.attention(db, g.clinic, g.user), lab=_labs.counts(db, g.clinic),
                           stock=_stock.summary(db, g.clinic) if g.user["role"] != "front_desk" else None,
                           privacy=_privacy.counts(db, g.clinic) if g.user["role"] == "manager" else None)


# ------------------------------------------------------------------ diary
@bp.get("/diary")
def diary():
    day = _date_arg()
    view = S.day_view(get_db(), g.clinic, day)
    return render_template("diary.html", day=day, prev=day - timedelta(days=1), next=day + timedelta(days=1),
                           view=view, today=_today())


# ------------------------------------------------------------------ patients
@bp.get("/patients")
def patients():
    q = (request.args.get("q") or "").strip()
    page = max(1, request.args.get("page", 1, type=int) or 1)
    archived = request.args.get("archived") == "1"
    rows, total = P.search(get_db(), g.clinic["id"], q, include_archived=archived, page=page)
    return render_template("patients.html", rows=rows, total=total, q=q, page=page, archived=archived,
                           booking=request.args.get("book") == "1", per_page=25)


def _guarantor_choices(exclude=None):
    rows, _ = P.search(get_db(), g.clinic["id"], "", per_page=500)
    return [r for r in rows if r["id"] != exclude]


@bp.route("/patients/new", methods=["GET", "POST"])
def patient_new():
    form, errors, dupes = request.form if request.method == "POST" else {}, {}, []
    if request.method == "POST":
        db = get_db()
        try:
            data = P.clean(request.form, db, g.clinic["id"], country=g.clinic["country"])
            pid = P.create(db, g.clinic["id"], g.user["id"], data,
                           allow_duplicates=request.form.get("allow_duplicate") == "1")
            flash("Patient saved.", "ok")
            return redirect(url_for("views.patient_view", pid=pid))
        except P.ValidationError as exc:
            errors = exc.errors
        except P.DuplicateWarning as exc:
            dupes = exc.matches
    return render_template("patient_form.html", form=form, errors=errors, dupes=dupes, patient=None,
                           heads=_guarantor_choices())


@bp.route("/patients/<int:pid>/edit", methods=["GET", "POST"])
def patient_edit(pid):
    patient = _patient_or_404(pid)
    errors, form = {}, (request.form if request.method == "POST" else dict(patient))
    if request.method == "POST":
        db = get_db()
        try:
            data = P.clean(request.form, db, g.clinic["id"], pid, country=g.clinic["country"])
            P.update(db, g.clinic["id"], g.user["id"], pid, data)
            flash("Changes saved.", "ok")
            return redirect(url_for("views.patient_view", pid=pid))
        except P.ValidationError as exc:
            errors = exc.errors
    return render_template("patient_form.html", form=form, errors=errors, dupes=[], patient=patient,
                           heads=_guarantor_choices(exclude=pid))


@bp.get("/patients/<int:pid>")
def patient_view(pid):
    db, cid = get_db(), g.clinic["id"]
    patient = _patient_or_404(pid)
    audit.log(db, cid, g.user["id"], "patient.view", "patient", pid)
    head, dependants = P.family(db, cid, pid)
    appts = db.execute(
        """SELECT a.*, pr.name AS provider_name FROM appointments a LEFT JOIN providers pr ON pr.id = a.provider_id
           WHERE a.clinic_id = ? AND a.patient_id = ? ORDER BY COALESCE(a.start_at, a.created_at) DESC LIMIT 30""",
        (cid, pid)).fetchall()
    return render_template("patient.html", p=patient, head=head, dependants=dependants, appts=appts,
                           msgs=messaging.history(db, cid, pid), bal=billing.totals(db, cid, pid))


@bp.post("/patients/<int:pid>/archive")
def patient_archive(pid):
    _patient_or_404(pid)
    restore = request.form.get("restore") == "1"
    P.set_archived(get_db(), g.clinic["id"], g.user["id"], pid, archived=not restore)
    flash("Patient restored." if restore else "Patient archived. Their records are kept.", "ok")
    return redirect(url_for("views.patient_view", pid=pid))


# ------------------------------------------------------------------ appointments
def _form_context(patient, appt=None):
    db, clinic = get_db(), g.clinic
    providers = db.execute("SELECT * FROM providers WHERE clinic_id = ? AND active = 1 ORDER BY id",
                           (clinic["id"],)).fetchall()
    day = _date_arg(default=(parse_dt(appt["start_at"]).date() if appt and appt["start_at"] else None))
    try:
        duration = int(request.values.get("duration") or (appt["duration_min"] if appt else 30))
    except ValueError:
        duration = 30
    provider_id = request.values.get("provider_id", type=int) or (
        appt["provider_id"] if appt and appt["provider_id"] else (providers[0]["id"] if providers else None))
    category = request.values.get("category") or (appt["category"] if appt else "Consultation")
    slots = []
    if provider_id and request.values.get("find") == "1":
        slots = S.free_slots(db, clinic, provider_id, day, duration)
    return dict(patient=patient, appt=appt, providers=providers, day=day, duration=duration,
                provider_id=provider_id, category=category, slots=slots,
                searched=request.values.get("find") == "1", categories=S.CATEGORIES, durations=S.DURATIONS,
                notes=request.values.get("notes", appt["notes"] if appt else ""))


@bp.get("/appointments/new")
def appointment_new():
    pid = request.args.get("patient_id", type=int)
    if not pid:
        return redirect(url_for("views.patients", book=1))
    patient = _patient_or_404(pid)
    if patient["status"] != "active":
        flash("Restore this patient before booking.", "error")
        return redirect(url_for("views.patient_view", pid=pid))
    return render_template("appointment_form.html", **_form_context(patient))


@bp.post("/appointments")
def appointment_create():
    db, clinic = get_db(), g.clinic
    patient = _patient_or_404(request.form.get("patient_id", type=int) or 0)
    try:
        mode = request.form.get("mode", "book")
        duration = request.form.get("duration", type=int) or 30
        category = request.form.get("category") or "Consultation"
        notes = (request.form.get("notes") or "").strip() or None
        if category not in S.CATEGORIES:
            raise S.SchedulingError("Pick a reason from the list.")
        if mode == "waiting":
            S.add_to_waiting_list(db, clinic, g.user["id"], patient["id"],
                                  status="asap" if request.form.get("asap") else "unscheduled",
                                  provider_id=request.form.get("provider_id", type=int),
                                  duration=duration, category=category, notes=notes)
            flash("Added to the waiting list.", "ok")
            return redirect(url_for("views.patient_view", pid=patient["id"]))
        slot = request.form.get("slot", "")
        try:
            time_part, chair = slot.split("|")
            start = parse_dt(f"{request.form.get('date')} {time_part}")
            chair_id = int(chair)
        except ValueError:
            raise S.SchedulingError("Pick a time first.")
        appt_id = S.book(db, clinic, g.user["id"], patient["id"], request.form.get("provider_id", type=int),
                         chair_id, start, duration, category, notes)
        flash("Booked.", "ok")
        return redirect(url_for("views.appointment_view", aid=appt_id))
    except S.SchedulingError as exc:
        db.rollback()
        flash(str(exc), "error")
        return redirect(url_for("views.appointment_new", patient_id=patient["id"]))


def _appt_or_404(aid):
    appt = S.get_appointment(get_db(), g.clinic["id"], aid)
    if not appt:
        abort(404)
    return appt


@bp.get("/appointments/<int:aid>")
def appointment_view(aid):
    appt = _appt_or_404(aid)
    return render_template("appointment.html", a=appt, start=parse_dt(appt["start_at"]) if appt["start_at"] else None,
                           can_message=bool(appt["reminders_ok"]) and appt["status"] == "scheduled",
                           next_statuses=S.TRANSITIONS[appt["status"]])


@bp.route("/appointments/<int:aid>/reschedule", methods=["GET", "POST"])
def appointment_reschedule(aid):
    appt = _appt_or_404(aid)
    if appt["status"] not in ("scheduled", "asap", "unscheduled", "planned"):
        flash("A closed appointment cannot be moved. Book a new one instead.", "error")
        return redirect(url_for("views.appointment_view", aid=aid))
    patient = _patient_or_404(appt["patient_id"])
    if request.method == "POST":
        db = get_db()
        try:
            time_part, chair = request.form.get("slot", "").split("|")
            start = parse_dt(f"{request.form.get('date')} {time_part}")
            S.place(db, g.clinic, g.user["id"], aid, request.form.get("provider_id", type=int), int(chair), start,
                    request.form.get("duration", type=int))
            flash("Appointment moved. Confirmation reset, so send a fresh message.", "ok")
            return redirect(url_for("views.appointment_view", aid=aid))
        except ValueError:
            flash("Pick a time first.", "error")
        except S.SchedulingError as exc:
            db.rollback()
            flash(str(exc), "error")
        return redirect(url_for("views.appointment_reschedule", aid=aid))
    return render_template("appointment_form.html", **_form_context(patient, appt))


@bp.post("/appointments/<int:aid>/status")
def appointment_status(aid):
    _appt_or_404(aid)
    try:
        S.set_status(get_db(), g.clinic["id"], g.user["id"], aid, request.form.get("status", ""),
                     request.form.get("reason"))
        flash("Updated.", "ok")
    except S.SchedulingError as exc:
        flash(str(exc), "error")
    return redirect(url_for("views.appointment_view", aid=aid))


@bp.post("/appointments/<int:aid>/confirmation")
def appointment_confirmation(aid):
    _appt_or_404(aid)
    try:
        S.set_confirmation(get_db(), g.clinic["id"], g.user["id"], aid, request.form.get("value", ""))
        flash("Updated.", "ok")
    except S.SchedulingError as exc:
        flash(str(exc), "error")
    return redirect(request.form.get("back") if (request.form.get("back") or "").startswith("/")
                    and not (request.form.get("back") or "").startswith("//")
                    else url_for("views.appointment_view", aid=aid))


@bp.post("/appointments/<int:aid>/whatsapp")
def appointment_whatsapp(aid):
    db = get_db()
    appt = _appt_or_404(aid)
    patient = _patient_or_404(appt["patient_id"])
    try:
        link = messaging.prepare(db, g.clinic, g.user["id"], patient, appt,
                                 "reminder" if request.form.get("kind") == "reminder" else "confirmation")
    except PermissionError as exc:
        flash(str(exc), "error")
        return redirect(url_for("views.appointment_view", aid=aid))
    return redirect(link)


# ------------------------------------------------------------------ manager screens
@bp.get("/audit")
@manager_required
def audit_view():
    return render_template("audit.html", rows=audit.recent(get_db(), g.clinic["id"]))


@bp.route("/staff", methods=["GET", "POST"])
@manager_required
def staff():
    db = get_db()
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        name = (request.form.get("display_name") or "").strip()
        role = request.form.get("role")
        password = request.form.get("password") or ""
        if not username or not name or role not in ("manager", "front_desk", "dentist", "assistant"):
            flash("Fill in every field.", "error")
        elif _passwords.check(password, username, name):
            flash(_passwords.check(password, username, name), "error")
        elif db.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone():
            flash("That username is taken.", "error")
        else:
            cur = db.execute(
                "INSERT INTO users(clinic_id, username, display_name, role, password_hash) VALUES (?,?,?,?,?)",
                (g.clinic["id"], username, name, role, generate_password_hash(password)))
            audit.log(db, g.clinic["id"], g.user["id"], "staff.create", "user", cur.lastrowid, role)
            flash("Staff login created.", "ok")
        return redirect(url_for("views.staff"))
    rows = db.execute("SELECT * FROM users WHERE clinic_id = ? ORDER BY display_name", (g.clinic["id"],)).fetchall()
    return render_template("staff.html", rows=rows)


@bp.post("/staff/<int:uid>/reset-password")
@manager_required
def staff_reset_password(uid):
    """Give a staff member a temporary password. They must choose their own at the next login."""
    import secrets as _secrets
    db = get_db()
    row = db.execute("SELECT * FROM users WHERE id = ? AND clinic_id = ?", (uid, g.clinic["id"])).fetchone()
    if not row:
        abort(404)
    if row["id"] == g.user["id"]:
        flash("Use Change my password for your own login.", "error")
        return redirect(url_for("views.staff"))
    temp = _secrets.token_urlsafe(9)
    db.execute("UPDATE users SET password_hash = ?, must_change_password = 1 WHERE id = ?",
               (generate_password_hash(temp), uid))
    db.execute("DELETE FROM login_attempts WHERE username = ?", (row["username"],))
    audit.log(db, g.clinic["id"], g.user["id"], "staff.reset_password", "user", uid)
    flash(f"Temporary password for {row['display_name']}: {temp}  Give it to them in person. It works once, and they "
          "must choose their own when they sign in. It will not be shown again.", "ok")
    return redirect(url_for("views.staff"))


@bp.post("/staff/<int:uid>/reset-2fa")
@manager_required
def staff_reset_2fa(uid):
    """For a staff member who lost their phone and their recovery codes. They set up again at their next sign-in."""
    db = get_db()
    row = db.execute("SELECT * FROM users WHERE id = ? AND clinic_id = ?", (uid, g.clinic["id"])).fetchone()
    if not row:
        abort(404)
    if row["id"] == g.user["id"]:
        flash("Use My security for your own two-step login.", "error")
    elif not row["totp_enabled"] and not row["totp_secret"]:
        flash("That person has not set up two-step login.", "error")
    else:
        _tf.disable(db, uid, g.user["id"], g.clinic["id"], "2fa.reset")
        flash(f"Two-step login reset for {row['display_name']}. They sign in with their password, then set it up again.", "ok")
    return redirect(url_for("views.staff"))


@bp.post("/staff/require-2fa")
@manager_required
def staff_require_2fa():
    db = get_db()
    on = 1 if request.form.get("on") == "1" else 0
    if on and not g.user["totp_enabled"]:
        flash("Set up two-step login for yourself first (My security), so you do not lock yourself out.", "error")
        return redirect(url_for("views.staff"))
    db.execute("UPDATE clinics SET require_2fa = ? WHERE id = ?", (on, g.clinic["id"]))
    audit.log(db, g.clinic["id"], g.user["id"], "settings.require_2fa", detail="on" if on else "off")
    flash("Everyone must now use two-step login. Anyone without it is asked to set it up at their next click." if on
          else "Two-step login is now optional.", "ok")
    return redirect(url_for("views.staff"))


@bp.post("/staff/<int:uid>/toggle")
@manager_required
def staff_toggle(uid):
    db = get_db()
    row = db.execute("SELECT * FROM users WHERE id = ? AND clinic_id = ?", (uid, g.clinic["id"])).fetchone()
    if not row:
        abort(404)
    if row["id"] == g.user["id"]:
        flash("You cannot switch off your own login.", "error")
    else:
        db.execute("UPDATE users SET active = ? WHERE id = ?", (0 if row["active"] else 1, uid))
        audit.log(db, g.clinic["id"], g.user["id"], "staff.toggle", "user", uid)
        flash("Updated.", "ok")
    return redirect(url_for("views.staff"))


@bp.route("/settings", methods=["GET", "POST"])
@manager_required
def settings():
    db, cid = get_db(), g.clinic["id"]
    if request.method == "POST":
        action = request.form.get("action")
        try:
            if action == "clinic":
                open_t, close_t = request.form.get("open_time", ""), request.form.get("close_time", "")
                datetime.strptime(open_t, "%H:%M"), datetime.strptime(close_t, "%H:%M")
                if open_t >= close_t:
                    raise ValueError("Opening time must be before closing time.")
                days = sorted({int(d) for d in request.form.getlist("days") if d in "1234567"})
                if not days:
                    raise ValueError("Pick at least one working day.")
                slot = request.form.get("slot_minutes", type=int)
                if slot not in (10, 15, 20, 30, 60):
                    raise ValueError("Pick a slot length from the list.")
                country = (request.form.get("country") or "").upper()
                if not regions.country(country):
                    raise ValueError("Pick a country from the list.")
                currency = (request.form.get("currency") or "").upper()
                if currency not in regions.CURRENCIES:
                    raise ValueError("Pick a currency from the list.")
                tz = (request.form.get("timezone") or "").strip()
                if not valid_timezone(tz):
                    raise ValueError("That timezone name is not recognised. Use a name like Africa/Accra or Europe/London.")
                language = _i18n.pick(request.form.get("language")) or g.clinic["language"]
                methods = [m for m in billing.METHODS if request.form.get("pay_" + m)]
                if "cash" not in methods:
                    methods.insert(0, "cash")
                whatsapp = (request.form.get("whatsapp") or "").strip()
                db.execute(
                    """UPDATE clinics SET name=?, open_time=?, close_time=?, slot_minutes=?, working_days=?,
                              whatsapp=?, country=?, currency=?, timezone=?, payment_methods=?, language=? WHERE id=?""",
                    ((request.form.get("name") or "").strip() or g.clinic["name"], open_t, close_t, slot,
                     ",".join(map(str, days)), normalise_phone(whatsapp, country) if whatsapp else None,
                     country, currency, tz, ",".join(methods), language, cid))
                audit.log(db, cid, g.user["id"], "settings.clinic")
            elif action == "tax":
                from .services import invoices as INV
                try:
                    INV.save_tax_settings(db, g.clinic, g.user["id"], request.form.get("tax_name"), request.form.get("tax_rate"),
                                          request.form.get("tax_inclusive") == "1", request.form.get("tax_number"),
                                          request.form.get("invoice_footer"))
                except INV.InvoiceError as exc:
                    raise ValueError(str(exc))
            elif action == "taxrate_add":
                try:
                    _taxes.add_rate(db, g.clinic, g.user["id"], request.form.get("rate_name"), request.form.get("rate_kind"), request.form.get("rate_pct"))
                except _taxes.TaxError as exc:
                    raise ValueError(str(exc))
            elif action == "taxrate_toggle":
                try:
                    _taxes.toggle_rate(db, cid, g.user["id"], request.form.get("id", type=int))
                except LookupError:
                    abort(404)
            elif action in ("provider", "chair"):
                name = (request.form.get("name") or "").strip()
                if not name:
                    raise ValueError("Enter a name.")
                table = "providers" if action == "provider" else "chairs"
                db.execute(f"INSERT INTO {table}(clinic_id, name) VALUES (?,?)", (cid, name))
                audit.log(db, cid, g.user["id"], f"settings.{action}.add")
            elif action in ("provider_toggle", "chair_toggle"):
                table = "providers" if action == "provider_toggle" else "chairs"
                db.execute(f"UPDATE {table} SET active = 1 - active WHERE id = ? AND clinic_id = ?",
                           (request.form.get("id", type=int), cid))
                audit.log(db, cid, g.user["id"], f"settings.{action}")
            flash("Saved.", "ok")
        except ValueError as exc:
            flash(str(exc) if str(exc) else "Check the form.", "error")
        return redirect(url_for("views.settings"))
    return render_template(
        "settings.html",
        providers=db.execute("SELECT * FROM providers WHERE clinic_id = ? ORDER BY id", (cid,)).fetchall(),
        chairs=db.execute("SELECT * FROM chairs WHERE clinic_id = ? ORDER BY id", (cid,)).fetchall(),
        days={int(d) for d in g.clinic["working_days"].split(",") if d},
        countries=sorted(((c, row[0]) for c, row in regions.COUNTRIES.items()), key=lambda x: x[1]),
        currencies=sorted(regions.CURRENCIES), methods=billing.METHODS,
        enabled=set(billing.enabled_methods(g.clinic)),
        tax_rates=_taxes.list_rates(db, cid), tax_kinds=_taxes.KINDS)
