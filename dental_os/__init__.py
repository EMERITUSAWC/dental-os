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
        DATABASE=os.environ.get("DATABASE_PATH", str(Path(app.instance_path) / "dental.sqlite")),
        SECRET_KEY=os.environ.get("SECRET_KEY") or _dev_secret(app.instance_path),
        FILES_PATH=os.environ.get("FILES_PATH") or None,       # where X-rays and documents are kept. Default: instance/files
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "0") == "1",
        PERMANENT_SESSION_LIFETIME=timedelta(hours=10),
        SESSION_IDLE_MINUTES=int(os.environ.get("SESSION_IDLE_MINUTES", "60")),   # signed out after this long with no activity. 0 turns it off
        MAX_CONTENT_LENGTH=256 * 1024,          # ordinary forms. Uploads get more, see before_request.
        MAX_FORM_PARTS=2000,                    # the gum chart form has a few hundred fields
    )
    if test_config:
        app.config.update(test_config)

    with app.app_context():
        conn = dbmod.connect(app.config["DATABASE"])
        dbmod.init_schema(conn)
        conn.close()

    app.teardown_appcontext(dbmod.close_db)

    # ---- template helpers
    def _doc_lang():
        return g.get("doc_lang") or g.get("lang") or "en"

    app.jinja_env.filters["date"] = lambda v: i18n.fmt_date(T.parse_date(v) if isinstance(v, str) else v, _doc_lang())
    app.jinja_env.filters["day"] = lambda v: i18n.fmt_day_date(T.parse_date(v) if isinstance(v, str) else v, _doc_lang())
    app.jinja_env.filters["time"] = lambda v: i18n.fmt_time(T.parse_dt(v) if isinstance(v, str) else v, _doc_lang())
    # _() is the language of the screen. _d() is the language of a document made for a patient (see services/i18n.py).
    app.jinja_env.globals["_"] = lambda text, **kw: i18n.t(g.get("lang") or "en", text, **kw)
    app.jinja_env.globals["_d"] = lambda text, **kw: i18n.t(_doc_lang(), text, **kw)
    # These read the signed-in clinic's own country and currency.
    app.jinja_env.filters["phone"] = lambda v: local_display(v, g.clinic["country"] if g.get("clinic") else None)
    app.jinja_env.filters["money"] = lambda v: moneymod.fmt(v, g.clinic["currency"])
    app.jinja_env.filters["money_in"] = lambda v, cur: moneymod.fmt(v, cur)
    app.jinja_env.filters["plain"] = lambda v: moneymod.plain(v, g.clinic["currency"])

    # Small helpers so any page can show a patient's alerts.
    app.jinja_env.globals["alerts_for"] = lambda pid: medical.active_alerts(dbmod.get_db(), g.clinic["id"], pid)
    app.jinja_env.globals["alert_level"] = lambda pid: medical.alert_level(dbmod.get_db(), g.clinic["id"], pid)

    @app.context_processor
    def inject():
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(24)
        total, group = 0, None
        if g.get("user") and g.get("clinic"):
            from .services import orgs as _orgs, outbox as _ob
            total = _ob.attention(dbmod.get_db(), g.clinic, g.user)["total"]
            group = _orgs.for_user(dbmod.get_db(), g.user)
        return {"csrf_token": session["csrf"], "user": g.get("user"), "clinic": g.get("clinic"), "attn_total": total, "group": group,
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
        if request.endpoint in ("record.files_view", "record.import_start"):
            request.max_content_length = 12 * 1024 * 1024
        is_api = request.path.startswith("/api/")            # only the API blueprint lives under /api/
        public = request.endpoint in ("auth.login", "auth.login_code", "static", "health") or request.blueprint in ("public", "api") or is_api
        idle = app.config["SESSION_IDLE_MINUTES"]
        if g.user and not is_api and request.endpoint != "static":
            now = int(time.time())
            if idle and now - session.get("seen", now) > idle * 60:
                session.clear()                          # no activity for too long: sign out, as a shared reception computer needs
                g.user = g.clinic = None
            else:
                session["seen"] = now
        g.doc_lang = None
        if g.user:
            g.lang = i18n.user_lang(g.user, g.clinic)
        else:
            chosen = i18n.pick(request.args.get("lang")) if not is_api and request.endpoint != "static" else None
            if chosen:
                session["lang"] = chosen                  # a visitor's own choice on the sign-in, booking and patient pages
            g.lang = i18n.pick(session.get("lang")) or i18n.from_header(request.headers.get("Accept-Language")) or i18n.DEFAULT
        if not g.user and not public:
            return redirect(url_for("auth.login", next=request.full_path if request.method == "GET" else None))
        if (g.user and g.user["must_change_password"] and not is_api
                and request.endpoint not in ("auth.change_password", "auth.logout", "static", "health")):
            return redirect(url_for("auth.change_password"))
        if (g.user and g.clinic["require_2fa"] and not g.user["totp_enabled"] and not is_api
                and request.endpoint not in ("auth.security", "auth.security_setup", "auth.change_password", "auth.logout", "static", "health")):
            return redirect(url_for("auth.security_setup"))
        if request.method == "POST" and request.endpoint != "static" and not is_api:
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

    @app.get("/health")
    def health():
        return "ok"

    from . import auth, views, clinical, record, commercial, finance, comms, public, api, ops, privacy
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
        cur = conn.execute(
            """INSERT INTO clinics(name, whatsapp, country, currency, timezone, payment_methods)
               VALUES (?,?,?,?,?,?)""", (name.strip(), wa, d["country"], currency, timezone, d["payment_methods"]))
        cid = cur.lastrowid
        conn.execute("INSERT INTO chairs(clinic_id, name) VALUES (?, 'Chair 1')", (cid,))
        from .services import recalls as _recalls, treatment as _tr
        _tr.ensure_catalogue(conn, cid)
        _recalls.ensure_default_type(conn, cid)
        from .services import consent as _consent
        _consent.ensure_templates(conn, cid)
        from .services import outbox as _outbox
        _outbox.ensure_defaults(conn, cid)
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

    @app.cli.command("seed-demo")
    @click.option("--clinic-id", type=int, prompt="Clinic id (see list-clinics)")
    def seed_demo(clinic_id):
        """Add made-up patients and appointments to one clinic so you can see the screens. Not real people."""
        from .demo import seed
        conn = dbmod.connect(app.config["DATABASE"])
        click.echo(seed(conn, clinic_id))

    return app
