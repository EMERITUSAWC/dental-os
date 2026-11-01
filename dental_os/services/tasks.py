"""Staff tasks: things someone in the clinic must do, with who, by when, and optionally which patient. A task that repeats makes
the next one when it is done. Nothing is deleted: a task is done or cancelled."""
from datetime import timedelta

from . import audit
from .timeutil import now_local, parse_date

PRIORITIES = {"normal": "Normal", "high": "High", "urgent": "Urgent"}
REPEATS = {"none": "Does not repeat", "daily": "Every day", "weekly": "Every week", "monthly": "Every month"}
_RANK = {"urgent": 0, "high": 1, "normal": 2}


class TaskError(Exception):
    pass


def _today(clinic):
    return now_local(clinic["timezone"]).date()


def _next(day, repeat):
    if repeat == "daily":
        return day + timedelta(days=1)
    if repeat == "weekly":
        return day + timedelta(days=7)
    if repeat == "monthly":
        y, m = divmod(day.month, 12)
        y, m = day.year + y, m + 1
        import calendar
        return day.replace(year=y, month=m, day=min(day.day, calendar.monthrange(y, m)[1]))
    return None


def add(db, clinic, user_id, title, assigned_to=None, due_on=None, priority="normal", patient_id=None, detail=None, repeat="none"):
    cid = clinic["id"]
    title = (title or "").strip()[:150]
    if not title:
        raise TaskError("Write what needs doing.")
    if priority not in PRIORITIES:
        raise TaskError("Pick a priority from the list.")
    if repeat not in REPEATS:
        raise TaskError("Pick how often it repeats.")
    if assigned_to and not db.execute("SELECT 1 FROM users WHERE id = ? AND clinic_id = ? AND active = 1", (assigned_to, cid)).fetchone():
        raise TaskError("Pick someone from this clinic.")
    if patient_id and not db.execute("SELECT 1 FROM patients WHERE id = ? AND clinic_id = ?", (patient_id, cid)).fetchone():
        raise TaskError("That patient was not found.")
    due = None
    if (due_on or "").strip():
        try:
            due = parse_date(due_on.strip())
        except ValueError:
            raise TaskError("Use a date like 2026-11-30.")
    if repeat != "none" and not due:
        raise TaskError("A task that repeats needs a first due date.")
    tid = db.execute("""INSERT INTO tasks(clinic_id, title, detail, patient_id, assigned_to, due_on, priority, repeat, created_by)
                        VALUES (?,?,?,?,?,?,?,?,?)""",
                     (cid, title, (detail or "").strip()[:1000] or None, patient_id or None, assigned_to or None,
                      due.isoformat() if due else None, priority, repeat, user_id)).lastrowid
    audit.log(db, cid, user_id, "task.add", "task", tid)
    return tid


def get(db, cid, tid):
    return db.execute("""SELECT t.*, u.display_name AS assigned_name, c.display_name AS created_name, p.first_name, p.last_name
                         FROM tasks t LEFT JOIN users u ON u.id = t.assigned_to LEFT JOIN users c ON c.id = t.created_by
                         LEFT JOIN patients p ON p.id = t.patient_id WHERE t.id = ? AND t.clinic_id = ?""", (tid, cid)).fetchone()


def finish(db, clinic, user_id, tid, outcome="done", note=None):
    """Mark a task done or cancelled. A repeating task that is done makes the next one."""
    cid = clinic["id"]
    t = get(db, cid, tid)
    if not t:
        raise TaskError("That task was not found.")
    if t["status"] != "open":
        raise TaskError("That task is already closed.")
    if outcome not in ("done", "cancelled"):
        raise TaskError("Choose done or cancelled.")
    db.execute("UPDATE tasks SET status = ?, closed_by = ?, closed_at = datetime('now'), close_note = ? WHERE id = ?",
               (outcome, user_id, (note or "").strip()[:300] or None, tid))
    audit.log(db, cid, user_id, f"task.{outcome}", "task", tid)
    nxt = None
    if outcome == "done" and t["repeat"] != "none" and t["due_on"]:
        day = _next(parse_date(t["due_on"]), t["repeat"])
        today = _today(clinic)
        while day <= today and t["repeat"] == "daily":
            day = _next(day, "daily")
        nxt = db.execute("""INSERT INTO tasks(clinic_id, title, detail, patient_id, assigned_to, due_on, priority, repeat, created_by, repeat_of)
                            VALUES (?,?,?,?,?,?,?,?,?,?)""",
                         (cid, t["title"], t["detail"], t["patient_id"], t["assigned_to"], day.isoformat(), t["priority"], t["repeat"],
                          user_id, t["repeat_of"] or tid)).lastrowid
    return nxt


def reassign(db, clinic, user_id, tid, assigned_to):
    cid = clinic["id"]
    t = get(db, cid, tid)
    if not t or t["status"] != "open":
        raise TaskError("That task was not found.")
    if assigned_to and not db.execute("SELECT 1 FROM users WHERE id = ? AND clinic_id = ? AND active = 1", (assigned_to, cid)).fetchone():
        raise TaskError("Pick someone from this clinic.")
    db.execute("UPDATE tasks SET assigned_to = ? WHERE id = ?", (assigned_to or None, tid))
    audit.log(db, cid, user_id, "task.assign", "task", tid)


def board(db, clinic, user_id=None, mine=False, status="open", patient_id=None):
    cid = clinic["id"]
    q = ("""SELECT t.*, u.display_name AS assigned_name, p.first_name, p.last_name FROM tasks t
            LEFT JOIN users u ON u.id = t.assigned_to LEFT JOIN patients p ON p.id = t.patient_id
            WHERE t.clinic_id = ? AND t.status = ?""")
    args = [cid, status]
    if mine:
        q += " AND (t.assigned_to = ? OR (t.assigned_to IS NULL AND t.created_by = ?))"
        args += [user_id, user_id]
    if patient_id:
        q += " AND t.patient_id = ?"
        args.append(patient_id)
    rows = db.execute(q + " ORDER BY t.id DESC LIMIT 300", args).fetchall()
    today = _today(clinic).isoformat()
    if status == "open":
        rows = sorted(rows, key=lambda r: (r["due_on"] is None, r["due_on"] or "", _RANK.get(r["priority"], 3), r["id"]))
    return [{"t": r, "late": bool(r["due_on"] and r["due_on"] < today), "today": r["due_on"] == today} for r in rows]


def counts(db, clinic, user_id):
    today = _today(clinic).isoformat()
    row = db.execute("""SELECT COUNT(*) AS open,
                               SUM(CASE WHEN due_on IS NOT NULL AND due_on <= ? THEN 1 ELSE 0 END) AS due
                        FROM tasks WHERE clinic_id = ? AND status = 'open' AND (assigned_to = ? OR (assigned_to IS NULL AND created_by = ?))""",
                     (today, clinic["id"], user_id, user_id)).fetchone()
    return {"open": row["open"] or 0, "due": row["due"] or 0}
