"""Version 0.75: AI help for daily work, step one. Everything here runs on the clinic's own server; nothing is sent anywhere.

The rule for all of it: the AI suggests, a person decides. Nothing here sends a message, books a visit, changes money or writes
the record. Each suggestion that a person uses is written in the activity log as 'ai.used'.

  * Reading patient replies: sorts each unread WhatsApp or SMS reply (confirming, cancelling, wants another time, asking a
    price, unhappy, a clinical worry, a question) and, for "another time", finds free times that fit what the patient asked for
    ("Thursday afternoon"). It drafts a reply in the patient's language with no money or treatment details in it. A clinical worry
    (pain, swelling, bleeding) is never answered by a draft: it is passed to a dentist.
  * Who may not come: a risk for each of tomorrow's visits from the patient's own history, so the desk calls those first.
  * Before each patient: a short brief for the dentist: alerts, last visit, work planned but not done, recall due.
  * The manager's morning summary: what to act on today in plain sentences, and money patterns worth a look (one person's voids
    or discounts far above everyone else's, cash-ups that keep coming up short).
  * Support at HQ: the help articles that best match a ticket, and a draft answer from the best one.

Pattern rules here, not a language model, so it works offline, costs nothing per use and sends no health data abroad. An
outside model can be added later behind the same functions, once a clinic has a legal basis to use one."""
import datetime
import json
import re

from . import audit, i18n

INTENTS = {"clinical": "Clinical worry: pass to a dentist", "complaint": "Unhappy", "cancel": "Cancelling", "reschedule": "Wants another time",
           "price": "Asking about cost", "confirm": "Confirming", "question": "Question", "other": "Other"}
_W = r"(?:^|\b)"
RULES = [
    ("clinical", r"\b(pain|painful|ache|aching|hurts?|hurting|swell|swollen|swelling|bleed|bleeding|blood|pus|fever|abscess|broke|broken|"
                 r"cracked|numb|can'?t open|cannot open)\b"),
    ("complaint", r"\b(complain|complaint|not happy|unhappy|disappointed|rude|bad service|poor service|waited too long|refund)\b"),
    ("cancel", r"\b(cancel|cancell?ing|can'?t make|cannot make|won'?t (?:be able|make|come)|not coming|unable to come|can'?t come|cannot come)\b"),
    ("reschedule", r"\b(reschedule|re-schedule|change (?:the |my )?(?:time|date|day|appointment)|another (?:day|time|date)|move (?:it|my)|"
                   r"postpone|later date|next week|different (?:day|time)|can i come|could i come|instead)\b"),
    ("price", r"\b(how much|price|prices|cost|costs|charge|charges|fee|fees|pay|payment|insurance|nhis)\b"),
    ("confirm", r"(?:^\s*(?:yes|yeah|yep|ok|okay|sure|confirm(?:ed)?|1|y)\b|\b(i will be there|i'?ll be there|see you|i will come|i'?ll come|"
                r"i confirm|confirmed)\b)"),
]
WEEKDAYS = {"monday": 1, "tuesday": 2, "wednesday": 3, "thursday": 4, "friday": 5, "saturday": 6, "sunday": 7,
            "mon": 1, "tue": 2, "tues": 2, "wed": 3, "thu": 4, "thur": 4, "thurs": 4, "fri": 5, "sat": 6, "sun": 7}
PARTS = {"morning": (0, 12 * 60), "afternoon": (12 * 60, 17 * 60), "evening": (17 * 60, 24 * 60)}

REPLIES = {
    "confirm": "Thank you, {first_name}. We look forward to seeing you. {clinic}",
    "cancel": "Thank you for letting us know, {first_name}. Would you like us to find you another time? {clinic}",
    "reschedule_slots": "Hello {first_name}, we can offer: {slots}. Reply with the one that suits you. {clinic}",
    "reschedule_none": "Hello {first_name}, we will call you to find a time that suits you. {clinic}",
    "price": "Hello {first_name}, thank you for asking. Our team will call you to talk about costs. {clinic}",
    "complaint": "Hello {first_name}, we are sorry to hear this. The practice manager will call you today. {clinic}",
    "question": "Hello {first_name}, thank you for your message. Our team will reply shortly. {clinic}",
}


def classify(text):
    """(intent, preference) for one message. The first rule that matches wins, in the order above."""
    t = " " + (text or "").lower().strip() + " "
    intent = "other"
    for name, pattern in RULES:
        if re.search(pattern, t):
            intent = name
            break
    else:
        if "?" in t:
            intent = "question"
    return intent, preference(text)


