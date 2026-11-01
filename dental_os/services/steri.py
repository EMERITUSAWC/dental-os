"""Sterilisation and instrument tracking (version 0.22).

What it keeps: each steriliser, the tests run on it (daily, weekly, yearly), every cycle with what the machine showed, the instrument
packs that came out of each cycle with a label code and a use-by date, and which pack was used on which patient.

The rules follow the published guidance it was built from (England's HTM 01-05 for dental practices, and the US CDC's advice on
sterilisation monitoring), but each clinic must check what its own regulator asks:
- A pack can be used only if its cycle passed (the machine reached its numbers and the indicator changed), it is in date, and it has not
  been recalled.
- A load with implants is held until the spore test run with it passes.
- A failed test takes the steriliser out of service. A failed spore test also opens a recall: every pack made on that steriliser since
  its last passing spore test is pulled from the shelf, and the patients those packs were already used on are listed for review.
- Back in service after a failed spore test needs one passing spore test; after two or more failed in a row it needs three passes in a
  row (the CDC's advice). After any other failed test, a pass of the same test.

Nothing is deleted. Every change is in the activity log. No web framework here."""
import datetime
import re

from . import audit
from .timeutil import now_local, parse_date

KINDS = {"vacuum": "Vacuum (B type)", "non_vacuum": "Non-vacuum (N type)", "s_type": "S type", "chemical": "Chemical vapour",
         "dry_heat": "Dry heat"}
TESTS = {"act": "Automatic control test", "steam_penetration": "Steam penetration test (Helix or Bowie-Dick)",
         "air_leakage": "Air leakage test", "residual_air": "Residual air test", "spore": "Spore test (biological indicator)",
         "thermometric": "Yearly validation (thermometric test)", "other": "Other test"}
RESULTS = {"pass": "Pass", "fail": "Fail", "pending": "Waiting for the result"}
CYCLE_RESULTS = {"pass": "Passed", "fail": "Failed", "held": "Held for the spore test"}
PACK_STATUS = {"stored": "On the shelf", "held": "Held", "used": "Used", "recalled": "Recalled", "failed": "Failed cycle",
               "reprocessed": "Sent back for cleaning"}
STEAM = ("vacuum", "non_vacuum", "s_type")
DAILY = {"act": STEAM, "steam_penetration": ("vacuum",)}
WEEKLY = {"spore": tuple(KINDS), "air_leakage": ("vacuum",)}
CODE = re.compile(r"^[A-Z0-9-]{3,40}$")


class SteriError(Exception):
    pass


def _now(clinic):
    return now_local(clinic["timezone"])


def _stamp(clinic):
    return _now(clinic).strftime("%Y-%m-%d %H:%M")


def _today(clinic):
    return _now(clinic).date()


# ------------------------------------------------------------------------------------------------------------ settings
def settings(db, cid):
    row = db.execute("SELECT * FROM steri_settings WHERE clinic_id = ?", (cid,)).fetchone()
    return {"require_daily": bool(row["require_daily"]) if row else True, "shelf_days": row["shelf_days"] if row else 90}


def save_settings(db, clinic, user_id, require_daily, shelf_days):
    try:
        days = int(str(shelf_days).strip())
    except ValueError:
        raise SteriError("Enter the shelf life as a number of days.")
    if not 1 <= days <= 730:
        raise SteriError("The shelf life must be from 1 to 730 days.")
    db.execute("""INSERT INTO steri_settings(clinic_id, require_daily, shelf_days) VALUES (?,?,?)
                  ON CONFLICT(clinic_id) DO UPDATE SET require_daily = excluded.require_daily, shelf_days = excluded.shelf_days""",
               (clinic["id"], 1 if require_daily else 0, days))
    audit.log(db, clinic["id"], user_id, "steri.settings")


# ---------------------------------------------------------------------------------------------------------- sterilisers
def sterilisers(db, cid, all_=False):
    q = "SELECT * FROM sterilisers WHERE clinic_id = ?" + ("" if all_ else " AND retired = 0") + " ORDER BY name"
    return db.execute(q, (cid,)).fetchall()


