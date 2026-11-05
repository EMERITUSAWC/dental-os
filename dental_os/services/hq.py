"""Version 0.71: Dental OS HQ, the work of the company's own departments.

Customer support: one inbox for every clinic's questions and faults (from shared problem reports, WhatsApp, calls, email or a
visit), each with an answer-by time and a solve-by time set by its priority, notes for the team and replies to the clinic, and a
help library. Sales: clinics we are talking to, by stage, with each salesperson's figures; a won clinic is handed to onboarding.
Onboarding: a checklist per new clinic, training sessions and the go-live day. Finance: subscriptions, bills, clinics that are
behind and a VAT report. Leadership: the company on one page.

Everything here is about clinics as customers. Nothing holds a patient's name or record, and the support text is checked for
phone numbers so that a patient's details are not typed in by mistake. No web framework here."""
import csv
import datetime
import io
import json
import re

from . import money as M, platform as PF

PRIORITIES = {"urgent": "Urgent: the clinic cannot work", "high": "High", "normal": "Normal", "low": "Low"}
SLA_HOURS = {"urgent": (1, 8), "high": (4, 24), "normal": (8, 72), "low": (24, 120)}       # (answer by, solve by), hours
CHANNELS = {"problem_report": "Problem report from the app", "whatsapp": "WhatsApp", "phone": "Phone call", "email": "Email",
            "visit": "Visit", "other": "Other"}
TICKET_STATES = {"new": "New", "open": "Being worked on", "waiting": "Waiting for the clinic", "solved": "Solved", "closed": "Closed"}
STAGES = {"new": "New", "contacted": "Contacted", "demo_booked": "Demo booked", "demo_done": "Demo done", "trial": "On a trial",
          "won": "Won", "lost": "Lost"}
OPEN_STAGES = ("new", "contacted", "demo_booked", "demo_done", "trial")
PROJECT_STATES = {"planned": "Planned", "in_progress": "In progress", "live": "Gone live", "paused": "Paused", "cancelled": "Cancelled"}
STEPS = [
    ("kickoff", "First call: who does what, and the go-live date agreed"),
    ("settings", "Clinic settings: opening hours, dentists, chairs and prices"),
    ("logins", "Staff logins made, and two-step login explained"),
    ("accounts", "Payment and message accounts connected (Paystack, WhatsApp, SMS)"),
    ("data_test", "Old system's data moved in a trial run and checked with the clinic"),
    ("train_desk", "Front desk trained"),
    ("train_clinical", "Dentists and assistants trained"),
    ("train_manager", "Manager trained: reports, cash-up and staff"),
    ("data_final", "Final data move done"),
    ("golive", "Go-live day: someone on hand all day"),
    ("followup", "Two-week check: problems closed and the clinic happy"),
]
STEP_KEYS = [k for k, _ in STEPS]
STEP_LABELS = dict(STEPS)
PHONE = re.compile(r"(?<![\d+])(?:\+\d{9,14}|(?:\+?233|0)[235]\d[\s-]?\d{3}[\s-]?\d{4})(?!\d)")

NO_PATIENT_DETAILS = "Leave out phone numbers and anything about a patient. Write about the clinic and the screen."
NOT_FOUND = "That was not found."
NEED_SUBJECT = "Write what it is about."
NEED_NAME = "Write the clinic's name."
LOST_WHY = "Say why it was lost. It helps the next one."
ALREADY_HANDED = "This clinic has already been handed to onboarding."
NOT_SUPPORT = "Pick someone in Customer support."
BAD_DATE = "Check the dates: use the date boxes."
BAD_RATE = "Type the VAT rate as a number, for example 15."
BAD_AMOUNT = "Type the amount as a number, for example 650 or 650.00."


class HQError(ValueError):
    pass


def _now(now=None):
    return (now or datetime.datetime.now(datetime.timezone.utc)).strftime("%Y-%m-%d %H:%M:%S")


def _clean(text, limit):
    return (text or "").strip()[:limit]


def _no_phone(*texts):
    for t in texts:
        if t and PHONE.search(t):
            raise HQError(NO_PATIENT_DETAILS)


def _date(v):
    v = (v or "").strip()
    if not v:
        return None
    try:
        return datetime.date.fromisoformat(v).isoformat()
    except ValueError as exc:
        raise HQError(BAD_DATE) from exc