def preference(text, today=None):
    """What day and part of the day a patient asked for: {'weekday': 1-7 or None, 'tomorrow': bool, 'next_week': bool,
    'part': 'morning'|'afternoon'|'evening'|None, 'time': 'HH:MM' or None}."""
    t = (text or "").lower()
    out = {"weekday": None, "tomorrow": "tomorrow" in t, "next_week": "next week" in t, "part": None, "time": None}
    for word, n in WEEKDAYS.items():
        if re.search(rf"\b{word}\b", t):
            out["weekday"] = n
            break
    for part in PARTS:
        if part in t:
            out["part"] = part
            break
    m = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", t) or re.search(r"\b(\d{1,2}):(\d{2})\b", t)
    if m:
        h = int(m.group(1))
        mins = int(m.group(2) or 0)
        ampm = m.group(3) if len(m.groups()) >= 3 else None
        if ampm == "pm" and h < 12:
            h += 12
        if ampm == "am" and h == 12:
            h = 0
        if 0 <= h <= 23 and 0 <= mins <= 59:
            out["time"] = f"{h:02d}:{mins:02d}"
    return out


def _mins(hhmm):
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def suggest_slots(db, clinic, patient_id, pref, today=None, want=3, duration=None):
    """Free times that fit the patient's wish, with their usual dentist first. Looks at the next 21 days."""
    from . import scheduling
    from .timeutil import now_local
    now = now_local(clinic["timezone"])
    today = today or now.date()
    usual = db.execute("""SELECT provider_id FROM appointments WHERE clinic_id = ? AND patient_id = ? AND provider_id IS NOT NULL
                          ORDER BY start_at DESC LIMIT 1""", (clinic["id"], patient_id)).fetchone()
    providers = [r["id"] for r in db.execute("SELECT id FROM providers WHERE clinic_id = ? AND active = 1 ORDER BY id", (clinic["id"],))]
    if usual and usual["provider_id"] in providers:
        providers.remove(usual["provider_id"])
        providers.insert(0, usual["provider_id"])
    names = {r["id"]: r["name"] for r in db.execute("SELECT id, name FROM providers WHERE clinic_id = ?", (clinic["id"],))}
    duration = duration or 30
    start = today + datetime.timedelta(days=1)
    if pref.get("next_week"):
        start = today + datetime.timedelta(days=(7 - today.weekday()))
    out = []
    for i in range(21):
        day = start + datetime.timedelta(days=i)
        if pref.get("tomorrow") and day != today + datetime.timedelta(days=1):
            continue
        if pref.get("weekday") and day.isoweekday() != pref["weekday"]:
            continue
        for pid in providers:
            for s in scheduling.free_slots(db, clinic, pid, day, duration, now=now):
                m = _mins(s["time"])
                if pref.get("part") and not (PARTS[pref["part"]][0] <= m < PARTS[pref["part"]][1]):
                    continue
                if pref.get("time") and abs(m - _mins(pref["time"])) > 60:
                    continue
                out.append({"day": day, "time": s["time"], "provider_id": pid, "provider": names.get(pid)})
                break                                                    # one time per dentist per day keeps the choice short
            if len(out) >= want:
                return out
    return out


def _slot_words(slots, lang):
    return "; ".join(f"{i18n.fmt_day_date(s['day'], lang)} {i18n.t(lang, 'at')} {s['time']}" for s in slots)


def draft_reply(db, clinic, patient, intent, slots=None):
    """A reply in the patient's language, or None for a clinical worry (a dentist decides). Never money or treatment details."""
    from . import msgtemplates
    if intent == "clinical":
        return None
    lang = i18n.patient_lang(patient, clinic)
    if intent == "reschedule":
        key = "reschedule_slots" if slots else "reschedule_none"
    elif intent in REPLIES:
        key = intent
    else:
        key = "question"
    text = i18n.t(lang, REPLIES[key]).format(first_name=patient["first_name"], clinic=clinic["name"], slots=_slot_words(slots or [], lang))
    if msgtemplates.MONEY_WORDS.search(text):
        return None                                                      # belt and braces: the rule is never money in a message
    return text


def read_replies(db, clinic, patient, limit=5):
    """The newest unread replies from one patient, each sorted, with free times and a draft for the newest."""
    rows = db.execute("""SELECT * FROM messages WHERE clinic_id = ? AND patient_id = ? AND direction = 'in' AND read_at IS NULL
                         ORDER BY id DESC LIMIT ?""", (clinic["id"], patient["id"], limit)).fetchall()
    if not rows:
        return None
    newest = rows[0]
    intent, pref = classify(newest["body"])
    slots = suggest_slots(db, clinic, patient["id"], pref) if intent == "reschedule" else []
    return {"message": newest, "intent": intent, "label": INTENTS[intent], "pref": pref, "slots": slots,
            "draft": draft_reply(db, clinic, patient, intent, slots), "others": [classify(r["body"])[0] for r in rows[1:]]}


