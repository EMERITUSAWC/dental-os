"""Campaigns: a message to a group of patients chosen by a rule, with consent and a gap between messages enforced.
A campaign only adds messages to the same send list that reminders use. Nobody is messaged unless a person opens it."""
from datetime import timedelta

from . import audit, msgtemplates as MT, outbox
from .timeutil import now_local

KINDS = {
    "overdue_recall": ("Overdue check-ups", "Patients whose check-up has been due for at least this many days and who have no visit booked.",
                       "Hello {first_name}, this is {clinic}. It has been a while since your last check-up. "
                       "Reply to this message and we will find you a time."),
    "lapsed": ("Patients we have not seen for a while", "Patients whose last finished visit was at least this many days ago and who have no visit booked.",
               "Hello {first_name}, this is {clinic}. We have not seen you for some time and would be glad to see you again. "
               "Reply to this message and we will find a time that suits you."),
    "unaccepted_plans": ("Quotes not accepted", "Patients with a quote given at least this many days ago that has not been accepted or declined.",
                         "Hello {first_name}, this is {clinic}. We wanted to check whether you have any questions about the treatment "
                         "we talked about. Reply to this message and we will be happy to help."),
}
PURPOSES = {"care": "Care message (needs agreement to appointment messages)", "offer": "Offer or promotion (needs agreement to offers)"}


class CampaignError(Exception):
    pass


def get(db, cid, campaign_id):
    return db.execute("SELECT * FROM campaigns WHERE id = ? AND clinic_id = ?", (campaign_id, cid)).fetchone()


def list_for(db, cid):
    return db.execute("SELECT * FROM campaigns WHERE clinic_id = ? ORDER BY id DESC", (cid,)).fetchall()


def create(db, clinic, user_id, name, kind, param_days, purpose, body, min_gap_days):
    name = (name or "").strip()
    if not name or len(name) > 80:
        raise CampaignError("Give the campaign a name.")
    if kind not in KINDS:
        raise CampaignError("Pick who this campaign is for.")
    if purpose not in PURPOSES:
        raise CampaignError("Say whether this is a care message or an offer.")
    if not isinstance(param_days, int) or not 1 <= param_days <= 1500:
        raise CampaignError("Enter a number of days from 1 to 1500.")
    if not isinstance(min_gap_days, int) or not 0 <= min_gap_days <= 365:
        raise CampaignError("The gap between messages must be 0 to 365 days.")
    try:
        body = MT.validate(body, allowed=MT.CAMPAIGN_FIELDS)
    except MT.TemplateError as exc:
        raise CampaignError(str(exc))
    cur = db.execute("""INSERT INTO campaigns(clinic_id, name, kind, param_days, purpose, body, min_gap_days, created_by)
                        VALUES (?,?,?,?,?,?,?,?)""", (clinic["id"], name, kind, param_days, purpose, body, min_gap_days, user_id))
    audit.log(db, clinic["id"], user_id, "campaign.create", "campaign", cur.lastrowid)
    return cur.lastrowid


def _candidates(db, clinic, camp, today):
    cid, iso = clinic["id"], today.isoformat()
    cutoff = (today - timedelta(days=camp["param_days"])).isoformat()
    now = now_local(clinic["timezone"]).strftime("%Y-%m-%d %H:%M")
    no_future = ("NOT EXISTS (SELECT 1 FROM appointments f WHERE f.clinic_id = p.clinic_id AND f.patient_id = p.id "
                 "AND f.status = 'scheduled' AND f.start_at >= ?)")
    if camp["kind"] == "overdue_recall":
        return db.execute(f"""SELECT DISTINCT p.* FROM recalls r JOIN patients p ON p.id = r.patient_id
                              WHERE r.clinic_id = ? AND r.status IN ('due','contacted') AND r.due_on <= ? AND p.status = 'active'
                              AND {no_future} ORDER BY p.last_name""", (cid, cutoff, now)).fetchall()
    if camp["kind"] == "lapsed":
        return db.execute(f"""SELECT p.* FROM patients p WHERE p.clinic_id = ? AND p.status = 'active' AND {no_future}
                              AND (SELECT MAX(date(a.start_at)) FROM appointments a WHERE a.clinic_id = p.clinic_id
                                   AND a.patient_id = p.id AND a.status = 'complete') <= ? ORDER BY p.last_name""", (cid, now, cutoff)).fetchall()
    return db.execute("""SELECT DISTINCT p.* FROM treatment_plans tp JOIN patients p ON p.id = tp.patient_id
                         WHERE tp.clinic_id = ? AND tp.status = 'presented' AND tp.presented_on <= ? AND p.status = 'active'
                         ORDER BY p.last_name""", (cid, cutoff)).fetchall()


