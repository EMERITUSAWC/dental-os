"""Gum (periodontal) charting: six probing points on each tooth. Pocket depth, recession and bleeding."""
import re

from . import audit
from .timeutil import now_local

SITES = ("DB", "B", "MB", "DL", "L", "ML")
BUCCAL = ("DB", "B", "MB")
LINGUAL = ("DL", "L", "ML")
TEETH = [n for q in (1, 2, 3, 4) for n in range(q * 10 + 1, q * 10 + 9)]
UPPER = list(range(18, 10, -1)) + list(range(21, 29))
LOWER = list(range(48, 40, -1)) + list(range(31, 39))
SEXTANTS = [("Upper right", (18, 17, 16, 15, 14)), ("Upper front", (13, 12, 11, 21, 22, 23)),
            ("Upper left", (24, 25, 26, 27, 28)), ("Lower left", (38, 37, 36, 35, 34)),
            ("Lower front", (33, 32, 31, 41, 42, 43)), ("Lower right", (44, 45, 46, 47, 48))]


class PerioError(Exception):
    pass


def site_order(tooth):
    """Sites left to right as they appear on the chart. Distal is away from the middle of the mouth."""
    right_side = tooth // 10 in (1, 4)
    return (("DB", "B", "MB"), ("DL", "L", "ML")) if right_side else (("MB", "B", "DB"), ("ML", "L", "DL"))


def parse_form(form, skip_teeth=()):
    """Read the pk_/rc_/bl_ fields. Returns readings and raises PerioError with a plain message."""
    readings = []
    for tooth in TEETH:
        if tooth in skip_teeth:
            continue
        for site in SITES:
            pk, rc = (form.get(f"pk_{tooth}_{site}") or "").strip(), (form.get(f"rc_{tooth}_{site}") or "").strip()
            bl = 1 if form.get(f"bl_{tooth}_{site}") else 0
            if not pk and not rc and not bl:
                continue
            try:
                pocket = int(pk) if pk else None
                rec = int(rc) if rc else None
            except ValueError:
                raise PerioError(f"Tooth {tooth}, {site}: use whole numbers of millimetres.")
            if pocket is not None and not 0 <= pocket <= 15:
                raise PerioError(f"Tooth {tooth}, {site}: pocket depth must be between 0 and 15 mm.")
            if rec is not None and not -5 <= rec <= 15:
                raise PerioError(f"Tooth {tooth}, {site}: recession must be between -5 and 15 mm.")
            readings.append({"tooth": tooth, "site": site, "pocket_mm": pocket, "recession_mm": rec, "bleeding": bl})
    if not readings:
        raise PerioError("Enter at least one reading.")
    return readings


def create_chart(db, clinic, user_id, pid, readings, notes=None):
    cid = clinic["id"]
    if not db.execute("SELECT 1 FROM patients WHERE id = ? AND clinic_id = ?", (pid, cid)).fetchone():
        raise PerioError("That patient was not found.")
    today = now_local(clinic["timezone"]).date().isoformat()
    cur = db.execute("INSERT INTO perio_charts(clinic_id, patient_id, charted_on, charted_by, notes) VALUES (?,?,?,?,?)",
                     (cid, pid, today, user_id, (notes or "").strip()[:500] or None))
    chart_id = cur.lastrowid
    db.executemany(
        "INSERT INTO perio_readings(chart_id, clinic_id, tooth, site, pocket_mm, recession_mm, bleeding) VALUES (?,?,?,?,?,?,?)",
        [(chart_id, cid, r["tooth"], r["site"], r["pocket_mm"], r["recession_mm"], r["bleeding"]) for r in readings])
    audit.log(db, cid, user_id, "perio.create", "patient", pid)
    return chart_id


def list_charts(db, cid, pid):
    return db.execute("SELECT c.*, u.display_name AS by_name FROM perio_charts c LEFT JOIN users u ON u.id = c.charted_by "
                      "WHERE c.clinic_id = ? AND c.patient_id = ? ORDER BY c.charted_on DESC, c.id DESC", (cid, pid)).fetchall()


def get_chart(db, cid, chart_id):
    return db.execute("SELECT * FROM perio_charts WHERE id = ? AND clinic_id = ?", (chart_id, cid)).fetchone()


def readings(db, cid, chart_id):
    rows = db.execute("SELECT * FROM perio_readings WHERE chart_id = ? AND clinic_id = ?", (chart_id, cid)).fetchall()
    return {(r["tooth"], r["site"]): r for r in rows}


def summary(rows):
    """Numbers a dentist reads at a glance. `rows` is the dict from readings()."""
    pockets = [r["pocket_mm"] for r in rows.values() if r["pocket_mm"] is not None]
    sites_bleeding = sum(1 for r in rows.values() if r["bleeding"])
    deepest = max(rows.values(), key=lambda r: (r["pocket_mm"] if r["pocket_mm"] is not None else -1), default=None)
    out = {
        "sites": len(rows), "probed": len(pockets),
        "mean": round(sum(pockets) / len(pockets), 1) if pockets else None,
        "bleeding_pct": round(100 * sites_bleeding / len(rows), 1) if rows else None,
        "sites_4plus": sum(1 for p in pockets if p >= 4), "sites_6plus": sum(1 for p in pockets if p >= 6),
        "deepest": (deepest["pocket_mm"], deepest["tooth"], deepest["site"]) if deepest and deepest["pocket_mm"] is not None else None,
        "teeth": len({r["tooth"] for r in rows.values()}),
    }
    out["sextants"] = [{"name": name, "score": sextant_score(rows, teeth)} for name, teeth in SEXTANTS]
    return out


def sextant_score(rows, teeth):
    """A simplified screening score for one sixth of the mouth. 0 healthy, 1 bleeding, 3 pockets of 4 to 5 mm,
    4 pockets of 6 mm or more. None when nothing was recorded. It does not use a calculus or margin code,
    so it is a guide and not a full BPE."""
    mine = [r for (t, _s), r in rows.items() if t in teeth]
    if not mine:
        return None
    deepest = max((r["pocket_mm"] for r in mine if r["pocket_mm"] is not None), default=None)
    if deepest is not None and deepest >= 6:
        return 4
    if deepest is not None and deepest >= 4:
        return 3
    if any(r["bleeding"] for r in mine):
        return 1
    return 0


def compare(new, old):
    """Change since the last chart. Negative numbers are better for pockets and bleeding."""
    if not new or not old or new["mean"] is None or old["mean"] is None:
        return None
    return {"mean": round(new["mean"] - old["mean"], 1),
            "bleeding_pct": round((new["bleeding_pct"] or 0) - (old["bleeding_pct"] or 0), 1),
            "sites_4plus": new["sites_4plus"] - old["sites_4plus"]}


def missing_teeth(db, cid, pid):
    return {r["tooth"] for r in db.execute(
        "SELECT tooth FROM tooth_conditions WHERE clinic_id=? AND patient_id=? AND status='active' AND condition='missing'",
        (cid, pid))}