def get_steriliser(db, cid, sid):
    return db.execute("SELECT * FROM sterilisers WHERE id = ? AND clinic_id = ?", (sid, cid)).fetchone()


def save_steriliser(db, clinic, user_id, name, kind, serial=None, validated_on=None, sid=None, retired=False):
    name = (name or "").strip()[:80]
    if not name:
        raise SteriError("Give the steriliser a name, for example Autoclave 1.")
    if kind not in KINDS:
        raise SteriError("Pick what kind of steriliser it is.")
    v = None
    if (validated_on or "").strip():
        try:
            v = parse_date(validated_on.strip())
        except ValueError:
            raise SteriError("Use a date like 2026-11-30.")
        if v > _today(clinic):
            raise SteriError("The validation date cannot be in the future.")
    serial = (serial or "").strip()[:60] or None
    if sid:
        if not get_steriliser(db, clinic["id"], sid):
            raise SteriError("That steriliser was not found.")
        db.execute("UPDATE sterilisers SET name = ?, kind = ?, serial = ?, validated_on = COALESCE(?, validated_on), retired = ? "
                   "WHERE id = ? AND clinic_id = ?", (name, kind, serial, v.isoformat() if v else None, 1 if retired else 0, sid, clinic["id"]))
    else:
        sid = db.execute("INSERT INTO sterilisers(clinic_id, name, kind, serial, validated_on) VALUES (?,?,?,?,?)",
                         (clinic["id"], name, kind, serial, v.isoformat() if v else None)).lastrowid
    audit.log(db, clinic["id"], user_id, "steri.steriliser", "steriliser", sid)
    return sid


def checks_due(db, clinic, s):
    """What is missing or late for one steriliser: today's tests, this week's tests, the yearly validation."""
    if s["retired"]:
        return []
    today = _today(clinic)
    out = []
    for kind, applies in DAILY.items():
        if s["kind"] in applies and not _passed_since(db, s, kind, today.isoformat()):
            out.append({"kind": kind, "when": "today"})
    week = (today - datetime.timedelta(days=6)).isoformat()
    for kind, applies in WEEKLY.items():
        if s["kind"] in applies and not _passed_since(db, s, kind, week):
            out.append({"kind": kind, "when": "week"})
    if not s["validated_on"] or s["validated_on"] < (today - datetime.timedelta(days=365)).isoformat():
        out.append({"kind": "thermometric", "when": "year"})
    return out


def _passed_since(db, s, kind, since_day):
    return db.execute("SELECT 1 FROM steri_tests WHERE steriliser_id = ? AND kind = ? AND result = 'pass' AND done_at >= ? LIMIT 1",
                      (s["id"], kind, since_day)).fetchone() is not None


def overview(db, clinic):
    """For the sterilisation home page and the Today screen."""
    cid = clinic["id"]
    rows = []
    for s in sterilisers(db, cid):
        rows.append({"s": s, "due": checks_due(db, clinic, s)})
    today = _today(clinic).isoformat()
    soon = (_today(clinic) + datetime.timedelta(days=7)).isoformat()
    expiring = db.execute("SELECT COUNT(*) FROM steri_packs WHERE clinic_id = ? AND status = 'stored' AND expires_on >= ? AND expires_on <= ?",
                          (cid, today, soon)).fetchone()[0]
    expired = db.execute("SELECT COUNT(*) FROM steri_packs WHERE clinic_id = ? AND status = 'stored' AND expires_on < ?",
                         (cid, today)).fetchone()[0]
    recalls = db.execute("SELECT r.*, s.name AS steriliser FROM steri_recalls r JOIN sterilisers s ON s.id = r.steriliser_id "
                         "WHERE r.clinic_id = ? AND r.closed_at IS NULL ORDER BY r.id DESC", (cid,)).fetchall()
    pending = db.execute("""SELECT t.*, s.name AS steriliser FROM steri_tests t JOIN sterilisers s ON s.id = t.steriliser_id
                            WHERE t.clinic_id = ? AND t.result = 'pending' ORDER BY t.id""", (cid,)).fetchall()
    warnings = sum(1 for r in rows if r["due"] or r["s"]["status"] == "out") + len(recalls) + (1 if expired else 0)
    return {"rows": rows, "expiring": expiring, "expired": expired, "recalls": recalls, "pending": pending, "warnings": warnings}


