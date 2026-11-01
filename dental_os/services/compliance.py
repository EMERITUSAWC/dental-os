"""Version 0.67: the security and compliance pack, ready for an outside audit.

Four things an auditor asks for and this module answers:

  * Proof the activity log has not been touched. The database refuses to change or delete a row (db.lock_logs). On top of
    that, a seal is made every day: an HMAC-SHA256 chain over every row in id order, keyed from the server's SECRET_KEY. Checking
    the log walks every row again and compares with each seal. A row changed, removed or slipped in behind the database's back
    breaks the chain from that seal on. Limit, said plainly: someone with the server's secret key AND write access to the database
    could rebuild every seal. So the newest seal value is printed in each evidence pack and on the page. An auditor who keeps it
    can check later that history was not rewritten (`flask audit-verify --anchor UPTO:CHAIN`).
  * An access review: a manager looks at every login, with role, two-step login and last sign-in, and decides keep, change or
    remove for each. The review is kept, and a login marked remove is switched off at once.
  * A list of every outside service the clinic has switched on, and what patient data goes to it.
  * One zip with all of it: the evidence pack, including a list of the software's parts (a CycloneDX SBOM).

Nothing here changes patient records, and nothing is deleted. No web framework here."""
import csv
import datetime
import hashlib
import hmac
import io
import json
import zipfile
from importlib import metadata

from . import audit
from .dataio import _safe_cell
from ..version import VERSION

REVIEW_EVERY_DAYS = 90                # access reviews: every quarter
IDLE_DAYS = 60                        # a login nobody has used for this long is flagged in the review
SEAL_STALE_HOURS = 49                 # the seal runs daily; two missed days is worth a look
LOG_FIELDS = ("id", "clinic_id", "user_id", "action", "object_type", "object_id", "detail", "at")
START = "0" * 64
BATCH = 5000

DECISIONS = {"keep": "Keep as it is", "change": "Change it (say what)", "remove": "Remove: switch the login off now"}
FLAGS = {"no_two_step": "No two-step login", "idle": "Not used for 60 days or more", "never": "Never signed in",
         "manager": "Manager: can change everything"}
STATES = {"ok": "In place", "attention": "Needs attention", "manual": "A person must check"}

# The controls the pack reports on. key: title. REFS holds where each sits in ISO/IEC 27001:2022 Annex A and the HIPAA Security
# Rule (45 CFR 164). The references help an auditor find their way; they are not a claim that the clinic meets either.
CONTROLS = {
    "accounts": "Every person has their own login",
    "two_step": "Two-step login",
    "idle": "Automatic sign-out when idle",
    "login_limits": "Limits on wrong passwords",
    "review": "Access review in the last 90 days",
    "managers": "Managers (full access) kept to a few",
    "log_locked": "Activity log cannot be changed or deleted",
    "records": "Signed notes and recorded payments cannot be changed",
    "log_sealed": "Activity log sealed and intact",
    "offsite": "Locked off-site copies, made every night",
    "drill": "Restore drill in the last 35 days",
    "https": "HTTPS and secure cookies",
    "secrets": "Keys and passwords for outside services stored encrypted",
    "incidents": "Data breaches written down and reported in time",
    "requests": "Patients' data requests answered in time",
    "outside": "Outside services listed, each with a contract",
    "sbom": "List of the software's parts (SBOM)",
    "code": "Automatic code checks",
    "contact": "A way to report a security problem (security.txt)",
    "host": "Disk encryption, server updates and staff training",
}
REFS = {
    "accounts": ("A.5.16", "164.312(a)(2)(i)"), "two_step": ("A.8.5", "164.312(d)"), "idle": ("A.8.5", "164.312(a)(2)(iii)"),
    "login_limits": ("A.8.5", "164.308(a)(5)(ii)(C)"), "review": ("A.5.18", "164.308(a)(4)"), "managers": ("A.8.2", "164.308(a)(4)"),
    "log_locked": ("A.8.15", "164.312(b)"), "log_sealed": ("A.8.15, A.5.28", "164.312(b), 164.312(c)(1)"),
    "records": ("A.5.33", "164.312(c)(1)"),
    "offsite": ("A.8.13", "164.308(a)(7)(ii)(A)"), "drill": ("A.8.13", "164.308(a)(7)(ii)(D)"), "https": ("A.8.24", "164.312(e)(1)"),
    "secrets": ("A.8.24", "164.312(a)(2)(iv)"), "incidents": ("A.5.24, A.5.26", "164.308(a)(6)"), "requests": ("A.5.34", ""),
    "outside": ("A.5.19, A.5.20", "164.308(b)(1)"), "sbom": ("A.8.8", "164.308(a)(1)(ii)(A)"), "code": ("A.8.28, A.8.29", ""),
    "contact": ("A.8.8", ""), "host": ("A.8.7, A.8.8, A.6.3", "164.308(a)(5)(i), 164.310(d)(1)"),
}


