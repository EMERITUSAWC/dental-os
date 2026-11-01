"""Running Dental OS as a service for many clinics (versions 0.53 and 0.54).

Clinics. A clinic can sign itself up (when the server allows it), which makes the clinic in a free trial with its first manager.
Each clinic has a state: trial, active, past_due (a bill is late), suspended or cancelled.

  trial / active       everything works.
  past_due             everything works, with a banner, for a grace period after the bill was due; after that the clinic can look
                       but not change anything, except paying the bill.
  suspended/cancelled  staff can sign in, see a page that says so, and take a full copy of the clinic's data. Nothing else.
                       A clinic is never locked out of its own records: they belong to the clinic, not to us.

Operators. The people who run the service sign in to a console of their own, apart from clinic staff. The console shows each clinic's
name, country, state, plan and how many patients, staff and bills it has, never a patient's name or record.

Support access. When a clinic wants help, its manager grants support access for a few hours. Only then can an operator open the
clinic, and only to look: every page they open is written in the clinic's activity log under "Dental OS support", and the manager can
end it at any time. No web framework here."""
import datetime
import re
import secrets

from werkzeug.security import check_password_hash, generate_password_hash

from . import audit, passwords, regions

STATES = {"trial": "Free trial", "active": "Active", "past_due": "Bill overdue", "suspended": "Paused", "cancelled": "Closed"}
TRIAL_DAYS = 30
GRACE_DAYS = 14
SUPPORT_HOURS = (1, 4, 24, 72)
USERNAME = re.compile(r"^[a-zA-Z0-9._-]{3,40}$")


class PlatformError(ValueError):
    pass


def _today():
    return datetime.date.today()


# --------------------------------------------------------------------------------------------------------- making a clinic
def setup_clinic(conn, name, country_code, currency=None, timezone=None, whatsapp=None, state="active", trial_days=TRIAL_DAYS):
    """Make a clinic with its starter lists. Used by the command line and by sign-up."""
    from . import consent, letters, outbox, recalls, treatment
    from .phone import normalise_phone
    from .timeutil import valid_timezone
    name = (name or "").strip()
    if not name or len(name) > 120:
        raise PlatformError("Give the clinic's name.")
    d = regions.defaults_for(country_code)
    currency = (currency or d["currency"]).upper()
    if currency not in regions.CURRENCIES:
        raise PlatformError(f"Unknown currency {currency}.")
    timezone = timezone or d["timezone"]
    if not valid_timezone(timezone):
        raise PlatformError(f"Unknown timezone {timezone}.")
    wa = normalise_phone(whatsapp, d["country"]) if whatsapp else None
    trial_end = (_today() + datetime.timedelta(days=trial_days)).isoformat() if state == "trial" else None
    cid = conn.execute("""INSERT INTO clinics(name, whatsapp, country, currency, timezone, payment_methods, plan_state, trial_ends_on)
                          VALUES (?,?,?,?,?,?,?,?)""", (name, wa, d["country"], currency, timezone, d["payment_methods"], state, trial_end)).lastrowid
    conn.execute("INSERT INTO chairs(clinic_id, name) VALUES (?, 'Chair 1')", (cid,))
    treatment.ensure_catalogue(conn, cid)
    recalls.ensure_default_type(conn, cid)
    consent.ensure_templates(conn, cid)
    outbox.ensure_defaults(conn, cid)
    letters.ensure_starters(conn, cid)
    return cid, d