# ---------------------------------------------------------------------------------------------------------------- tests
def record_test(db, clinic, user_id, sid, kind, result, lot=None, notes=None, cycle_id=None):
    cid = clinic["id"]
    s = get_steriliser(db, cid, sid)
    if not s or s["retired"]:
        raise SteriError("Pick one of your sterilisers.")
    if kind not in TESTS:
        raise SteriError("Pick which test it was.")
    if result not in RESULTS or (result == "pending" and kind != "spore"):
        raise SteriError("Pick the result. Only a spore test can wait for its result.")
    if kind == "spore" and not (lot or "").strip():
        raise SteriError("Write the lot number of the spore test.")
    if cycle_id:
        c = db.execute("SELECT * FROM steri_cycles WHERE id = ? AND clinic_id = ? AND steriliser_id = ?", (cycle_id, cid, sid)).fetchone()
        if not c:
            raise SteriError("That cycle is not on this steriliser.")
    tid = db.execute("""INSERT INTO steri_tests(clinic_id, steriliser_id, kind, result, lot, notes, cycle_id, done_at, done_by)
                        VALUES (?,?,?,?,?,?,?,?,?)""",
                     (cid, sid, kind, result, (lot or "").strip()[:40] or None, (notes or "").strip()[:500] or None, cycle_id or None,
                      _stamp(clinic), user_id)).lastrowid
    if kind == "thermometric" and result == "pass":
        db.execute("UPDATE sterilisers SET validated_on = ? WHERE id = ?", (_today(clinic).isoformat(), sid))
    audit.log(db, cid, user_id, "steri.test", "steriliser", sid, f"{kind}:{result}")
    if result != "pending":
        _after_result(db, clinic, user_id, tid)
    return tid


def read_result(db, clinic, user_id, test_id, result):
    """A spore test comes back from the incubator or the postal lab."""
    t = db.execute("SELECT * FROM steri_tests WHERE id = ? AND clinic_id = ?", (test_id, clinic["id"])).fetchone()
    if not t or t["result"] != "pending":
        raise SteriError("That test is not waiting for a result.")
    if result not in ("pass", "fail"):
        raise SteriError("Pick pass or fail.")
    db.execute("UPDATE steri_tests SET result = ?, read_at = ?, read_by = ? WHERE id = ?", (result, _stamp(clinic), user_id, test_id))
    audit.log(db, clinic["id"], user_id, "steri.test_result", "steriliser", t["steriliser_id"], f"{t['kind']}:{result}")
    _after_result(db, clinic, user_id, test_id)


