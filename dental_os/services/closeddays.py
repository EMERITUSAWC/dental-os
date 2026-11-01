"""Days the clinic is closed (version 0.47): public holidays and the clinic's own closures.

The clinic types in each day it is closed, with a name (Independence Day, Christmas Day, staff training). On a closed day nobody can
be booked, the online booking page offers no times, the diary says why, and working-day counts (such as the three working days to
acknowledge a complaint) skip it. A year's days can be copied to the next year for the holidays that fall on the same date each year;
holidays that move (Easter, Eid) are typed in when the government announces them.

The list is not filled in for the clinic: public holidays differ by country, and some are declared only weeks ahead, so the clinic
takes them from the official notice (in Ghana, the Ministry of the Interior). No web framework here."""
import datetime

from . import audit


class ClosedError(ValueError):
    pass


def _day(text):
    try:
        return datetime.date.fromisoformat((text or "").strip()).isoformat()
    except ValueError:
        raise ClosedError("Use a real date.")


def add(db, clinic, user_id, day, name):
    cid = clinic["id"]
    day = _day(day)
    name = (name or "").strip()[:80]
    if not name:
        raise ClosedError("Say why the clinic is closed that day, for example Independence Day.")
    if db.execute("SELECT 1 FROM closed_days WHERE clinic_id = ? AND day = ? AND removed_at IS NULL", (cid, day)).fetchone():
        raise ClosedError("That day is already on the list.")
    db.execute("INSERT INTO closed_days(clinic_id, day, name, created_by) VALUES (?,?,?,?)", (cid, day, name, user_id))
    audit.log(db, cid, user_id, "closed.add", None, None, f"{day} {name}")


def remove(db, clinic, user_id, cd_id):
    n = db.execute("UPDATE closed_days SET removed_at = datetime('now'), removed_by = ? WHERE id = ? AND clinic_id = ? AND removed_at IS NULL",
                   (user_id, cd_id, clinic["id"])).rowcount
    if not n:
        raise ClosedError("That day was not found.")
    audit.log(db, clinic["id"], user_id, "closed.remove", None, None, str(cd_id))


def listing(db, cid, year=None):
    sql = "SELECT * FROM closed_days WHERE clinic_id = ? AND removed_at IS NULL"
    args = [cid]
    if year:
        sql += " AND substr(day, 1, 4) = ?"
        args.append(str(year))
    return db.execute(sql + " ORDER BY day", args).fetchall()


def name_on(db, cid, day):
    d = day.isoformat() if isinstance(day, (datetime.date, datetime.datetime)) else str(day)[:10]
    row = db.execute("SELECT name FROM closed_days WHERE clinic_id = ? AND day = ? AND removed_at IS NULL", (cid, d[:10])).fetchone()
    return row["name"] if row else None


def days_set(db, cid):
    return {r["day"] for r in db.execute("SELECT day FROM closed_days WHERE clinic_id = ? AND removed_at IS NULL", (cid,))}


def copy_year(db, clinic, user_id, from_year, picked_ids):
    """Copy the chosen days of one year to the same dates the next year. Returns how many were added."""
    cid = clinic["id"]
    n = 0
    for r in listing(db, cid, from_year):
        if r["id"] not in picked_ids:
            continue
        d = datetime.date.fromisoformat(r["day"])
        try:
            nxt = d.replace(year=d.year + 1).isoformat()
        except ValueError:
            continue
        if not db.execute("SELECT 1 FROM closed_days WHERE clinic_id = ? AND day = ? AND removed_at IS NULL", (cid, nxt)).fetchone():
            db.execute("INSERT INTO closed_days(clinic_id, day, name, created_by) VALUES (?,?,?,?)", (cid, nxt, r["name"], user_id))
            n += 1
    audit.log(db, cid, user_id, "closed.copy", None, None, f"{from_year}: {n}")
    return n