def _log(conn, op_id, action, detail=None, clinic_id=None):
    conn.execute("INSERT INTO operator_log(operator_id, clinic_id, action, detail) VALUES (?,?,?,?)", (op_id, clinic_id, action, detail))


def people_in(conn, dept):
    """Active people in a department, and owners, for 'assign to' lists."""
    from . import opteam
    out = []
    for r in conn.execute("SELECT * FROM operators WHERE active = 1 ORDER BY display_name").fetchall():
        if r["role"] == "owner" or dept in opteam.memberships(conn, r["id"]):
            out.append(r)
    return out


def _clinic_ok(conn, cid):
    if cid in (None, "", 0):
        return None
    if not conn.execute("SELECT 1 FROM clinics WHERE id = ?", (cid,)).fetchone():
        raise HQError(NOT_FOUND)
    return int(cid)


# ---------------------------------------------------------------------------------------------------------- customer support
def new_ticket(conn, op_id, clinic_id, contact, channel, subject, body, priority, assigned_to=None, problem_report_id=None, now=None,
               check=True):
    subject, body, contact = _clean(subject, 160), _clean(body, 4000), _clean(contact, 160)
    if not subject:
        raise HQError(NEED_SUBJECT)
    if check:
        _no_phone(subject, body)
    channel = channel if channel in CHANNELS else "other"
    priority = priority if priority in PRIORITIES else "normal"
    start = now or datetime.datetime.now(datetime.timezone.utc)
    reply_h, solve_h = SLA_HOURS[priority]
    if assigned_to and assigned_to not in [r["id"] for r in people_in(conn, "support")]:
        raise HQError(NOT_SUPPORT)
    tid = conn.execute("""INSERT INTO hq_tickets(clinic_id, contact, channel, subject, body, priority, assigned_to, problem_report_id,
                          reply_due, solve_due, created_by, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                       (_clinic_ok(conn, clinic_id), contact or None, channel, subject, body or None, priority, assigned_to or None,
                        problem_report_id, _now(start + datetime.timedelta(hours=reply_h)), _now(start + datetime.timedelta(hours=solve_h)),
                        op_id, _now(start))).lastrowid
    _log(conn, op_id, "ticket.new", f"#{tid} {priority}", _clinic_ok(conn, clinic_id))
    return tid


def import_reports(conn, op_id, now=None):
    """Every open problem report a clinic chose to share becomes a ticket, once. Returns how many were made."""
    made = 0
    pri = {"blocking": "urgent", "annoying": "normal", "idea": "low"}
    for r in conn.execute("""SELECT r.* FROM problem_reports r WHERE r.shared = 1 AND r.status = 'open'
                             AND NOT EXISTS (SELECT 1 FROM hq_tickets t WHERE t.problem_report_id = r.id) ORDER BY r.id""").fetchall():
        subject = (r["what"] or "").strip().splitlines()[0][:150] if (r["what"] or "").strip() else "Problem report"
        new_ticket(conn, op_id, r["clinic_id"], None, "problem_report", subject, (r["what"] or "") + (f"\n\nScreen: {r['page']}" if r["page"] else ""),
                   pri.get(r["urgency"], "normal"), None, r["id"], now, check=False)
        made += 1
    return made


def ticket(conn, tid):
    row = conn.execute("""SELECT t.*, c.name AS clinic_name, o.display_name AS assignee FROM hq_tickets t
                          LEFT JOIN clinics c ON c.id = t.clinic_id LEFT JOIN operators o ON o.id = t.assigned_to WHERE t.id = ?""",
                       (tid,)).fetchone()
    if not row:
        raise HQError(NOT_FOUND)
    return row


def ticket_notes(conn, tid):
    return conn.execute("""SELECT n.*, o.display_name FROM hq_ticket_notes n LEFT JOIN operators o ON o.id = n.operator_id
                           WHERE n.ticket_id = ? ORDER BY n.id""", (tid,)).fetchall()


def add_note(conn, op_id, tid, kind, body, now=None):
    t = ticket(conn, tid)
    body = _clean(body, 4000)
    if not body:
        raise HQError(NEED_SUBJECT)
    _no_phone(body)
    kind = "reply" if kind == "reply" else "internal"
    conn.execute("INSERT INTO hq_ticket_notes(ticket_id, operator_id, kind, body, at) VALUES (?,?,?,?,?)", (tid, op_id, kind, body, _now(now)))
    if kind == "reply":
        conn.execute("UPDATE hq_tickets SET replied_at = COALESCE(replied_at, ?), status = CASE WHEN status = 'new' THEN 'open' ELSE status END WHERE id = ?",
                     (_now(now), tid))
    _log(conn, op_id, "ticket." + kind, f"#{tid}", t["clinic_id"])


def set_ticket(conn, op_id, tid, status=None, assigned_to=None, priority=None, now=None):
    t = ticket(conn, tid)
    if status is not None:
        if status not in TICKET_STATES:
            raise HQError(NOT_FOUND)
        if status in ("solved", "closed"):
            conn.execute("UPDATE hq_tickets SET status = ?, solved_at = COALESCE(solved_at, ?) WHERE id = ?", (status, _now(now), tid))
        else:
            conn.execute("UPDATE hq_tickets SET status = ?, solved_at = NULL WHERE id = ?", (status, tid))
    if assigned_to is not None:
        if assigned_to and assigned_to not in [r["id"] for r in people_in(conn, "support")]:
            raise HQError(NOT_SUPPORT)
        conn.execute("UPDATE hq_tickets SET assigned_to = ?, status = CASE WHEN status = 'new' THEN 'open' ELSE status END WHERE id = ?",
                     (assigned_to or None, tid))
    if priority is not None and priority in PRIORITIES and priority != t["priority"]:
        start = datetime.datetime.strptime(t["created_at"], "%Y-%m-%d %H:%M:%S")
        reply_h, solve_h = SLA_HOURS[priority]
        conn.execute("UPDATE hq_tickets SET priority = ?, reply_due = ?, solve_due = ? WHERE id = ?",
                     (priority, _now(start + datetime.timedelta(hours=reply_h)), _now(start + datetime.timedelta(hours=solve_h)), tid))
    _log(conn, op_id, "ticket.change", f"#{tid} {status or ''} {assigned_to if assigned_to is not None else ''} {priority or ''}".strip(), t["clinic_id"])


def clocks(t, now=None):
    """'late' flags for a ticket: no answer by the answer-by time, or not solved by the solve-by time."""
    now = _now(now)
    done = t["status"] in ("solved", "closed")
    return {"reply_late": not t["replied_at"] and not done and now > t["reply_due"],
            "solve_late": not done and now > t["solve_due"]}


def tickets(conn, which="open", op_id=None, now=None):
    where = {"open": "t.status IN ('new','open','waiting')", "mine": "t.status IN ('new','open','waiting') AND t.assigned_to = ?",
             "unassigned": "t.status IN ('new','open','waiting') AND t.assigned_to IS NULL", "done": "t.status IN ('solved','closed')"}
    sql = where.get(which, where["open"])
    args = (op_id,) if which == "mine" else ()
    rows = conn.execute(f"""SELECT t.*, c.name AS clinic_name, o.display_name AS assignee FROM hq_tickets t LEFT JOIN clinics c ON c.id = t.clinic_id
                            LEFT JOIN operators o ON o.id = t.assigned_to WHERE {sql}
                            ORDER BY CASE t.priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1 WHEN 'normal' THEN 2 ELSE 3 END, t.solve_due
                            LIMIT 300""", args).fetchall()
    return [{"t": r, **clocks(r, now)} for r in rows]


def support_numbers(conn, now=None):
    rows = tickets(conn, "open", now=now)
    month = _now(now)[:7]
    solved = conn.execute("SELECT created_at, solved_at, replied_at, reply_due FROM hq_tickets WHERE solved_at LIKE ?", (month + "%",)).fetchall()
    on_time = sum(1 for r in solved if r["replied_at"] and r["replied_at"] <= r["reply_due"])
    return {"open": len(rows), "late": sum(1 for x in rows if x["reply_late"] or x["solve_late"]),
            "unassigned": sum(1 for x in rows if not x["t"]["assigned_to"]), "urgent": sum(1 for x in rows if x["t"]["priority"] == "urgent"),
            "solved_month": len(solved), "answered_on_time": on_time}


# ------------------------------------------------------------------------------------------------------------- help library
def save_article(conn, op_id, article_id, title, body, audience, published):
    title, body = _clean(title, 160), _clean(body, 20000)
    if not title or not body:
        raise HQError(NEED_SUBJECT)
    _no_phone(title, body)
    audience = "team" if audience == "team" else "clinic"
    if article_id:
        if not conn.execute("SELECT 1 FROM hq_articles WHERE id = ?", (article_id,)).fetchone():
            raise HQError(NOT_FOUND)
        conn.execute("UPDATE hq_articles SET title = ?, body = ?, audience = ?, published = ?, updated_by = ?, updated_at = ? WHERE id = ?",
                     (title, body, audience, 1 if published else 0, op_id, _now(), article_id))
    else:
        article_id = conn.execute("INSERT INTO hq_articles(title, body, audience, published, updated_by, updated_at) VALUES (?,?,?,?,?,?)",
                                  (title, body, audience, 1 if published else 0, op_id, _now())).lastrowid
    _log(conn, op_id, "article.save", f"#{article_id}")
    return article_id


def articles(conn, audience=None, published_only=False):
    sql = "SELECT a.*, o.display_name FROM hq_articles a LEFT JOIN operators o ON o.id = a.updated_by WHERE 1 = 1"
    args = []
    if audience:
        sql += " AND a.audience = ?"
        args.append(audience)
    if published_only:
        sql += " AND a.published = 1"
    return conn.execute(sql + " ORDER BY a.title", args).fetchall()


def article(conn, article_id, clinic_side=False):
    row = conn.execute("SELECT * FROM hq_articles WHERE id = ?" + (" AND audience = 'clinic' AND published = 1" if clinic_side else ""),
                       (article_id,)).fetchone()
    if not row:
        raise HQError(NOT_FOUND)
    return row


def paragraphs(text):
    return [p.strip() for p in re.split(r"\n\s*\n", text or "") if p.strip()]


# ------------------------------------------------------------------------------------------------------------------- sales
def _money(raw, currency):
    raw = (raw or "").strip()
    if not raw:
        return 0
    try:
        return M.parse_money(raw, currency)
    except (ValueError, TypeError) as exc:
        raise HQError(BAD_AMOUNT) from exc


def save_lead(conn, op_id, lead_id, f):
    name = _clean(f.get("name"), 120)
    if not name:
        raise HQError(NEED_NAME)
    from . import regions
    currency = f.get("currency") if f.get("currency") in regions.CURRENCIES else "GHS"
    stage = f.get("stage") if f.get("stage") in STAGES else "new"
    lost = _clean(f.get("lost_reason"), 300) or None
    if stage == "lost" and not lost:
        raise HQError(LOST_WHY)
    next_step = _clean(f.get("next_step"), 200) or None
    _no_phone(next_step, lost)
    owner = f.get("owner_id")
    owner = int(owner) if str(owner or "").isdigit() else None
    if owner and owner not in [r["id"] for r in people_in(conn, "sales")]:
        raise HQError(NOT_FOUND)
    vals = (name, _clean(f.get("contact"), 120) or None, _clean(f.get("phone"), 40) or None, _clean(f.get("email"), 120) or None,
            _clean(f.get("city"), 80) or None, _clean(f.get("country"), 2).upper() or None, _clean(f.get("source"), 80) or None, stage,
            _money(f.get("value"), currency), currency, owner, next_step, _date(f.get("next_on")), lost if stage == "lost" else None,
            _clinic_ok(conn, int(f.get("clinic_id")) if str(f.get("clinic_id") or "").isdigit() else None))
    if lead_id:
        old = lead(conn, lead_id)
        conn.execute("""UPDATE hq_leads SET name = ?, contact = ?, phone = ?, email = ?, city = ?, country = ?, source = ?, stage = ?, value_p = ?,
                        currency = ?, owner_id = ?, next_step = ?, next_on = ?, lost_reason = ?, clinic_id = ?, updated_at = ?,
                        won_at = CASE WHEN ? = 'won' THEN COALESCE(won_at, ?) ELSE NULL END WHERE id = ?""",
                     vals + (_now(), stage, _now(), lead_id))
        if old["stage"] != stage:
            _log(conn, op_id, "lead.stage", f"#{lead_id} {old['stage']} to {stage}")
    else:
        lead_id = conn.execute("""INSERT INTO hq_leads(name, contact, phone, email, city, country, source, stage, value_p, currency, owner_id,
                                  next_step, next_on, lost_reason, clinic_id, won_at, created_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                               vals + (_now() if stage == "won" else None, op_id)).lastrowid
        _log(conn, op_id, "lead.new", f"#{lead_id}")
    return lead_id


def lead(conn, lead_id):
    row = conn.execute("""SELECT l.*, o.display_name AS owner_name, c.name AS clinic_name FROM hq_leads l LEFT JOIN operators o ON o.id = l.owner_id
                          LEFT JOIN clinics c ON c.id = l.clinic_id WHERE l.id = ?""", (lead_id,)).fetchone()
    if not row:
        raise HQError(NOT_FOUND)
    return row


def lead_notes(conn, lead_id):
    return conn.execute("""SELECT n.*, o.display_name FROM hq_lead_notes n LEFT JOIN operators o ON o.id = n.operator_id WHERE n.lead_id = ?
                           ORDER BY n.id DESC""", (lead_id,)).fetchall()


def add_lead_note(conn, op_id, lead_id, body):
    lead(conn, lead_id)
    body = _clean(body, 2000)
    if not body:
        raise HQError(NEED_SUBJECT)
    conn.execute("INSERT INTO hq_lead_notes(lead_id, operator_id, body) VALUES (?,?,?)", (lead_id, op_id, body))
    conn.execute("UPDATE hq_leads SET updated_at = ? WHERE id = ?", (_now(), lead_id))


def leads(conn, stage=None, owner_id=None):
    sql = """SELECT l.*, o.display_name AS owner_name FROM hq_leads l LEFT JOIN operators o ON o.id = l.owner_id WHERE 1 = 1"""
    args = []
    if stage == "open":
        sql += " AND l.stage IN ('new','contacted','demo_booked','demo_done','trial')"
    elif stage in STAGES:
        sql += " AND l.stage = ?"
        args.append(stage)
    if owner_id:
        sql += " AND l.owner_id = ?"
        args.append(owner_id)
    return conn.execute(sql + " ORDER BY COALESCE(l.next_on, '9999'), l.id DESC LIMIT 500", args).fetchall()


def pipeline(conn):
    """For each stage: how many, and the expected monthly value by currency."""
    out = []
    for key, label in STAGES.items():
        rows = conn.execute("SELECT currency, COUNT(*) AS n, SUM(value_p) AS v FROM hq_leads WHERE stage = ? GROUP BY currency", (key,)).fetchall()
        out.append({"key": key, "label": label, "n": sum(r["n"] for r in rows), "value": {r["currency"]: r["v"] or 0 for r in rows}})
    return out


def sales_figures(conn, month=None):
    """Per salesperson: open, won and lost this month, and next steps that are late."""
    month = month or datetime.date.today().isoformat()[:7]
    today = datetime.date.today().isoformat()
    out = []
    for r in people_in(conn, "sales"):
        q = lambda sql, *a: conn.execute(sql, (r["id"],) + a).fetchone()[0]
        out.append({"o": r, "open": q("SELECT COUNT(*) FROM hq_leads WHERE owner_id = ? AND stage IN ('new','contacted','demo_booked','demo_done','trial')"),
                    "won": q("SELECT COUNT(*) FROM hq_leads WHERE owner_id = ? AND stage = 'won' AND won_at LIKE ?", month + "%"),
                    "lost": q("SELECT COUNT(*) FROM hq_leads WHERE owner_id = ? AND stage = 'lost' AND updated_at LIKE ?", month + "%"),
                    "late": q("SELECT COUNT(*) FROM hq_leads WHERE owner_id = ? AND stage IN ('new','contacted','demo_booked','demo_done','trial') AND next_on < ?", today)})
    return out


def handover(conn, op_id, lead_id):
    """A won clinic goes to onboarding as a new project. Once per lead."""
    l = lead(conn, lead_id)
    if conn.execute("SELECT 1 FROM hq_projects WHERE lead_id = ?", (lead_id,)).fetchone():
        raise HQError(ALREADY_HANDED)
    pid = conn.execute("INSERT INTO hq_projects(name, clinic_id, lead_id, created_by) VALUES (?,?,?,?)",
                       (l["name"], l["clinic_id"], lead_id, op_id)).lastrowid
    _log(conn, op_id, "project.handover", f"lead #{lead_id} to project #{pid}", l["clinic_id"])
    return pid


# -------------------------------------------------------------------------------------------------------------- onboarding
def save_project(conn, op_id, project_id, f):
    name = _clean(f.get("name"), 120)
    if not name:
        raise HQError(NEED_NAME)
    owner = f.get("owner_id")
    owner = int(owner) if str(owner or "").isdigit() else None
    if owner and owner not in [r["id"] for r in people_in(conn, "onboarding")]:
        raise HQError(NOT_FOUND)
    status = f.get("status") if f.get("status") in PROJECT_STATES else "planned"
    cid = _clinic_ok(conn, int(f.get("clinic_id")) if str(f.get("clinic_id") or "").isdigit() else None)
    vals = (name, cid, owner, _clean(f.get("old_system"), 80) or None, _date(f.get("go_live_on")), status)
    if project_id:
        project(conn, project_id)
        conn.execute("UPDATE hq_projects SET name = ?, clinic_id = ?, owner_id = ?, old_system = ?, go_live_on = ?, status = ? WHERE id = ?",
                     vals + (project_id,))
    else:
        project_id = conn.execute("INSERT INTO hq_projects(name, clinic_id, owner_id, old_system, go_live_on, status, created_by) VALUES (?,?,?,?,?,?,?)",
                                  vals + (op_id,)).lastrowid
    _log(conn, op_id, "project.save", f"#{project_id} {status}", cid)
    return project_id


def project(conn, project_id):
    row = conn.execute("""SELECT p.*, o.display_name AS owner_name, c.name AS clinic_name FROM hq_projects p LEFT JOIN operators o ON o.id = p.owner_id
                          LEFT JOIN clinics c ON c.id = p.clinic_id WHERE p.id = ?""", (project_id,)).fetchone()
    if not row:
        raise HQError(NOT_FOUND)
    return row


def tick(conn, op_id, project_id, step, done, note=None):
    project(conn, project_id)
    if step not in STEP_KEYS:
        raise HQError(NOT_FOUND)
    conn.execute("DELETE FROM hq_project_steps WHERE project_id = ? AND step = ?", (project_id, step))
    if done:
        conn.execute("INSERT INTO hq_project_steps(project_id, step, done_by, note) VALUES (?,?,?,?)", (project_id, step, op_id, _clean(note, 200) or None))
        if step == "golive":
            conn.execute("UPDATE hq_projects SET status = 'live' WHERE id = ?", (project_id,))
        elif conn.execute("SELECT status FROM hq_projects WHERE id = ?", (project_id,)).fetchone()[0] == "planned":
            conn.execute("UPDATE hq_projects SET status = 'in_progress' WHERE id = ?", (project_id,))
    _log(conn, op_id, "project.step", f"#{project_id} {step} {'done' if done else 'undone'}")


def steps_done(conn, project_id):
    return {r["step"]: r for r in conn.execute("""SELECT s.*, o.display_name FROM hq_project_steps s LEFT JOIN operators o ON o.id = s.done_by
                                                  WHERE s.project_id = ?""", (project_id,)).fetchall()}


def add_training(conn, op_id, project_id, on_date, topic, people):
    project(conn, project_id)
    topic = _clean(topic, 120)
    if not topic:
        raise HQError(NEED_SUBJECT)
    on = _date(on_date)
    if not on:
        raise HQError(BAD_DATE)
    people = int(people) if str(people or "").isdigit() and int(people) < 1000 else None
    return conn.execute("INSERT INTO hq_trainings(project_id, on_date, topic, people, trainer_id) VALUES (?,?,?,?,?)",
                        (project_id, on, topic, people, op_id)).lastrowid


def training_done(conn, op_id, training_id):
    row = conn.execute("SELECT * FROM hq_trainings WHERE id = ?", (training_id,)).fetchone()
    if not row:
        raise HQError(NOT_FOUND)
    conn.execute("UPDATE hq_trainings SET done = 1 WHERE id = ?", (training_id,))
    return row["project_id"]


def trainings(conn, project_id):
    return conn.execute("""SELECT t.*, o.display_name FROM hq_trainings t LEFT JOIN operators o ON o.id = t.trainer_id WHERE t.project_id = ?
                           ORDER BY t.on_date""", (project_id,)).fetchall()


def projects(conn, which="open"):
    sql = "p.status IN ('planned','in_progress','paused')" if which == "open" else "1 = 1"
    rows = conn.execute(f"""SELECT p.*, o.display_name AS owner_name, c.name AS clinic_name FROM hq_projects p LEFT JOIN operators o ON o.id = p.owner_id
                            LEFT JOIN clinics c ON c.id = p.clinic_id WHERE {sql} ORDER BY COALESCE(p.go_live_on, '9999'), p.id""").fetchall()
    out = []
    for r in rows:
        done = conn.execute("SELECT COUNT(*) FROM hq_project_steps WHERE project_id = ?", (r["id"],)).fetchone()[0]
        app_ticks = conn.execute("SELECT COUNT(*) FROM golive_ticks WHERE clinic_id = ?", (r["clinic_id"],)).fetchone()[0] if r["clinic_id"] else None
        out.append({"p": r, "done": done, "of": len(STEPS), "app_ticks": app_ticks})
    return out


# ------------------------------------------------------------------------------------------------------------------ finance
def _prices(plan_prices):
    try:
        return json.loads(plan_prices or "{}")
    except ValueError:
        return {}


def subscriptions(conn):
    out = []
    for c in conn.execute("""SELECT c.*, p.name AS plan_name, p.prices FROM clinics c LEFT JOIN plans p ON p.id = c.plan_id ORDER BY c.name""").fetchall():
        owed = conn.execute("SELECT COALESCE(SUM(amount_p), 0) FROM platform_invoices WHERE clinic_id = ? AND status = 'open'", (c["id"],)).fetchone()[0]
        out.append({"c": c, "state": PF.state(c), "price": _prices(c["prices"]).get(c["currency"]) if c["plan_name"] else None, "owed": owed})
    return out


def monthly_income(conn):
    """Expected subscription income a month, by currency: paying clinics (active or behind) on a plan."""
    totals = {}
    for s in subscriptions(conn):
        if s["state"] in ("active", "past_due") and s["price"]:
            totals[s["c"]["currency"]] = totals.get(s["c"]["currency"], 0) + s["price"]
    return totals


def owed(conn):
    return {r["currency"]: r["v"] for r in conn.execute("SELECT currency, SUM(amount_p) AS v FROM platform_invoices WHERE status = 'open' GROUP BY currency").fetchall()}


def overdue(conn, today=None):
    today = (today or datetime.date.today()).isoformat()
    rows = conn.execute("""SELECT i.*, c.name AS clinic_name FROM platform_invoices i JOIN clinics c ON c.id = i.clinic_id
                           WHERE i.status = 'open' AND i.due_on < ? ORDER BY i.due_on""", (today,)).fetchall()
    t = datetime.date.fromisoformat(today)
    return [{"i": r, "days": (t - datetime.date.fromisoformat(r["due_on"])).days} for r in rows]


def invoices(conn, status=None):
    sql = "SELECT i.*, c.name AS clinic_name FROM platform_invoices i JOIN clinics c ON c.id = i.clinic_id"
    args = ()
    if status in ("open", "paid", "void"):
        sql += " WHERE i.status = ?"
        args = (status,)
    return conn.execute(sql + " ORDER BY i.id DESC LIMIT 300", args).fetchall()


def vat_report(conn, start, end, rate, included=True):
    """Bills paid between two days, by currency: what was received, the VAT in it and the rest. The rate is the clinic owner's
    own figure from the tax office, as a percentage. With included=True the prices already contain VAT."""
    start, end = _date(start), _date(end)
    if not start or not end or end < start:
        raise HQError(BAD_DATE)
    try:
        rate = float(str(rate).strip())
    except (TypeError, ValueError) as exc:
        raise HQError(BAD_RATE) from exc
    if not 0 <= rate <= 100:
        raise HQError(BAD_RATE)
    rows = conn.execute("""SELECT i.*, c.name AS clinic_name FROM platform_invoices i JOIN clinics c ON c.id = i.clinic_id
                           WHERE i.status = 'paid' AND i.paid_on >= ? AND i.paid_on <= ? ORDER BY i.paid_on, i.number""", (start, end)).fetchall()
    lines, totals = [], {}
    for r in rows:
        amount = r["amount_p"]
        if included:
            vat = round(amount * rate / (100 + rate))
            gross, net = amount, amount - vat
        else:
            vat = round(amount * rate / 100)
            gross, net = amount + vat, amount
        lines.append({"i": r, "net": net, "vat": vat, "gross": gross})
        t = totals.setdefault(r["currency"], {"net": 0, "vat": 0, "gross": 0, "n": 0})
        t["net"] += net
        t["vat"] += vat
        t["gross"] += gross
        t["n"] += 1
    return {"start": start, "end": end, "rate": rate, "included": included, "lines": lines, "totals": totals}


def vat_csv(report):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["paid_on", "bill_number", "clinic", "currency", "net", "vat", "gross", "method", "reference"])
    for x in report["lines"]:
        i = x["i"]
        w.writerow([i["paid_on"], i["number"], i["clinic_name"], i["currency"], M.plain(x["net"], i["currency"]),
                    M.plain(x["vat"], i["currency"]), M.plain(x["gross"], i["currency"]), i["paid_method"] or "", i["paid_ref"] or ""])
    return buf.getvalue()


