"""The list of messages waiting for a person to send, and the rules that fill it.

Nothing here sends anything by itself. The official WhatsApp Business API is not connected, so each message is prepared
and a person opens WhatsApp and presses send. The list is filled by `build`, which is safe to run as often as you like:
every message has a key, and the same key is never queued twice. Consent is checked when a message is queued and again
when it is opened. 'care' messages (reminders, recalls, quote follow-ups) need the patient's agreement to appointment
messages. 'offer' messages (birthday greetings, promotions) need their agreement to offers."""
from datetime import timedelta

from . import audit, i18n, msgtemplates as MT, plans as PL, recalls
from .messaging import wa_link
from .timeutil import now_local, parse_date, parse_dt

KINDS = {"reminder": "Visit reminder", "recall": "Check-up due", "review_request": "Review request", "birthday": "Birthday greeting",
         "plan_followup": "Quote follow-up", "campaign": "Campaign"}
MAX_PLAN_CHASES = 3
REVIEW_GAP_DAYS = 90


class MessageError(Exception):
    pass


def _today(clinic):
    return now_local(clinic["timezone"]).date()


# ----------------------------------------------------------------------------- settings
def ensure_defaults(db, clinic_id):
    db.execute("INSERT OR IGNORE INTO clinic_comms(clinic_id) VALUES (?)", (clinic_id,))
    MT.ensure_templates(db, clinic_id)
    if not db.execute("SELECT 1 FROM reminder_rules WHERE clinic_id = ?", (clinic_id,)).fetchone():
        db.execute("INSERT INTO reminder_rules(clinic_id, days_before) VALUES (?, 2)", (clinic_id,))


def comms(db, clinic_id):
    ensure_defaults(db, clinic_id)
    return db.execute("SELECT * FROM clinic_comms WHERE clinic_id = ?", (clinic_id,)).fetchone()


def save_settings(db, clinic, user_id, auto_recall, auto_review, auto_birthday, auto_plan, review_link):
    link = (review_link or "").strip()
    if link and (not link.startswith("https://") or " " in link or len(link) > 300):
        raise MessageError("The review link must start with https:// and have no spaces.")
    if auto_review and not link:
        raise MessageError("Add the review link before switching on review requests.")
    comms(db, clinic["id"])
    db.execute("""UPDATE clinic_comms SET auto_recall = ?, auto_review = ?, auto_birthday = ?, auto_plan = ?, review_link = ?
                  WHERE clinic_id = ?""", (1 if auto_recall else 0, 1 if auto_review else 0, 1 if auto_birthday else 0,
                                           1 if auto_plan else 0, link or None, clinic["id"]))
    audit.log(db, clinic["id"], user_id, "messaging.settings")


def rules(db, clinic_id):
    ensure_defaults(db, clinic_id)
    return db.execute("SELECT * FROM reminder_rules WHERE clinic_id = ? ORDER BY days_before DESC", (clinic_id,)).fetchall()


def save_rule(db, clinic, user_id, days_before, skip_confirmed, active):
    if not isinstance(days_before, int) or not 0 <= days_before <= 14:
        raise MessageError("Remind from 0 to 14 days before the visit.")
    ensure_defaults(db, clinic["id"])
    db.execute("""INSERT INTO reminder_rules(clinic_id, days_before, skip_confirmed, active) VALUES (?,?,?,?)
                  ON CONFLICT(clinic_id, days_before) DO UPDATE SET skip_confirmed = excluded.skip_confirmed, active = excluded.active""",
               (clinic["id"], days_before, 1 if skip_confirmed else 0, 1 if active else 0))
    audit.log(db, clinic["id"], user_id, "messaging.rule", None, None, f"{days_before} days")