def used(db, clinic, user_id, what, patient_id=None):
    """A person used a suggestion. Logged so a clinic can see how the help is used; the text itself is not logged."""
    audit.log(db, clinic["id"], user_id, "ai.used", "patient" if patient_id else None, patient_id, what[:40])


# --------------------------------------------------------------------------------------------------------- who may not come
def no_show_risk(db, clinic, appt, today=None):
    """0 to 100, from the patient's own history: past visits missed, whether this visit is confirmed, a first visit, and a
    booking made long ago. With the reasons, so the desk knows why."""
    today = today or datetime.date.today()
    cid, pid = clinic["id"], appt["patient_id"]
    past = db.execute("""SELECT COUNT(*) AS n, SUM(CASE WHEN status = 'broken' AND break_reason = 'missed' THEN 1 ELSE 0 END) AS missed
                         FROM appointments WHERE clinic_id = ? AND patient_id = ? AND status IN ('complete','broken') AND id != ?""",
                      (cid, pid, appt["id"])).fetchone()
    n, missed = past["n"] or 0, past["missed"] or 0
    score, why = 10, []
    if n:
        rate = missed / n
        score += round(rate * 60)
        if missed:
            why.append(("missed", missed, n))
    else:
        score += 15
        why.append(("first", 0, 0))
    if appt["confirmation"] != "confirmed":
        score += 15
        why.append(("unconfirmed", 0, 0))
    made = (appt["created_at"] or "")[:10]
    if made and (today - datetime.date.fromisoformat(made)).days > 30:
        score += 10
        why.append(("long_ago", 0, 0))
    return min(score, 100), why


RISK_WORDS = {"missed": "Missed %(a)s of %(b)s visits", "first": "First visit", "unconfirmed": "Not confirmed", "long_ago": "Booked over a month ago"}


def risky_visits(db, clinic, day, at_least=40):
    rows = db.execute("""SELECT a.*, p.first_name, p.last_name, p.phone FROM appointments a JOIN patients p ON p.id = a.patient_id
                         WHERE a.clinic_id = ? AND a.status = 'scheduled' AND a.start_at >= ? AND a.start_at < ? ORDER BY a.start_at""",
                      (clinic["id"], f"{day} 00:00", f"{day + datetime.timedelta(days=1)} 00:00")).fetchall()
    out = []
    for a in rows:
        score, why = no_show_risk(db, clinic, a)
        if score >= at_least:
            out.append({"a": a, "score": score, "why": why})
    return sorted(out, key=lambda x: -x["score"])


# ---------------------------------------------------------------------------------------------------------- before a patient
def brief(db, clinic, patient_id, with_money=False):
    """What a dentist should know before the patient sits down, in a few short facts."""
    cid = clinic["id"]
    alerts = [r["text"] for r in db.execute("""SELECT text FROM patient_alerts WHERE clinic_id = ? AND patient_id = ? AND active = 1
                                               ORDER BY CASE severity WHEN 'critical' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END""", (cid, patient_id))]
    last = db.execute("""SELECT a.start_at, pr.name AS provider FROM appointments a LEFT JOIN providers pr ON pr.id = a.provider_id
                         WHERE a.clinic_id = ? AND a.patient_id = ? AND a.status = 'complete' ORDER BY a.start_at DESC LIMIT 1""",
                      (cid, patient_id)).fetchone()
    last_note = db.execute("""SELECT title, created_at FROM clinical_notes WHERE clinic_id = ? AND patient_id = ? AND status = 'signed'
                              ORDER BY id DESC LIMIT 1""", (cid, patient_id)).fetchone()
    planned = db.execute("""SELECT c.name, pr.tooth FROM procedures pr JOIN procedure_codes c ON c.id = pr.code_id
                            WHERE pr.clinic_id = ? AND pr.patient_id = ? AND pr.status = 'planned' ORDER BY pr.planned_on LIMIT 6""",
                         (cid, patient_id)).fetchall()
    recall = db.execute("SELECT MIN(due_on) FROM recalls WHERE clinic_id = ? AND patient_id = ? AND status != 'disabled'", (cid, patient_id)).fetchone()[0]
    out = {"alerts": alerts, "last_visit": last["start_at"][:10] if last else None, "last_with": last["provider"] if last else None,
           "last_note": last_note["title"] if last_note else None, "planned": planned, "recall_due": recall}
    if with_money:
        from . import billing
        out["balance_p"] = max(billing.totals(db, cid, patient_id)["balance"], 0)
    return out


