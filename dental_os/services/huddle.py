"""Version 0.73: the morning huddle, goals, notes waiting to be signed, and membership savings on quotes.

Goals: each month the manager sets goals for the clinic and, if wanted, for each dentist: production, collections, new patients
and how much of the treatment offered is accepted. Progress is measured against the pace for the working days gone so far (the
clinic's working days, less its closed days), so "behind" means behind where it should be by today, not behind the whole month.

The morning huddle: the day before, the month so far against goal, today's patients with what to do for each (money owed,
treatment not booked, recalls due, medical alerts, lab work), free chair time, and tomorrow's unconfirmed visits.

Notes waiting to be signed: a draft note is not part of the legal record. Each signer sees their own; managers see everyone's.
Drafts older than UNSIGNED_DAYS are flagged on the Today screen and in the huddle.

Membership savings: on a quote for a patient with no active cover and no membership, the clinic's own plans are shown with what
each would save on this treatment, so the patient can choose. No web framework here."""
import calendar
from datetime import date, timedelta

from . import audit, closeddays, kpi
from .timeutil import now_local

UNSIGNED_DAYS = 2
FIELDS = ("production_p", "collections_p", "new_patients", "acceptance_pct")
FIELD_LABELS = {"production_p": "Production", "collections_p": "Collections", "new_patients": "New patients",
                "acceptance_pct": "Treatment accepted (by value)"}


class GoalError(ValueError):
    pass


def _today(clinic):
    return now_local(clinic["timezone"]).date()


def _month_bounds(month):
    y, m = (int(x) for x in month.split("-"))
    return date(y, m, 1), date(y, m, calendar.monthrange(y, m)[1])


def check_month(month):
    try:
        y, m = (int(x) for x in (month or "").split("-"))
        if not (2000 <= y <= 2100 and 1 <= m <= 12) or len(month) != 7:
            raise ValueError
    except ValueError as exc:
        raise GoalError("Pick a month.") from exc
    return month


def working_days(db, clinic, start, end):
    """The clinic's working days between two dates (both counted), less its closed days."""
    days = {int(d) for d in (clinic["working_days"] or "").split(",") if d.strip()}
    closed = closeddays.days_set(db, clinic["id"])
    n, d = 0, start
    while d <= end:
        if d.isoweekday() in days and d.isoformat() not in closed:
            n += 1
        d += timedelta(days=1)
    return n


# ------------------------------------------------------------------------------------------------------------------ goals
def goals(db, cid, month):
    """{provider_id or None: row} for one month."""
    return {r["provider_id"]: r for r in db.execute("SELECT * FROM practice_goals WHERE clinic_id = ? AND month = ?", (cid, month))}


def save_goal(db, clinic, user_id, month, provider_id, values):
    """values: {field: int or None}. An empty set of values removes the goal."""
    cid = clinic["id"]
    month = check_month(month)
    if provider_id and not db.execute("SELECT 1 FROM providers WHERE id = ? AND clinic_id = ?", (provider_id, cid)).fetchone():
        raise LookupError("provider")
    clean = {}
    for f in FIELDS:
        v = values.get(f)
        if v is None:
            clean[f] = None
            continue
        if not isinstance(v, int) or v < 0 or (f == "acceptance_pct" and v > 100):
            raise GoalError("Goals are whole numbers of zero or more; acceptance is 0 to 100.")
        clean[f] = v
    if provider_id:
        db.execute("DELETE FROM practice_goals WHERE clinic_id = ? AND month = ? AND provider_id = ?", (cid, month, provider_id))
    else:
        db.execute("DELETE FROM practice_goals WHERE clinic_id = ? AND month = ? AND provider_id IS NULL", (cid, month))
    if any(v is not None for v in clean.values()):
        db.execute("""INSERT INTO practice_goals(clinic_id, month, provider_id, production_p, collections_p, new_patients, acceptance_pct, set_by)
                      VALUES (?,?,?,?,?,?,?,?)""", (cid, month, provider_id or None, clean["production_p"], clean["collections_p"],
                                                    clean["new_patients"], clean["acceptance_pct"], user_id))
    audit.log(db, cid, user_id, "goals.save", None, None, f"{month} {'dentist ' + str(provider_id) if provider_id else 'clinic'}")


def copy_goals(db, clinic, user_id, from_month, to_month):
    """Start a month from the one before. Goals already set for the new month are kept."""
    cid = clinic["id"]
    check_month(from_month)
    check_month(to_month)
    have = goals(db, cid, to_month)
    n = 0
    for pid, r in goals(db, cid, from_month).items():
        if pid in have:
            continue
        save_goal(db, clinic, user_id, to_month, pid, {f: r[f] for f in FIELDS})
        n += 1
    return n


def _pace(goal, done_days, all_days):
    if goal is None or not all_days:
        return None
    return round(goal * done_days / all_days)