def _after_result(db, clinic, user_id, test_id):
    t = db.execute("SELECT * FROM steri_tests WHERE id = ?", (test_id,)).fetchone()
    s = db.execute("SELECT * FROM sterilisers WHERE id = ?", (t["steriliser_id"],)).fetchone()
    if t["result"] == "fail":
        fails = (s["spore_fails"] or 0) + 1 if t["kind"] == "spore" else s["spore_fails"] or 0
        db.execute("UPDATE sterilisers SET status = 'out', out_since = ?, out_test = ?, out_reason = ?, spore_fails = ?, passes_since = 0 "
                   "WHERE id = ?", (t["done_at"], t["kind"], TESTS[t["kind"]], fails, s["id"]))
        if t["cycle_id"]:
            _fail_cycle(db, t["cycle_id"])
        if t["kind"] == "spore":
            _open_recall(db, clinic, user_id, s, t)
        return
    # a pass
    if t["cycle_id"]:
        c = db.execute("SELECT * FROM steri_cycles WHERE id = ?", (t["cycle_id"],)).fetchone()
        if c["result"] == "held" and t["kind"] == "spore":
            db.execute("UPDATE steri_cycles SET result = 'pass' WHERE id = ?", (c["id"],))
            db.execute("UPDATE steri_packs SET status = 'stored' WHERE cycle_id = ? AND status = 'held'", (c["id"],))
    if t["kind"] == "spore":
        if s["status"] == "out" and s["out_test"] == "spore":
            db.execute("UPDATE sterilisers SET passes_since = passes_since + 1 WHERE id = ?", (s["id"],))
        elif s["status"] != "out":
            db.execute("UPDATE sterilisers SET spore_fails = 0 WHERE id = ?", (s["id"],))
    elif s["status"] == "out" and s["out_test"] == t["kind"]:
        db.execute("UPDATE sterilisers SET passes_since = passes_since + 1 WHERE id = ?", (s["id"],))


def passes_needed(s):
    if s["status"] != "out":
        return 0
    if s["out_test"] == "spore":
        return 3 if (s["spore_fails"] or 0) >= 2 else 1
    return 1


def can_return(s):
    return s["status"] == "out" and (s["passes_since"] or 0) >= passes_needed(s)


def return_to_service(db, clinic, user_id, sid, note):
    s = get_steriliser(db, clinic["id"], sid)
    if not s or s["status"] != "out":
        raise SteriError("That steriliser is already in service.")
    note = (note or "").strip()[:500]
    if not note:
        raise SteriError("Write what was found and done, for example: door seal replaced by the engineer.")
    if not can_return(s):
        if passes_needed(s) == 3:
            raise SteriError("After two failed spore tests in a row it needs three passing spore tests in a row first.")
        if s["out_test"] == "spore":
            raise SteriError("Record a passing spore test first.")
        raise SteriError("Record a pass of the test that failed first.")
    db.execute("UPDATE sterilisers SET status = 'in', out_since = NULL, out_test = NULL, out_reason = NULL, passes_since = 0, "
               "spore_fails = CASE WHEN out_test = 'spore' THEN 0 ELSE spore_fails END WHERE id = ?", (sid,))
    db.execute("INSERT INTO steri_service_log(clinic_id, steriliser_id, note, by_user, at) VALUES (?,?,?,?,?)",
               (clinic["id"], sid, note, user_id, _stamp(clinic)))
    audit.log(db, clinic["id"], user_id, "steri.return", "steriliser", sid)


def tests_for(db, cid, sid, limit=100):
    return db.execute("""SELECT t.*, u.display_name AS by_name FROM steri_tests t LEFT JOIN users u ON u.id = t.done_by
                         WHERE t.clinic_id = ? AND t.steriliser_id = ? ORDER BY t.id DESC LIMIT ?""", (cid, sid, limit)).fetchall()


# --------------------------------------------------------------------------------------------------------------- cycles
def _number(raw, message, lo, hi, dec=False):
    raw = (raw or "").strip().replace(",", ".")
    if not raw:
        return None
    try:
        v = float(raw) if dec else int(raw)
    except ValueError:
        raise SteriError(message)
    if not lo <= v <= hi:
        raise SteriError(message)
    return v