class ComplianceError(Exception):
    pass


def _utc(now=None):
    return now or datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


def _ts(value):
    try:
        return datetime.datetime.fromisoformat(str(value)[:19])
    except (TypeError, ValueError):
        return None


# ----------------------------------------------------------------------------------------------------------- the activity seal
def _key(secret_key):
    return hmac.new(str(secret_key).encode(), b"dental-os activity seal v1", hashlib.sha256).digest()


def key_id(secret_key):
    return hashlib.sha256(_key(secret_key)).hexdigest()[:12]


def _line(row):
    return json.dumps([row[f] for f in LOG_FIELDS], separators=(",", ":"), ensure_ascii=False, default=str).encode()


def _step(key, chain, row):
    return hmac.new(key, chain.encode() + b"\n" + _line(row), hashlib.sha256).hexdigest()


def _rows_after(conn, after_id, upto_id=None):
    last = after_id
    while True:
        sql = "SELECT " + ", ".join(LOG_FIELDS) + " FROM audit_log WHERE id > ?" + (" AND id <= ?" if upto_id is not None else "") + " ORDER BY id LIMIT ?"
        args = (last, upto_id, BATCH) if upto_id is not None else (last, BATCH)
        rows = conn.execute(sql, args).fetchall()
        if not rows:
            return
        yield from rows
        last = rows[-1]["id"]


def latest_seal(conn):
    return conn.execute("SELECT * FROM audit_seals ORDER BY id DESC LIMIT 1").fetchone()


def seal(conn, secret_key, user_id=None):
    """Seal every activity row added since the last seal. Returns the newest seal (None when the log is still empty)."""
    last = latest_seal(conn)
    chain, upto, count = (last["chain"], last["upto_id"], 0) if last else (START, 0, 0)
    key = _key(secret_key)
    for row in _rows_after(conn, upto):
        chain, upto, count = _step(key, chain, row), row["id"], count + 1
    if count:
        conn.execute("INSERT INTO audit_seals(upto_id, rows_sealed, chain, sealed_by, key_id) VALUES (?,?,?,?,?)",
                     (upto, count, chain, user_id, key_id(secret_key)))
        conn.commit()
    return latest_seal(conn)


def verify(conn, secret_key, anchor=None):
    """Walk the whole log again and compare with every seal. anchor is "UPTO:CHAIN" kept from an earlier pack, or None.

    Returns a dict: ok, checked (rows), seals, sealed_upto, unsealed (rows after the last seal), broken (the first seal that does
    not match, or None), anchor ("match", "different", "unknown" or None), other_key (seals made with an earlier SECRET_KEY,
    which cannot be checked now; check them before changing the key)."""
    key, kid = _key(secret_key), key_id(secret_key)
    seals = conn.execute("SELECT * FROM audit_seals ORDER BY id").fetchall()
    chain, upto, checked, broken, other = START, 0, 0, None, 0
    mine = False
    for s in seals:
        if s["key_id"] != kid:                # made before SECRET_KEY was changed: cannot be checked with today's key
            if mine:                          # but keys only change forwards, so one after today's key's seals is forged
                broken = s
                break
            chain, upto, other = s["chain"], s["upto_id"], other + 1
            continue
        mine = True
        n = 0
        for row in _rows_after(conn, upto, s["upto_id"]):
            chain, n = _step(key, chain, row), n + 1
        checked += n
        upto = s["upto_id"]
        if n != s["rows_sealed"] or not hmac.compare_digest(chain, s["chain"]):
            broken = s
            chain = s["chain"]                # carry on from the stored value, so one break is not reported over and over
            break
    unsealed = conn.execute("SELECT COUNT(*) FROM audit_log WHERE id > ?", (upto,)).fetchone()[0]
    result = {"ok": broken is None, "checked": checked, "seals": len(seals), "sealed_upto": upto, "unsealed": unsealed,
              "broken": broken, "anchor": None, "other_key": other}
    if anchor:
        try:
            a_upto, a_chain = str(anchor).strip().split(":", 1)
            a_upto = int(a_upto)
        except ValueError as exc:
            raise ComplianceError("The anchor looks like 1234:ab12…, the numbers printed in an earlier evidence pack.") from exc
        match = next((s for s in seals if s["upto_id"] == a_upto), None)
        if match is None:
            result["anchor"] = "unknown"
        else:
            result["anchor"] = "match" if hmac.compare_digest(match["chain"], a_chain.strip().lower()) else "different"
        result["ok"] = result["ok"] and result["anchor"] == "match"
    return result