# ----------------------------------------------------------------------------- filling the list
def _enqueue(db, clinic_id, patient_id, kind, purpose, due_on, key, appointment_id=None, recall_id=None, plan_id=None, campaign_id=None):
    cur = db.execute(
        """INSERT OR IGNORE INTO outbox(clinic_id, patient_id, kind, purpose, appointment_id, recall_id, plan_id, campaign_id, dedupe_key, due_on)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (clinic_id, patient_id, kind, purpose, appointment_id, recall_id, plan_id, campaign_id, key, due_on))
    return cur.rowcount == 1


def build(db, clinic, today=None):
    """Queue what is due. Returns {'queued': n, 'no_consent': n}."""
    cid = clinic["id"]
    now = now_local(clinic["timezone"])
    today = today or now.date()
    c = comms(db, cid)
    queued = no_consent = 0
    iso = today.isoformat()

    # visit reminders
    for rule in db.execute("SELECT * FROM reminder_rules WHERE clinic_id = ? AND active = 1", (cid,)).fetchall():
        until = f"{(today + timedelta(days=rule['days_before'])).isoformat()} 23:59"
        for a in db.execute(
                """SELECT a.*, p.reminders_ok FROM appointments a JOIN patients p ON p.id = a.patient_id
                   WHERE a.clinic_id = ? AND a.status = 'scheduled' AND a.start_at > ? AND a.start_at <= ? AND p.status = 'active'""",
                (cid, now.strftime("%Y-%m-%d %H:%M"), until)).fetchall():
            due = parse_date(a["start_at"][:10]) - timedelta(days=rule["days_before"])
            booked_on = parse_date(a["created_at"][:10])
            if booked_on >= due:                       # booked inside the window, so they were told at booking
                continue
            if a["confirmation"] == "declined" or (rule["skip_confirmed"] and a["confirmation"] == "confirmed"):
                continue
            if not a["reminders_ok"]:
                no_consent += 1
                continue
            queued += _enqueue(db, cid, a["patient_id"], "reminder", "care", iso, f"rem:{a['id']}:{rule['id']}", appointment_id=a["id"])

    # check-ups that are due
    if c["auto_recall"]:
        for r in recalls.upcoming(db, cid, today, 0):
            if r["status"] != "due" or r["due_on"] > iso:
                continue
            if not r["reminders_ok"]:
                no_consent += 1
                continue
            queued += _enqueue(db, cid, r["patient_id"], "recall", "care", iso, f"recall:{r['id']}:{r['due_on']}", recall_id=r["id"])

    # a review request after a finished visit
    if c["auto_review"] and c["review_link"]:
        since = (today - timedelta(days=2)).isoformat()
        for a in db.execute(
                """SELECT a.id, a.patient_id, p.reminders_ok FROM appointments a JOIN patients p ON p.id = a.patient_id
                   WHERE a.clinic_id = ? AND a.status = 'complete' AND date(a.start_at) BETWEEN ? AND ? AND p.status = 'active'""",
                (cid, since, iso)).fetchall():
            if not a["reminders_ok"]:
                no_consent += 1
                continue
            recent = db.execute(
                """SELECT 1 FROM outbox WHERE clinic_id = ? AND patient_id = ? AND kind = 'review_request' AND status != 'skipped'
                   AND date(created_at) >= ?""", (cid, a["patient_id"], (today - timedelta(days=REVIEW_GAP_DAYS)).isoformat())).fetchone()
            if recent:
                continue
            queued += _enqueue(db, cid, a["patient_id"], "review_request", "care", iso, f"review:{a['id']}", appointment_id=a["id"])

    # birthdays (offers consent), today and the two days before, so a weekend is not missed
    if c["auto_birthday"]:
        for back in range(3):
            day = today - timedelta(days=back)
            for p in db.execute(
                    """SELECT id FROM patients WHERE clinic_id = ? AND status = 'active' AND offers_ok = 1 AND dob IS NOT NULL
                       AND strftime('%m-%d', dob) = ?""", (cid, day.strftime("%m-%d"))).fetchall():
                queued += _enqueue(db, cid, p["id"], "birthday", "offer", day.isoformat(), f"bday:{p['id']}:{day.year}")

    # quotes still waiting for an answer
    if c["auto_plan"]:
        for p in db.execute(
                """SELECT tp.id, tp.patient_id, tp.follow_up_on, pa.reminders_ok FROM treatment_plans tp JOIN patients pa ON pa.id = tp.patient_id
                   WHERE tp.clinic_id = ? AND tp.status = 'presented' AND tp.follow_up_on <= ? AND pa.status = 'active'""", (cid, iso)).fetchall():
            chased = db.execute("SELECT COUNT(*) FROM outbox WHERE clinic_id = ? AND plan_id = ? AND kind = 'plan_followup' AND status = 'sent'",
                                (cid, p["id"])).fetchone()[0]
            if chased >= MAX_PLAN_CHASES:
                continue
            if not p["reminders_ok"]:
                no_consent += 1
                continue
            queued += _enqueue(db, cid, p["patient_id"], "plan_followup", "care", iso, f"plan:{p['id']}:{p['follow_up_on']}", plan_id=p["id"])
    return {"queued": queued, "no_consent": no_consent}


def refresh(db, clinic, today=None):
    """Drop queued messages that no longer make sense: the visit was cancelled, the patient withdrew consent, the quote was decided."""
    cid = clinic["id"]
    now = now_local(clinic["timezone"])
    skipped = 0
    for o in db.execute(
            """SELECT o.*, p.reminders_ok, p.offers_ok, p.status AS pstatus FROM outbox o JOIN patients p ON p.id = o.patient_id
               WHERE o.clinic_id = ? AND o.status = 'queued'""", (cid,)).fetchall():
        why = None
        if o["pstatus"] != "active":
            why = "Patient archived"
        elif not (o["reminders_ok"] if o["purpose"] == "care" else o["offers_ok"]):
            why = "The patient no longer agrees to these messages"
        elif o["kind"] == "reminder":
            a = db.execute("SELECT * FROM appointments WHERE id = ?", (o["appointment_id"],)).fetchone()
            rule_id = int(o["dedupe_key"].split(":")[2]) if o["dedupe_key"].count(":") == 2 else None
            rule = db.execute("SELECT skip_confirmed FROM reminder_rules WHERE id = ?", (rule_id,)).fetchone() if rule_id else None
            if not a or a["status"] != "scheduled" or a["start_at"] <= now.strftime("%Y-%m-%d %H:%M"):
                why = "The visit is no longer booked"
            elif a["confirmation"] == "declined" or (a["confirmation"] == "confirmed" and (not rule or rule["skip_confirmed"])):
                why = "The visit has already been answered"
        elif o["kind"] == "recall":
            r = recalls.get(db, cid, o["recall_id"])
            if not r or r["status"] != "due" or db.execute(
                    "SELECT 1 FROM appointments WHERE clinic_id = ? AND patient_id = ? AND status = 'scheduled' AND start_at >= ?",
                    (cid, o["patient_id"], now.strftime("%Y-%m-%d %H:%M"))).fetchone():
                why = "Already handled"
        elif o["kind"] == "plan_followup":
            p = PL.get(db, cid, o["plan_id"])
            if not p or p["status"] != "presented":
                why = "The quote has been decided"
        elif o["kind"] == "campaign":
            camp = db.execute("SELECT status FROM campaigns WHERE id = ?", (o["campaign_id"],)).fetchone()
            if not camp or camp["status"] == "closed":
                why = "The campaign was closed"
        if why:
            db.execute("UPDATE outbox SET status = 'skipped', skip_reason = ?, done_at = datetime('now') WHERE id = ?", (why, o["id"]))
            skipped += 1
    return skipped


# ----------------------------------------------------------------------------- reading and sending
def context(db, clinic, o):
    """The values a message can use, for one queued item."""
    p = db.execute("SELECT * FROM patients WHERE id = ?", (o["patient_id"],)).fetchone()
    lang = i18n.patient_lang(p, clinic)
    ctx = {"first_name": p["first_name"], "clinic": clinic["name"], "dentist": i18n.t(lang, "our team")}
    if o["appointment_id"]:
        a = db.execute("""SELECT a.*, pr.name AS provider_name FROM appointments a LEFT JOIN providers pr ON pr.id = a.provider_id
                          WHERE a.id = ?""", (o["appointment_id"],)).fetchone()
        if a and a["start_at"]:
            start = parse_dt(a["start_at"])
            ctx.update(day=i18n.fmt_day_date(start, lang), time=i18n.fmt_time(start, lang), dentist=a["provider_name"] or i18n.t(lang, "our team"))
    ctx["review_link"] = comms(db, clinic["id"])["review_link"] or ""
    return p, ctx


def text_for(db, clinic, o):
    p, ctx = context(db, clinic, o)
    if o["kind"] == "campaign":
        body = db.execute("SELECT body FROM campaigns WHERE id = ?", (o["campaign_id"],)).fetchone()["body"]
    else:
        body = MT.body_for(db, clinic["id"], {"reminder": "reminder", "recall": "recall", "review_request": "review_request",
                                              "birthday": "birthday", "plan_followup": "plan_followup"}[o["kind"]],
                           i18n.patient_lang(p, clinic))
    try:
        return p, MT.render(body, ctx)
    except MT.TemplateError as exc:
        raise MessageError(str(exc))


def queue(db, clinic, today=None):
    cid = clinic["id"]
    out = []
    for o in db.execute(
            """SELECT o.*, p.first_name, p.last_name, p.phone FROM outbox o JOIN patients p ON p.id = o.patient_id
               WHERE o.clinic_id = ? AND o.status = 'queued' ORDER BY o.due_on, o.id LIMIT 300""", (cid,)).fetchall():
        try:
            text, error = text_for(db, clinic, o)[1], None
        except MessageError as exc:
            text, error = None, str(exc)
        out.append({"item": o, "text": text, "error": error})
    return out


def counts(db, cid):
    rows = db.execute("SELECT status, COUNT(*) n FROM outbox WHERE clinic_id = ? GROUP BY status", (cid,)).fetchall()
    return {r["status"]: r["n"] for r in rows}


def get(db, cid, outbox_id):
    return db.execute("SELECT * FROM outbox WHERE id = ? AND clinic_id = ?", (outbox_id, cid)).fetchone()


def send(db, clinic, user_id, outbox_id):
    """Log the message and return the WhatsApp link to open."""
    cid = clinic["id"]
    o = get(db, cid, outbox_id)
    if not o:
        raise LookupError("outbox")
    if o["status"] != "queued":
        raise MessageError("That message has already been dealt with.")
    p, text = text_for(db, clinic, o)
    if p["status"] != "active":
        raise MessageError("This patient is archived.")
    if not (p["reminders_ok"] if o["purpose"] == "care" else p["offers_ok"]):
        raise MessageError("This patient has not agreed to these messages.")
    kind = "campaign" if o["kind"] == "campaign" else o["kind"]
    cur = db.execute(
        "INSERT INTO messages(clinic_id, patient_id, appointment_id, kind, body, status, created_by) VALUES (?,?,?,?,?, 'opened', ?)",
        (cid, p["id"], o["appointment_id"], kind, text, user_id))
    db.execute("UPDATE outbox SET status = 'sent', message_id = ?, done_at = datetime('now'), done_by = ? WHERE id = ?",
               (cur.lastrowid, user_id, outbox_id))
    if o["kind"] == "reminder" and o["appointment_id"]:
        db.execute("UPDATE appointments SET confirmation = 'sent', updated_at = datetime('now') "
                   "WHERE id = ? AND clinic_id = ? AND confirmation = 'none'", (o["appointment_id"], cid))
    elif o["kind"] == "recall" and o["recall_id"]:
        recalls.mark_contacted(db, clinic, o["recall_id"])
    elif o["kind"] == "plan_followup" and o["plan_id"]:
        PL.log_followup(db, clinic, {"id": user_id}, o["plan_id"], "Message opened in WhatsApp", 7)
    audit.log(db, cid, user_id, f"message.{kind}", "message", cur.lastrowid)
    return wa_link(p["phone"], text)


def skip(db, clinic, user_id, outbox_id, reason=None):
    o = get(db, clinic["id"], outbox_id)
    if not o:
        raise LookupError("outbox")
    if o["status"] != "queued":
        raise MessageError("That message has already been dealt with.")
    db.execute("UPDATE outbox SET status = 'skipped', skip_reason = ?, done_at = datetime('now'), done_by = ? WHERE id = ?",
               ((reason or "").strip()[:150] or "Skipped by staff", user_id, outbox_id))
    audit.log(db, clinic["id"], user_id, "message.skip", "outbox", outbox_id)


def attention(db, clinic, user):
    """Numbers for the badge and the Today screen."""
    from . import permissions
    cid = clinic["id"]
    n = {"queue": db.execute("SELECT COUNT(*) FROM outbox WHERE clinic_id = ? AND status = 'queued'", (cid,)).fetchone()[0],
         "replies": db.execute("SELECT COUNT(DISTINCT patient_id) FROM messages WHERE clinic_id = ? AND direction = 'in' AND read_at IS NULL", (cid,)).fetchone()[0],
         "requests": db.execute("SELECT COUNT(*) FROM booking_requests WHERE clinic_id = ? AND status = 'new'", (cid,)).fetchone()[0],
         "intake": 0}
    if permissions.can_clinical(user):
        n["intake"] = db.execute("SELECT COUNT(*) FROM intake_submissions WHERE clinic_id = ? AND status = 'pending'", (cid,)).fetchone()[0]
    n["total"] = sum(n.values())
    return n