def record_cycle(db, clinic, user_id, sid, packs, program=None, temp_c=None, pressure_kpa=None, hold_min=None,
                 mechanical_ok=False, indicator_ok=False, implants=False, notes=None, machine_cycle_no=None):
    """packs: a list of (what is in it, how many). Returns the cycle id."""
    cid = clinic["id"]
    s = get_steriliser(db, cid, sid)
    if not s or s["retired"]:
        raise SteriError("Pick one of your sterilisers.")
    if s["status"] == "out":
        raise SteriError("This steriliser is out of service. Use another one, or put it back in service first.")
    if settings(db, cid)["require_daily"]:
        if any(d["when"] == "today" for d in checks_due(db, clinic, s)):
            raise SteriError("Do today's tests on this steriliser first. The Sterilisation page shows which are missing.")
    lines = []
    for what, count in packs or []:
        what = (what or "").strip()[:120]
        if not what:
            continue
        try:
            n = int(count or 1)
        except ValueError:
            raise SteriError("The number of packs must be a whole number.")
        if not 1 <= n <= 100:
            raise SteriError("Use from 1 to 100 packs on a line.")
        lines.append((what, n))
    if not lines:
        raise SteriError("Write what went into the load, for example Exam kit, and how many packs.")
    if sum(n for _, n in lines) > 300:
        raise SteriError("That is more packs than one load can hold. Check the numbers.")
    temp = _number(temp_c, "The temperature looks wrong. Write it in °C as the machine shows it, for example 134.", 100, 200, dec=True)
    pres = _number(pressure_kpa, "The pressure looks wrong. Write it in kPa as the machine shows it, for example 210.", 50, 400)
    hold = _number(hold_min, "The holding time looks wrong. Write it in minutes, for example 3.5.", 0.5, 120, dec=True)
    result = "pass" if (mechanical_ok and indicator_ok) else "fail"
    if result == "pass" and implants:
        result = "held"
    no = (db.execute("SELECT COALESCE(MAX(cycle_no), 0) FROM steri_cycles WHERE steriliser_id = ?", (sid,)).fetchone()[0]) + 1
    stamp = _stamp(clinic)
    cyc = db.execute("""INSERT INTO steri_cycles(clinic_id, steriliser_id, cycle_no, machine_cycle_no, program, temp_tenths, pressure_kpa,
                        hold_seconds, mechanical_ok, indicator_ok, implants, result, notes, started_at, done_by)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                     (cid, sid, no, (machine_cycle_no or "").strip()[:30] or None, (program or "").strip()[:60] or None,
                      round(temp * 10) if temp is not None else None, pres, round(hold * 60) if hold is not None else None,
                      1 if mechanical_ok else 0, 1 if indicator_ok else 0, 1 if implants else 0, result,
                      (notes or "").strip()[:500] or None, stamp, user_id)).lastrowid
    expires = (_today(clinic) + datetime.timedelta(days=settings(db, cid)["shelf_days"])).isoformat()
    status = {"pass": "stored", "held": "held", "fail": "failed"}[result]
    k = 0
    for what, n in lines:
        for _ in range(n):
            k += 1
            code = f"S{sid}-{no:05d}-{k:03d}"
            db.execute("""INSERT INTO steri_packs(clinic_id, code, cycle_id, contents, status, packed_by, packed_at, expires_on)
                          VALUES (?,?,?,?,?,?,?,?)""", (cid, code, cyc, what, status, user_id, stamp, expires))
    audit.log(db, cid, user_id, "steri.cycle", "steriliser", sid, result)
    return cyc


def _fail_cycle(db, cycle_id):
    db.execute("UPDATE steri_cycles SET result = 'fail' WHERE id = ?", (cycle_id,))
    db.execute("UPDATE steri_packs SET status = 'failed' WHERE cycle_id = ? AND status IN ('stored','held')", (cycle_id,))


def get_cycle(db, cid, cycle_id):
    return db.execute("""SELECT c.*, s.name AS steriliser, s.kind AS steriliser_kind, u.display_name AS by_name FROM steri_cycles c
                         JOIN sterilisers s ON s.id = c.steriliser_id LEFT JOIN users u ON u.id = c.done_by
                         WHERE c.id = ? AND c.clinic_id = ?""", (cycle_id, cid)).fetchone()


def cycles(db, cid, sid=None, limit=100):
    q = """SELECT c.*, s.name AS steriliser, (SELECT COUNT(*) FROM steri_packs p WHERE p.cycle_id = c.id) AS packs
           FROM steri_cycles c JOIN sterilisers s ON s.id = c.steriliser_id WHERE c.clinic_id = ?"""
    args = [cid]
    if sid:
        q += " AND c.steriliser_id = ?"
        args.append(sid)
    return db.execute(q + " ORDER BY c.id DESC LIMIT ?", args + [limit]).fetchall()


def packs_of(db, cid, cycle_id):
    return db.execute("""SELECT k.*, p.first_name, p.last_name FROM steri_packs k LEFT JOIN patients p ON p.id = k.used_on
                         WHERE k.clinic_id = ? AND k.cycle_id = ? ORDER BY k.id""", (cid, cycle_id)).fetchall()


# ---------------------------------------------------------------------------------------------------------------- packs
def find_pack(db, cid, code):
    code = (code or "").strip().upper()
    return db.execute("""SELECT k.*, c.result AS cycle_result, c.cycle_no, c.steriliser_id, c.implants, s.name AS steriliser,
                         p.first_name, p.last_name FROM steri_packs k JOIN steri_cycles c ON c.id = k.cycle_id
                         JOIN sterilisers s ON s.id = c.steriliser_id LEFT JOIN patients p ON p.id = k.used_on
                         WHERE k.clinic_id = ? AND k.code = ?""", (cid, code)).fetchone()


def use_pack(db, clinic, user_id, code, pid, appointment_id=None):
    """Record that a pack was opened for a patient. Refuses anything that is not safe to use, and says why."""
    cid = clinic["id"]
    if not CODE.match((code or "").strip().upper()):
        raise SteriError("Type or scan the code on the pack label.")
    k = find_pack(db, cid, code)
    if not k:
        raise SteriError("No pack has that code in this clinic.")
    if not db.execute("SELECT 1 FROM patients WHERE id = ? AND clinic_id = ?", (pid, cid)).fetchone():
        raise SteriError("That patient was not found.")
    why = {"used": "That pack was already used. Each pack is used once.",
           "recalled": "Do not use this pack: it is recalled. Send it back for cleaning.",
           "failed": "Do not use this pack: its cycle failed. Send it back for cleaning.",
           "held": "Do not use this pack yet: its load had implants and is held until the spore test passes.",
           "reprocessed": "That pack was sent back for cleaning."}
    if k["status"] in why:
        raise SteriError(why[k["status"]])
    if k["expires_on"] < _today(clinic).isoformat():
        raise SteriError("Do not use this pack: it is past its use-by date. Send it back for cleaning.")
    if appointment_id and not db.execute("SELECT 1 FROM appointments WHERE id = ? AND clinic_id = ? AND patient_id = ?",
                                         (appointment_id, cid, pid)).fetchone():
        raise SteriError("That visit does not belong to this patient.")
    db.execute("UPDATE steri_packs SET status = 'used', used_on = ?, used_appointment = ?, used_at = ?, used_by = ? WHERE id = ?",
               (pid, appointment_id or None, _stamp(clinic), user_id, k["id"]))
    audit.log(db, cid, user_id, "steri.use", "patient", pid, k["code"])
    return k["id"]


def send_back(db, clinic, user_id, code, reason):
    """A pack that cannot be used (recalled, failed, out of date, damaged wrap) goes back to be cleaned and sterilised again."""
    k = find_pack(db, clinic["id"], code)
    if not k:
        raise SteriError("No pack has that code in this clinic.")
    if k["status"] in ("used", "reprocessed"):
        raise SteriError("That pack is not on the shelf.")
    reason = (reason or "").strip()[:200]
    if not reason:
        raise SteriError("Say why, for example: wrap torn, out of date.")
    db.execute("UPDATE steri_packs SET status = 'reprocessed', back_reason = ?, back_at = ? WHERE id = ?", (reason, _stamp(clinic), k["id"]))
    audit.log(db, clinic["id"], user_id, "steri.send_back", "steriliser", k["steriliser_id"], k["code"])


def for_patient(db, cid, pid):
    return db.execute("""SELECT k.*, c.cycle_no, c.started_at AS cycle_at, s.name AS steriliser, u.display_name AS by_name
                         FROM steri_packs k JOIN steri_cycles c ON c.id = k.cycle_id JOIN sterilisers s ON s.id = c.steriliser_id
                         LEFT JOIN users u ON u.id = k.used_by WHERE k.clinic_id = ? AND k.used_on = ? ORDER BY k.used_at DESC""",
                      (cid, pid)).fetchall()


def shelf(db, cid, today, status="stored"):
    return db.execute("""SELECT k.*, c.cycle_no, s.name AS steriliser, (k.expires_on < ?) AS expired FROM steri_packs k
                         JOIN steri_cycles c ON c.id = k.cycle_id JOIN sterilisers s ON s.id = c.steriliser_id
                         WHERE k.clinic_id = ? AND k.status = ? ORDER BY k.expires_on, k.code LIMIT 500""",
                      (today.isoformat(), cid, status)).fetchall()


# -------------------------------------------------------------------------------------------------------------- recalls
def _open_recall(db, clinic, user_id, s, test):
    last = db.execute("""SELECT MAX(done_at) FROM steri_tests WHERE steriliser_id = ? AND kind = 'spore' AND result = 'pass'
                         AND id < ?""", (s["id"], test["id"])).fetchone()[0]
    rid = db.execute("""INSERT INTO steri_recalls(clinic_id, steriliser_id, test_id, since, opened_at, opened_by)
                        VALUES (?,?,?,?,?,?)""", (clinic["id"], s["id"], test["id"], last, _stamp(clinic), user_id)).lastrowid
    q = """UPDATE steri_packs SET status = 'recalled', recall_id = ? WHERE status IN ('stored','held') AND cycle_id IN
           (SELECT id FROM steri_cycles WHERE steriliser_id = ?""" + (" AND started_at >= ?)" if last else ")")
    db.execute(q, (rid, s["id"], last) if last else (rid, s["id"]))
    q = """UPDATE steri_packs SET recall_id = ? WHERE status = 'used' AND recall_id IS NULL AND cycle_id IN
           (SELECT id FROM steri_cycles WHERE steriliser_id = ?""" + (" AND started_at >= ?)" if last else ")")
    db.execute(q, (rid, s["id"], last) if last else (rid, s["id"]))
    audit.log(db, clinic["id"], user_id, "steri.recall", "steriliser", s["id"])
    return rid


def recall(db, cid, rid):
    r = db.execute("""SELECT r.*, s.name AS steriliser FROM steri_recalls r JOIN sterilisers s ON s.id = r.steriliser_id
                      WHERE r.id = ? AND r.clinic_id = ?""", (rid, cid)).fetchone()
    if not r:
        return None
    packs = db.execute("""SELECT k.*, c.cycle_no, c.implants, p.first_name, p.last_name FROM steri_packs k
                          JOIN steri_cycles c ON c.id = k.cycle_id LEFT JOIN patients p ON p.id = k.used_on
                          WHERE k.recall_id = ? ORDER BY k.status DESC, k.code""", (rid,)).fetchall()
    return {"r": r, "used": [k for k in packs if k["used_on"]], "pulled": [k for k in packs if not k["used_on"]]}


def close_recall(db, clinic, user_id, rid, note):
    r = db.execute("SELECT * FROM steri_recalls WHERE id = ? AND clinic_id = ?", (rid, clinic["id"])).fetchone()
    if not r or r["closed_at"]:
        raise SteriError("That recall is already closed.")
    note = (note or "").strip()[:1000]
    if not note:
        raise SteriError("Write what was done, for example: packs re-sterilised, patients reviewed by the dentist.")
    db.execute("UPDATE steri_recalls SET closed_at = ?, closed_by = ?, note = ? WHERE id = ?", (_stamp(clinic), user_id, note, rid))
    audit.log(db, clinic["id"], user_id, "steri.recall_close", "steriliser", r["steriliser_id"])
