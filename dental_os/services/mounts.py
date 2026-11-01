"""X-ray layouts ("mounts"): images put into the standard positions of a full mouth series or a set of bitewings, so they can be
read side by side the way they were taken. A mount only points at files already on the record: nothing is copied."""
from . import audit

# Each layout: (name, rows). A row is a list of slot labels; None leaves a gap. Slots are numbered in reading order.
LAYOUTS = {
    "fmx18": ("Full mouth series, 18 images", [
        ["Upper right molars", "Upper right premolars", "Upper right canine", "Upper incisors", "Upper left canine", "Upper left premolars", "Upper left molars"],
        ["Right molar bitewing", "Right premolar bitewing", None, None, None, "Left premolar bitewing", "Left molar bitewing"],
        ["Lower right molars", "Lower right premolars", "Lower right canine", "Lower incisors", "Lower left canine", "Lower left premolars", "Lower left molars"],
    ]),
    "bw4": ("Four bitewings", [["Right molar bitewing", "Right premolar bitewing", "Left premolar bitewing", "Left molar bitewing"]]),
    "bw2": ("Two bitewings", [["Right bitewing", "Left bitewing"]]),
    "pano": ("Panoramic with bitewings", [["Panoramic (OPG)"], ["Right bitewing", "Left bitewing"]]),
    "photos": ("Clinical photos", [["Smile, front", "Right side", "Left side"], ["Upper teeth", "Lower teeth", "Other"]]),
}


class MountError(Exception):
    pass


def slots(layout):
    out, n = [], 0
    for row in LAYOUTS[layout][1]:
        cells = []
        for label in row:
            if label is None:
                cells.append(None)
            else:
                n += 1
                cells.append({"slot": n, "label": label})
        out.append(cells)
    return out


def slot_count(layout):
    return sum(1 for row in LAYOUTS[layout][1] for label in row if label is not None)


def create(db, clinic, user_id, pid, layout, title=None, taken_on=None):
    cid = clinic["id"]
    if layout not in LAYOUTS:
        raise MountError("Pick a layout from the list.")
    if not db.execute("SELECT 1 FROM patients WHERE id = ? AND clinic_id = ?", (pid, cid)).fetchone():
        raise MountError("That patient was not found.")
    mid = db.execute("INSERT INTO image_mounts(clinic_id, patient_id, layout, title, taken_on, created_by) VALUES (?,?,?,?,?,?)",
                     (cid, pid, layout, (title or "").strip()[:80] or None, (taken_on or "").strip()[:10] or None, user_id)).lastrowid
    audit.log(db, cid, user_id, "mount.create", "patient", pid, layout)
    return mid


def get(db, cid, mid):
    return db.execute("SELECT * FROM image_mounts WHERE id = ? AND clinic_id = ?", (mid, cid)).fetchone()


def placed(db, mid):
    return {r["slot"]: r for r in db.execute(
        """SELECT s.slot, f.* FROM image_mount_slots s JOIN patient_files f ON f.id = s.file_id WHERE s.mount_id = ?""", (mid,))}


def place(db, clinic, user_id, mid, slot, file_id):
    cid = clinic["id"]
    m = get(db, cid, mid)
    if not m:
        raise MountError("That layout was not found.")
    try:
        slot = int(slot)
    except (TypeError, ValueError):
        slot = 0
    if not 1 <= slot <= slot_count(m["layout"]):
        raise MountError("Pick a position in the layout.")
    if not file_id:
        db.execute("DELETE FROM image_mount_slots WHERE mount_id = ? AND slot = ?", (mid, slot))
        audit.log(db, cid, user_id, "mount.clear", "patient", m["patient_id"])
        return m["patient_id"]
    f = db.execute("""SELECT * FROM patient_files WHERE id = ? AND clinic_id = ? AND patient_id = ? AND archived = 0
                      AND mime IN ('image/png','image/jpeg','image/webp','application/dicom')""", (file_id, cid, m["patient_id"])).fetchone()
    if not f:
        raise MountError("Pick one of this patient's pictures.")
    db.execute("DELETE FROM image_mount_slots WHERE mount_id = ? AND slot = ?", (mid, slot))
    db.execute("INSERT INTO image_mount_slots(clinic_id, mount_id, slot, file_id) VALUES (?,?,?,?)", (cid, mid, slot, file_id))
    audit.log(db, cid, user_id, "mount.place", "patient", m["patient_id"])
    return m["patient_id"]


def for_patient(db, cid, pid):
    rows = db.execute("SELECT * FROM image_mounts WHERE clinic_id = ? AND patient_id = ? ORDER BY id DESC", (cid, pid)).fetchall()
    return [{"m": r, "filled": db.execute("SELECT COUNT(*) FROM image_mount_slots WHERE mount_id = ?", (r["id"],)).fetchone()[0],
             "total": slot_count(r["layout"])} for r in rows]
