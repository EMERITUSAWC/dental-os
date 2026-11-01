import json
import os
import secrets
import time
from datetime import timedelta
from pathlib import Path

import click
from flask import Flask, abort, g, redirect, request, session, url_for
from werkzeug.security import generate_password_hash

from . import db as dbmod
from .services import audit, i18n, medical, passwords
from .services import timeutil as T
from .services import money as moneymod
from .services import regions
from .services.phone import local_display, normalise_phone


def _dev_secret(instance_path):
    path = Path(instance_path) / "secret_key"
    if path.exists():
        return path.read_text().strip()
    key = secrets.token_hex(32)
    path.write_text(key)
    path.chmod(0o600)
    return key


def create_app(test_config=None):
    app = Flask(__name__, instance_relative_config=True)
    Path(app.instance_path).mkdir(parents=True, exist_ok=True)
    app.config.update(
        DATABASE=os.environ.get("DATABASE_URL") or os.environ.get("DATABASE_PATH", str(Path(app.instance_path) / "dental.sqlite")),
        SECRET_KEY=os.environ.get("SECRET_KEY") or _dev_secret(app.instance_path),
        FILES_PATH=os.environ.get("FILES_PATH") or None,       # where X-rays and documents are kept. Default: instance/files
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "0") == "1",
        PERMANENT_SESSION_LIFETIME=timedelta(hours=10),
        PUBLIC_URL=os.environ.get("PUBLIC_URL") or None,       # the address patients reach, for example https://clinic.example.com
        SESSION_IDLE_MINUTES=int(os.environ.get("SESSION_IDLE_MINUTES", "60")),
        ALLOW_SIGNUP=os.environ.get("ALLOW_SIGNUP", "0") == "1",           # 0.53: clinics can sign themselves up for a trial
        PLATFORM_PAYSTACK_SECRET=os.environ.get("PLATFORM_PAYSTACK_SECRET") or None,   # 0.54: the service's own account, for clinics' bills
        PLATFORM_STRIPE_SECRET=os.environ.get("PLATFORM_STRIPE_SECRET") or None,   # signed out after this long with no activity. 0 turns it off
        MAX_CONTENT_LENGTH=256 * 1024,          # ordinary forms. Uploads get more, see before_request.
        MAX_FORM_PARTS=2000,                    # the gum chart form has a few hundred fields
        # 0.50: the copy kept off site. A folder (a USB disk or a synced cloud folder) or an https WebDAV address.
        OFFSITE_TARGET=os.environ.get("OFFSITE_TARGET") or None,
        OFFSITE_USER=os.environ.get("OFFSITE_USER") or None,
        OFFSITE_PASSWORD=os.environ.get("OFFSITE_PASSWORD") or None,
        OFFSITE_PASSPHRASE=os.environ.get("OFFSITE_PASSPHRASE") or None,   # locks the copy. Keep it written down, away from the computer
        NET_CHECK_URL=os.environ.get("NET_CHECK_URL") or "https://www.gstatic.com/generate_204",
        READ_ONLY=os.environ.get("READ_ONLY", "0") == "1",                 # 0.51: a viewing copy that refuses every change
    )
    if test_config:
        app.config.update(test_config)
    # 0.56: behind a proxy (nginx, Caddy, a load balancer), set TRUST_PROXY to how many proxies sit in front, usually 1. Then the
    # visitor's real address and https come from the proxy's headers, so the per-visitor limits work. Never set it with no proxy:
    # anyone could then claim any address.
    trust = int(os.environ.get("TRUST_PROXY", app.config.get("TRUST_PROXY", 0)) or 0)
    if trust > 0:
        from werkzeug.middleware.proxy_fix import ProxyFix
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=trust, x_proto=trust, x_host=trust)

    with app.app_context():
        conn = dbmod.connect(app.config["DATABASE"])
        dbmod.init_schema(conn)
        conn.close()

    app.teardown_appcontext(dbmod.close_db)

    @app.teardown_request
    def _reset_notation(exc=None):
        from .services import teeth as _teeth
        _teeth.NOTATION.set("fdi")

    # ---- template helpers
    def _doc_lang():
        return g.get("doc_lang") or g.get("lang") or "en"

    # a date filter also takes "YYYY-MM-DD HH:MM" text and shows only the date
    app.jinja_env.filters["date"] = lambda v: i18n.fmt_date(T.parse_date(v[:10]) if isinstance(v, str) else v, _doc_lang())
    app.jinja_env.filters["day"] = lambda v: i18n.fmt_day_date(T.parse_date(v[:10]) if isinstance(v, str) else v, _doc_lang())
    app.jinja_env.filters["time"] = lambda v: i18n.fmt_time(T.parse_dt(v) if isinstance(v, str) else v, _doc_lang())
    # _() is the language of the screen. _d() is the language of a document made for a patient (see services/i18n.py).
    app.jinja_env.globals["_"] = lambda text, **kw: i18n.t(g.get("lang") or "en", text, **kw)
    app.jinja_env.globals["_d"] = lambda text, **kw: i18n.t(_doc_lang(), text, **kw)
    # These read the signed-in clinic's own country and currency.
    app.jinja_env.filters["phone"] = lambda v: local_display(v, g.clinic["country"] if g.get("clinic") else None)
    app.jinja_env.filters["money"] = lambda v: moneymod.fmt(v, g.clinic["currency"])
    app.jinja_env.filters["money_in"] = lambda v, cur: moneymod.fmt(v, cur)
    app.jinja_env.filters["plain"] = lambda v: moneymod.plain(v, g.clinic["currency"])
    from .services import teeth as _teeth
    app.jinja_env.filters["tooth"] = lambda v: _teeth.show(v)          # stored FDI, shown in the clinic's own numbering
    app.jinja_env.globals["tooth_hint"] = lambda: _teeth.HINT[_teeth.current()]

    # Small helpers so any page can show a patient's alerts.
    app.jinja_env.globals["alerts_for"] = lambda pid: medical.active_alerts(dbmod.get_db(), g.clinic["id"], pid)
    app.jinja_env.globals["alert_level"] = lambda pid: medical.alert_level(dbmod.get_db(), g.clinic["id"], pid)
    from .services import vitals as _vitals
    app.jinja_env.globals["bp_today"] = lambda pid: _vitals.latest_today(dbmod.get_db(), g.clinic, pid)
    app.jinja_env.globals["bp_grades"] = _vitals.GRADES

    @app.context_processor
    def inject():
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(24)
        total, group = 0, None
        if g.get("user") and g.get("clinic"):
            from .services import orgs as _orgs, outbox as _ob
            total = _ob.attention(dbmod.get_db(), g.clinic, g.user)["total"]
            group = _orgs.for_user(dbmod.get_db(), g.user)
        clocked, task_due, phone_on, nhs_on, team_unread = None, 0, False, False, 0
        from .services import permgroups as _pg
        mine = _pg.areas_of(dbmod.get_db(), g.user) if g.get("user") else set(_pg.AREAS)

        def can(area):
            return area in mine
        if g.get("user") and g.get("clinic"):
            from .services import voice as _voice
            phone_on = _voice.is_on(dbmod.get_db(), g.clinic["id"])
            from .services import nhs as _nhs
            nhs_on = _nhs.is_on(dbmod.get_db(), g.clinic["id"])
            from .services import team as _team
            team_unread = _team.unread(dbmod.get_db(), g.user)
        if g.get("user") and g.get("clinic"):
            from .services import tasks as _tasks, timeclock as _tc
            clocked = _tc.open_entry(dbmod.get_db(), g.clinic["id"], g.user["id"])
            task_due = _tasks.counts(dbmod.get_db(), g.clinic, g.user["id"])["due"]
        from .services import offsite as _off
        offline = backup_stale = read_only = None
        if g.get("user"):
            try:
                offline = _off.offline_since(dbmod.get_db())
                if g.user["role"] == "manager" and app.config.get("OFFSITE_TARGET") and not app.config["READ_ONLY"]:
                    backup_stale = _off.stale(dbmod.get_db())
            except Exception:                                  # a status line must never stop a page
                offline = backup_stale = None
        if app.config["READ_ONLY"]:
            stamp = Path(app.config["DATABASE"]).with_name("read_only_copy.json") if not dbmod.dbpg.is_url(app.config["DATABASE"]) else None
            read_only = json.loads(stamp.read_text()) if stamp and stamp.exists() else {}
        plan_banner = None
        if g.get("user") and g.get("clinic") and not app.config["READ_ONLY"]:
            from .services import platform as _plat
            st = _plat.state(g.clinic)
            if st == "trial":
                plan_banner = ("trial", g.clinic["trial_ends_on"])
            elif st == "past_due":
                plan_banner = ("look" if g.get("access") == "look" else "past_due", _plat.due_since(dbmod.get_db(), g.clinic))
        return {"csrf_token": session["csrf"], "user": g.get("user"), "clinic": g.get("clinic"), "attn_total": total, "group": group,
                "plan_banner": plan_banner, "support_on": g.get("support"),
                "offline_since": offline, "backup_stale": backup_stale, "read_only": read_only,
                "clocked_in": clocked, "task_due": task_due, "phone_on": phone_on, "nhs_on": nhs_on, "team_unread": team_unread, "can": can, "kiosk_on": bool(session.get("kiosk")),
                "lang": g.get("lang") or "en", "languages": i18n.LANGS}

    # ---- request lifecycle
    @app.before_request
    def load_user_and_check_csrf():
        g.user = g.clinic = None
        uid = session.get("uid")
        if uid:
            conn = dbmod.get_db()
            user = conn.execute("SELECT * FROM users WHERE id = ? AND active = 1", (uid,)).fetchone()
            if user:
                g.user = user
                g.clinic = conn.execute("SELECT * FROM clinics WHERE id = ?", (user["clinic_id"],)).fetchone()
            else:
                session.clear()
        g.support = False
        sup = session.get("support")
        if not g.user and sup:                                    # 0.53: an operator looking at a clinic that granted support access
            from .services import platform as _plat
            conn = dbmod.get_db()
            op = conn.execute("SELECT * FROM operators WHERE id = ? AND active = 1", (session.get("op_id"),)).fetchone()
            grant = _plat.live_grant(conn, sup.get("clinic"))
            if op and grant and grant["id"] == sup.get("grant"):
                g.user = _plat.support_user(conn, sup["clinic"])
                g.clinic = conn.execute("SELECT * FROM clinics WHERE id = ?", (sup["clinic"],)).fetchone()
                g.support = True
            else:
                session.pop("support", None)
        if request.endpoint in ("record.files_view", "record.import_start"):
            request.max_content_length = 12 * 1024 * 1024
        elif request.endpoint == "migrate.home" and g.user and g.user["role"] == "manager":
            request.max_content_length = 101 * 1024 * 1024      # a whole clinic's records from another system
        elif request.endpoint == "migrate.preview":
            request.max_form_parts = 5000                       # one column-match box per column of every file
        elif request.endpoint == "api.imaging_file":
            request.max_content_length = 15 * 1024 * 1024       # a 10 MB image as base64
        if app.config["READ_ONLY"] and request.method not in ("GET", "HEAD", "OPTIONS") and request.endpoint not in (
                "auth.login", "auth.login_code", "auth.logout", "static"):
            abort(403, "This is a read-only copy. Changes are made at the clinic.")
        is_api = request.path.startswith("/api/")            # only the API blueprint lives under /api/
        hook = request.blueprint in ("wahook", "voicehook") or request.endpoint == "paypub.hook"   # Meta and the payment company call these: a signature is required
        public = request.endpoint in ("auth.login", "auth.login_code", "static", "health") or request.blueprint in ("public", "api", "wahook", "paypub", "voicehook", "formpub", "rxpub", "operator", "signup") or is_api
        idle = app.config["SESSION_IDLE_MINUTES"]
        if g.user and not is_api and request.endpoint != "static":
            now = int(time.time())
            if idle and now - session.get("seen", now) > idle * 60 and not session.get("kiosk"):
                session.clear()                          # no activity for too long: sign out, as a shared reception computer needs
                g.user = g.clinic = None
            else:
                session["seen"] = now
        g.doc_lang = None
        from .services import teeth as _teeth
        _teeth.NOTATION.set(g.clinic["tooth_notation"] if (g.clinic and not is_api) else "fdi")   # the API always speaks FDI
        if g.user:
            g.lang = i18n.user_lang(g.user, g.clinic)
        else:
            chosen = i18n.pick(request.args.get("lang")) if not is_api and request.endpoint != "static" else None
            if chosen:
                session["lang"] = chosen                  # a visitor's own choice on the sign-in, booking and patient pages
            g.lang = i18n.pick(session.get("lang")) or i18n.from_header(request.headers.get("Accept-Language")) or i18n.DEFAULT
        if not g.user and not public:
            return redirect(url_for("auth.login", next=request.full_path if request.method == "GET" else None))
        if g.user and session.get("kiosk") and not is_api:
            from .practice import KIOSK_ENDPOINTS
            if session.get("kiosk") != g.clinic["id"]:
                session.pop("kiosk", None)
            elif request.endpoint not in KIOSK_ENDPOINTS:
                return redirect(url_for("practice.kiosk"))   # a check-in tablet shows nothing else until staff sign it out of kiosk mode
        if (g.user and g.user["must_change_password"] and not is_api
                and request.endpoint not in ("auth.change_password", "auth.logout", "static", "health")):
            return redirect(url_for("auth.change_password"))
        if (g.user and g.clinic["require_2fa"] and not g.user["totp_enabled"] and not is_api and not g.support
                and request.endpoint not in ("auth.security", "auth.security_setup", "auth.change_password", "auth.logout", "static", "health")):
            return redirect(url_for("auth.security_setup"))
        if g.support and request.endpoint not in ("static", "health"):
            if request.method not in ("GET", "HEAD") and request.endpoint != "operator.support_end":
                abort(403, "Support can look, not change anything.")
            from .services import audit as _audit
            _audit.log(dbmod.get_db(), g.clinic["id"], g.user["id"], "support.view", detail=request.path[:200])
        g.access = "full"
        if g.user and g.clinic and not is_api and request.endpoint not in ("static", "health"):
            from .services import platform as _plat
            g.access = _plat.access(dbmod.get_db(), g.clinic)
            free = ("auth.logout", "auth.login", "auth.change_password", "record.export_data", "billing.home", "billing.pay", "billing.back",
                    "billing.closed", "billing.support", "operator.support_end")
            if g.access == "closed" and request.endpoint not in free:
                if request.method == "GET":
                    return redirect(url_for("billing.closed"))
                abort(403, "This clinic's account is paused.")
            if g.access == "look" and request.method not in ("GET", "HEAD") and request.endpoint not in free:
                abort(403, "The bill is overdue, so changes are paused until it is paid. Everything can still be looked at.")
        if g.user and not is_api and request.endpoint != "static":
            from .services import permgroups as _pg
            if not _pg.allowed(dbmod.get_db(), g.user, request.endpoint):
                abort(403)
        if request.method == "POST" and request.endpoint != "static" and not is_api and not hook:
            token = session.get("csrf")
            if not token or not secrets.compare_digest(token, request.form.get("_csrf", "")):
                abort(400, "The form expired. Go back, refresh the page and try again.")

    @app.after_request
    def finish(resp):
        if "db" in g and resp.status_code < 500:
            g.db.commit()
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        resp.headers.setdefault("Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
            "frame-ancestors 'none'; base-uri 'self'")
        resp.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()")
        if app.config.get("SESSION_COOKIE_SECURE"):
            # Only once the site is on HTTPS: tells browsers never to use plain HTTP here for a year.
            resp.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        if request.endpoint != "static":
            resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.errorhandler(400)
    @app.errorhandler(403)
    @app.errorhandler(404)
    @app.errorhandler(405)
    @app.errorhandler(413)
    def error_page(err):
        from flask import jsonify, render_template
        if request.path.startswith("/api/"):
            return jsonify({"error": {"code": err.name.lower().replace(" ", "_"), "message": err.description}}), err.code
        if err.code in (405, 413):
            return err
        return render_template("error.html", err=err), err.code

    @app.errorhandler(500)
    def broken_page(err):
        """0.59: a page that breaks shows a reference number to quote, and the error list keeps which screen it was."""
        from flask import jsonify, render_template
        from .services import pilot as P
        ref = P.new_ref()
        exc = getattr(err, "original_exception", None) or err
        app.logger.exception("Error %s on %s %s", ref, request.method, request.endpoint, exc_info=exc)
        try:
            if "db" in g:
                g.db.rollback()
            conn = dbmod.connect(app.config["DATABASE"])
            try:
                P.log_error(conn, ref, g.clinic["id"] if g.get("clinic") else None, g.user["id"] if g.get("user") else None,
                            request.method, request.endpoint, exc)
            finally:
                conn.close()
        except Exception:                                          # the log must never make things worse
            app.logger.exception("Could not write error %s to the error list", ref)
        if request.path.startswith("/api/"):
            return jsonify({"error": {"code": "server_error", "message": "Something went wrong.", "ref": ref}}), 500
        return render_template("error_500.html", ref=ref), 500

    @app.get("/health")
    def health():
        return "ok"

    from . import auth, views, clinical, record, commercial, finance, comms, public, api, ops, privacy, wahook, diaryviews, care, practice, payonline, voiceviews, sterilviews, ediviews, nhsviews, orthoviews, statementviews, reportviews, teamviews, mailviews, smsviews, formviews, implantviews, payviews, govviews, cpdviews, feeviews, mergeviews, reactviews, moneyviews, aftercareviews, kitviews, autocodeviews, closedviews, migrateviews, backupviews, ghclaimviews, platformviews, pilotviews, xrayaiviews, chairviews, rxviews, fhirapi
    app.register_blueprint(auth.bp)
    app.register_blueprint(views.bp)
    app.register_blueprint(clinical.bp)
    app.register_blueprint(record.bp)
    app.register_blueprint(commercial.bp)
    app.register_blueprint(finance.bp)
    app.register_blueprint(comms.bp)
    app.register_blueprint(public.bp)
    app.register_blueprint(api.bp)
    app.register_blueprint(ops.bp)
    app.register_blueprint(privacy.bp)
    app.register_blueprint(wahook.bp)
    app.register_blueprint(diaryviews.bp)
    app.register_blueprint(care.bp)
    app.register_blueprint(practice.bp)
    app.register_blueprint(payonline.bp)
    app.register_blueprint(payonline.pub)
    app.register_blueprint(voiceviews.bp)
    app.register_blueprint(voiceviews.hooks)
    app.register_blueprint(sterilviews.bp)
    app.register_blueprint(ediviews.bp)
    app.register_blueprint(nhsviews.bp)
    app.register_blueprint(orthoviews.bp)
    app.register_blueprint(statementviews.bp)
    app.register_blueprint(reportviews.bp)
    app.register_blueprint(teamviews.bp)
    app.register_blueprint(mailviews.bp)
    app.register_blueprint(smsviews.bp)
    app.register_blueprint(formviews.bp)
    app.register_blueprint(formviews.pub)
    app.register_blueprint(implantviews.bp)
    app.register_blueprint(payviews.bp)
    app.register_blueprint(govviews.bp)
    app.register_blueprint(cpdviews.bp)
    app.register_blueprint(feeviews.bp)
    app.register_blueprint(mergeviews.bp)
    app.register_blueprint(reactviews.bp)
    app.register_blueprint(moneyviews.bp)
    app.register_blueprint(aftercareviews.bp)
    app.register_blueprint(kitviews.bp)
    app.register_blueprint(autocodeviews.bp)
    app.register_blueprint(closedviews.bp)
    app.register_blueprint(migrateviews.bp)
    app.register_blueprint(backupviews.bp)
    app.register_blueprint(ghclaimviews.bp)
    app.register_blueprint(platformviews.signup_bp)
    app.register_blueprint(platformviews.operator_bp)
    app.register_blueprint(platformviews.billing_bp)
    app.register_blueprint(pilotviews.bp)
    app.register_blueprint(xrayaiviews.bp)
    app.register_blueprint(chairviews.bp)
    app.register_blueprint(rxviews.bp)
    app.register_blueprint(rxviews.pub)
    app.register_blueprint(fhirapi.bp)

    # ---- command line
    @app.cli.command("create-clinic")
    @click.option("--name", prompt="Clinic name")
    @click.option("--country", "country_code", prompt="Country code (for example GH, NG, KE, GB, US)")
    @click.option("--currency", default=None, help="ISO currency code. Defaults to the country's currency.")
    @click.option("--timezone", default=None, help="IANA timezone name. Defaults to the country's main timezone.")
    @click.option("--whatsapp", default=None, help="The number patients message, in local or international form.")
    def create_clinic(name, country_code, currency, timezone, whatsapp):
        """Add a clinic. Each clinic has its own country, currency, timezone and staff. Run it once per clinic."""
        try:
            d = regions.defaults_for(country_code)
        except ValueError as exc:
            raise click.ClickException(str(exc))
        currency = (currency or d["currency"]).upper()
        if currency not in regions.CURRENCIES:
            raise click.ClickException(f"Unknown currency {currency}. Known: {', '.join(sorted(regions.CURRENCIES))}.")
        timezone = timezone or d["timezone"]
        if not T.valid_timezone(timezone):
            raise click.ClickException(f"Unknown timezone {timezone}. Use a name like Africa/Accra or Europe/London.")
        try:
            wa = normalise_phone(whatsapp, d["country"]) if whatsapp else None
        except ValueError as exc:
            raise click.ClickException(str(exc))
        conn = dbmod.connect(app.config["DATABASE"])
        from .services import platform as _plat
        try:
            cid, d = _plat.setup_clinic(conn, name, country_code, currency, timezone, whatsapp)
        except (ValueError, _plat.PlatformError) as exc:
            raise click.ClickException(str(exc))
        conn.commit()
        click.echo(f"Created clinic {cid}: {name.strip()} ({d['country_name']}, {currency}, {timezone}).\n"
                   f"Next: create-user --clinic-id {cid}. Then, signed in as the manager, set opening hours, add dentists "
                   "and chairs under Settings, and enter prices under Procedures and prices.\n"
                   "Opening hours start as Mon to Sat, 8 am to 5 pm, which is only a starting point.")

    @app.cli.command("run-renewals")
    def run_renewals():
        """Create membership fees that have fallen due, for every clinic. Run once a day from the scheduler."""
        from .services import memberships
        conn = dbmod.connect(app.config["DATABASE"])
        total = 0
        for clinic in conn.execute("SELECT * FROM clinics").fetchall():
            total += memberships.run_renewals(conn, clinic)
        conn.commit()
        click.echo(f"{total} membership fees created.")

    @app.cli.command("build-queue")
    def build_queue():
        """Fill the send list for every clinic with reminders and other messages that are due. Run it a few times a day."""
        from .services import outbox
        conn = dbmod.connect(app.config["DATABASE"])
        total = 0
        for clinic in conn.execute("SELECT * FROM clinics").fetchall():
            total += outbox.build(conn, clinic)["queued"]
            outbox.refresh(conn, clinic)
        conn.commit()
        click.echo(f"{total} messages added to send lists.")

    @app.cli.command("send-whatsapp")
    def send_whatsapp():
        """Send the send list through the WhatsApp Business API, for clinics that switched on automatic sending. Run it every few
        minutes from the scheduler. It sends nothing for a clinic that has not switched it on."""
        from .services import outbox, whatsapp as wa
        conn = dbmod.connect(app.config["DATABASE"])
        total = left = 0
        for clinic in conn.execute("SELECT c.* FROM clinics c JOIN wa_account a ON a.clinic_id = c.id WHERE a.enabled = 1 AND a.auto_send = 1").fetchall():
            outbox.build(conn, clinic)
            outbox.refresh(conn, clinic)
            res = wa.send_ready(conn, clinic, None, app.config["SECRET_KEY"])
            total += res["sent"]
            left += res["left"]
            if res["stopped"]:
                click.echo(f"{clinic['name']}: stopped. {res['stopped']}")
            conn.commit()
        wa.clean_events(conn)
        conn.commit()
        click.echo(f"{total} sent, {left} left for a person.")

    def _files_path():
        return app.config["FILES_PATH"] or str(Path(app.instance_path) / "files")

    @app.cli.command("offsite-copy")
    def offsite_copy():
        """Make the locked off-site copy (the database and every patient file) and check it. Run it every night, and more often if you like."""
        from .services import offsite as OS
        conn = dbmod.connect(app.config["DATABASE"])
        try:
            where, size = OS.make_and_send(app.config, app.config["DATABASE"], _files_path(), conn)
        except OS.OffsiteError as exc:
            raise click.ClickException(str(exc))
        click.echo(f"Copy made and checked: {where} ({size / 1048576:.1f} MB).")

    @app.cli.command("offsite-drill")
    @click.option("--to", "folder", default=None, help="Unpack the copy here as well, to look at it. Never the live folder.")
    def offsite_drill(folder):
        """A restore drill: fetch the newest off-site copy, unlock it and check it. Changes nothing here."""
        from .services import offsite as OS
        conn = dbmod.connect(app.config["DATABASE"])
        name = None
        try:
            name, blob = OS.latest(app.config)
            info = OS.restore_to(blob, app.config.get("OFFSITE_PASSPHRASE") or "", folder)[1] if folder else OS.drill(blob, app.config.get("OFFSITE_PASSPHRASE") or "")
        except OS.OffsiteError as exc:
            OS.record_drill(conn, name, error=exc)          # 0.58: a failed drill is written down too, so the page shows it
            conn.close()
            raise click.ClickException(str(exc))
        ok, why = OS.record_drill(conn, name, info)
        conn.close()
        if not ok:
            raise click.ClickException(why)
        click.echo(f"{name} opens and is sound. Made {info['made_at']} UTC; {info.get('patients', '?')} patients, "
                   f"{info.get('appointments', '?')} appointments, {info.get('payments', '?')} payments, {info['files']} files.")

    @app.cli.command("offsite-load")
    def offsite_load():
        """On a read-only viewing server only (READ_ONLY=1): load the newest off-site copy in place of what is there."""
        from .services import offsite as OS
        if not app.config["READ_ONLY"]:
            raise click.ClickException("This replaces the whole database, so it only runs on a server set to READ_ONLY=1.")
        if dbmod.dbpg.is_url(app.config["DATABASE"]):
            raise click.ClickException("A read-only viewing server uses SQLite. Load a PostgreSQL copy with pg_restore.")
        try:
            name, blob = OS.latest(app.config)
            info = OS.load_read_only(blob, app.config.get("OFFSITE_PASSPHRASE") or "", app.config["DATABASE"], _files_path())
        except OS.OffsiteError as exc:
            raise click.ClickException(str(exc))
        click.echo(f"Loaded {name}, made {info['made_at']} UTC.")

    @app.cli.command("net-check")
    def net_check():
        """See whether the internet is there, so screens can say so. Run it every few minutes."""
        from .services import offsite as OS
        conn = dbmod.connect(app.config["DATABASE"])
        ok = OS.net_check(conn, app.config["NET_CHECK_URL"])
        click.echo("The internet is there." if ok else "No internet.")

    @app.cli.command("create-operator")
    @click.option("--username", prompt=True)
    @click.option("--name", "display_name", prompt="Full name")
    @click.password_option(confirmation_prompt=True)
    def create_operator(username, display_name, password):
        """Add a person who runs the service. They sign in at /operator/login, apart from clinic staff."""
        from .services import platform as _plat
        conn = dbmod.connect(app.config["DATABASE"])
        try:
            _plat.add_operator(conn, username, display_name, password)
        except _plat.PlatformError as exc:
            raise click.ClickException(str(exc))
        conn.commit()
        click.echo(f"Operator {username} added.")

    @app.cli.command("fetch-claims")
    def fetch_claims():
        """0.63: for each clinic connected to a clearinghouse, fetch new acknowledgements and remittances. Every 30 minutes."""
        from .services import clearinghouse as CH
        conn = dbmod.connect(app.config["DATABASE"])
        try:
            for clinic in conn.execute("SELECT c.* FROM clinics c JOIN clearinghouse_account a ON a.clinic_id = c.id WHERE a.enabled = 1").fetchall():
                try:
                    got = CH.fetch(conn, clinic, None, app.config["SECRET_KEY"], transport=app.config.get("CLEARINGHOUSE_TRANSPORT"))
                    conn.commit()
                    click.echo(f"Clinic {clinic['id']}: {got['acks']} acknowledgements, {got['remits']} remittances to check.")
                except CH.ClearinghouseError as exc:
                    conn.rollback()
                    CH.log(conn, clinic["id"], None, "fetch", False, str(exc) + (f" {exc.detail}" if exc.detail else ""))
                    conn.commit()
                    click.echo(f"Clinic {clinic['id']}: {exc}")
        finally:
            conn.close()

    @app.cli.command("bill-clinics")
    def bill_clinics():
        """Make the month's bills for clinics whose paid-up period has run out, and mark late ones overdue. Run it once a day."""
        from .services import platformbill as _pb
        conn = dbmod.connect(app.config["DATABASE"])
        n = _pb.make_bills(conn)
        conn.commit()
        click.echo(f"{n} bills made.")

    @app.cli.command("move-in")
    @click.option("--clinic", "clinic_id", required=True, type=int, help="The clinic's number.")
    @click.option("--user", "username", required=True, help="The manager the import is recorded under.")
    @click.option("--file", "path", required=True, type=click.Path(exists=True, dir_okay=False), help="A ZIP of CSV files, or one CSV.")
    @click.option("--teeth", "notation", default="universal", help="How the old system numbers teeth: universal, fdi or palmer.")
    @click.option("--import", "keep", is_flag=True, help="Really import. Without it, only a preview is shown and nothing is kept.")
    def move_in(clinic_id, username, path, notation, keep):
        """Bring records in from another system (Open Dental, Dentrix, a spreadsheet). For files too large for the screen."""
        import json as _json
        from pathlib import Path as _P
        from .services import migrate as MI
        conn = dbmod.connect(app.config["DATABASE"])
        clinic = conn.execute("SELECT * FROM clinics WHERE id = ?", (clinic_id,)).fetchone()
        user = conn.execute("SELECT * FROM users WHERE username = ? AND clinic_id = ? AND role = 'manager'", (username, clinic_id)).fetchone()
        if not clinic or not user:
            raise click.ClickException("That clinic or manager was not found.")
        data = _P(path).read_bytes()
        try:
            result = MI.run(conn, clinic, user["id"], MI.files_in(_P(path).name, data), {}, notation, keep=keep)
        except MI.MigrateError as exc:
            raise click.ClickException(str(exc))
        for k, label in MI.KINDS.items():
            if result["counts"][k] or result["already"][k]:
                click.echo(f"{label}: {result['counts'][k]} new, {result['already'][k]} already here")
        for why, n in result["skipped"].items():
            click.echo(f"Left out: {why}: {n}")
        if result["no_phone"]:
            click.echo(f"{result['no_phone']} patients have no phone number that could be read.")
        for p in result["problems"][:50]:
            click.echo(f"{p['file']} line {p['line']}: {p['text']}")
        for f in result["files"]:
            if f["missing"] or not f["kind"]:
                click.echo(f"{f['name']}: " + ("not recognised, left out" if not f["kind"] else "columns still needed: " + ", ".join(f["missing"])))
        click.echo("Imported." if keep else "Preview only: nothing was kept. Add --import to bring the records in.")

    @app.cli.command("send-sms")
    def send_sms():
        """Text the send list for patients who prefer SMS, for clinics that switched on automatic SMS. Run it every few minutes."""
        from .services import outbox, sms
        conn = dbmod.connect(app.config["DATABASE"])
        total = left = 0
        for clinic in conn.execute("SELECT c.* FROM clinics c JOIN sms_accounts a ON a.clinic_id = c.id WHERE a.enabled = 1 AND a.auto_send = 1").fetchall():
            outbox.build(conn, clinic)
            outbox.refresh(conn, clinic)
            res = sms.send_ready(conn, clinic, None, app.config["SECRET_KEY"])
            total += res["sent"]
            left += res["left"]
            if res["stopped"]:
                click.echo(f"{clinic['name']}: stopped. {res['stopped']}")
            conn.commit()
        click.echo(f"{total} texted, {left} left for a person.")

    @app.cli.command("send-email")
    def send_email():
        """Email the send list for patients who prefer email, for clinics that switched on automatic email. Run it every few minutes."""
        from .services import mailer, outbox
        conn = dbmod.connect(app.config["DATABASE"])
        total = left = 0
        for clinic in conn.execute("SELECT c.* FROM clinics c JOIN mail_accounts a ON a.clinic_id = c.id WHERE a.enabled = 1 AND a.auto_send = 1").fetchall():
            outbox.build(conn, clinic)
            outbox.refresh(conn, clinic)
            res = mailer.send_ready(conn, clinic, None, app.config["SECRET_KEY"])
            total += res["sent"]
            left += res["left"]
            if res["stopped"]:
                click.echo(f"{clinic['name']}: stopped. {res['stopped']}")
            conn.commit()
        click.echo(f"{total} emailed, {left} left for a person.")

    @app.cli.command("run-autopay")
    def run_autopay():
        """Charge saved cards for membership fees and ortho monthly amounts that are due, where the patient agreed to automatic
        charges. Run it once a day, after post-ortho-fees and run-renewals. Each amount is tried once; failures are left for staff."""
        from .services import savedcards as SC
        conn = dbmod.connect(app.config["DATABASE"])
        paid = failed = 0
        for c in conn.execute("SELECT * FROM clinics").fetchall():
            r = SC.run_automatic(conn, c, app.config["SECRET_KEY"])
            conn.commit()
            paid, failed = paid + r["paid"], failed + r["failed"]
        click.echo(f"{paid} charged, {failed} failed.")

    @app.cli.command("post-ortho-fees")
    def post_ortho_fees():
        """Put ortho contract amounts that are due on patients' accounts. Run it once a day. Safe to run more often."""
        from .services import ortho
        conn = dbmod.connect(app.config["DATABASE"])
        total = 0
        for c in conn.execute("SELECT * FROM clinics").fetchall():
            total += ortho.post_due(conn, c)
            conn.commit()
        click.echo(f"{total} ortho amounts posted.")

    @app.cli.command("deliver-webhooks")
    def deliver_webhooks():
        """Send webhook messages that are due, and retry ones that failed. Run it every minute or two from the scheduler."""
        from .services import webhooks
        conn = dbmod.connect(app.config["DATABASE"])
        total = {"sent": 0, "retry": 0, "dead": 0}
        for _ in range(20):                                   # up to 1000 messages a run
            done = webhooks.deliver_due(conn, allow_private=bool(app.config.get("WEBHOOKS_ALLOW_PRIVATE")))
            conn.commit()
            for k, v in done.items():
                total[k] += v
            if sum(done.values()) < 50:
                break
        click.echo(f"{total['sent']} sent, {total['retry']} will retry, {total['dead']} gave up.")

    @app.cli.command("create-org")
    @click.option("--name", prompt="Name of the group")
    def create_org(name):
        """Make a group of clinics, so a manager can see them side by side. Then use org-add-clinic and org-add-user."""
        from .services import orgs
        conn = dbmod.connect(app.config["DATABASE"])
        try:
            org_id = orgs.create(conn, name)
        except orgs.OrgError as exc:
            raise click.ClickException(str(exc))
        conn.commit()
        click.echo(f"Created group {org_id}: {name.strip()}.")

    @app.cli.command("org-add-clinic")
    @click.option("--org-id", type=int, required=True)
    @click.option("--clinic-id", type=int, required=True)
    def org_add_clinic(org_id, clinic_id):
        from .services import orgs
        conn = dbmod.connect(app.config["DATABASE"])
        try:
            orgs.add_clinic(conn, org_id, clinic_id)
        except orgs.OrgError as exc:
            raise click.ClickException(str(exc))
        conn.commit()
        click.echo("Clinic added to the group.")

    @app.cli.command("org-add-user")
    @click.option("--org-id", type=int, required=True)
    @click.option("--username", required=True)
    def org_add_user(org_id, username):
        """Let a clinic manager see the group view. The manager's clinic must already be in the group."""
        from .services import orgs
        conn = dbmod.connect(app.config["DATABASE"])
        row = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
        if not row:
            raise click.ClickException("No user has that username.")
        try:
            orgs.add_member(conn, org_id, row["id"])
        except orgs.OrgError as exc:
            raise click.ClickException(str(exc))
        conn.commit()
        click.echo("Done. That manager now sees All clinics.")

    @app.cli.command("backup")
    @click.option("--to", "folder", default=None, help="Folder for backups. Defaults to a backups folder next to the database.")
    @click.option("--keep", default=14, type=int, help="How many recent backups to keep.")
    def backup(folder, keep):
        """Copy the whole database (every clinic) to a dated file. Safe to run while the app is in use.
        This is for the person who runs the system. Keep the copies somewhere other than the same disk."""
        import sqlite3
        from datetime import datetime as _dt
        if dbmod.dbpg.is_url(app.config["DATABASE"]):
            import shutil
            import subprocess
            if not shutil.which("pg_dump"):
                raise click.ClickException("pg_dump was not found. Install the PostgreSQL client tools, or take backups with your host's own tool.")
            dest_dir = Path(folder) if folder else Path(app.instance_path) / "backups"
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / f"dental-{_dt.utcnow():%Y%m%d-%H%M%S}.dump"
            result = subprocess.run(["pg_dump", "--format=custom", "--file", str(dest), app.config["DATABASE"]], capture_output=True, text=True)
            if result.returncode:
                dest.unlink(missing_ok=True)
                raise click.ClickException("pg_dump failed: " + result.stderr.strip()[:300])
            dest.chmod(0o600)
            for stale in (sorted(dest_dir.glob("dental-*.dump"))[:-keep] if keep > 0 else []):
                stale.unlink()
            click.echo(f"Backup written to {dest}. Restore it with pg_restore. Patient files in the instance folder are not in this copy: back up the files folder too.")
            return
        src_path = Path(app.config["DATABASE"])
        dest_dir = Path(folder) if folder else src_path.parent / "backups"
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"dental-{_dt.utcnow():%Y%m%d-%H%M%S}.sqlite"
        src, dst = sqlite3.connect(src_path), sqlite3.connect(dest)
        with dst:
            src.backup(dst)
        src.close()
        dst.close()
        dest.chmod(0o600)
        old = sorted(dest_dir.glob("dental-*.sqlite"))
        for stale in old[:-keep] if keep > 0 else []:
            stale.unlink()
        click.echo(f"Backup written to {dest}. Keeping the latest {keep}. Patient files in the instance folder are not in this "
                   "copy: back up the files folder too.")

    @app.cli.command("copy-to-postgres")
    @click.option("--to", "url", required=True, help="postgresql://user:password@host:5432/database. The database must be empty.")
    def copy_to_postgres(url):
        """Copy the SQLite database this app uses into an empty PostgreSQL database. Stop the app first."""
        import sqlite3
        from .services import pgcopy
        if dbmod.dbpg.is_url(app.config["DATABASE"]):
            raise click.ClickException("The app is already using PostgreSQL. Point DATABASE_PATH at the SQLite file you want to copy.")
        if not dbmod.dbpg.is_url(url):
            raise click.ClickException("--to must start with postgresql://")
        src = sqlite3.connect(app.config["DATABASE"])
        dst = dbmod.dbpg.connect(url)
        try:
            counts = pgcopy.copy(src, dst, log=click.echo)
        except pgcopy.CopyError as exc:
            dst.rollback()
            raise click.ClickException(str(exc))
        finally:
            src.close()
            dst.close()
        click.echo(f"Done. {sum(counts.values())} rows in {len(counts)} tables copied and counted. Start the app with DATABASE_URL set to the new address.")

    @app.cli.command("list-clinics")
    def list_clinics():
        conn = dbmod.connect(app.config["DATABASE"])
        for r in conn.execute("SELECT id, name, country, currency, timezone FROM clinics ORDER BY id"):
            click.echo(f"{r['id']}: {r['name']} ({r['country']}, {r['currency']}, {r['timezone']})")

    @app.cli.command("create-user")
    @click.option("--username", prompt=True)
    @click.option("--name", "display_name", prompt="Full name")
    @click.option("--role", type=click.Choice(["manager", "front_desk", "dentist", "assistant"]), default="manager")
    @click.option("--clinic-id", type=int, prompt="Clinic id (see list-clinics)")
    @click.password_option(confirmation_prompt=True)
    def create_user(username, display_name, role, clinic_id, password):
        problem = passwords.check(password, username, display_name)
        if problem:
            raise click.ClickException(problem)
        conn = dbmod.connect(app.config["DATABASE"])
        if not conn.execute("SELECT 1 FROM clinics WHERE id = ?", (clinic_id,)).fetchone():
            raise click.ClickException("No clinic has that id. Run list-clinics, or create-clinic first.")
        conn.execute(
            "INSERT INTO users(clinic_id, username, display_name, role, password_hash) VALUES (?,?,?,?,?)",
            (clinic_id, username, display_name, role, generate_password_hash(password)))
        conn.commit()
        click.echo(f"Created {role} {username}.")

    @app.cli.command("reset-2fa")
    @click.option("--username", prompt=True)
    def reset_2fa(username):
        """For a manager who has lost both their phone and their recovery codes."""
        from .services import twofactor
        conn = dbmod.connect(app.config["DATABASE"])
        row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        if not row:
            raise click.ClickException("No one has that username.")
        twofactor.disable(conn, row["id"], None, row["clinic_id"], "2fa.reset.cli")
        conn.commit()
        click.echo(f"Two-step login is off for {username}. They can set it up again after signing in.")

    @app.cli.command("doctor")
    def doctor():
        """0.59: check the server's settings and each clinic's go-live list. Changes nothing. Exit code 1 if something must be fixed."""
        from .services import pilot as P
        bad = 0
        cfg = app.config
        server = [
            ("SECRET_KEY is set on the server, not left to the file the app makes", bool(os.environ.get("SECRET_KEY")), True),
            ("Off-site copies have a place (OFFSITE_TARGET) and a passphrase (OFFSITE_PASSPHRASE)", bool(cfg.get("OFFSITE_TARGET") and cfg.get("OFFSITE_PASSPHRASE")), True),
            ("PUBLIC_URL is set, so links sent to patients point at the right address", bool(cfg.get("PUBLIC_URL")), False),
            ("COOKIE_SECURE=1 (only when the site is on https)", bool(cfg.get("SESSION_COOKIE_SECURE")), False),
            ("Not a read-only viewing copy", not cfg.get("READ_ONLY"), False),
        ]
        click.echo("Server")
        for label, ok, must in server:
            bad += (not ok and must)
            click.echo(f"  [{'ok' if ok else ('MUST' if must else 'advised')}] {label}")
        conn = dbmod.connect(cfg["DATABASE"])
        try:
            with app.test_request_context():
                for clinic in conn.execute("SELECT * FROM clinics ORDER BY id").fetchall():
                    items = P.checklist(conn, clinic, cfg)
                    click.echo(f"Clinic {clinic['id']}: {clinic['name']} ({'ready' if P.ready(items) else 'not ready'})")
                    for _k, label, done, must, _f in items:
                        bad += (not done and must)
                        click.echo(f"  [{'ok' if done else ('MUST' if must else 'advised')}] {label}")
        finally:
            conn.close()
        if bad:
            raise SystemExit(1)

    @app.cli.command("seed-demo")
    @click.option("--clinic-id", type=int, prompt="Clinic id (see list-clinics)")
    def seed_demo(clinic_id):
        """Add made-up patients and appointments to one clinic so you can see the screens. Not real people."""
        from .demo import seed
        conn = dbmod.connect(app.config["DATABASE"])
        click.echo(seed(conn, clinic_id))

    return app
