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
# 0.71: Dental OS HQ. A person belongs to one or more departments; owners make up Leadership and reach everything.
DEPTS = {
    "support": "Customer support", "finance": "Finance", "sales": "Sales", "onboarding": "Onboarding",
    "engineering": "Engineering", "compliance": "Compliance",
}
DEPT_ABOUT = {
    "support": "One inbox for every clinic's questions and faults, with answer-by times, and the help library. Opens a clinic only when its manager grants support access, and only to look.",
    "finance": "Subscriptions, bills, payments in, clinics that are behind, pausing a clinic that has not paid, and the VAT report.",
    "sales": "Clinics we are talking to, demos, trials, quotes, won and lost, and each salesperson's figures. Hands won clinics to onboarding.",
    "onboarding": "Each new clinic's set-up checklist, moving their data, training sessions and go-live day.",
    "engineering": "Pages that broke, problem reports clinics shared, and which version each clinic runs. The code itself is in GitHub and on the servers.",
    "compliance": "Data requests and incidents, the outside services that touch clinic data, and checks of who in HQ has access.",
}
ALL = set(DEPTS)
CAN = {
    "clinics": ALL,                                          # the list of clinics, counts only
    "clinic.state": {"finance"},                             # pause, close, reopen
    "clinic.trial": {"finance", "sales"},                    # start or extend a trial
    "clinic.plan": {"finance", "sales"},
    "clinic.bills": {"finance"},
    "clinic.support": {"support"},                           # open a clinic that has granted support access
    "health": {"engineering", "support"},
    "plans.view": {"finance", "sales"},
    "plans.edit": set(),                                     # owners only
    "team": set(),                                           # owners only
    "hq.leadership": set(),                                  # owners only: the company dashboard
    "hq.support": {"support"},
    "hq.articles": {"support", "onboarding"},
    "hq.finance": {"finance"},
    "hq.sales": {"sales"},
    "hq.onboarding": {"onboarding"},
    "hq.engineering": {"engineering"},                       # 0.72: server health, clinic servers, releases
    "hq.requests": {"engineering", "support", "sales"},      # features clinics ask for
    "hq.compliance": {"compliance"},
}
TEMP_LENGTH = 12


class TeamError(Exception):
    pass


def can(op, action, depts=None):
    """Owners reach everything. Anyone else needs one of their departments in CAN[action]. Without a list of departments
    (older callers), the 0.69 role stands in for one department."""
    if not op:
        return False
    if (op["role"] or "owner") == "owner":
        return True
    mine = depts if depts is not None else ({op["role"]} if op["role"] in DEPTS else set())
    return bool(set(mine) & CAN[action])


def memberships(conn, op_id):
    """{dept: is_head} for one person. A 0.69 login with a role and no departments yet counts as being in that role's department."""
    rows = conn.execute("SELECT dept, is_head FROM hq_members WHERE operator_id = ?", (op_id,)).fetchall()
    if rows:
        return {r["dept"]: bool(r["is_head"]) for r in rows if r["dept"] in DEPTS}
    role = conn.execute("SELECT role FROM operators WHERE id = ?", (op_id,)).fetchone()
    return {role[0]: False} if role and role[0] in DEPTS else {}


def is_owner(op):
    return bool(op) and (op["role"] or "owner") == "owner"


def heads_of(conn, op):
    """The departments this person leads (owners lead them all)."""
    if is_owner(op):
        return set(DEPTS)
    return {d for d, head in memberships(conn, op["id"]).items() if head}