def progress(db, clinic, today=None):
    """The month so far against goal and against the pace for today, for the clinic and each dentist with a goal."""
    today = today or _today(clinic)
    month = today.isoformat()[:7]
    start, end = _month_bounds(month)
    all_days = working_days(db, clinic, start, end)
    done_days = working_days(db, clinic, start, today)
    g = goals(db, clinic["id"], month)
    cur = kpi.period(db, clinic, start, today)
    actual = {"production_p": cur["gross_p"], "collections_p": cur["collected_p"], "new_patients": cur["new_patients"],
              "acceptance_pct": cur["plans"]["value_rate"]}

    def rows(goal, have):
        out = []
        for f in FIELDS:
            target = goal[f] if goal else None
            pace = target if f == "acceptance_pct" else _pace(target, done_days, all_days)
            a = have.get(f)
            out.append({"field": f, "goal": target, "pace": pace, "actual": a,
                        "ahead": None if pace is None or a is None else a >= pace,
                        "pct_of_goal": round(a * 100 / target) if target and a is not None and f != "acceptance_pct" else None})
        return out
    clinic_rows = rows(g.get(None), actual)
    dentists = []
    for p in db.execute("SELECT id, name FROM providers WHERE clinic_id = ? AND active = 1 ORDER BY name", (clinic["id"],)):
        if p["id"] not in g:
            continue
        prod = kpi._production(db, clinic["id"], start.isoformat(), today.isoformat(), p["id"])
        goal = g[p["id"]]
        pace = _pace(goal["production_p"], done_days, all_days)
        dentists.append({"id": p["id"], "name": p["name"], "goal": goal["production_p"], "pace": pace, "actual": prod,
                         "ahead": None if pace is None else prod >= pace})
    return {"month": month, "days_done": done_days, "days_all": all_days, "clinic": clinic_rows, "dentists": dentists,
            "has_goals": bool(g)}


# ------------------------------------------------------------------------------------------------------- notes to be signed
def unsigned(db, clinic, user=None, today=None):
    """Draft notes. For a signer who is not a manager, only their own. Each with how many days it has waited."""
    today = today or _today(clinic)
    sql = """SELECT n.id, n.patient_id, n.title, n.created_at, n.author_id, u.display_name AS author, p.first_name, p.last_name
             FROM clinical_notes n JOIN patients p ON p.id = n.patient_id LEFT JOIN users u ON u.id = n.author_id
             WHERE n.clinic_id = ? AND n.status = 'draft'"""
    args = [clinic["id"]]
    if user is not None and user["role"] != "manager":
        sql += " AND n.author_id = ?"
        args.append(user["id"])
    out = []
    for r in db.execute(sql + " ORDER BY n.created_at", args).fetchall():
        age = (today - date.fromisoformat(r["created_at"][:10])).days
        out.append({"n": r, "days": age, "old": age >= UNSIGNED_DAYS})
    return out


def unsigned_counts(db, clinic, user, today=None):
    rows = unsigned(db, clinic, user, today)
    return {"all": len(rows), "old": sum(1 for x in rows if x["old"])}


# -------------------------------------------------------------------------------------------------------------- the huddle
def last_working_day(db, clinic, today):
    d = today - timedelta(days=1)
    for _ in range(14):
        if working_days(db, clinic, d, d):
            return d
        d -= timedelta(days=1)
    return today - timedelta(days=1)


def day_figures(db, clinic, day):
    p = kpi.period(db, clinic, day, day)
    return {"day": day, "production_p": p["gross_p"], "collections_p": p["collected_p"], "new_patients": p["new_patients"],
            "done": p["visits"]["done"], "missed": p["visits"]["missed"], "cancelled": p["visits"]["cancelled"],
            "accepted": p["plans"]["accepted"], "accepted_p": p["plans"]["accepted_p"]}


