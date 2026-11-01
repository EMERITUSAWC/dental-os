import time
from functools import wraps
from urllib.parse import urlparse

from flask import Blueprint, abort, current_app, flash, g, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash

from werkzeug.security import generate_password_hash

from .safeurl import local_path
from .db import get_db
from .services import audit, i18n, passwords, permissions, qr, throttle, twofactor

bp = Blueprint("auth", __name__)

_DUMMY = "scrypt:32768:8:1$dummy$" + "0" * 128   # keeps timing similar when the username is unknown


def manager_required(view):
    @wraps(view)
    def wrapped(*a, **kw):
        if g.user["role"] != "manager":
            abort(403)
        return view(*a, **kw)
    return wrapped


def clinical_required(view):
    """Managers, dentists and assistants only. Front desk staff see alerts but not clinical records."""
    @wraps(view)
    def wrapped(*a, **kw):
        if not permissions.can_clinical(g.user):
            abort(403)
        return view(*a, **kw)
    return wrapped


def signer_required(view):
    @wraps(view)
    def wrapped(*a, **kw):
        if not permissions.can_sign(g.user):
            abort(403)
        return view(*a, **kw)
    return wrapped


LOGIN_FAILS_PER_ADDRESS = 30     # wrong passwords from one address in 15 minutes, across all usernames


def _safe_next(target):
    return local_path(target)


def _sign_in(row, nxt):
    session.clear()
    session.permanent = True
    session["uid"] = row["id"]
    session["seen"] = int(time.time())
    if row["must_change_password"]:
        return redirect(url_for("auth.change_password"))
    return redirect(nxt or url_for("views.dashboard"))


@bp.route("/login/code", methods=["GET", "POST"])
def login_code():
    """The second step: a code from the authenticator app, or one recovery code."""
    if g.user:                                    # already signed in: opening this page must not sign them out
        return redirect(url_for("views.dashboard"))
    uid, started = session.get("pending_uid"), session.get("pending_at", 0)
    if not uid or time.time() - started > 300:
        session.clear()
        return redirect(url_for("auth.login"))
    db = get_db()
    row = db.execute("SELECT * FROM users WHERE id = ? AND active = 1", (uid,)).fetchone()
    if not row:
        session.clear()
        return redirect(url_for("auth.login"))
    error = None
    if request.method == "POST":
        result = twofactor.verify_login(db, current_app.config["SECRET_KEY"], row, request.form.get("code"))
        if result in ("totp", "recovery"):
            audit.log(db, row["clinic_id"], row["id"], "login.ok", detail="recovery code" if result == "recovery" else "two-step")
            nxt = session.get("pending_next")
            resp = _sign_in(row, nxt)
            if result == "recovery":
                left = twofactor.recovery_left(db, row["id"])
                flash(f"You used a recovery code. {left} left. Make new ones under My security when you can.", "ok")
            return resp
        audit.log(db, row["clinic_id"], row["id"], "login.2fa.failed")
        error = ("Too many wrong codes. Wait about 15 minutes, or ask a manager to reset your two-step login."
                 if result == "locked" else "That code is not right. Try the newest code in your app, or use a recovery code.")
    return render_template("login_code.html", error=error)


@bp.route("/login", methods=["GET", "POST"])
def login():
    if g.user:
        return redirect(url_for("views.dashboard"))
    error = None
    if request.method == "POST":
        db = get_db()
        username = (request.form.get("username") or "").strip()
        failures = db.execute(
            "SELECT COUNT(*) FROM login_attempts WHERE username = ? AND ok = 0 AND at > datetime('now','-15 minutes')",
            (username,)).fetchone()[0]
        # 0.56: also a limit per network address, so one address cannot try a common password on many usernames.
        ip_bucket = "login-bad:" + throttle.key(current_app.config["SECRET_KEY"], request.remote_addr or "unknown")
        if failures >= 5:
            error = "Too many tries. Wait 15 minutes, or ask the manager to help."
        elif throttle.count(db, ip_bucket, 15) >= LOGIN_FAILS_PER_ADDRESS:
            error = "Too many wrong logins from this network. Wait 15 minutes and try again."
        else:
            row = db.execute("SELECT * FROM users WHERE username = ? AND active = 1", (username,)).fetchone()
            ok = False
            try:
                ok = check_password_hash(row["password_hash"] if row else _DUMMY, request.form.get("password") or "")
            except ValueError:
                ok = False
            ok = bool(ok and row)
            db.execute("INSERT INTO login_attempts(username, ok) VALUES (?,?)", (username, 1 if ok else 0))
            if not ok:
                throttle.record(db, ip_bucket)
            audit.log(db, row["clinic_id"] if row else None, row["id"] if row else None,
                      ("login.password.ok" if row["totp_enabled"] else "login.ok") if ok else "login.failed",
                      detail=None if ok else username[:40])
            if ok:
                nxt = _safe_next(request.args.get("next"))
                if row["totp_enabled"]:
                    session.clear()                       # the password alone does not sign anyone in
                    session["pending_uid"] = row["id"]
                    session["pending_at"] = int(time.time())
                    session["pending_next"] = nxt
                    return redirect(url_for("auth.login_code"))
                return _sign_in(row, nxt)
            error = error or "That username and password do not match."
    return render_template("login.html", error=error)


