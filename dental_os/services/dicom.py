"""Reading DICOM X-rays (version 0.25), so they can be seen in the browser.

Reads the DICOM file format (Part 10) in its common forms: implicit and explicit VR little endian, and deflated explicit VR. Pixel
data that is not compressed is turned into a PNG here, with the file's own rescale and window settings, MONOCHROME1 shown the right
way round, and 8, 12 or 16 bit data. JPEG and JPEG 2000 compressed pixel data is decoded with Pillow when it is installed.
JPEG lossless and RLE are not decoded here: the screen says so and the original can still be opened in the imaging software.

It also reads the tags the screens need: patient name and ID in the file, study date, modality, maker, and pixel spacing (so the
viewer can measure in millimetres without being told a scale). No web framework here."""
import io
import struct
import zlib

IMPLICIT_LE = "1.2.840.10008.1.2"
EXPLICIT_LE = "1.2.840.10008.1.2.1"
DEFLATED_LE = "1.2.840.10008.1.2.1.99"
EXPLICIT_BE = "1.2.840.10008.1.2.2"
JPEG_SYNTAXES = {"1.2.840.10008.1.2.4.50", "1.2.840.10008.1.2.4.51", "1.2.840.10008.1.2.4.90", "1.2.840.10008.1.2.4.91"}
LONG_VRS = {b"OB", b"OD", b"OF", b"OL", b"OV", b"OW", b"SQ", b"SV", b"UC", b"UN", b"UR", b"UT", b"UV"}
MAX_PIXELS = 40_000_000

WANTED = {
    (0x0002, 0x0010): "transfer_syntax", (0x0008, 0x0020): "study_date", (0x0008, 0x0060): "modality",
    (0x0008, 0x0070): "manufacturer", (0x0008, 0x1090): "model", (0x0010, 0x0010): "patient_name", (0x0010, 0x0020): "patient_id",
    (0x0010, 0x0030): "patient_dob", (0x0018, 0x1164): "imager_spacing", (0x0028, 0x0002): "samples", (0x0028, 0x0004): "photometric",
    (0x0028, 0x0006): "planar", (0x0028, 0x0008): "frames", (0x0028, 0x0010): "rows", (0x0028, 0x0011): "columns",
    (0x0028, 0x0030): "pixel_spacing", (0x0028, 0x0100): "bits_allocated", (0x0028, 0x0101): "bits_stored",
    (0x0028, 0x0103): "pixel_rep", (0x0028, 0x1050): "window_center", (0x0028, 0x1051): "window_width",
    (0x0028, 0x1052): "intercept", (0x0028, 0x1053): "slope", (0x7FE0, 0x0010): "pixels",
}
US_TAGS = {"samples", "planar", "rows", "columns", "bits_allocated", "bits_stored", "pixel_rep"}


class DicomError(Exception):
    pass


def _text(raw):
    return raw.rstrip(b"\x00 ").decode("latin-1", "replace").strip()


class _Reader:
    def __init__(self, data, pos, explicit, little=True):
        self.d, self.p, self.explicit = data, pos, explicit
        self.e = "<" if little else ">"

    def tag(self):
        g, e = struct.unpack_from(self.e + "HH", self.d, self.p)
        self.p += 4
        return g, e

    def element(self):
        g, e = self.tag()
        if g == 0xFFFE:                                       # item, item end, sequence end
            ln = struct.unpack_from(self.e + "I", self.d, self.p)[0]
            self.p += 4
            return g, e, None, ln
        vr = None
        if self.explicit:
            vr = self.d[self.p:self.p + 2]
            if vr in LONG_VRS:
                ln = struct.unpack_from(self.e + "I", self.d, self.p + 4)[0]
                self.p += 8
            else:
                ln = struct.unpack_from(self.e + "H", self.d, self.p + 2)[0]
                self.p += 4
        else:
            ln = struct.unpack_from(self.e + "I", self.d, self.p)[0]
            self.p += 4
        return g, e, vr, ln


