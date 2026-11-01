"""Self check-in on a tablet at the front desk. A member of staff starts kiosk mode on the device; from then on the device only shows
the check-in screen until someone enters a staff password to leave it. A patient checks in with the last four digits of their phone
number and their date of birth. Nothing about anyone is shown until both match a visit booked for today, and then only the first
name. Tries are limited, so the screen cannot be used to guess who is booked."""
from . import scheduling as S
from .timeutil import now_local, parse_date

TRIES_PER_10_MIN = 12


class KioskError(Exception):
    pass


def find(db, clinic, last4, dob):
    """Today's booked, not yet arrived visits for the person with this phone ending and date of birth."""
    last4 = "".join(ch for ch in (last4 or "") if ch.isdigit())
    if len(last4) != 4:
        raise KioskError("Enter the last four digits of your phone number.")
    try:
        born = parse_date((dob or "").strip()).isoformat()
    except ValueError:
        raise KioskError("Enter your date of birth.")
    day = now_local(clinic["timezone"]).date().isoformat()
    rows = db.execute("""SELECT a.*, p.first_name, p.phone, p.dob FROM appointments a JOIN patients p ON p.id = a.patient_id
                         WHERE a.clinic_id = ? AND a.status = 'scheduled' AND a.start_at >= ? AND a.start_at < ?
                         AND a.arrived_at IS NULL AND p.dob = ? ORDER BY a.start_at""",
                      (clinic["id"], f"{day} 00:00", f"{day} 99", born)).fetchall()
    return [r for r in rows if (r["phone"] or "").endswith(last4)]


def check_in(db, clinic, kiosk_user, appt_id):
    S.move_stage(db, clinic, kiosk_user["id"], appt_id, "arrive")
