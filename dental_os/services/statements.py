"""Statement runs and late fees (version 0.27).

A statement run picks every patient who owes at least a set amount, optionally only where some of it is older than a set number of
days, and leaves out anyone sent a statement recently. For each one the clinic can print the statement (all of them at once, one
per page) or send it by WhatsApp. The WhatsApp message never says the amount: it carries the patient's private link, where the
balance and the Pay button are (a message can be read by anyone holding the phone).

Late fees are off until a manager switches them on, and the rule is the clinic's to set and check against local law and its own
terms: a fixed amount or a percentage of what is overdue, only on balances overdue by more than a set number of days, at most once in
a set number of days, never on the part insurers are expected to pay, and never for a patient on an instalment plan. A fee is one
line on the account, so it shows on statements and reports and can be voided like any other line. No web framework here."""
import datetime

from . import audit, ageing, i18n, links, msgtemplates as MT, treatment as TR
from .messaging import wa_link
from .timeutil import now_local

LATE_CODE = ("LATE-FEE", "Late payment fee")
STATEMENT_TEXT = {"en": "Hello {first_name}, this is {clinic}. Your latest statement is ready. You can see it, and pay if you wish, "
                        "at your private link: {link} Please do not share it.",
                  "fr": "Bonjour {first_name}, ici {clinic}. Votre dernier relevé est prêt. Vous pouvez le consulter, et payer si vous le "
                        "souhaitez, avec votre lien privé : {link} Merci de ne pas le partager."}


class StatementError(Exception):
    pass


def _today(clinic):
    return now_local(clinic["timezone"]).date()


def _int(raw, message, lo, hi):
    try:
        v = int(str(raw).strip())
    except (TypeError, ValueError):
        raise StatementError(message)
    if not lo <= v <= hi:
        raise StatementError(message)
    return v


def _money(raw, message):
    try:
        v = round(float(str(raw).replace(",", "").strip()) * 100)
    except (TypeError, ValueError):
        raise StatementError(message)
    if v < 0 or v > 10**9:
        raise StatementError(message)
    return v


# ------------------------------------------------------------------------------------------------------------ settings
DEFAULTS = {"late_on": 0, "late_kind": "fixed", "late_value": 0, "late_after_days": 60, "late_min_p": 0, "late_gap_days": 30, "late_cap_p": None}


def settings(db, cid):
    row = db.execute("SELECT * FROM statement_settings WHERE clinic_id = ?", (cid,)).fetchone()
    return dict(row) if row else dict(DEFAULTS, clinic_id=cid)


def save_settings(db, clinic, user_id, form):
    on = form.get("late_on") == "1"
    kind = form.get("late_kind") or "fixed"
    if kind not in ("fixed", "percent"):
        raise StatementError("Pick a fixed amount or a percentage.")
    if kind == "fixed":
        value = _money(form.get("late_value") or "0", "Write the late fee as an amount, for example 20.")
    else:
        try:
            value = round(float(str(form.get("late_value") or "0").strip()) * 100)
        except ValueError:
            raise StatementError("Write the percentage as a number, for example 2 or 1.5.")
        if not 0 <= value <= 2500:
            raise StatementError("The percentage must be from 0 to 25.")
    if on and value <= 0:
        raise StatementError("Set the late fee before switching it on.")
    after = _int(form.get("late_after_days") or 60, "Write after how many days a balance is late, from 1 to 365.", 1, 365)
    gap = _int(form.get("late_gap_days") or 30, "Write how often a fee can be added, from 7 to 365 days.", 7, 365)
    min_p = _money(form.get("late_min") or "0", "Write the smallest overdue amount as a number.")
    cap = (form.get("late_cap") or "").strip()
    cap_p = _money(cap, "Write the largest fee as a number, or leave it empty.") if cap else None
    db.execute("""INSERT INTO statement_settings(clinic_id, late_on, late_kind, late_value, late_after_days, late_min_p, late_gap_days, late_cap_p)
                  VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(clinic_id) DO UPDATE SET late_on = excluded.late_on, late_kind = excluded.late_kind,
                  late_value = excluded.late_value, late_after_days = excluded.late_after_days, late_min_p = excluded.late_min_p,
                  late_gap_days = excluded.late_gap_days, late_cap_p = excluded.late_cap_p""",
               (clinic["id"], 1 if on else 0, kind, value, after, min_p, gap, cap_p))
    audit.log(db, clinic["id"], user_id, "statements.settings", None, None, "late fees on" if on else "late fees off")


