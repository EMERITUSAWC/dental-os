"""Message wording. Each clinic can edit the text of each kind of message. Only the fields listed here can be put in
a message, and never treatment details or money: a WhatsApp message can be read by anyone holding the phone."""
import re

FIELDS = {
    "first_name": "The patient's first name",
    "clinic": "The clinic's name",
    "dentist": "The dentist",
    "day": "The day of the visit, for example Tuesday 3 November",
    "time": "The time of the visit",
    "review_link": "The clinic's review page",
    "link": "The patient's private link",
}

# kind: (label, starter wording, fields that must be in it)
KINDS = {
    "confirmation": ("Booking confirmation",
                     "Hello {first_name}, this is {clinic}. We have booked you with {dentist} on {day} at {time}. "
                     "Please reply YES to confirm, or tell us if you need a different time.", ()),
    "reminder": ("Visit reminder",
                 "Hello {first_name}, a reminder from {clinic}: your appointment with {dentist} is on {day} at {time}. "
                 "Please reply YES to confirm.", ()),
    "recall": ("Check-up due",
               "Hello {first_name}, this is {clinic}. It is time for your next check-up. "
               "Reply to this message and we will find a time that suits you.", ()),
    "plan_followup": ("Follow-up on a quote",
                      "Hello {first_name}, this is {clinic}. We are following up on the treatment we talked about. "
                      "If you have any questions, or would like to book, just reply to this message.", ()),
    "review_request": ("Review request",
                       "Hello {first_name}, thank you for visiting {clinic}. If you were happy with your visit, "
                       "would you leave us a short review? {review_link}", ("review_link",)),
    "reactivation": ("Not seen for a while",
                     "Hello {first_name}, this is {clinic}. It has been a while since we last saw you. "
                     "If you would like a check-up, reply to this message and we will find a time that suits you.", ()),
    "birthday": ("Birthday greeting",
                 "Happy birthday {first_name}, from all of us at {clinic}.", ()),
    "portal_link": ("Patient link",
                    "Hello {first_name}, here is your private link to {clinic}: {link} It shows your visits and lets you "
                    "fill in your health form. Please do not share it.", ("link",)),
}
CAMPAIGN_FIELDS = ("first_name", "clinic")


class TemplateError(Exception):
    pass


def names_in(body):
    return re.findall(r"\{(\w*)\}", body)


def validate(body, required=(), allowed=None):
    body = (body or "").strip()
    if not body:
        raise TemplateError("Write the message.")
    if len(body) > 600:
        raise TemplateError("That message is too long. Keep it under 600 characters.")
    allowed = set(allowed if allowed is not None else FIELDS)
    used = names_in(body)
    for name in used:
        if name not in allowed:
            raise TemplateError("{" + name + "} is not a field you can use here. Fields: "
                                + ", ".join("{" + f + "}" for f in sorted(allowed)) + ".")
    if re.sub(r"\{\w*\}", "", body).count("{") or re.sub(r"\{\w*\}", "", body).count("}"):
        raise TemplateError("Check the curly brackets. Each field is written like {first_name}.")
    for name in required:
        if name not in used:
            raise TemplateError("This message needs {" + name + "} in it.")
    return body


def render(body, ctx):
    """Fill the fields. A field with no value is an error, so a half-empty message never goes out."""
    def fill(match):
        name = match.group(1)
        value = (ctx.get(name) or "").strip() if isinstance(ctx.get(name), str) else ctx.get(name)
        if not value:
            raise TemplateError(f"There is no value for {{{name}}}.")
        return str(value)
    return re.sub(r"\{(\w+)\}", fill, body)


def ensure_templates(db, clinic_id):
    for kind, (_label, body, _req) in KINDS.items():
        db.execute("INSERT OR IGNORE INTO message_templates(clinic_id, kind, body) VALUES (?,?,?)", (clinic_id, kind, body))


def body_for(db, clinic_id, kind, lang="en"):
    """The clinic's wording for a kind of message. In another language, a clinic that has not changed the starter wording sends the
    starter in the patient's language (French, Spanish, Portuguese or Arabic).
    A clinic that wrote its own wording sends its own wording to everyone."""
    row = db.execute("SELECT body FROM message_templates WHERE clinic_id = ? AND kind = ?", (clinic_id, kind)).fetchone()
    body = row["body"] if row else KINDS[kind][1]
    if body == KINDS[kind][1]:
        from . import i18n
        translated = i18n.starters(lang)
        if translated and kind in translated:
            return translated[kind]
    return body


def list_for(db, clinic_id):
    ensure_templates(db, clinic_id)
    return [{"kind": k, "label": v[0], "body": body_for(db, clinic_id, k), "starter": v[1], "required": v[2]}
            for k, v in KINDS.items()]


def save(db, clinic_id, user_id, kind, body):
    from . import audit
    if kind not in KINDS:
        raise LookupError("template")
    body = validate(body, required=KINDS[kind][2])
    db.execute("""INSERT INTO message_templates(clinic_id, kind, body) VALUES (?,?,?)
                  ON CONFLICT(clinic_id, kind) DO UPDATE SET body = excluded.body, updated_at = datetime('now')""",
               (clinic_id, kind, body))
    audit.log(db, clinic_id, user_id, "template.save", "template", None, kind)


def reset(db, clinic_id, user_id, kind):
    from . import audit
    if kind not in KINDS:
        raise LookupError("template")
    db.execute("UPDATE message_templates SET body = ?, updated_at = datetime('now') WHERE clinic_id = ? AND kind = ?",
               (KINDS[kind][1], clinic_id, kind))
    audit.log(db, clinic_id, user_id, "template.reset", "template", None, kind)