def seal_stale(conn, now=None):
    last = latest_seal(conn)
    if not last:
        return conn.execute("SELECT 1 FROM audit_log LIMIT 1").fetchone() is not None
    when = _ts(last["sealed_at"])
    return when is None or (_utc(now) - when).total_seconds() > SEAL_STALE_HOURS * 3600


# ------------------------------------------------------------------------------------------------------------- the access review
def last_sign_in(db, user_id):
    return db.execute("SELECT MAX(at) FROM audit_log WHERE user_id = ? AND action = 'login.ok'", (user_id,)).fetchone()[0]


def access_list(db, cid, now=None):
    """Every login of this clinic, switched on first, with what the review needs to see."""
    now = _utc(now)
    groups = {r["id"]: r["name"] for r in db.execute("SELECT id, name FROM perm_groups WHERE clinic_id = ?", (cid,))}
    out = []
    for u in db.execute("SELECT * FROM users WHERE clinic_id = ? ORDER BY active DESC, display_name", (cid,)).fetchall():
        seen = last_sign_in(db, u["id"])
        flags = []
        if u["role"] == "manager":
            flags.append("manager")
        if not u["totp_enabled"]:
            flags.append("no_two_step")
        if not seen:
            created = _ts(u["created_at"])
            if created and (now - created).days >= IDLE_DAYS:
                flags.append("never")
        elif (now - (_ts(seen) or now)).days >= IDLE_DAYS:
            flags.append("idle")
        out.append({"u": u, "last": seen, "group": groups.get(u["perm_group_id"]), "flags": flags})
    return out


def review_status(db, cid, now=None):
    last = db.execute("""SELECT r.*, u.display_name FROM access_reviews r LEFT JOIN users u ON u.id = r.reviewed_by
                         WHERE r.clinic_id = ? ORDER BY r.id DESC LIMIT 1""", (cid,)).fetchone()
    if not last:
        return {"last": None, "due": None, "overdue": True}
    due = (_ts(last["reviewed_at"]) or _utc(now)) + datetime.timedelta(days=REVIEW_EVERY_DAYS)
    return {"last": last, "due": due.date(), "overdue": _utc(now) > due}


def reviews(db, cid, limit=20):
    return db.execute("""SELECT r.*, u.display_name FROM access_reviews r LEFT JOIN users u ON u.id = r.reviewed_by
                         WHERE r.clinic_id = ? ORDER BY r.id DESC LIMIT ?""", (cid, limit)).fetchall()


def review_items(db, cid, review_id):
    return db.execute("""SELECT i.*, u.display_name, u.username FROM access_review_items i JOIN users u ON u.id = i.user_id
                         WHERE i.clinic_id = ? AND i.review_id = ? ORDER BY u.display_name""", (cid, review_id)).fetchall()


def record_review(db, clinic, reviewer_id, decisions, notes, note=None):
    """decisions and notes: {user_id: text}. Every login that is switched on needs a decision; change and remove need a note.
    A login marked remove is switched off now. Your own login cannot be removed here."""
    cid = clinic["id"]
    rows = [r for r in access_list(db, cid) if r["u"]["active"]]
    if not rows:
        raise ComplianceError("There are no logins to review.")
    missing = [r["u"]["display_name"] for r in rows if decisions.get(r["u"]["id"]) not in DECISIONS]
    if missing:
        raise ComplianceError("Choose keep, change or remove for every login.")
    for r in rows:
        d = decisions[r["u"]["id"]]
        if d in ("change", "remove") and not (notes.get(r["u"]["id"]) or "").strip():
            raise ComplianceError("Say why for each change or removal: a short note is needed.")
        if d == "remove" and r["u"]["id"] == reviewer_id:
            raise ComplianceError("You cannot remove your own login. Another manager can.")
    count = {k: sum(1 for r in rows if decisions[r["u"]["id"]] == k) for k in DECISIONS}
    rid = db.execute("INSERT INTO access_reviews(clinic_id, reviewed_by, logins, kept, changed, removed, note) VALUES (?,?,?,?,?,?,?)",
                     (cid, reviewer_id, len(rows), count["keep"], count["change"], count["remove"], (note or "").strip()[:500] or None)).lastrowid
    for r in rows:
        u, d = r["u"], decisions[r["u"]["id"]]
        db.execute("""INSERT INTO access_review_items(clinic_id, review_id, user_id, decision, role, two_step, last_sign_in, note)
                      VALUES (?,?,?,?,?,?,?,?)""", (cid, rid, u["id"], d, u["role"], 1 if u["totp_enabled"] else 0, r["last"],
                                                    (notes.get(u["id"]) or "").strip()[:300] or None))
        if d == "remove":
            db.execute("UPDATE users SET active = 0 WHERE id = ? AND clinic_id = ?", (u["id"], cid))
            audit.log(db, cid, reviewer_id, "staff.toggle", "user", u["id"], "access review")
    audit.log(db, cid, reviewer_id, "access.review", "access_review", rid, f"{len(rows)} logins, {count['remove']} removed")
    db.commit()
    return rid