def signup(conn, form, ip_key, allowed=True):
    """A clinic signing itself up. Returns (clinic id, user id). Refuses quietly when sign-up is off."""
    from . import throttle
    if not allowed:
        raise PlatformError("Sign-up is not open on this server. Contact us to set up your clinic.")
    bucket = "signup:" + ip_key
    if throttle.count(conn, bucket, 60) >= 5:
        raise PlatformError("Too many sign-ups from here in the last hour. Try again later.")
    throttle.record(conn, bucket)
    conn.commit()                                         # the try is counted even if the form is wrong
    clinic_name = (form.get("clinic_name") or "").strip()
    person = (form.get("your_name") or "").strip()[:80]
    username = (form.get("username") or "").strip()
    pw = form.get("password") or ""
    if not clinic_name or not person:
        raise PlatformError("Give the clinic's name and your own name.")
    if not USERNAME.match(username):
        raise PlatformError("A username is 3 to 40 letters, digits, dots, dashes or underscores.")
    if conn.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone():
        raise PlatformError("That username is taken. Pick another.")
    problem = passwords.check(pw, username, person)
    if problem:
        raise PlatformError(problem)
    if pw != (form.get("password2") or ""):
        raise PlatformError("The two passwords are not the same.")
    if form.get("agree") != "1":
        raise PlatformError("Tick that you agree to the terms of service and the data processing terms.")
    try:
        cid, _d = setup_clinic(conn, clinic_name, form.get("country") or "", state="trial")
    except ValueError as exc:
        raise PlatformError(str(exc))
    uid = conn.execute("INSERT INTO users(clinic_id, username, display_name, role, password_hash) VALUES (?,?,?, 'manager', ?)",
                       (cid, username, person, generate_password_hash(pw))).lastrowid
    audit.log(conn, cid, uid, "clinic.signup", None, None, clinic_name[:80])
    return cid, uid


# ----------------------------------------------------------------------------------------------------- a clinic's state
def state(clinic, today=None):
    """The state that counts today: a trial that has ended counts as overdue."""
    s = clinic["plan_state"] or "active"
    if s == "trial" and clinic["trial_ends_on"] and clinic["trial_ends_on"] < (today or _today()).isoformat():
        return "past_due"
    return s


def due_since(conn, clinic):
    """When the clinic first owed us: the end of its trial, or its oldest unpaid bill's due date."""
    row = conn.execute("SELECT MIN(due_on) FROM platform_invoices WHERE clinic_id = ? AND status = 'open'", (clinic["id"],)).fetchone()
    dates = [d for d in (row[0] if row else None, clinic["trial_ends_on"] if clinic["plan_state"] == "trial" else None) if d]
    return min(dates) if dates else None


def access(conn, clinic, today=None):
    """'full', 'look' (can look and pay, not change), or 'closed' (only a copy of the data)."""
    today = today or _today()
    s = state(clinic, today)
    if s in ("suspended", "cancelled"):
        return "closed"
    if s == "past_due":
        since = due_since(conn, clinic)
        if since and datetime.date.fromisoformat(since) + datetime.timedelta(days=GRACE_DAYS) < today:
            return "look"
    return "full"


def set_state(conn, op_id, cid, new, reason=None, trial_days=None):
    if new not in STATES:
        raise PlatformError("Pick a state.")
    c = conn.execute("SELECT * FROM clinics WHERE id = ?", (cid,)).fetchone()
    if not c:
        raise PlatformError("That clinic was not found.")
    reason = (reason or "").strip()[:300] or None
    if new in ("suspended", "cancelled") and not reason:
        raise PlatformError("Say why. The clinic's manager will see it.")
    trial_end = c["trial_ends_on"]
    if new == "trial":
        days = trial_days if isinstance(trial_days, int) and 1 <= trial_days <= 365 else TRIAL_DAYS
        trial_end = (_today() + datetime.timedelta(days=days)).isoformat()
    conn.execute("UPDATE clinics SET plan_state = ?, state_reason = ?, trial_ends_on = ? WHERE id = ?", (new, reason, trial_end, cid))
    conn.execute("INSERT INTO operator_log(operator_id, clinic_id, action, detail) VALUES (?,?,?,?)", (op_id, cid, "state." + new, reason))


# ------------------------------------------------------------------------------------------------------------ operators
def add_operator(conn, username, display_name, password):
    username = (username or "").strip()
    if not USERNAME.match(username):
        raise PlatformError("A username is 3 to 40 letters, digits, dots, dashes or underscores.")
    problem = passwords.check(password or "", username, display_name)
    if problem:
        raise PlatformError(problem)
    if conn.execute("SELECT 1 FROM operators WHERE username = ?", (username,)).fetchone():
        raise PlatformError("That operator already exists.")
    return conn.execute("INSERT INTO operators(username, display_name, password_hash) VALUES (?,?,?)",
                        (username, (display_name or username).strip()[:80], generate_password_hash(password))).lastrowid


