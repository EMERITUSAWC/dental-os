"""Version 0.69: the company's own staff and their roles.

The people who run the service (the operators) used to share one kind of login with full power. Now each has a role, and each role
reaches only the part of the operator area its job needs. The owner adds people, changes roles, switches logins off and resets
passwords or two-step login. A new person signs in with a one-time password, then must choose their own and set up two-step
login before they see anything. Everything is written in the operator log. There must always be one owner left.

Software engineers do not need more than the error list here: their work happens in GitHub and on the server, each with its own
access. No web framework here."""
import secrets

from werkzeug.security import generate_password_hash, check_password_hash

from . import passwords

ROLES = {"owner": "Owner", "support": "Customer service", "finance": "Finance", "sales": "Sales", "engineering": "Engineering"}
ROLE_ABOUT = {
    "owner": "Everything, and the team: adds people, changes roles, switches logins off.",
    "support": "Looks inside a clinic only after its manager grants support access, and only to look. Sees the error list.",
    "finance": "Bills, payments and pausing a clinic that has not paid. Cannot open a clinic.",
    "sales": "Trials and plans. Cannot see bills or open a clinic.",
    "engineering": "The error list and problem reports. Cannot open a clinic or change money.",
}
ALL = set(ROLES)
CAN = {
    "clinics": ALL,                                          # the list of clinics, counts only
    "clinic.state": {"owner", "finance"},                    # pause, close, reopen
    "clinic.trial": {"owner", "finance", "sales"},           # start or extend a trial
    "clinic.plan": {"owner", "finance", "sales"},
    "clinic.bills": {"owner", "finance"},
    "clinic.support": {"owner", "support"},                  # open a clinic that has granted support access
    "health": {"owner", "engineering", "support"},
    "plans.view": {"owner", "finance", "sales"},
    "plans.edit": {"owner"},
    "team": {"owner"},
}
TEMP_LENGTH = 12


class TeamError(Exception):
    pass


def can(op, action):
    return bool(op) and (op["role"] or "owner") in CAN[action]


def _log(conn, op_id, action, detail=None):
    conn.execute("INSERT INTO operator_log(operator_id, action, detail) VALUES (?,?,?)", (op_id, action, detail))


def _temp():
    return secrets.token_urlsafe(TEMP_LENGTH)[:TEMP_LENGTH]


def _get(conn, op_id):
    row = conn.execute("SELECT * FROM operators WHERE id = ?", (op_id,)).fetchone()
    if not row:
        raise LookupError("operator")
    return row


def _owners_left(conn, without=None):
    return conn.execute("SELECT COUNT(*) FROM operators WHERE role = 'owner' AND active = 1 AND id <> ?", (without or 0,)).fetchone()[0]


def team(conn):
    rows = conn.execute("SELECT * FROM operators ORDER BY active DESC, display_name").fetchall()
    out = []
    for r in rows:
        last = conn.execute("SELECT MAX(at) FROM operator_log WHERE operator_id = ? AND action = 'login'", (r["id"],)).fetchone()[0]
        out.append({"o": r, "last": last, "two_step": bool(r["totp_secret"])})
    return out


def add(conn, owner, username, display_name, role):
    """Returns (id, one-time password). The person must choose their own password and set up two-step login when they sign in."""
    from . import platform as PF
    if role not in ROLES:
        raise TeamError("Pick a role.")
    temp = _temp() + "-Dx7"                                 # always passes the password rules; it works once
    try:
        oid = PF.add_operator(conn, username, display_name, temp)
    except PF.PlatformError as exc:
        raise TeamError(str(exc)) from exc
    conn.execute("UPDATE operators SET role = ?, must_change = 1 WHERE id = ?", (role, oid))
    _log(conn, owner["id"], "team.add", f"{username.strip()} as {role}")
    return oid, temp


def set_role(conn, owner, op_id, role):
    if role not in ROLES:
        raise TeamError("Pick a role.")
    row = _get(conn, op_id)
    if row["id"] == owner["id"]:
        raise TeamError(OWN_LOGIN)
    if row["role"] == "owner" and role != "owner" and not _owners_left(conn, row["id"]):
        raise TeamError(LAST_OWNER)
    conn.execute("UPDATE operators SET role = ? WHERE id = ?", (role, op_id))
    _log(conn, owner["id"], "team.role", f"{row['username']} to {role}")


def switch(conn, owner, op_id, on):
    row = _get(conn, op_id)
    if row["id"] == owner["id"]:
        raise TeamError(OWN_LOGIN)
    if not on and row["role"] == "owner" and not _owners_left(conn, row["id"]):
        raise TeamError(LAST_OWNER)
    conn.execute("UPDATE operators SET active = ? WHERE id = ?", (1 if on else 0, op_id))
    _log(conn, owner["id"], "team.on" if on else "team.off", row["username"])


def reset_password(conn, owner, op_id):
    row = _get(conn, op_id)
    if row["id"] == owner["id"]:
        raise TeamError(OWN_LOGIN)
    temp = _temp() + "-Dx7"
    conn.execute("UPDATE operators SET password_hash = ?, must_change = 1 WHERE id = ?", (generate_password_hash(temp), op_id))
    _log(conn, owner["id"], "team.reset_password", row["username"])
    return temp


def reset_2fa(conn, owner, op_id):
    row = _get(conn, op_id)
    if row["id"] == owner["id"]:
        raise TeamError(OWN_LOGIN)
    conn.execute("UPDATE operators SET totp_secret = NULL, totp_last_step = 0 WHERE id = ?", (op_id,))
    _log(conn, owner["id"], "team.reset_2fa", row["username"])


def review(conn, owner):
    """The owner confirms the team is right: everyone still needs their login, in the role they have. Kept in the log."""
    n = conn.execute("SELECT COUNT(*) FROM operators WHERE active = 1").fetchone()[0]
    _log(conn, owner["id"], "team.review", f"{n} logins")


def last_review(conn):
    return conn.execute("""SELECT l.at, o.display_name FROM operator_log l LEFT JOIN operators o ON o.id = l.operator_id
                           WHERE l.action = 'team.review' ORDER BY l.id DESC LIMIT 1""").fetchone()


def change_password(conn, op, current, new):
    row = _get(conn, op["id"])
    if not check_password_hash(row["password_hash"], current or ""):
        raise TeamError("Your current password is not right.")
    if (current or "") == (new or ""):
        raise TeamError("Choose a new password, not the one you have now.")
    problem = passwords.check(new or "", row["username"], row["display_name"])
    if problem:
        raise TeamError(problem)
    conn.execute("UPDATE operators SET password_hash = ?, must_change = 0 WHERE id = ?", (generate_password_hash(new), op["id"]))
    _log(conn, op["id"], "password.change")


def needs_setup(op, require_2fa):
    """What a person must do before using the operator area: 'password', '2fa' or None."""
    if op["must_change"]:
        return "password"
    if require_2fa and not op["totp_secret"]:
        return "2fa"
    return None


OWN_LOGIN = "Ask another owner to change your own login."
LAST_OWNER = "There must always be one owner. Make someone else an owner first."