def _skip_sequence(r, ln):
    """Step over a sequence of undefined length: items until the sequence end marker."""
    if ln != 0xFFFFFFFF:
        r.p += ln
        return
    depth = 1
    while r.p < len(r.d) and depth:
        g, e = struct.unpack_from(r.e + "HH", r.d, r.p)
        if g == 0xFFFE and e == 0xE0DD:
            depth -= 1
            r.p += 8
        elif g == 0xFFFE and e == 0xE000:
            ln2 = struct.unpack_from(r.e + "I", r.d, r.p + 4)[0]
            r.p += 8
            if ln2 != 0xFFFFFFFF:
                r.p += ln2
        elif g == 0xFFFE and e == 0xE00D:
            r.p += 8
        else:
            gg, ee, vr, ln2 = r.element()
            if vr == b"SQ" or ln2 == 0xFFFFFFFF:
                _skip_sequence(r, ln2)
            else:
                r.p += ln2


def parse(data):
    """The tags we use and the raw pixel data. Raises DicomError for anything that is not a DICOM Part 10 file."""
    if len(data) < 132 or data[128:132] != b"DICM":
        raise DicomError("That is not a DICOM file.")
    out = {}
    r = _Reader(data, 132, explicit=True)
    while r.p + 8 <= len(data):                               # file meta, always explicit little endian
        g, _e = struct.unpack_from("<HH", data, r.p)
        if g != 0x0002:
            break
        g, e, vr, ln = r.element()
        if (g, e) in WANTED:
            out[WANTED[(g, e)]] = _text(data[r.p:r.p + ln])
        r.p += ln
    ts = out.get("transfer_syntax", EXPLICIT_LE)
    body, pos = data, r.p
    if ts == DEFLATED_LE:
        try:
            body, pos = zlib.decompress(data[r.p:], -15), 0
        except zlib.error:
            raise DicomError("The file's compressed data is damaged.")
    if ts == EXPLICIT_BE:
        raise DicomError("This DICOM uses big-endian byte order, which is not shown here. Open it in the imaging software.")
    r = _Reader(body, pos, explicit=(ts != IMPLICIT_LE))
    try:
        while r.p + 8 <= len(body):
            g, e, vr, ln = r.element()
            if g == 0xFFFE:
                continue
            key = WANTED.get((g, e))
            if key == "pixels":
                if ln == 0xFFFFFFFF:
                    out["pixels"] = _fragments(r)
                    out["encapsulated"] = True
                else:
                    out["pixels"] = body[r.p:r.p + ln]
                    out["pixel_vr"] = vr
                break
            if vr == b"SQ" or ln == 0xFFFFFFFF:
                _skip_sequence(r, ln)
                continue
            if key:
                raw = body[r.p:r.p + ln]
                if key in US_TAGS and (vr == b"US" or (vr is None and ln == 2)):
                    out[key] = struct.unpack(r.e + "H", raw[:2])[0] if len(raw) >= 2 else 0
                else:
                    out[key] = _text(raw)
            r.p += ln
    except struct.error:
        raise DicomError("The DICOM file is cut short or damaged.")
    out["transfer_syntax"] = ts
    for k in US_TAGS:
        if isinstance(out.get(k), str):
            try:
                out[k] = int(out[k])
            except ValueError:
                out[k] = 0
    return out


def _fragments(r):
    frags = []
    while r.p + 8 <= len(r.d):
        g, e = struct.unpack_from("<HH", r.d, r.p)
        ln = struct.unpack_from("<I", r.d, r.p + 4)[0]
        r.p += 8
        if g == 0xFFFE and e == 0xE0DD:
            break
        frags.append(r.d[r.p:r.p + ln])
        r.p += ln
    return frags[1:] if frags else []                         # the first item is the frame offset table


def _num(v, default=None, index=0):
    try:
        return float(str(v).split("\\")[index])
    except (ValueError, IndexError, TypeError):
        return default


def spacing_mm(info):
    """Millimetres per pixel across the image, from PixelSpacing or the detector's ImagerPixelSpacing."""
    for k in ("pixel_spacing", "imager_spacing"):
        v = _num(info.get(k))
        if v and 0 < v < 5:
            return v
    return None


def summary(info):
    name = info.get("patient_name", "").replace("^", " ").strip()
    d = info.get("study_date", "")
    return {"patient_name": " ".join(name.split()), "patient_id": info.get("patient_id", ""),
            "study_date": f"{d[:4]}-{d[4:6]}-{d[6:8]}" if len(d) == 8 and d.isdigit() else "",
            "modality": info.get("modality", ""), "maker": " ".join(x for x in (info.get("manufacturer", ""), info.get("model", "")) if x),
            "size": f"{info.get('columns', 0)} × {info.get('rows', 0)}", "mm_per_px": spacing_mm(info)}


