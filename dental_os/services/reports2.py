"""More reports: the day sheet, production by treatment, treatment planned but not booked, and missed or cancelled visits."""
from datetime import timedelta


def day_sheet(db, cid, day):
    nxt = (day + timedelta(days=1)).isoformat()
    d = day.isoformat()
    visits = db.execute("""SELECT a.*, p.first_name, p.last_name, pr.name AS provider_name FROM appointments a JOIN patients p ON p.id = a.patient_id
                           LEFT JOIN providers pr ON pr.id = a.provider_id WHERE a.clinic_id = ? AND a.start_at >= ? AND a.start_at < ?
                           ORDER BY a.start_at""", (cid, f"{d} 00:00", f"{nxt} 00:00")).fetchall()
    work = db.execute("""SELECT pr.*, c.code, c.name AS code_name, pa.first_name, pa.last_name, d.name AS provider_name FROM procedures pr
                         JOIN procedure_codes c ON c.id = pr.code_id JOIN patients pa ON pa.id = pr.patient_id LEFT JOIN providers d ON d.id = pr.provider_id
                         WHERE pr.clinic_id = ? AND pr.status = 'done' AND pr.done_on = ? ORDER BY d.name, pr.id""", (cid, d)).fetchall()
    pays = db.execute("""SELECT p.*, pa.first_name, pa.last_name FROM payments p JOIN patients pa ON pa.id = p.patient_id
                         WHERE p.clinic_id = ? AND p.received_on = ? ORDER BY p.receipt_no""", (cid, d)).fetchall()
    new = db.execute("SELECT COUNT(*) FROM patients WHERE clinic_id = ? AND date(created_at) = ?", (cid, d)).fetchone()[0]
    by_dentist = {}
    for w in work:
        by_dentist.setdefault(w["provider_name"] or "", 0)
        by_dentist[w["provider_name"] or ""] += w["fee_p"]
    return {"visits": visits, "work": work, "payments": pays, "new_patients": new, "production": sum(w["fee_p"] for w in work),
            "collected": sum(p["amount_p"] for p in pays if not p["voided"]), "by_dentist": sorted(by_dentist.items())}


def production_by_code(db, cid, start, end):
    return db.execute("""SELECT c.code, c.name, c.category, COUNT(*) AS n, SUM(pr.fee_p) AS total FROM procedures pr
                         JOIN procedure_codes c ON c.id = pr.code_id WHERE pr.clinic_id = ? AND pr.status = 'done' AND pr.done_on BETWEEN ? AND ?
                         GROUP BY c.code, c.name, c.category ORDER BY total DESC, c.code""", (cid, start, end)).fetchall()


def unscheduled(db, cid, limit=300):
    """Planned treatment with no visit booked for it in the future: the work patients have agreed to but not booked."""
    return db.execute("""SELECT pr.*, c.name AS code_name, pa.first_name, pa.last_name, pa.phone FROM procedures pr
                         JOIN procedure_codes c ON c.id = pr.code_id JOIN patients pa ON pa.id = pr.patient_id
                         WHERE pr.clinic_id = ? AND pr.status = 'planned' AND pa.status = 'active'
                         AND NOT EXISTS (SELECT 1 FROM appointments a WHERE a.clinic_id = pr.clinic_id AND a.patient_id = pr.patient_id
                                         AND a.status = 'scheduled' AND a.start_at >= datetime('now'))
                         ORDER BY pr.planned_on, pr.id LIMIT ?""", (cid, limit)).fetchall()


def broken(db, cid, start, end):
    return db.execute("""SELECT a.*, p.first_name, p.last_name, p.phone, pr.name AS provider_name,
                                (SELECT COUNT(*) FROM appointments b WHERE b.clinic_id = a.clinic_id AND b.patient_id = a.patient_id
                                 AND b.status = 'scheduled' AND b.start_at > a.start_at) AS rebooked
                         FROM appointments a JOIN patients p ON p.id = a.patient_id LEFT JOIN providers pr ON pr.id = a.provider_id
                         WHERE a.clinic_id = ? AND a.status = 'broken' AND a.start_at >= ? AND a.start_at < ?
                         ORDER BY a.start_at DESC""", (cid, f"{start} 00:00", f"{end} 99")).fetchall()