def todays_patients(db, clinic, day):
    """Today's visits, each with what the team should do: money owed, treatment planned but not booked, recall due, alerts, lab work."""
    from . import billing
    cid = clinic["id"]
    rows = db.execute("""SELECT a.id, a.patient_id, a.start_at, a.duration_min, a.status, a.confirmation, p.first_name, p.last_name,
                         pr.name AS provider_name FROM appointments a JOIN patients p ON p.id = a.patient_id
                         LEFT JOIN providers pr ON pr.id = a.provider_id
                         WHERE a.clinic_id = ? AND a.start_at >= ? AND a.start_at < ? AND a.status IN ('scheduled','complete')
                         ORDER BY a.start_at""", (cid, f"{day} 00:00", f"{day + timedelta(days=1)} 00:00")).fetchall()
    out = []
    for a in rows:
        pid = a["patient_id"]
        balance = billing.totals(db, cid, pid)["balance"]
        planned = db.execute("""SELECT COUNT(*) AS n, COALESCE(SUM(fee_p), 0) AS v FROM procedures WHERE clinic_id = ? AND patient_id = ?
                                AND status = 'planned' AND (appointment_id IS NULL OR appointment_id != ?)""", (cid, pid, a["id"])).fetchone()
        recall = db.execute("""SELECT MIN(due_on) FROM recalls WHERE clinic_id = ? AND patient_id = ? AND status != 'disabled' AND due_on <= ?""",
                            (cid, pid, (day + timedelta(days=30)).isoformat())).fetchone()[0]
        alerts = db.execute("SELECT COUNT(*) FROM patient_alerts WHERE clinic_id = ? AND patient_id = ? AND active = 1",
                            (cid, pid)).fetchone()[0]
        lab = db.execute("""SELECT COUNT(*) FROM lab_cases WHERE clinic_id = ? AND patient_id = ? AND status IN ('draft','sent')
                            AND due_on <= ?""", (cid, pid, day.isoformat())).fetchone()[0]
        out.append({"a": a, "balance_p": max(balance, 0), "planned": planned["n"], "planned_p": planned["v"], "recall_due": recall,
                    "alerts": alerts, "lab_late": lab})
    return out


def huddle(db, clinic, user, today=None):
    today = today or _today(clinic)
    yday = last_working_day(db, clinic, today)
    tomorrow = today + timedelta(days=1)
    chairs = kpi.chair_use(db, clinic, today, today)
    unconfirmed = db.execute("""SELECT COUNT(*) FROM appointments WHERE clinic_id = ? AND status = 'scheduled'
                                AND confirmation IN ('none','sent','failed') AND start_at >= ? AND start_at < ?""",
                             (clinic["id"], f"{tomorrow} 00:00", f"{tomorrow + timedelta(days=1)} 00:00")).fetchone()[0]
    patients = todays_patients(db, clinic, today)
    return {"today": today, "yesterday": day_figures(db, clinic, yday), "progress": progress(db, clinic, today),
            "patients": patients, "free_min": max(chairs["available_min"] - chairs["booked_min"], 0), "chairs": chairs,
            "unconfirmed_tomorrow": unconfirmed, "tomorrow": tomorrow,
            "owed_today_p": sum(x["balance_p"] for x in patients), "planned_today": sum(1 for x in patients if x["planned"]),
            "unsigned": unsigned_counts(db, clinic, user, today)}


# ---------------------------------------------------------------------------------------------------- membership savings
def has_cover(db, cid, pid, today):
    t = today.isoformat()
    return bool(db.execute("""SELECT 1 FROM patient_cover WHERE clinic_id = ? AND patient_id = ? AND active = 1
                              AND (valid_from IS NULL OR valid_from <= ?) AND (valid_to IS NULL OR valid_to >= ?)""", (cid, pid, t, t)).fetchone())


def has_membership(db, cid, pid):
    return bool(db.execute("SELECT 1 FROM memberships WHERE clinic_id = ? AND patient_id = ? AND status = 'active'", (cid, pid)).fetchone())


def membership_offers(db, clinic, pid, plan_rows, today=None):
    """For a patient with no cover and no membership: each active membership plan that would save money on these lines,
    best first. Included services count up to the number a year; everything else gets the plan's discount."""
    from . import memberships
    today = today or _today(clinic)
    cid = clinic["id"]
    if not clinic["offer_membership"] or has_cover(db, cid, pid, today) or has_membership(db, cid, pid):
        return []
    lines = [r for r in plan_rows if not r["removed"]] if plan_rows and "removed" in plan_rows[0].keys() else list(plan_rows or [])
    total = sum(r["fee_p"] for r in lines)
    if not total:
        return []
    out = []
    for x in memberships.list_plans(db, cid, active_only=True):
        plan = x["plan"]
        left = {b["code_id"]: b["qty_per_year"] for b in x["benefits"]}
        saved = 0
        for r in sorted(lines, key=lambda r: -r["fee_p"]):
            if left.get(r["code_id"], 0) > 0:
                left[r["code_id"]] -= 1
                saved += r["fee_p"]
            else:
                saved += r["fee_p"] * plan["discount_pct"] // 100
        per_year = plan["fee_p"] * 12 // memberships.CYCLES[plan["cycle"]][1]
        if saved > 0:
            out.append({"plan": plan, "saved_p": saved, "per_year_p": per_year, "net_p": saved - per_year, "total_p": total})
    return sorted(out, key=lambda o: -o["net_p"])


def join_link(clinic, plan_name):
    """A WhatsApp link to the clinic with the patient's question already written, for the QR code on a printed quote."""
    from .messaging import wa_link
    from .phone import normalise_phone
    try:
        number = normalise_phone(clinic["whatsapp"], clinic["country"]) if clinic["whatsapp"] else None
    except ValueError:
        number = None
    if not number:
        return None
    return wa_link(number, f"Hello, I would like to join the {plan_name} plan.")
