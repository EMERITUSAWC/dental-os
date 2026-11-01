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
VIEWABLE = INLINE | {"application/dicom"}                # can be opened in the viewer (DICOM through a PNG made from it)


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
    if storage is None or not storage.filename:
        raise FileError("Choose a file to upload.")
    return save_bytes(db, clinic, user["id"], pid, storage.read(MAX_BYTES + 1), storage.filename, kind, note, taken_on, root)


def save_bytes(db, clinic, user_id, pid, data, filename, kind, note, taken_on, root):
    """Store one file on a patient's record. The type is read from the file's first bytes, never from its name."""
    cid = clinic["id"]
    if not db.execute("SELECT 1 FROM patients WHERE id = ? AND clinic_id = ?", (pid, cid)).fetchone():
        raise FileError("That patient was not found.")
    if kind not in KINDS:
        raise FileError("Pick what kind of file this is.")
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
    original = secure_filename(filename or "") or "file"
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
             taken_on or None, user_id))
    except Exception:
        path.unlink(missing_ok=True)
        raise
    audit.log(db, cid, user_id, "file.upload", "patient", pid, kind)
    if kind == "xray":                               # 0.60: sharpness and exposure, so a bad picture is retaken in the chair
        from . import xrayqc
        xrayqc.check_file(db, cid, {"id": cur.lastrowid, "kind": kind, "mime": mime}, data)
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


def preview_png(root, row):
    """A PNG made from a DICOM file, kept beside it so it is made only once. Raises dicom.DicomError when it cannot be shown."""
    from . import dicom
    src = path_for(root, row)
    out = src.with_name(src.stem + ".preview.png")
    if out.exists():
        return out
    png = dicom.to_png(src.read_bytes())
    tmp = out.with_suffix(".tmp")
    tmp.write_bytes(png)
    tmp.replace(out)
    return out


def dicom_summary(root, row):
    from . import dicom
    try:
        return dicom.summary(dicom.parse(path_for(root, row).read_bytes()))
    except (dicom.DicomError, OSError):
        return None
