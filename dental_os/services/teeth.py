"""Tooth numbering. Everything is stored in the FDI two-digit system (ISO 3950). A clinic chooses how teeth are shown and typed:
FDI (most of the world), Universal (the United States: 1 to 32, baby teeth A to T) or Palmer (common in the UK: a quadrant and a
number, written here as UR6, or URD for a baby tooth). The choice is held for the length of one request in NOTATION, so the
services can read typed teeth without knowing about the web layer."""
from contextvars import ContextVar

NOTATIONS = {"fdi": "FDI (11 to 48, baby teeth 51 to 85)", "universal": "Universal (1 to 32, baby teeth A to T)",
             "palmer": "Palmer (UR1 to LL8, baby teeth URA to LLE)"}
NOTATION = ContextVar("tooth_notation", default="fdi")

PERMANENT = [n for q in (1, 2, 3, 4) for n in range(q * 10 + 1, q * 10 + 9)]
PRIMARY = [n for q in (5, 6, 7, 8) for n in range(q * 10 + 1, q * 10 + 6)]
VALID = set(PERMANENT + PRIMARY)

# Universal permanent: 1 is the upper right third molar (FDI 18), counting round the upper arch to 16 (FDI 28),
# then 17 is the lower left third molar (FDI 38) round to 32 (FDI 48).
_UNI_PERM = [18, 17, 16, 15, 14, 13, 12, 11, 21, 22, 23, 24, 25, 26, 27, 28,
             38, 37, 36, 35, 34, 33, 32, 31, 41, 42, 43, 44, 45, 46, 47, 48]
# Universal baby teeth: A is the upper right second molar (FDI 55) round to J (FDI 65), K (FDI 75) round to T (FDI 85).
_UNI_PRIM = [55, 54, 53, 52, 51, 61, 62, 63, 64, 65, 75, 74, 73, 72, 71, 81, 82, 83, 84, 85]
FDI_TO_UNI = {f: str(i + 1) for i, f in enumerate(_UNI_PERM)}
FDI_TO_UNI.update({f: chr(ord("A") + i) for i, f in enumerate(_UNI_PRIM)})
UNI_TO_FDI = {v: k for k, v in FDI_TO_UNI.items()}

_QUAD = {1: "UR", 2: "UL", 3: "LL", 4: "LR", 5: "UR", 6: "UL", 7: "LL", 8: "LR"}
_QUAD_BACK_PERM = {"UR": 1, "UL": 2, "LL": 3, "LR": 4}
_QUAD_BACK_PRIM = {"UR": 5, "UL": 6, "LL": 7, "LR": 8}
_PRIM_LETTERS = "ABCDE"

HELP = {
    "fdi": "Tooth numbers use the FDI system, for example 36 or 54.",
    "universal": "Tooth numbers use the Universal system: 1 to 32, or A to T for baby teeth.",
    "palmer": "Write the tooth like UR6 or LLD: upper or lower, right or left, then 1 to 8, or A to E for a baby tooth.",
}
HINT = {"fdi": "FDI, for example 36", "universal": "Universal, for example 19", "palmer": "Palmer, for example LL6"}


class ToothError(ValueError):
    pass


def current():
    n = NOTATION.get()
    return n if n in NOTATIONS else "fdi"


def show(fdi, notation=None):
    """The tooth as people in this clinic write it. Anything that is not an FDI tooth comes back unchanged."""
    notation = notation or current()
    try:
        n = int(fdi)
    except (TypeError, ValueError):
        return fdi if fdi is not None else ""
    if n not in VALID or notation == "fdi":
        return str(fdi)
    if notation == "universal":
        return FDI_TO_UNI[n]
    q, pos = divmod(n, 10)
    return _QUAD[q] + (str(pos) if q <= 4 else _PRIM_LETTERS[pos - 1])


def parse(raw, notation=None):
    """A typed tooth, read in the clinic's own system, as an FDI number. Empty gives None. Raises ToothError."""
    notation = notation or current()
    text = (str(raw).strip().upper().replace(" ", "") if raw is not None else "")
    if not text:
        return None
    if notation == "universal":
        if text.isdigit():
            text = str(int(text))
        if text in UNI_TO_FDI:
            return UNI_TO_FDI[text]
        raise ToothError(HELP["universal"])
    if notation == "palmer":
        quad, rest = text[:2], text[2:]
        if quad in _QUAD_BACK_PERM and rest.isdigit() and 1 <= int(rest) <= 8 and len(rest) == 1:
            return _QUAD_BACK_PERM[quad] * 10 + int(rest)
        if quad in _QUAD_BACK_PRIM and len(rest) == 1 and rest in _PRIM_LETTERS:
            return _QUAD_BACK_PRIM[quad] * 10 + _PRIM_LETTERS.index(rest) + 1
        raise ToothError(HELP["palmer"])
    try:
        n = int(text)
    except ValueError:
        n = -1
    if n not in VALID:
        raise ToothError(HELP["fdi"])
    return n


def parse_or_none(raw, notation=None):
    try:
        return parse(raw, notation)
    except ToothError:
        return None