def audience(db, clinic, camp, today=None):
    """Who the rule finds, and who is left out and why."""
    cid = clinic["id"]
    today = today or now_local(clinic["timezone"]).date()
    gap_from = (today - timedelta(days=camp["min_gap_days"])).isoformat()
    flag = "reminders_ok" if camp["purpose"] == "care" else "offers_ok"
    out = {"send": [], "no_consent": 0, "too_soon": 0, "already": 0}
    for p in _candidates(db, clinic, camp, today):
        if db.execute("SELECT 1 FROM outbox WHERE clinic_id = ? AND dedupe_key = ?", (cid, f"camp:{camp['id']}:{p['id']}")).fetchone():
            out["already"] += 1
        elif not p[flag]:
            out["no_consent"] += 1
        elif db.execute("""SELECT 1 FROM messages WHERE clinic_id = ? AND patient_id = ? AND direction = 'out'
                           AND date(created_at) >= ?""", (cid, p["id"], gap_from)).fetchone():
            out["too_soon"] += 1
        else:
            out["send"].append(p)
    return out


def build(db, clinic, user_id, campaign_id, today=None):
    camp = get(db, clinic["id"], campaign_id)
    if not camp:
        raise LookupError("campaign")
    if camp["status"] == "closed":
        raise CampaignError("This campaign is closed.")
    today = today or now_local(clinic["timezone"]).date()
    aud = audience(db, clinic, camp, today)
    for p in aud["send"]:
        outbox._enqueue(db, clinic["id"], p["id"], "campaign", camp["purpose"], today.isoformat(), f"camp:{campaign_id}:{p['id']}",
                        campaign_id=campaign_id)
    db.execute("UPDATE campaigns SET status = 'active', built_at = datetime('now') WHERE id = ?", (campaign_id,))
    audit.log(db, clinic["id"], user_id, "campaign.build", "campaign", campaign_id, f"{len(aud['send'])} queued")
    return len(aud["send"])


def close(db, clinic, user_id, campaign_id):
    camp = get(db, clinic["id"], campaign_id)
    if not camp:
        raise LookupError("campaign")
    if camp["status"] == "closed":
        raise CampaignError("This campaign is already closed.")
    db.execute("UPDATE campaigns SET status = 'closed' WHERE id = ?", (campaign_id,))
    db.execute("UPDATE outbox SET status = 'skipped', skip_reason = 'The campaign was closed', done_at = datetime('now') "
               "WHERE campaign_id = ? AND status = 'queued'", (campaign_id,))
    audit.log(db, clinic["id"], user_id, "campaign.close", "campaign", campaign_id)


def results(db, cid, campaign_id):
    """What happened. 'Replied' and 'booked' look 30 days after the message was opened."""
    r = {"queued": 0, "sent": 0, "skipped": 0}
    for row in db.execute("SELECT status, COUNT(*) n FROM outbox WHERE clinic_id = ? AND campaign_id = ? GROUP BY status", (cid, campaign_id)):
        r[row["status"]] = row["n"]
    r["replied"] = db.execute(
        """SELECT COUNT(*) FROM outbox o JOIN messages m ON m.id = o.message_id WHERE o.clinic_id = ? AND o.campaign_id = ? AND EXISTS (
             SELECT 1 FROM messages i WHERE i.clinic_id = o.clinic_id AND i.patient_id = o.patient_id AND i.direction = 'in'
             AND i.created_at >= m.created_at AND i.created_at <= datetime(m.created_at, '+30 days'))""", (cid, campaign_id)).fetchone()[0]
    r["booked"] = db.execute(
        """SELECT COUNT(*) FROM outbox o JOIN messages m ON m.id = o.message_id WHERE o.clinic_id = ? AND o.campaign_id = ? AND EXISTS (
             SELECT 1 FROM appointments a WHERE a.clinic_id = o.clinic_id AND a.patient_id = o.patient_id AND a.status IN ('scheduled','complete')
             AND a.created_at >= m.created_at AND a.created_at <= datetime(m.created_at, '+30 days'))""", (cid, campaign_id)).fetchone()[0]
    return r