# ----------------------------------------------------------------------------------------------------------------- leadership
def company(conn, today=None):
    today = today or datetime.date.today()
    subs = subscriptions(conn)
    by_state = {}
    for s in subs:
        by_state[s["state"]] = by_state.get(s["state"], 0) + 1
    soon = (today + datetime.timedelta(days=14)).isoformat()
    trials_ending = [s for s in subs if s["state"] == "trial" and s["c"]["trial_ends_on"] and s["c"]["trial_ends_on"] <= soon]
    since = (today - datetime.timedelta(days=90)).isoformat()
    left = conn.execute("SELECT COUNT(DISTINCT clinic_id) FROM operator_log WHERE action = 'state.cancelled' AND at >= ?", (since,)).fetchone()[0]
    month = today.isoformat()[:7]
    in_month = (today + datetime.timedelta(days=30)).isoformat()
    team = conn.execute("SELECT COUNT(*) AS n, SUM(CASE WHEN totp_secret IS NULL THEN 1 ELSE 0 END) AS no2 FROM operators WHERE active = 1").fetchone()
    return {
        "clinics": len(subs), "by_state": by_state, "trials_ending": trials_ending, "left_90": left,
        "income": monthly_income(conn), "owed": owed(conn), "overdue": len(overdue(conn, today)),
        "support": support_numbers(conn),
        "pipeline": pipeline(conn),
        "won_month": conn.execute("SELECT COUNT(*) FROM hq_leads WHERE stage = 'won' AND won_at LIKE ?", (month + "%",)).fetchone()[0],
        "projects_open": len(projects(conn)),
        "golives": conn.execute("""SELECT p.*, c.name AS clinic_name FROM hq_projects p LEFT JOIN clinics c ON c.id = p.clinic_id
                                   WHERE p.status IN ('planned','in_progress') AND p.go_live_on BETWEEN ? AND ? ORDER BY p.go_live_on""",
                                (today.isoformat(), in_month)).fetchall(),
        "team": team["n"], "team_no_2fa": team["no2"] or 0,
    }