# ------------------------------------------------------------------------------------------------------- who is owed
def overdue_p(db, cid, pid, today, days):
    """What is still unpaid from charges older than `days`, oldest charges paid off first (as the ageing report does)."""
    charges = [(r["done_on"], r["fee_p"]) for r in db.execute(
        "SELECT done_on, fee_p FROM procedures WHERE clinic_id = ? AND patient_id = ? AND status = 'done' AND fee_p > 0", (cid, pid))]
    credit = 0
    for r in db.execute("SELECT adjusted_on, amount_p FROM adjustments WHERE clinic_id = ? AND patient_id = ?", (cid, pid)):
        if r["amount_p"] > 0:
            charges.append((r["adjusted_on"], r["amount_p"]))
        else:
            credit += -r["amount_p"]
    credit += db.execute("SELECT COALESCE(SUM(amount_p),0) FROM payments WHERE clinic_id = ? AND patient_id = ? AND voided = 0 "
                         "AND purpose = 'treatment'", (cid, pid)).fetchone()[0]
    from . import refunds
    credit -= refunds.total(db, cid, pid)                # money given back is credit the patient no longer holds
    out = 0
    for day, amount in sorted(charges):
        used = min(credit, amount)
        credit -= used
        if amount - used and (today - datetime.date.fromisoformat(day)).days > days:
            out += amount - used
    return out


def _last_statement(db, cid, pid):
    return db.execute("SELECT MAX(r.made_on) FROM statement_items i JOIN statement_runs r ON r.id = i.run_id "
                      "WHERE i.clinic_id = ? AND i.patient_id = ?", (cid, pid)).fetchone()[0]


def _last_fee(db, cid, pid):
    return db.execute("""SELECT MAX(pr.done_on) FROM procedures pr JOIN procedure_codes pc ON pc.id = pr.code_id
                         WHERE pr.clinic_id = ? AND pr.patient_id = ? AND pc.code = ? AND pr.status = 'done'""", (cid, pid, LATE_CODE[0])).fetchone()[0]


def late_fee_for(db, clinic, pid, today=None):
    """(fee, why not) for one patient under the clinic's rule. A fee of 0 comes with the reason."""
    cid = clinic["id"]
    s = settings(db, cid)
    today = today or _today(clinic)
    if not s["late_on"]:
        return 0, "Late fees are off."
    if db.execute("SELECT 1 FROM payment_plans WHERE clinic_id = ? AND patient_id = ? AND status = 'active'", (cid, pid)).fetchone():
        return 0, "On an instalment plan."
    last = _last_fee(db, cid, pid)
    if last and (today - datetime.date.fromisoformat(last)).days < s["late_gap_days"]:
        return 0, "A late fee was added recently."
    over = overdue_p(db, cid, pid, today, s["late_after_days"]) - ageing.insurers_owe(db, cid, pid)
    if over <= 0 or over < s["late_min_p"]:
        return 0, "Nothing overdue enough."
    fee = s["late_value"] if s["late_kind"] == "fixed" else round(over * s["late_value"] / 10000)
    if s["late_cap_p"] is not None:
        fee = min(fee, s["late_cap_p"])
    return max(fee, 0), None


def candidates(db, clinic, min_balance_p=100, older_than_days=0, gap_days=25, today=None):
    cid = clinic["id"]
    today = today or _today(clinic)
    out = []
    from .billing import all_balances
    owing = {pid for pid, (_b, unpaid) in all_balances(db, cid).items() if unpaid >= max(min_balance_p, 1)}   # 0.57
    for p in db.execute("SELECT * FROM patients WHERE clinic_id = ? AND status = 'active' ORDER BY last_name, first_name", (cid,)).fetchall():
        if p["id"] not in owing:
            continue
        b = ageing.patient_ageing(db, cid, p["id"], today)
        total = sum(b.values())
        if total < max(min_balance_p, 1):
            continue
        if older_than_days and not overdue_p(db, cid, p["id"], today, older_than_days):
            continue
        last = _last_statement(db, cid, p["id"])
        if last and gap_days and (today - datetime.date.fromisoformat(last)).days < gap_days:
            continue
        fee, why = late_fee_for(db, clinic, p["id"], today)
        out.append({"patient": p, "balance_p": total, "buckets": b, "insurers_p": min(ageing.insurers_owe(db, cid, p["id"]), total),
                    "last": last, "fee_p": fee, "why_no_fee": why})
    return out


# -------------------------------------------------------------------------------------------------------------- a run
def _fee_code(db, cid):
    code, name = LATE_CODE
    row = db.execute("SELECT id FROM procedure_codes WHERE clinic_id = ? AND code = ?", (cid, code)).fetchone()
    if row:
        return row["id"]
    return db.execute("INSERT INTO procedure_codes(clinic_id, code, name, category, fee_p) VALUES (?,?,?, 'Account', 0)", (cid, code, name)).lastrowid


