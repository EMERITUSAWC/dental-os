"""Version 0.74: one place for everything that needs this person's attention.

It gathers what already lives on separate screens: messages to send and replies to read, tasks due, team messages, notes to sign,
lab work late, stock low or expired, sterilisation warnings, equipment due, data requests and incidents, plans waiting for a
follow-up, membership fees and instalments overdue. Each item says how many, how urgent, and where to go. What a person sees
follows their role and their permission group. No web framework here: the caller turns each 'endpoint' into a link."""
from . import permgroups

URGENT, SOON, INFO = "urgent", "soon", "info"


LABELS = {
    "replies": "Unread replies from patients",
    "requests": "Online booking requests",
    "queue": "Messages waiting to be sent",
    "intake": "Health forms to check",
    "tasks": "Tasks due",
    "team": "Team messages",
    "unsigned": "Notes waiting to be signed",
    "lab_late": "Lab cases late",
    "lab_ready": "Lab cases back and ready to fit",
    "stock_expired": "Stock past its use-by date",
    "stock_low": "Stock running low",
    "steri": "Sterilisation checks or recalls",
    "equipment": "Equipment faults or services due",
    "data_requests": "Patients' data requests open",
    "incidents": "Data incidents open",
    "plans": "Treatment plans to follow up",
    "membership_fees": "Membership fees due or failed",
    "instalments": "Instalments overdue",
}


def _item(key, n, endpoint, level=SOON):
    return {"key": key, "label": LABELS[key], "n": n, "endpoint": endpoint, "level": level}


def items(db, clinic, user):
    from . import huddle, instalments, labs, memberships, outbox, permissions, plans, privacy, steri, stock, tasks, team
    areas = permgroups.areas_of(db, user)
    role = user["role"]
    out = []
    a = outbox.attention(db, clinic, user)
    if "messages" in areas:
        if a["replies"]:
            out.append(_item("replies", a["replies"], "comms.replies", URGENT))
        if a["requests"]:
            out.append(_item("requests", a["requests"], "comms.requests_list", URGENT))
        if a["queue"]:
            out.append(_item("queue", a["queue"], "comms.queue"))
    if a["intake"]:
        out.append(_item("intake", a["intake"], "comms.forms", URGENT))
    due = tasks.counts(db, clinic, user["id"])["due"]
    if due:
        out.append(_item("tasks", due, "practice.tasks"))
    unread = team.unread(db, user)
    if unread:
        out.append(_item("team", unread, "team.inbox"))
    if permissions.can_sign(user):
        u = huddle.unsigned_counts(db, clinic, user)
        if u["all"]:
            out.append(_item("unsigned", u["all"], "huddle.unsigned", URGENT if u["old"] else SOON))
    if "lab" in areas:
        lab = labs.counts(db, clinic)
        if lab["late"]:
            out.append(_item("lab_late", lab["late"], "ops.lab_board", URGENT))
        if lab["ready"]:
            out.append(_item("lab_ready", lab["ready"], "ops.lab_board", INFO))
    if role != "front_desk" and "stock" in areas:
        st = stock.summary(db, clinic)
        if st["expired"]:
            out.append(_item("stock_expired", st["expired"], "ops.stock_list", URGENT))
        if st["low"]:
            out.append(_item("stock_low", st["low"], "ops.stock_list"))
    if role != "front_desk" and "sterilisation" in areas:
        s = steri.overview(db, clinic)
        if s.get("warnings"):
            out.append(_item("steri", 1, "steri.home", URGENT))
    if role == "manager":
        eq = team.due_summary(db, clinic)
        if eq["faults"] or eq["overdue"]:
            out.append(_item("equipment", len(eq["faults"]) + len(eq["overdue"]), "team.equipment",
                             URGENT if eq["faults"] else SOON))
        pv = privacy.counts(db, clinic)
        if pv["requests_open"]:
            out.append(_item("data_requests", pv["requests_open"], "privacy.requests_list",
                             URGENT if pv["requests_late"] else SOON))
        if pv["incidents_open"]:
            out.append(_item("incidents", pv["incidents_open"], "privacy.incidents_list", URGENT))
    if "plans" in areas:
        waiting = [w for w in plans.waiting(db, clinic) if w["due"]]
        if waiting:
            out.append(_item("plans", len(waiting), "commercial.plans_board"))
    if "memberships" in areas:
        m = memberships.attention(db, clinic)
        if m:
            out.append(_item("membership_fees", len(m), "commercial.memberships_home"))
        i = instalments.overdue(db, clinic)
        if i:
            out.append(_item("instalments", len(i), "commercial.memberships_home"))
    order = {URGENT: 0, SOON: 1, INFO: 2}
    return sorted(out, key=lambda x: order[x["level"]])


def total(rows):
    return sum(r["n"] for r in rows if r["level"] != INFO)