# ------------------------------------------------------------------------------------------------------- the manager's summary
def money_patterns(db, clinic, today=None, days=30):
    """Patterns worth a look, not accusations: a person whose voids or discounts are far above the rest, and cash-ups short
    again and again. Each with the numbers behind it."""
    today = today or datetime.date.today()
    since = (today - datetime.timedelta(days=days)).isoformat()
    cid = clinic["id"]
    out = []
    for action, label in (("payment.void", "voids"), ("adjustment.add", "discounts")):
        rows = db.execute("""SELECT a.user_id, u.display_name, COUNT(*) AS n FROM audit_log a LEFT JOIN users u ON u.id = a.user_id
                             WHERE a.clinic_id = ? AND a.action = ? AND a.at >= ? GROUP BY a.user_id, u.display_name""",
                          (cid, action, since)).fetchall()
        total = sum(r["n"] for r in rows)
        if total < 5 or len(rows) < 1:
            continue
        for r in rows:
            others = total - r["n"]
            share = r["n"] / total
            if r["n"] >= 5 and (len(rows) == 1 or share >= 0.6) and r["n"] >= 2 * max(others, 1):
                out.append({"kind": label, "who": r["display_name"], "n": r["n"], "total": total})
    short = []
    for d in db.execute("SELECT day, lines FROM day_closes WHERE clinic_id = ? AND status = 'closed' AND day >= ? ORDER BY day", (cid, since)):
        try:
            lines = json.loads(d["lines"])
        except ValueError:
            continue
        diff = sum(int(v.get("difference", 0) or 0) for v in lines.values() if isinstance(v, dict))
        if diff < 0:
            short.append((d["day"], diff))
    if len(short) >= 3:
        out.append({"kind": "cash_short", "who": None, "n": len(short), "total": sum(x[1] for x in short)})
    return out


PATTERN_WORDS = {"voids": "%(who)s made %(n)s of the %(total)s payment voids in the last 30 days.",
                 "discounts": "%(who)s gave %(n)s of the %(total)s discounts and write-offs in the last 30 days.",
                 "cash_short": "The cash-up came up short on %(n)s days in the last 30 days."}


def summary(db, clinic, user, today=None):
    """The manager's morning summary as plain sentences, most important first. Built from the huddle's own figures."""
    from . import huddle
    h = huddle.huddle(db, clinic, user, today)
    lines = []
    prod = next((r for r in h["progress"]["clinic"] if r["field"] == "production_p"), None)
    if prod and prod["goal"] is not None and prod["pace"]:
        gap = prod["actual"] - prod["pace"]
        lines.append(("ahead" if gap >= 0 else "behind", abs(gap)))
    risky = risky_visits(db, clinic, h["today"])
    if risky:
        lines.append(("risky", len(risky)))
    if h["unconfirmed_tomorrow"]:
        lines.append(("unconfirmed", h["unconfirmed_tomorrow"]))
    if h["planned_today"]:
        lines.append(("planned", h["planned_today"]))
    if h["owed_today_p"]:
        lines.append(("owed", h["owed_today_p"]))
    if h["unsigned"]["old"]:
        lines.append(("unsigned", h["unsigned"]["old"]))
    return {"lines": lines, "patterns": money_patterns(db, clinic, today), "risky": risky}


SUMMARY_WORDS = {"ahead": "Production is %(amount)s ahead of pace for the month.", "behind": "Production is %(amount)s behind pace for the month.",
                 "risky": "%(n)s of today's patients may not come: call them first.", "unconfirmed": "%(n)s visits tomorrow are not confirmed yet.",
                 "planned": "%(n)s of today's patients have treatment planned but not booked: book the next visit before they leave.",
                 "owed": "Today's patients owe %(amount)s between them.", "unsigned": "%(n)s notes have waited over two days for a signature."}


# ---------------------------------------------------------------------------------------------------------- support at HQ
_STOP = set("a an and are as at be but by can do for from has have how i if in is it its me my no not of on or our so that the "
            "their them then there this to was we what when where which who why will with you your".split())


def _words(text):
    return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if w not in _STOP and len(w) > 2}


def match_articles(db, subject, body, limit=3):
    """Published help articles that share the most meaningful words with a ticket, best first."""
    want = _words(f"{subject} {subject} {body}")
    if not want:
        return []
    scored = []
    for a in db.execute("SELECT * FROM hq_articles WHERE published = 1"):
        have = _words(f"{a['title']} {a['title']} {a['body']}")
        common = want & have
        if len(common) >= 2:
            scored.append((len(common) / (len(want) ** 0.5 * len(have) ** 0.5), a))
    return [a for _s, a in sorted(scored, key=lambda x: -x[0])[:limit]]


def support_draft(article, contact=None):
    first = (article["body"] or "").strip().split("\n\n")[0][:600]
    hello = f"Hello{(' ' + contact.split(',')[0].strip()) if contact else ''},"
    return f"{hello}\n\n{first}\n\nThe full steps are in the app under Help: {article['title']}."