def make_run(db, clinic, user_id, min_balance, older_than_days, gap_days, add_fees, patient_ids=None):
    cid = clinic["id"]
    min_p = _money(min_balance or "1", "Write the smallest balance as a number.")
    older = _int(older_than_days or 0, "Write the age of the balance in days, from 0 to 365.", 0, 365)
    gap = _int(gap_days if gap_days not in (None, "") else 25, "Write the gap between statements in days, from 0 to 365.", 0, 365)
    rows = candidates(db, clinic, min_p, older, gap)
    if patient_ids is not None:
        wanted = {int(x) for x in patient_ids}
        rows = [r for r in rows if r["patient"]["id"] in wanted]
    if not rows:
        raise StatementError("Nobody matches. Lower the smallest balance or the age, or check who had a statement recently.")
    if add_fees and not settings(db, cid)["late_on"]:
        raise StatementError("Late fees are off. A manager sets the rule under Statements, Late fees.")
    today = _today(clinic)
    rid = db.execute("INSERT INTO statement_runs(clinic_id, made_on, min_balance_p, older_than_days, fees_added, made_by) VALUES (?,?,?,?,?,?)",
                     (cid, today.isoformat(), min_p, older, 1 if add_fees else 0, user_id)).lastrowid
    for r in rows:
        pid = r["patient"]["id"]
        fee_proc = None
        if add_fees and r["fee_p"]:
            fee_proc = TR.add_planned(db, clinic, user_id, pid, _fee_code(db, cid), fee_p=r["fee_p"], notes="Late payment fee, statement run")
            TR.mark_done(db, clinic, user_id, fee_proc)
        balance = sum(ageing.patient_ageing(db, cid, pid, today).values())
        db.execute("INSERT INTO statement_items(clinic_id, run_id, patient_id, balance_p, fee_p, fee_proc_id) VALUES (?,?,?,?,?,?)",
                   (cid, rid, pid, balance, r["fee_p"] if fee_proc else 0, fee_proc))
    audit.log(db, cid, user_id, "statements.run", None, None, f"{len(rows)} patients")
    return rid


def run(db, cid, rid):
    r = db.execute("SELECT r.*, u.display_name AS by_name FROM statement_runs r LEFT JOIN users u ON u.id = r.made_by WHERE r.id = ? AND r.clinic_id = ?",
                   (rid, cid)).fetchone()
    if not r:
        return None
    items = db.execute("""SELECT i.*, p.first_name, p.last_name, p.phone, p.email, p.reminders_ok FROM statement_items i JOIN patients p ON p.id = i.patient_id
                          WHERE i.run_id = ? ORDER BY p.last_name, p.first_name""", (rid,)).fetchall()
    return {"run": r, "lines": items, "total_p": sum(i["balance_p"] for i in items), "fees_p": sum(i["fee_p"] for i in items)}


def runs(db, cid, limit=30):
    return db.execute("""SELECT r.*, (SELECT COUNT(*) FROM statement_items i WHERE i.run_id = r.id) AS n,
                         (SELECT COUNT(*) FROM statement_items i WHERE i.run_id = r.id AND i.sent_at IS NOT NULL) AS sent
                         FROM statement_runs r WHERE r.clinic_id = ? ORDER BY r.id DESC LIMIT ?""", (cid, limit)).fetchall()


def send_whatsapp(db, clinic, user_id, item_id, portal_url_for):
    """Make a fresh private link, log the message without the link, and return the WhatsApp address to open."""
    cid = clinic["id"]
    it = db.execute("SELECT * FROM statement_items WHERE id = ? AND clinic_id = ?", (item_id, cid)).fetchone()
    if not it:
        raise StatementError("That statement was not found.")
    p = db.execute("SELECT * FROM patients WHERE id = ?", (it["patient_id"],)).fetchone()
    if not p["reminders_ok"]:
        raise StatementError("This patient has not agreed to messages. Print the statement or give it in person.")
    if not p["phone"]:
        raise StatementError("This patient has no phone number.")
    try:
        token = links.create(db, clinic, user_id, p["id"])
    except links.LinkError as exc:
        raise StatementError(str(exc))
    url = portal_url_for(token)
    lang = i18n.patient_lang(p, clinic)
    body = MT.render(STATEMENT_TEXT.get(lang, STATEMENT_TEXT["en"]), {"first_name": p["first_name"], "clinic": clinic["name"], "link": url})
    db.execute("INSERT INTO messages(clinic_id, patient_id, kind, body, status, created_by) VALUES (?,?, 'statement', ?, 'opened', ?)",
               (cid, p["id"], body.replace(url, "[private link]"), user_id))
    db.execute("UPDATE statement_items SET sent_at = datetime('now'), sent_by = ?, sent_how = 'whatsapp' WHERE id = ?", (user_id, item_id))
    audit.log(db, cid, user_id, "statements.send", "patient", p["id"])
    return wa_link(p["phone"], body)


def mark_printed(db, clinic, user_id, rid):
    db.execute("UPDATE statement_items SET sent_at = datetime('now'), sent_by = ?, sent_how = 'print' WHERE run_id = ? AND clinic_id = ? AND sent_at IS NULL",
               (user_id, rid, clinic["id"]))
    audit.log(db, clinic["id"], user_id, "statements.print", None, None, str(rid))
