"""Made-up data so the screens have something in them. None of these are real people or real prices."""
from datetime import timedelta

from .services import recalls, regions, treatment
from .services import timeutil as T

# first name, last name, agreed to appointment messages, agreed to offers
PEOPLE = [
    ("Alex", "Demo", 1, 1), ("Sam", "Demo", 1, 0), ("Jordan", "Demo", 0, 1), ("Casey", "Demo", 1, 1),
    ("Robin", "Demo", 1, 0), ("Taylor", "Demo", 1, 0), ("Morgan", "Demo", 1, 1), ("Jamie", "Demo", 1, 0),
]


def _fake_phone(country_code, n):
    """A made-up number with the right shape for the clinic's country. Not a real subscriber."""
    _name, cc, _trunk, lens, _tz, _cur = regions.country(country_code)
    width = lens[0]
    return "+" + cc + str(5 * 10 ** (width - 1) + n)


def seed(conn, clinic_id):
    clinic = conn.execute("SELECT * FROM clinics WHERE id = ?", (clinic_id,)).fetchone()
    if not clinic:
        return "No clinic has that id. Run list-clinics, or create-clinic first."
    cid = clinic["id"]
    if conn.execute("SELECT 1 FROM patients WHERE clinic_id = ? AND last_name = 'Demo'", (cid,)).fetchone():
        return "Demo data is already there."
    today = T.now_local(clinic["timezone"]).date()
    if not conn.execute("SELECT 1 FROM providers WHERE clinic_id = ?", (cid,)).fetchone():
        for name in ("Dr Demo One", "Dr Demo Two"):
            conn.execute("INSERT INTO providers(clinic_id, name) VALUES (?,?)", (cid, name))
    while conn.execute("SELECT COUNT(*) FROM chairs WHERE clinic_id = ?", (cid,)).fetchone()[0] < 2:
        n = conn.execute("SELECT COUNT(*) FROM chairs WHERE clinic_id = ?", (cid,)).fetchone()[0] + 1
        conn.execute("INSERT INTO chairs(clinic_id, name) VALUES (?, ?)", (cid, f"Chair {n}"))
    providers = [r["id"] for r in conn.execute("SELECT id FROM providers WHERE clinic_id = ? ORDER BY id", (cid,))]
    chairs = [r["id"] for r in conn.execute("SELECT id FROM chairs WHERE clinic_id = ? ORDER BY id", (cid,))]
    ids = []
    for i, (first, last, reminders, offers) in enumerate(PEOPLE):
        born = today + timedelta(days=3 if i == 0 else 40 + i * 20)
        dob = born.replace(year=1990 + i).isoformat()
        cur = conn.execute(
            """INSERT INTO patients(clinic_id, first_name, last_name, dob, phone, reminders_ok, offers_ok, notes)
               VALUES (?,?,?,?,?,?,?, 'Demo record')""",
            (cid, first, last, dob, _fake_phone(clinic["country"], i + 1), reminders, offers))
        ids.append(cur.lastrowid)
    plan = [(0, 0, 0, "09:00", "Consultation", "confirmed"), (1, 1, 1, "09:30", "Fillings & restorations", "sent"),
            (2, 0, 0, "11:00", "Cleaning & preventive care", "none"), (3, 2, 1, "14:00", "Root canal treatment", "none"),
            (4, 1, 0, "09:00", "Crowns", "none"), (5, 0, 1, "10:30", "Tooth extractions", "sent"),
            (6, 2, 0, "15:00", "Orthodontics & smile enhancement", "none")]
    for n, (pi, prov, chair, hhmm, cat, conf) in enumerate(plan):
        day = today + timedelta(days=0 if n < 3 else 1)
        conn.execute(
            """INSERT INTO appointments(clinic_id, patient_id, provider_id, chair_id, start_at, duration_min, status,
                                        confirmation, category)
               VALUES (?,?,?,?,?,?, 'scheduled', ?,?)""",
            (cid, ids[pi], providers[prov % len(providers)], chairs[chair % len(chairs)], f"{day} {hhmm}", 30,
             conf, cat))
    conn.execute("INSERT INTO appointments(clinic_id, patient_id, provider_id, duration_min, status, category) "
                 "VALUES (?,?,?,?, 'asap', 'Tooth extractions')", (cid, ids[7], providers[0], 45))
    # Demo money. These fees are made up. They are not anyone's real prices.
    treatment.ensure_catalogue(conn, cid)
    recalls.ensure_default_type(conn, cid)
    code = {r["code"]: r["id"] for r in conn.execute("SELECT id, code FROM procedure_codes WHERE clinic_id = ?", (cid,))}
    scale = 10 ** regions.currency_digits(clinic["currency"])      # keeps the made-up fees in whole units
    for pi, name, units, status in ((0, "CONS", 200, "done"), (0, "SCALE", 150, "done"), (0, "CROWN", 800, "planned"),
                                    (1, "FILL", 250, "done"), (3, "RCT", 1200, "planned")):
        conn.execute(
            """INSERT INTO procedures(clinic_id, patient_id, code_id, tooth, status, fee_p, provider_id, planned_on, done_on, notes)
               VALUES (?,?,?,?,?,?,?,?,?, 'Demo')""",
            (cid, ids[pi], code[name], 36 if name in ("FILL", "CROWN") else None, status, units * scale, providers[0],
             today.isoformat(), today.isoformat() if status == "done" else None))
    next_receipt = conn.execute("SELECT COALESCE(MAX(receipt_no),0)+1 FROM payments WHERE clinic_id = ?", (cid,)).fetchone()[0]
    conn.execute("""INSERT INTO payments(clinic_id, patient_id, amount_p, method, receipt_no, received_on)
                    VALUES (?,?,?, 'cash', ?, ?)""", (cid, ids[0], 200 * scale, next_receipt, today.isoformat()))
    conn.execute("""INSERT INTO payments(clinic_id, patient_id, amount_p, method, reference, receipt_no, received_on)
                    VALUES (?,?,?, 'card', 'DEMO-0001', ?, ?)""", (cid, ids[1], 100 * scale, next_receipt + 1, today.isoformat()))
    conn.execute("INSERT INTO recalls(clinic_id, patient_id, type_id, due_on) VALUES (?,?,?,?)",
                 (cid, ids[7], recalls.ensure_default_type(conn, cid), (today - timedelta(days=12)).isoformat()))
    conn.commit()
    return f"Added {len(PEOPLE)} demo patients and {len(plan) + 1} appointments."