@bp.post("/logout")
def logout():
    if g.user:
        audit.log(get_db(), g.user["clinic_id"], g.user["id"], "logout")
    session.clear()
    return redirect(url_for("auth.login"))


@bp.route("/account/password", methods=["GET", "POST"])
def change_password():
    error = None
    if request.method == "POST":
        db = get_db()
        current, new, again = (request.form.get(k) or "" for k in ("current", "new", "again"))
        try:
            good = check_password_hash(g.user["password_hash"], current)
        except ValueError:
            good = False
        if not good:
            error = "The current password is not right."
        elif passwords.check(new, g.user["username"], g.user["display_name"]):
            error = passwords.check(new, g.user["username"], g.user["display_name"])
        elif new == current:
            error = "Choose a password you have not used just now."
        elif new != again:
            error = "The two new passwords do not match."
        else:
            db.execute("UPDATE users SET password_hash = ?, must_change_password = 0 WHERE id = ?",
                       (generate_password_hash(new), g.user["id"]))
            audit.log(db, g.clinic["id"], g.user["id"], "password.change", "user", g.user["id"])
            flash("Password changed.", "ok")
            return redirect(url_for("views.dashboard"))
    return render_template("password.html", error=error, forced=bool(g.user["must_change_password"]))


# ----------------------------------------------------------------------------- my security (two-step login)
def _password_ok(user, text):
    try:
        return check_password_hash(user["password_hash"], text or "")
    except ValueError:
        return False


def _qr_for(secret):
    label = f"{g.clinic['name']}: {g.user['username']}"
    link = twofactor.uri(secret, g.user["username"], "Dental OS")
    try:
        return link, qr.svg(link, 220, "QR code for your authenticator app")
    except qr.QRError:
        return link, None


@bp.get("/account/security")
def security():
    db = get_db()
    return render_template("security.html", left=twofactor.recovery_left(db, g.user["id"]) if g.user["totp_enabled"] else 0,
                           required=bool(g.clinic["require_2fa"]), error=None)


@bp.post("/account/language")
def security_language():
    db = get_db()
    choice = i18n.pick(request.form.get("language"))
    db.execute("UPDATE users SET language = ? WHERE id = ?", (choice, g.user["id"]))
    audit.log(db, g.clinic["id"], g.user["id"], "user.language", "user", g.user["id"], choice or "clinic")
    flash("Saved.", "ok")
    return redirect(url_for("auth.security"))


@bp.route("/account/security/setup", methods=["GET", "POST"])
def security_setup():
    db = get_db()
    if g.user["totp_enabled"]:
        return redirect(url_for("auth.security"))
    key = current_app.config["SECRET_KEY"]
    error = None
    if request.method == "POST":
        try:
            codes = twofactor.confirm_setup(db, key, g.user, request.form.get("code"))
            return render_template("security_codes.html", codes=codes, fresh=True)
        except twofactor.TwoFactorError as exc:
            error = str(exc)
    secret = twofactor.begin_setup(db, key, g.user)
    link, svg = _qr_for(secret)
    return render_template("security_setup.html", secret=twofactor.spaced(secret), link=link, svg=svg, error=error,
                           required=bool(g.clinic["require_2fa"]))


@bp.post("/account/security/codes")
def security_new_codes():
    db = get_db()
    if not g.user["totp_enabled"]:
        abort(404)
    key = current_app.config["SECRET_KEY"]
    if not _password_ok(g.user, request.form.get("password")):
        return render_template("security.html", left=twofactor.recovery_left(db, g.user["id"]), required=bool(g.clinic["require_2fa"]),
                               error="Your password is not right."), 400
    if twofactor.verify_login(db, key, g.user, request.form.get("code")) not in ("totp", "recovery"):
        return render_template("security.html", left=twofactor.recovery_left(db, g.user["id"]), required=bool(g.clinic["require_2fa"]),
                               error="That code is not right."), 400
    codes = twofactor.new_recovery_codes(db, g.user["id"])
    audit.log(db, g.clinic["id"], g.user["id"], "2fa.codes")
    return render_template("security_codes.html", codes=codes, fresh=False)


@bp.post("/account/security/off")
def security_off():
    db = get_db()
    if not g.user["totp_enabled"]:
        abort(404)
    left = twofactor.recovery_left(db, g.user["id"])
    if g.clinic["require_2fa"]:
        return render_template("security.html", left=left, required=True,
                               error="Your clinic requires two-step login, so it cannot be switched off."), 400
    if not _password_ok(g.user, request.form.get("password")):
        return render_template("security.html", left=left, required=False, error="Your password is not right."), 400
    twofactor.disable(db, g.user["id"], g.user["id"], g.clinic["id"])
    flash("Two-step login is off.", "ok")
    return redirect(url_for("auth.security"))
