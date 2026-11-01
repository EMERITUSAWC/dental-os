"""Images and documents on a patient's record. Files are checked by their first bytes (not by name), stored under random
names outside the web folder, and handed back only to signed-in staff of the same clinic."""
import re
import uuid
from pathlib import Path

from werkzeug.utils import secure_filename

from . import audit

MAX_BYTES = 10 * 1024 * 1024
KINDS = {"xray": "X-ray", "photo": "Photo", "document": "Document", "referral": "Referral letter",
         "consent": "Scanned consent", "other": "Other"}
INLINE = {"image/png", "image/jpeg", "image/webp"}      # shown in the page. Everything else downloads.


class FileError(Exception):
    pass


def detect(head):
    """(mime, extension) from the first bytes, or None when the type is not allowed."""
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", ".png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", ".jpg"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp", ".webp"
    if head.startswith(b"%PDF-"):
        return "application/pdf", ".pdf"
    if len(head) >= 132 and head[128:132] == b"DICM":
        return "application/dicom", ".dcm"
    return None


def save_upload(db, clinic, user, pid, storage, kind, note, taken_on, root):
    cid = clinic["id"]
    if not db.execute("SELECT 1 FROM patients WHERE id = ? AND clinic_id = ?", (pid, cid)).fetchone():
        raise FileError("That patient was not found.")
    if kind not in KINDS:
        raise FileError("Pick what kind of file this is.")
    if storage is None or not storage.filename:
        raise FileError("Choose a file to upload.")
    data = storage.read(MAX_BYTES + 1)
    if not data:
        raise FileError("That file is empty.")
    if len(data) > MAX_BYTES:
        raise FileError("That file is bigger than 10 MB.")
    found = detect(data[:200])
    if not found:
        raise FileError("Only PNG, JPEG, WebP, PDF and DICOM files can be uploaded.")
    mime, ext = found
    if taken_on and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", taken_on):
        raise FileError("Use the date picker for the date.")
    original = secure_filename(storage.filename) or "file"
    stored = uuid.uuid4().hex + ext
    folder = Path(root) / str(cid)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / stored
    path.write_bytes(data)
    try:
        cur = db.execute(
            """INSERT INTO patient_files(clinic_id, patient_id, kind, original_name, stored_name, mime, size_bytes, note,
                                         taken_on, uploaded_by) VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (cid, pid, kind, original[:120], stored, mime, len(data), (note or "").strip()[:200] or None,
             taken_on or None, user["id"]))
    except Exception:
        path.unlink(missing_ok=True)
        raise
    audit.log(db, cid, user["id"], "file.upload", "patient", pid, kind)
    return cur.lastrowid


def list_for_patient(db, cid, pid, include_archived=False):
    return db.execute(
        "SELECT * FROM patient_files WHERE clinic_id = ? AND patient_id = ?" + ("" if include_archived else " AND archived = 0") +
        " ORDER BY COALESCE(taken_on, substr(created_at,1,10)) DESC, id DESC", (cid, pid)).fetchall()


def get(db, cid, file_id):
    return db.execute("SELECT * FROM patient_files WHERE id = ? AND clinic_id = ?", (file_id, cid)).fetchone()


def path_for(root, row):
    path = (Path(root) / str(row["clinic_id"]) / row["stored_name"]).resolve()
    if Path(root).resolve() not in path.parents:
        raise FileError("Bad file path.")
    return path


def archive(db, clinic, user, file_id):
    row = get(db, clinic["id"], file_id)
    if not row or row["archived"]:
        raise FileError("That file was not found.")
    db.execute("UPDATE patient_files SET archived = 1 WHERE id = ?", (file_id,))
    audit.log(db, clinic["id"], user["id"], "file.archive", "patient", row["patient_id"])
    return row["patient_id"]