def my_work(conn, op, depts, today=None):
    """What waits for this person on the HQ home, from each department they are in."""
    today = (today or datetime.date.today()).isoformat()
    owner = op["role"] == "owner"
    out = {}
    if owner or "support" in depts:
        out["tickets_mine"] = tickets(conn, "mine", op["id"])
        out["tickets_unassigned"] = tickets(conn, "unassigned")
        out["reports_waiting"] = conn.execute("""SELECT COUNT(*) FROM problem_reports r WHERE r.shared = 1 AND r.status = 'open'
                                                 AND NOT EXISTS (SELECT 1 FROM hq_tickets t WHERE t.problem_report_id = r.id)""").fetchone()[0]
    if owner or "sales" in depts:
        out["leads_due"] = conn.execute("""SELECT * FROM hq_leads WHERE stage IN ('new','contacted','demo_booked','demo_done','trial')
                                          AND (owner_id = ? OR owner_id IS NULL) AND (next_on IS NULL OR next_on <= ?) ORDER BY next_on LIMIT 20""",
                                        (op["id"], today)).fetchall()
    if owner or "onboarding" in depts:
        out["projects"] = [x for x in projects(conn) if owner or x["p"]["owner_id"] in (op["id"], None)]
    if owner or "finance" in depts:
        out["overdue"] = overdue(conn)
    return out