# ---------------------------------------------------------------------------------------------------------- outside services
SERVICES = {   # key: (name, what goes there)
    "whatsapp": ("WhatsApp Business API", "First name, phone number and the message: dentist, day and time. Never treatment or money."),
    "sms": ("Text messages", "Phone number and the message: name, dentist, day and time."),
    "email": ("Email", "Email address and the message: name, dentist, day and time."),
    "calls": ("Phone calls", "Callers' phone numbers and call times."),
    "payments": ("Online payments", "Amount, the patient's name and email. Card details go straight to the payment company; Dental OS never sees them."),
    "ai_notes": ("AI help with clinical notes", "The dentist's shorthand, with names, phone numbers and emails removed. Health information, sent abroad."),
    "assistant": ("The assistant", "Staff questions and what the records say, with names, phones and emails replaced by codes. Sent abroad."),
    "xray_ai": ("X-ray AI", "The X-ray picture with its hidden details removed, and the tooth area. No name."),
    "erx": ("E-prescribing network", "The prescription: the patient's name, date of birth, the medicines and the prescriber."),
    "clearinghouse": ("Claims clearinghouse", "Claims: patient, cover, treatment codes, dates and fees."),
    "voice_cloud": ("Voice to text by the browser maker", "The sound of tooth numbers and measurements spoken at the gum chart."),
    "api_keys": ("API keys for other programs", "Whatever each key's access allows. Every FHIR read is in the patient's access log."),
    "webhooks": ("Webhooks", "Appointment and payment events. No clinical data."),
    "offsite": ("Off-site copies", "Everything, locked with the clinic's passphrase before it leaves."),
}


def _row(db, sql, cid):
    try:
        return db.execute(sql, (cid,)).fetchone()
    except Exception:                                      # noqa: BLE001 - a table from a later version not there yet: off
        return None


def outside_services(db, clinic, cfg=None):
    """Every outside company the clinic can send data to, whether it is on, and what goes there. For the record of processing
    and the auditor's list of processors. A service that is off sends nothing."""
    cid, cfg = clinic["id"], cfg or {}
    wa = _row(db, "SELECT enabled FROM wa_account WHERE clinic_id = ?", cid)
    sms = _row(db, "SELECT enabled, provider FROM sms_accounts WHERE clinic_id = ?", cid)
    mail = _row(db, "SELECT enabled, host FROM mail_accounts WHERE clinic_id = ?", cid)
    voice = _row(db, "SELECT enabled FROM voice_accounts WHERE clinic_id = ?", cid)
    pay = _row(db, "SELECT enabled, provider FROM pay_accounts WHERE clinic_id = ?", cid)
    ai = _row(db, "SELECT enabled, assistant FROM ai_account WHERE clinic_id = ?", cid)
    xr = _row(db, "SELECT enabled, vendor FROM xray_ai_account WHERE clinic_id = ?", cid)
    erx = _row(db, "SELECT enabled, name FROM erx_network WHERE clinic_id = ?", cid)
    ch = _row(db, "SELECT enabled, provider FROM clearinghouse_account WHERE clinic_id = ?", cid)
    keys = db.execute("SELECT COUNT(*), COALESCE(SUM(fhir), 0) FROM api_keys WHERE clinic_id = ? AND revoked = 0", (cid,)).fetchone()
    hooks = db.execute("SELECT COUNT(*) FROM webhook_endpoints WHERE clinic_id = ? AND active = 1", (cid,)).fetchone()[0]

    def on(r, col="enabled"):
        return bool(r and r[col])

    def col(r, name):
        return (r[name] if r else "") or ""
    target = str(cfg.get("OFFSITE_TARGET") or "")
    state = [
        ("whatsapp", on(wa), "Meta"), ("sms", on(sms), col(sms, "provider")), ("email", on(mail), col(mail, "host")),
        ("calls", on(voice), "Twilio"), ("payments", on(pay), col(pay, "provider")), ("ai_notes", on(ai), "Anthropic"),
        ("assistant", on(ai, "assistant"), "Anthropic"), ("xray_ai", on(xr), col(xr, "vendor")), ("erx", on(erx), col(erx, "name")),
        ("clearinghouse", on(ch), col(ch, "provider")), ("voice_cloud", bool(clinic["voice_cloud"]), "Google (Chrome)"),
        ("api_keys", bool(keys[0]), f"{keys[0]} (FHIR: {keys[1]})"), ("webhooks", bool(hooks), str(hooks)),
        ("offsite", bool(target), target.split("://", 1)[-1].split("/", 1)[0] if target else ""),
    ]
    return [{"key": k, "name": SERVICES[k][0], "data": SERVICES[k][1], "on": o, "who": w} for k, o, w in state]