def set_departments(conn, owner, op_id, depts, heads, owner_flag):
    """The owner places someone: Leadership (owner) or not, their departments, and which of those they lead."""
    row = _get(conn, op_id)
    depts = [d for d in DEPTS if d in set(depts or [])]
    heads = [d for d in depts if d in set(heads or [])]
    if row["id"] == owner["id"]:
        raise TeamError(OWN_LOGIN)
    if not owner_flag and not depts:
        raise TeamError(NO_DEPT)
    if row["role"] == "owner" and not owner_flag and not _owners_left(conn, row["id"]):
        raise TeamError(LAST_OWNER)
    conn.execute("DELETE FROM hq_members WHERE operator_id = ?", (op_id,))
    for d in depts:
        conn.execute("INSERT INTO hq_members(operator_id, dept, is_head, added_by) VALUES (?,?,?,?)", (op_id, d, 1 if d in heads else 0, owner["id"]))
    role = "owner" if owner_flag else (depts[0] if depts[0] in ROLES else "staff")
    conn.execute("UPDATE operators SET role = ? WHERE id = ?", (role, op_id))
    _log(conn, owner["id"], "team.depts", f"{row['username']}: {'owner ' if owner_flag else ''}{', '.join(depts)}"
                                           + (f"; leads {', '.join(heads)}" if heads else ""))


def set_dates(conn, owner, op_id, starts_on, ends_on, email=None):
    import datetime as _dt
    row = _get(conn, op_id)
    vals = []
    for v in (starts_on, ends_on):
        v = (v or "").strip()
        if v:
            try:
                _dt.date.fromisoformat(v)
            except ValueError as exc:
                raise TeamError(BAD_DATE) from exc
        vals.append(v or None)
    if vals[0] and vals[1] and vals[1] < vals[0]:
        raise TeamError(BAD_DATE)
    if row["id"] == owner["id"] and vals[1]:
        raise TeamError(OWN_LOGIN)
    email = (email or "").strip()[:120] or None
    conn.execute("UPDATE operators SET starts_on = ?, ends_on = ?, email = ? WHERE id = ?", (vals[0], vals[1], email, op_id))
    _log(conn, owner["id"], "team.dates", f"{row['username']}: {vals[0] or '-'} to {vals[1] or '-'}")


def in_dates(op, today=None):
    """True when today falls between the login's start and end days (either may be empty)."""
    import datetime as _dt
    today = today or _dt.date.today().isoformat()
    keys = op.keys() if hasattr(op, "keys") else []
    s = op["starts_on"] if "starts_on" in keys else None
    e = op["ends_on"] if "ends_on" in keys else None
    return not ((s and today < s) or (e and today > e))


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


def team(conn, dept=None):
    rows = conn.execute("SELECT * FROM operators ORDER BY active DESC, display_name").fetchall()
    out = []
    for r in rows:
        m = memberships(conn, r["id"])
        if dept and dept not in m:
            continue
        last = conn.execute("SELECT MAX(at) FROM operator_log WHERE operator_id = ? AND action = 'login'", (r["id"],)).fetchone()[0]
        out.append({"o": r, "last": last, "two_step": bool(r["totp_secret"]), "depts": m, "owner": r["role"] == "owner",
                    "in_dates": in_dates(r)})
    return out


def add(conn, owner, username, display_name, role, depts=None):
    """Returns (id, one-time password). The person must choose their own password and set up two-step login when they sign in.
    0.71: role is 'owner' or a first department; depts adds more departments."""
    from . import platform as PF
    if role not in ROLES and role not in DEPTS:
        raise TeamError("Pick a role.")
    temp = _temp() + "-Dx7"                                 # always passes the password rules; it works once
    try:
        oid = PF.add_operator(conn, username, display_name, temp)
    except PF.PlatformError as exc:
        raise TeamError(str(exc)) from exc
    conn.execute("UPDATE operators SET role = ?, must_change = 1 WHERE id = ?", (role if role in ROLES else "staff", oid))
    if role != "owner":
        for d in [role] + [d for d in (depts or []) if d in DEPTS and d != role]:
            conn.execute("INSERT INTO hq_members(operator_id, dept, added_by) VALUES (?,?,?)", (oid, d, owner["id"]))
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
    conn.execute("DELETE FROM hq_members WHERE operator_id = ?", (op_id,))
    if role in DEPTS:
        conn.execute("INSERT INTO hq_members(operator_id, dept, added_by) VALUES (?,?,?)", (op_id, role, owner["id"]))
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
NO_DEPT = "Put the person in at least one department, or make them an owner."
BAD_DATE = "Check the dates: use the date boxes, and the end cannot be before the start."
