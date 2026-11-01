"""The shapes sent out by the API and by webhooks. One place, so both agree. No clinical notes, medical history, files or
message text ever appear here. Appointment times are the clinic's own wall-clock time. created_at and updated_at are UTC. Money is in minor units of the clinic's currency."""


def _b(v):
    return bool(v)


def patient(r):
    return {"id": r["id"], "first_name": r["first_name"], "last_name": r["last_name"], "dob": r["dob"], "gender": r["gender"] or None,
            "phone": r["phone"], "email": r["email"], "address": r["address"], "preferred_contact": r["preferred_contact"],
            "agreed_to_appointment_messages": _b(r["reminders_ok"]), "agreed_to_offers": _b(r["offers_ok"]),
            "status": r["status"], "created_at": r["created_at"], "updated_at": r["updated_at"]}


def appointment(r):
    return {"id": r["id"], "patient_id": r["patient_id"], "provider_id": r["provider_id"], "chair_id": r["chair_id"],
            "start_at": r["start_at"], "duration_min": r["duration_min"], "status": r["status"], "confirmation": r["confirmation"],
            "category": r["category"], "break_reason": r["break_reason"], "notes": r["notes"],
            "created_at": r["created_at"], "updated_at": r["updated_at"]}


def payment(r):
    return {"id": r["id"], "patient_id": r["patient_id"], "amount_p": r["amount_p"], "method": r["method"],
            "receipt_no": r["receipt_no"], "received_on": r["received_on"], "purpose": r["purpose"]}


def invoice(r):
    return {"id": r["id"], "invoice_no": r["invoice_no"], "patient_id": r["patient_id"], "issued_on": r["issued_on"],
            "due_on": r["due_on"], "subtotal_p": r["subtotal_p"], "tax_p": r["tax_p"], "total_p": r["total_p"]}


def plan(r, total_p):
    return {"id": r["id"], "patient_id": r["patient_id"], "title": r["title"], "status": r["status"], "decided_on": r["decided_on"],
            "total_p": total_p}


def booking_request(r):
    return {"id": r["id"], "wanted_start": r["wanted_start"], "reason": r["reason"], "provider_id": r["provider_id"]}


def clinic(r):
    return {"id": r["id"], "name": r["name"], "country": r["country"], "currency": r["currency"], "timezone": r["timezone"],
            "open_time": r["open_time"], "close_time": r["close_time"], "slot_minutes": r["slot_minutes"]}
