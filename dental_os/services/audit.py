def log(db, clinic_id, user_id, action, object_type=None, object_id=None, detail=None):
    """Record who did what. Never put message text or clinical notes in detail."""
    db.execute(
        "INSERT INTO audit_log(clinic_id, user_id, action, object_type, object_id, detail) VALUES (?,?,?,?,?,?)",
        (clinic_id, user_id, action, object_type, object_id, detail),
    )


def recent(db, clinic_id, limit=200):
    return db.execute(
        """SELECT a.*, u.display_name AS user_name FROM audit_log a
           LEFT JOIN users u ON u.id = a.user_id
           WHERE a.clinic_id = ? ORDER BY a.id DESC LIMIT ?""",
        (clinic_id, limit),
    ).fetchall()