def operator_login(conn, username, password, ip_key):
    from . import throttle
    bucket = "oplogin:" + ip_key
    if throttle.count(conn, bucket, 15) >= 5:
        raise PlatformError("Too many tries. Wait 15 minutes.")
    row = conn.execute("SELECT * FROM operators WHERE username = ? AND active = 1", ((username or "").strip(),)).fetchone()
    ok = bool(row) and check_password_hash(row["password_hash"], password or "")
    if not ok:
        throttle.record(conn, bucket)
        raise PlatformError("That username and password do not match.")
    conn.execute("INSERT INTO operator_log(operator_id, action) VALUES (?, 'login')", (row["id"],))
    return row


def clinics_overview(conn):
    """Each clinic with counts only. No patient names or records."""
    out = []
    for c in conn.execute("SELECT * FROM clinics ORDER BY id").fetchall():
        n = lambda sql: conn.execute(sql, (c["id"],)).fetchone()[0]
        out.append({"c": c, "state": state(c), "patients": n("SELECT COUNT(*) FROM patients WHERE clinic_id = ? AND status = 'active'"),
                    "staff": n("SELECT COUNT(*) FROM users WHERE clinic_id = ? AND active = 1"),
                    "last_seen": conn.execute("""SELECT MAX(a.at) FROM audit_log a WHERE a.clinic_id = ? AND a.action LIKE 'login%'""", (c["id"],)).fetchone()[0],
                    "open_bills": n("SELECT COUNT(*) FROM platform_invoices WHERE clinic_id = ? AND status = 'open'"),
                    "plan": conn.execute("SELECT name FROM plans WHERE id = ?", (c["plan_id"],)).fetchone() if c["plan_id"] else None})
    return out


# -------------------------------------------------------------------------------------------------------- support access
def grant_support(conn, clinic, manager_id, hours, why=None):
    if hours not in SUPPORT_HOURS:
        raise PlatformError("Pick how long support may look.")
    until = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    gid = conn.execute("INSERT INTO support_grants(clinic_id, granted_by, until_at, why) VALUES (?,?,?,?)",
                       (clinic["id"], manager_id, until, (why or "").strip()[:300] or None)).lastrowid
    _support_user(conn, clinic["id"])
    audit.log(conn, clinic["id"], manager_id, "support.grant", None, None, f"{hours} hours")
    return gid


def end_support(conn, clinic, manager_id):
    n = conn.execute("UPDATE support_grants SET ended_at = datetime('now'), ended_by = ? WHERE clinic_id = ? AND ended_at IS NULL",
                     (manager_id, clinic["id"])).rowcount
    audit.log(conn, clinic["id"], manager_id, "support.end", None, None, str(n))
    return n


def live_grant(conn, cid, now=None):
    now = (now or datetime.datetime.now(datetime.timezone.utc)).strftime("%Y-%m-%d %H:%M:%S")
    return conn.execute("""SELECT * FROM support_grants WHERE clinic_id = ? AND ended_at IS NULL AND until_at > ? ORDER BY id DESC LIMIT 1""",
                        (cid, now)).fetchone()


def _support_user(conn, cid):
    """The stand-in user that support views the clinic as. It cannot sign in itself (no password works)."""
    row = conn.execute("SELECT * FROM users WHERE clinic_id = ? AND username = ?", (cid, f"support-{cid}")).fetchone()
    if row:
        return row
    conn.execute("INSERT INTO users(clinic_id, username, display_name, role, password_hash, active) VALUES (?,?, 'Dental OS support', 'manager', ?, 0)",
                 (cid, f"support-{cid}", "!" + secrets.token_hex(16)))
    return conn.execute("SELECT * FROM users WHERE clinic_id = ? AND username = ?", (cid, f"support-{cid}")).fetchone()


def support_user(conn, cid):
    return _support_user(conn, cid)


def grants(conn, cid):
    return conn.execute("SELECT g.*, u.display_name FROM support_grants g LEFT JOIN users u ON u.id = g.granted_by WHERE g.clinic_id = ? ORDER BY g.id DESC LIMIT 20",
                        (cid,)).fetchall()