# ------------------------------------------------------------------------------------------------------------- to PNG
def _png(width, height, rows_bytes, colour):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + row for row in rows_bytes)
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2 if colour else 0, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b"")


def to_png(data):
    """A PNG of the first frame. Raises DicomError with a plain reason when it cannot be shown."""
    info = parse(data)
    if "pixels" not in info:
        raise DicomError("This DICOM file has no picture in it.")
    rows, cols = int(info.get("rows") or 0), int(info.get("columns") or 0)
    if not rows or not cols or rows * cols > MAX_PIXELS:
        raise DicomError("The picture size in this DICOM file is not right.")
    if info.get("encapsulated"):
        return _compressed(info)
    samples = int(info.get("samples") or 1)
    bits = int(info.get("bits_allocated") or 16)
    px = info["pixels"]
    if samples == 3:
        if bits != 8:
            raise DicomError("This colour DICOM uses a pixel format that is not shown here.")
        n = rows * cols * 3
        if len(px) < n:
            raise DicomError("The DICOM file is cut short or damaged.")
        plane = px[:n]
        if int(info.get("planar") or 0) == 1:                 # RRR...GGG...BBB to RGBRGB
            k = rows * cols
            r_, g_, b_ = plane[:k], plane[k:2 * k], plane[2 * k:]
            plane = bytes(x for t in zip(r_, g_, b_) for x in t)
        return _png(cols, rows, [plane[i * cols * 3:(i + 1) * cols * 3] for i in range(rows)], True)
    if samples != 1 or bits not in (8, 16):
        raise DicomError("This DICOM uses a pixel format that is not shown here.")
    count = rows * cols
    need = count * (bits // 8)
    if len(px) < need:
        raise DicomError("The DICOM file is cut short or damaged.")
    signed = int(info.get("pixel_rep") or 0) == 1
    stored = int(info.get("bits_stored") or bits)
    values = _values(px[:need], bits, signed, stored, count)
    slope, intercept = _num(info.get("slope"), 1.0), _num(info.get("intercept"), 0.0)
    centre, width = _num(info.get("window_center")), _num(info.get("window_width"))
    out = _window(values, slope, intercept, centre, width, info.get("photometric", "MONOCHROME2") == "MONOCHROME1")
    return _png(cols, rows, [out[i * cols:(i + 1) * cols] for i in range(rows)], False)


def _values(px, bits, signed, stored, count):
    try:
        import numpy as np                                      # much faster when it is there; not required
        a = np.frombuffer(px, dtype=(np.int8 if signed else np.uint8) if bits == 8 else ("<i2" if signed else "<u2"), count=count)
        if bits == 16 and stored < 16:
            mask = (1 << stored) - 1
            a = a.astype(np.int32) & mask
            if signed:
                a = np.where(a >= (1 << (stored - 1)), a - (1 << stored), a)
        return a.astype(np.float64)
    except ImportError:
        if bits == 8:
            return list(struct.unpack(f"{count}{'b' if signed else 'B'}", px))
        vals = list(struct.unpack(f"<{count}{'h' if signed else 'H'}", px))
        if stored < 16:
            mask, top = (1 << stored) - 1, 1 << (stored - 1)
            vals = [((v & mask) - (1 << stored)) if signed and (v & mask) >= top else (v & mask) for v in vals]
        return vals


def _window(values, slope, intercept, centre, width, invert):
    try:
        import numpy as np
        v = values * slope + intercept
        if not width or width <= 1:
            lo, hi = float(v.min()), float(v.max())
        else:
            lo, hi = centre - width / 2, centre + width / 2
        span = (hi - lo) or 1.0
        out = np.clip((v - lo) / span * 255.0, 0, 255).astype(np.uint8)
        if invert:
            out = 255 - out
        return out.tobytes()
    except ImportError:
        v = [x * slope + intercept for x in values]
        if not width or width <= 1:
            lo, hi = min(v), max(v)
        else:
            lo, hi = centre - width / 2, centre + width / 2
        span = (hi - lo) or 1.0
        out = bytes(min(255, max(0, int((x - lo) / span * 255))) for x in v)
        return bytes(255 - b for b in out) if invert else out


def _compressed(info):
    ts = info["transfer_syntax"]
    if ts not in JPEG_SYNTAXES:
        raise DicomError("This DICOM is compressed in a way that is not shown here (for example JPEG lossless). Open it in the imaging software.")
    try:
        from PIL import Image
    except ImportError:
        raise DicomError("Showing compressed DICOM needs the Pillow package on the server (pip install Pillow).")
    frame = b"".join(info["pixels"])
    try:
        im = Image.open(io.BytesIO(frame))
        im.load()
    except Exception:                                           # noqa: BLE001  Pillow raises many kinds
        raise DicomError("The compressed picture in this DICOM could not be opened. Open it in the imaging software.")
    if im.mode not in ("L", "RGB"):
        im = im.convert("I") if im.mode.startswith("I") else im.convert("L")
        if im.mode == "I":
            lo, hi = im.getextrema()
            im = im.point(lambda x: (x - lo) * 255 / ((hi - lo) or 1)).convert("L")
    if info.get("photometric") == "MONOCHROME1" and im.mode == "L":
        from PIL import ImageOps
        im = ImageOps.invert(im)
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def make(rows, cols, pixels, bits=16, photometric="MONOCHROME2", explicit=True, patient_name="TEST^PATIENT", patient_id="",
         spacing=None, window=None, study_date="20261020", stored=None, transfer_syntax=None, frames=None):
    """A small DICOM file, used by the tests and by anyone checking the viewer without an X-ray machine."""
    def el(g, e, vr, value, explicit_=True):
        if len(value) % 2:
            value += b"\x00" if vr in (b"OB", b"UI") else b" "
        if not explicit_:
            return struct.pack("<HHI", g, e, len(value)) + value
        if vr in LONG_VRS:
            return struct.pack("<HH", g, e) + vr + b"\x00\x00" + struct.pack("<I", len(value)) + value
        return struct.pack("<HH", g, e) + vr + struct.pack("<H", len(value)) + value
    ts = (transfer_syntax or (EXPLICIT_LE if explicit else IMPLICIT_LE)).encode()
    meta_body = el(0x0002, 0x0001, b"OB", b"\x00\x01") + el(0x0002, 0x0010, b"UI", ts)
    meta = el(0x0002, 0x0000, b"UL", struct.pack("<I", len(meta_body))) + meta_body
    us = lambda v: struct.pack("<H", v)                         # noqa: E731
    fields = [(0x0008, 0x0020, b"DA", study_date.encode()), (0x0008, 0x0060, b"CS", b"IO"), (0x0008, 0x0070, b"LO", b"TEST MAKER"),
              (0x0010, 0x0010, b"PN", patient_name.encode()), (0x0010, 0x0020, b"LO", patient_id.encode()),
              (0x0028, 0x0002, b"US", us(1)), (0x0028, 0x0004, b"CS", photometric.encode()), (0x0028, 0x0010, b"US", us(rows)),
              (0x0028, 0x0011, b"US", us(cols))]
    if spacing:
        fields.append((0x0028, 0x0030, b"DS", f"{spacing}\\{spacing}".encode()))
    fields += [(0x0028, 0x0100, b"US", us(bits)), (0x0028, 0x0101, b"US", us(stored or bits)), (0x0028, 0x0103, b"US", us(0))]
    if window:
        fields += [(0x0028, 0x1050, b"DS", str(window[0]).encode()), (0x0028, 0x1051, b"DS", str(window[1]).encode())]
    body = b"".join(el(g, e, vr, v, explicit) for g, e, vr, v in fields)
    if frames is not None:                                    # encapsulated (compressed) pixel data: an empty offset table, then each frame
        items = struct.pack("<HHI", 0xFFFE, 0xE000, 0)
        for f in frames:
            f = f + b"\x00" * (len(f) % 2)
            items += struct.pack("<HHI", 0xFFFE, 0xE000, len(f)) + f
        body += (struct.pack("<HH", 0x7FE0, 0x0010) + b"OB\x00\x00" + struct.pack("<I", 0xFFFFFFFF) + items
                 + struct.pack("<HHI", 0xFFFE, 0xE0DD, 0))
    else:
        raw = struct.pack(f"<{len(pixels)}{'H' if bits == 16 else 'B'}", *pixels)
        body += el(0x7FE0, 0x0010, b"OW" if bits == 16 else b"OB", raw, explicit)
    return b"\x00" * 128 + b"DICM" + meta + body