# --------------------------------------------------------------------------------------------------------------- the controls
def controls(db, clinic, cfg, now=None):
    """[(key, state, detail, values)] for every control, worked out from what the system knows today. The detail is fixed
    wording with %(name)s places, so the page can show it in French; `explain` fills it in."""
    from .. import db as dbmod
    from . import offsite, seccheck
    now, cid = _utc(now), clinic["id"]
    users = [r for r in access_list(db, cid, now) if r["u"]["active"]]
    no2 = [r for r in users if "no_two_step" in r["flags"]]
    managers = [r for r in users if r["u"]["role"] == "manager"]
    rs = review_status(db, cid, now)
    last_seal = latest_seal(db)
    copy = offsite.status(db)["last_ok"]
    drill = offsite.drill_status(db, now)
    good = drill["last_ok"]
    idle = cfg.get("SESSION_IDLE_MINUTES") or 0
    locked = dbmod.logs_locked(db)
    kept = dbmod.records_locked(db)
    late_breach = db.execute("""SELECT COUNT(*) FROM incidents WHERE clinic_id = ? AND risk <> 'low' AND regulator_told = 0
                                AND status <> 'closed' AND found_at < ?""",
                             (cid, (now - datetime.timedelta(hours=72)).strftime("%Y-%m-%d %H:%M"))).fetchone()[0]
    late_requests = db.execute("SELECT COUNT(*) FROM data_requests WHERE clinic_id = ? AND status = 'open' AND due_on < ?",
                               (cid, now.date().isoformat())).fetchone()[0]
    services = [s for s in outside_services(db, clinic, cfg) if s["on"]]
    findings = seccheck.run()
    seal_old = seal_stale(db, now)
    return [
        ("accounts", "manual", "%(n)s logins switched on. Each must belong to one person: see the list of logins.", {"n": len(users)}),
        ("two_step", "ok" if clinic["require_2fa"] or (users and not no2) else "attention",
         "Required for everyone." if clinic["require_2fa"] else "Not required. Logins without it: %(n)s.", {"n": len(no2)}),
        ("idle", "ok" if 0 < idle <= 60 else "attention", "After %(n)s minutes." if idle else "Switched off on this server.", {"n": idle}),
        ("login_limits", "ok", "5 wrong passwords per login, 30 per network address, in 15 minutes.", {}),
        ("review", "attention" if rs["overdue"] else "ok", "Last on %(last)s, next due %(due)s." if rs["last"] else "Never done.",
         {"last": str(rs["last"]["reviewed_at"])[:10] if rs["last"] else "", "due": str(rs["due"] or "")}),
        ("managers", "ok" if len(managers) <= max(2, len(users) // 3) else "attention", "%(n)s of %(all)s logins.",
         {"n": len(managers), "all": len(users)}),
        ("log_locked", "ok" if locked else "attention", "The database refuses changes and deletions." if locked else
         "The database locks are missing. Restart the app to put them back, and find out who removed them.", {}),
        ("records", "ok" if kept else "attention", "The database refuses to change a signed note or a payment's amount, date or receipt."
         if kept else "The database locks are missing. Restart the app to put them back, and find out who removed them.", {}),
        ("log_sealed", "attention" if seal_old else "ok",
         "Last sealed %(when)s UTC, up to row %(upto)s." if last_seal else "Not sealed yet.",
         {"when": str(last_seal["sealed_at"])[:16] if last_seal else "", "upto": last_seal["upto_id"] if last_seal else 0}),
        ("offsite", "ok" if (copy and not offsite.stale(db, now=now)) else "attention",
         "Last good copy %(when)s UTC." if copy else "No good off-site copy yet.", {"when": str(copy["created_at"])[:16] if copy else ""}),
        ("drill", "attention" if drill["due"] else "ok", "Last good drill %(when)s UTC." if good else "No restore drill has passed yet.",
         {"when": str(good["created_at"])[:16] if good else ""}),
        ("https", "ok" if cfg.get("SESSION_COOKIE_SECURE") else "attention",
         "Secure cookies and HSTS on." if cfg.get("SESSION_COOKIE_SECURE") else "COOKIE_SECURE is not set on the server.", {}),
        ("secrets", "ok", "Encrypted with a key from SECRET_KEY, never shown again and never logged. Staff passwords are hashed with scrypt.", {}),
        ("incidents", "attention" if late_breach else "ok",
         "Medium or high risk breaches older than 72 hours with no decision on telling the regulator: %(n)s." if late_breach
         else "None waiting past 72 hours.", {"n": late_breach}),
        ("requests", "attention" if late_requests else "ok", "Open past their due date: %(n)s." if late_requests else "None late.",
         {"n": late_requests}),
        ("outside", "manual", "Switched on: %(n)s. Each needs a contract and, where data goes abroad, a legal basis.", {"n": len(services)}),
        ("sbom", "ok", "In the evidence pack, with every part and its version.", {}),
        ("code", "ok" if not findings else "attention", "No findings." if not findings else "Findings: %(n)s. See the evidence pack.",
         {"n": len(findings)}),
        ("contact", "ok" if cfg.get("SECURITY_CONTACT") else "attention",
         "Served at /.well-known/security.txt." if cfg.get("SECURITY_CONTACT") else "Set SECURITY_CONTACT on the server.", {}),
        ("host", "manual", "Outside the software. The host and the clinic must show these themselves.", {}),
    ]


def explain(detail, values):
    return detail % values if values else detail


# ---------------------------------------------------------------------------------------------------------------------- SBOM
def _req_name(spec):
    name = spec.split(";", 1)[0]
    for ch in "<>=!~[ (":
        name = name.split(ch, 1)[0]
    return name.strip()


def sbom(roots=("Flask", "gunicorn", "cryptography"), extra=("psycopg", "Pillow", "pydicom"), now=None):
    """A CycloneDX 1.5 list of the Python packages Dental OS runs on, with versions, found from what is installed."""
    seen, todo = {}, list(roots) + list(extra)
    while todo:
        name = todo.pop()
        key = name.lower().replace("_", "-")
        if key in seen:
            continue
        try:
            dist = metadata.distribution(name)
        except metadata.PackageNotFoundError:
            continue
        seen[key] = dist
        for spec in dist.requires or []:
            if "extra ==" in spec:
                continue
            todo.append(_req_name(spec))
    comps = []
    for key in sorted(seen):
        d = seen[key]
        lic = d.metadata.get("License-Expression") or d.metadata.get("License") or ""
        c = {"type": "library", "name": d.metadata["Name"], "version": d.version, "purl": f"pkg:pypi/{key}@{d.version}"}
        if lic and len(lic) < 80:
            c["licenses"] = [{"license": {"name": lic}}]
        comps.append(c)
    return {"bomFormat": "CycloneDX", "specVersion": "1.5", "version": 1,
            "metadata": {"timestamp": _utc(now).strftime("%Y-%m-%dT%H:%M:%SZ"),
                         "component": {"type": "application", "name": "Dental OS", "version": VERSION}},
            "components": comps}


# ------------------------------------------------------------------------------------------------------------- the evidence pack
def _csv(rows, header):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    for r in rows:
        w.writerow([_safe_cell("" if v is None else str(v)) for v in r])
    return buf.getvalue()


def _settings(cfg, clinic):
    from .. import auth
    lines = [
        f"Dental OS version: {VERSION}",
        f"Two-step login required for every login: {'yes' if clinic['require_2fa'] else 'no'}",
        f"Sign-out after idle minutes: {cfg.get('SESSION_IDLE_MINUTES') or 'off'}",
        f"Secure cookies and HSTS (COOKIE_SECURE): {'yes' if cfg.get('SESSION_COOKIE_SECURE') else 'no'}",
        f"Visitor address taken from the proxy (TRUST_PROXY): {'yes' if cfg.get('TRUST_PROXY') else 'no'}",
        f"Wrong passwords allowed per login in 15 minutes: 5; per network address: {auth.LOGIN_FAILS_PER_ADDRESS}",
        "Passwords: scrypt hashes; at least 10 characters; common passwords and the username refused",
        "Pages: Content-Security-Policy default-src 'self', X-Frame-Options DENY, no caching of signed-in pages",
        f"Off-site copies configured (OFFSITE_TARGET and OFFSITE_PASSPHRASE): {'yes' if cfg.get('OFFSITE_TARGET') and cfg.get('OFFSITE_PASSPHRASE') else 'no'}",
        f"Security contact (SECURITY_CONTACT): {cfg.get('SECURITY_CONTACT') or 'not set'}",
        f"Read-only viewing copy: {'yes' if cfg.get('READ_ONLY') else 'no'}",
    ]
    return "\n".join(lines) + "\n"


README = """Dental OS evidence pack
=======================

Clinic: {clinic}
Made: {made} UTC, by {who}
Dental OS version: {version}

What is in it
  controls.csv          each control, where it sits in ISO/IEC 27001:2022 Annex A and the HIPAA Security Rule, its state today, and
                        which file shows it. "A person must check" means the software cannot know; ask for the evidence.
  settings.txt          the security settings in force on this server.
  users.csv             every login: role, permission group, two-step login, created, last sign-in. No passwords.
  access_reviews.csv    every access review, and access_review_<n>.csv with each decision of the newest one.
  audit_chain.txt       the activity log checked against its seals today, and the newest seal value. Keep that value: next time,
                        `flask audit-verify --anchor UPTO:CHAIN` proves history was not rewritten in between.
  activity_summary.csv  how many of each kind of activity row, by month, for this clinic. No names.
  outside_services.csv  every outside company the clinic can send data to, whether it is on, and what goes there.
  api_keys.csv          keys other programs use: name, first characters, access, last used. Never the key.
  backups.csv           the last 60 off-site copies and every restore drill.
  incidents.csv         data breaches: dates, risk, who was told. No patient names or descriptions.
  data_requests.csv     patients' requests: kind, dates, outcome. No names.
  sbom.cdx.json         the software's parts and versions (CycloneDX 1.5).
  code_checks.txt       the automatic code checks run on this copy of the software.

The rows here come from the live system. Patient names, notes and message text are left out on purpose.
Making this pack is itself written in the activity log.
"""


def evidence_pack(db, clinic, cfg, user, secret_key, now=None):
    """The zip, as bytes. Seals the log first, so the check covers every row up to now."""
    from .. import db as dbmod
    from . import seccheck
    now, cid = _utc(now), clinic["id"]
    seal(db, secret_key, user["id"] if user else None)
    check = verify(db, secret_key)
    last = latest_seal(db)
    files = {}
    files["README.txt"] = README.format(clinic=clinic["name"], made=now.strftime("%Y-%m-%d %H:%M"), who=user["display_name"] if user else "the server",
                                        version=VERSION)
    files["controls.csv"] = _csv([(k, CONTROLS[k], REFS[k][0], REFS[k][1], STATES[s], explain(d, v)) for k, s, d, v in controls(db, clinic, cfg, now)],
                                 ["key", "control", "ISO/IEC 27001:2022 Annex A", "HIPAA 45 CFR", "state", "detail"])
    files["settings.txt"] = _settings(cfg, clinic)
    files["users.csv"] = _csv([(r["u"]["username"], r["u"]["display_name"], r["u"]["role"], r["group"] or "", "yes" if r["u"]["active"] else "no",
                                "yes" if r["u"]["totp_enabled"] else "no", str(r["u"]["created_at"])[:16], str(r["last"] or "")[:16],
                                " | ".join(FLAGS[f] for f in r["flags"])) for r in access_list(db, cid, now)],
                              ["username", "name", "role", "permission group", "switched on", "two-step login", "created (UTC)",
                               "last sign-in (UTC)", "flags"])
    revs = reviews(db, cid, 200)
    files["access_reviews.csv"] = _csv([(r["id"], str(r["reviewed_at"])[:16], r["display_name"], r["logins"], r["kept"], r["changed"], r["removed"], r["note"])
                                        for r in revs], ["review", "when (UTC)", "by", "logins", "kept", "changed", "removed", "note"])
    if revs:
        files[f"access_review_{revs[0]['id']}.csv"] = _csv(
            [(i["username"], i["display_name"], i["role"], "yes" if i["two_step"] else "no", str(i["last_sign_in"] or "")[:16], DECISIONS[i["decision"]], i["note"])
             for i in review_items(db, cid, revs[0]["id"])],
            ["username", "name", "role then", "two-step then", "last sign-in then", "decision", "note"])
    files["audit_chain.txt"] = (
        f"Checked {now.strftime('%Y-%m-%d %H:%M')} UTC.\n"
        f"Result: {'INTACT' if check['ok'] else 'BROKEN'}\n"
        f"Rows checked: {check['checked']}; seals: {check['seals']}; rows not yet sealed: {check['unsealed']}\n"
        + (f"Seals made with an earlier SECRET_KEY, which cannot be checked now: {check['other_key']}\n" if check["other_key"] else "")
        + (f"First seal that does not match: number {check['broken']['id']}, made {check['broken']['sealed_at']} UTC, up to row "
           f"{check['broken']['upto_id']}. Rows at or before it were changed, removed or added outside the app.\n" if check["broken"] else "")
        + (f"Newest seal (keep this): {last['upto_id']}:{last['chain']}\n" if last else "No seal yet.\n")
        + f"Database locks on the log in place: {'yes' if dbmod.logs_locked(db) else 'NO'}\n"
        + "The chain covers every clinic on this server, so it is the same in every clinic's pack.\n")
    months = db.execute("""SELECT substr(at, 1, 7) AS month, action, COUNT(*) AS n FROM audit_log WHERE clinic_id = ?
                           GROUP BY substr(at, 1, 7), action ORDER BY 1 DESC, 2""", (cid,)).fetchall()
    files["activity_summary.csv"] = _csv([(r["month"], r["action"], r["n"]) for r in months], ["month", "action", "rows"])
    files["outside_services.csv"] = _csv([(s["name"], "on" if s["on"] else "off", s["who"], s["data"]) for s in outside_services(db, clinic, cfg)],
                                         ["service", "state", "company or detail", "what goes there"])
    files["api_keys.csv"] = _csv([(k["name"], k["key_prefix"] + "…", k["access"], "yes" if k["fhir"] else "no", "yes" if k["revoked"] else "no",
                                   str(k["created_at"])[:16], str(k["last_used_at"] or "")[:16])
                                  for k in db.execute("SELECT * FROM api_keys WHERE clinic_id = ? ORDER BY id", (cid,)).fetchall()],
                                 ["name", "starts", "access", "clinical records (FHIR)", "revoked", "created (UTC)", "last used (UTC)"])
    copies = db.execute("SELECT 'copy' AS kind, ok, size_bytes AS n, NULL AS made_at, created_at, detail FROM offsite_log ORDER BY id DESC LIMIT 60").fetchall()
    drills = db.execute("SELECT 'restore drill' AS kind, ok, patients AS n, made_at, created_at, detail FROM restore_drills ORDER BY id DESC").fetchall()
    files["backups.csv"] = _csv([(r["kind"], "ok" if r["ok"] else "FAILED", str(r["created_at"])[:16], str(r["made_at"] or "")[:16], r["n"], (r["detail"] or "")[:200])
                                 for r in list(copies) + list(drills)],
                                ["kind", "result", "when (UTC)", "copy made (drills)", "bytes (copies) or patients (drills)", "detail"])
    files["incidents.csv"] = _csv([(r["id"], str(r["occurred_on"] or ""), str(r["found_at"])[:16], r["risk"], r["people_affected"],
                                    "yes" if r["regulator_told"] else "no", r["regulator_on"] or "", "yes" if r["patients_told"] else "no", r["status"])
                                   for r in db.execute("SELECT * FROM incidents WHERE clinic_id = ? ORDER BY id", (cid,)).fetchall()],
                                  ["number", "happened", "found (UTC)", "risk", "people affected", "regulator told", "regulator told on",
                                   "patients told", "status"])
    files["data_requests.csv"] = _csv([(r["id"], r["kind"], r["received_on"], r["due_on"], r["closed_on"] or "", r["status"],
                                        "yes" if r["identity_checked"] else "no",
                                        "late" if (r["closed_on"] or now.date().isoformat()) > r["due_on"] else "")
                                       for r in db.execute("SELECT * FROM data_requests WHERE clinic_id = ? ORDER BY id", (cid,)).fetchall()],
                                      ["number", "kind", "received", "due", "closed", "status", "identity checked", "late"])
    files["sbom.cdx.json"] = json.dumps(sbom(now=now), indent=2)
    files["code_checks.txt"] = seccheck.report()
    audit.log(db, cid, user["id"] if user else None, "compliance.pack", detail=f"{len(files)} files")
    db.commit()
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for name, text in files.items():
            z.writestr(name, text)
    return out.getvalue()


def security_txt(cfg, now=None):
    """RFC 9116. None when no contact is set, so the address answers 404 rather than an empty promise."""
    contacts = [c.strip() for c in str(cfg.get("SECURITY_CONTACT") or "").split(",") if c.strip()]
    if not contacts:
        return None
    expires = (_utc(now) + datetime.timedelta(days=180)).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines = [f"Contact: {c}" for c in contacts] + [f"Expires: {expires}", "Preferred-Languages: en, fr"]
    if cfg.get("SECURITY_POLICY_URL"):
        lines.append(f"Policy: {cfg['SECURITY_POLICY_URL']}")
    if cfg.get("PUBLIC_URL"):
        lines.append(f"Canonical: {str(cfg['PUBLIC_URL']).rstrip('/')}/.well-known/security.txt")
    return "\n".join(lines) + "\n"
