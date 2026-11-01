"""Version 0.60: a quick check of each X-ray as it comes in, so a blurred or badly exposed picture is retaken while the patient
is still in the chair, not found a week later when a claim is refused.

It looks only at the picture's sharpness, brightness, contrast and size, on this computer, with no internet and no outside
company. It does not read the X-ray and says nothing about the teeth. The limits are starting points: the pilot will show
whether they need moving for each clinic's sensor, and they are named here so they are easy to change.
"""
import io

BLUR_BELOW = 6.0          # strongest edges against the contrast. Sharp test pictures score 16 to 20, blurred or shaken ones 4 or less
DARK_SHARE = 0.55         # more than this share of the picture almost black
BRIGHT_SHARE = 0.40       # more than this share almost white (burnt out)
CONTRAST_BELOW = 18.0     # the spread of grey levels; a flat, washed-out picture is under this
SMALL_BELOW = 300         # pixels on the short side

ISSUES = {
    "blurred": "Looks blurred. Check for movement and retake before the patient leaves if it cannot be read.",
    "too_dark": "Looks too dark. Check the exposure settings.",
    "too_bright": "Looks too bright, with parts burnt out. Check the exposure settings.",
    "low_contrast": "Looks flat and washed out. Check the exposure and the sensor.",
    "too_small": "The picture is very small. Check it was not saved as a thumbnail.",
}


class QualityError(Exception):
    pass


def measure(data):
    """{'w','h','sharpness','dark','bright','contrast'} for PNG, JPEG or WebP bytes. Raises QualityError if it cannot be opened."""
    try:
        from PIL import Image, ImageFilter, ImageOps, ImageStat
    except ImportError:                                      # Pillow is optional (requirements-imaging.txt)
        raise QualityError("The picture check needs Pillow on the server.")
    try:
        img = Image.open(io.BytesIO(data))
        img = ImageOps.exif_transpose(img)
        w, h = img.size
        gray = img.convert("L")
    except Exception:
        raise QualityError("The picture could not be opened.")
    gray.thumbnail((800, 800))
    hist = gray.histogram()
    n = float(sum(hist)) or 1.0
    contrast = ImageStat.Stat(gray).stddev[0]
    # Sharpness: how strong the strongest 1% of edges are, against the picture's own contrast, so a dim picture is not called blurred.
    edges = gray.filter(ImageFilter.Kernel((3, 3), [0, 1, 0, 1, -4, 1, 0, 1, 0], scale=1, offset=128)).histogram()
    mag = [0] * 129
    for v, c in enumerate(edges):
        mag[min(abs(v - 128), 128)] += c
    top, seen = 0, 0
    for v in range(128, -1, -1):
        seen += mag[v]
        if seen >= n * 0.01:
            top = v
            break
    return {"w": w, "h": h, "sharpness": round(top / (contrast or 1.0) * 10, 2), "dark": round(sum(hist[:16]) / n, 3),
            "bright": round(sum(hist[240:]) / n, 3), "contrast": round(contrast, 2)}


def judge(m):
    """The issue codes for a measurement, worst first."""
    out = []
    if min(m["w"], m["h"]) < SMALL_BELOW:
        out.append("too_small")
    if m["dark"] > DARK_SHARE:
        out.append("too_dark")
    if m["bright"] > BRIGHT_SHARE:
        out.append("too_bright")
    if m["contrast"] < CONTRAST_BELOW and "too_dark" not in out and "too_bright" not in out:
        out.append("low_contrast")
    if m["sharpness"] < BLUR_BELOW:
        out.append("blurred")
    return out


def pixels_png(data, mime):
    """The picture alone as PNG bytes: DICOM converted, photos re-drawn without their hidden details (EXIF, names, dates)."""
    if mime == "application/dicom":
        from . import dicom
        try:
            return dicom.to_png(data)
        except dicom.DicomError as exc:
            raise QualityError(str(exc))
    try:
        from PIL import Image, ImageOps
        img = ImageOps.exif_transpose(Image.open(io.BytesIO(data)))
        if img.mode not in ("L", "RGB"):
            img = img.convert("RGB")
        clean = Image.new(img.mode, img.size)
        clean.paste(img)                                      # a fresh picture carries none of the original's metadata
        out = io.BytesIO()
        clean.save(out, "PNG")
        return out.getvalue()
    except QualityError:
        raise
    except Exception:
        raise QualityError("The picture could not be opened.")


def check_file(db, cid, file_row, data):
    """Measure one stored X-ray and keep the result. Never raises: a picture that cannot be checked is simply left unchecked."""
    if file_row["kind"] != "xray":
        return None
    try:
        m = measure(pixels_png(data, file_row["mime"]))
    except QualityError:
        return None
    issues = judge(m)
    db.execute("DELETE FROM xray_checks WHERE clinic_id = ? AND file_id = ?", (cid, file_row["id"]))
    db.execute("""INSERT INTO xray_checks(clinic_id, file_id, ok, issues, sharpness, dark, bright, contrast, width, height)
                  VALUES (?,?,?,?,?,?,?,?,?,?)""",
               (cid, file_row["id"], 0 if issues else 1, ",".join(issues), m["sharpness"], m["dark"], m["bright"], m["contrast"], m["w"], m["h"]))
    return issues


def for_files(db, cid, file_ids):
    """{file id: row} for showing next to each picture."""
    ids = [int(i) for i in file_ids]
    if not ids:
        return {}
    rows = db.execute(f"SELECT * FROM xray_checks WHERE clinic_id = ? AND file_id IN ({','.join('?' * len(ids))})", (cid, *ids)).fetchall()
    return {r["file_id"]: dict(r, issue_list=[i for i in (r["issues"] or "").split(",") if i]) for r in rows}
