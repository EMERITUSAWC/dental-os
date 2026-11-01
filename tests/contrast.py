"""Colour contrast from the WCAG 2 formula, for the text and background pairs app.css actually uses. Used by tests.test_v58."""
import re
from pathlib import Path

# (text colour, background) as the stylesheet pairs them.
PAIRS = [("ink", "bg"), ("ink", "card"), ("muted", "card"), ("muted", "bg"), ("brand", "card"), ("brand", "bg"), ("brand-ink", "brand"),
         ("ok", "ok-bg"), ("warn", "warn-bg"), ("bad", "bad-bg"), ("bad", "card"), ("ink", "info-bg"), ("ink", "ok-bg"), ("ink", "warn-bg"),
         ("ink", "bad-bg"), ("warn", "card"), ("ok", "card")]


def _lum(hexcol):
    h = hexcol.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    out = []
    for i in (0, 2, 4):
        c = int(h[i:i + 2], 16) / 255
        out.append(c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4)
    return 0.2126 * out[0] + 0.7152 * out[1] + 0.0722 * out[2]


def ratio(a, b):
    la, lb = sorted((_lum(a), _lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def themes(css_text):
    """{'light': {name: '#hex'}, 'dark': {...}} from the two :root blocks."""
    blocks = re.findall(r":root\s*\{([^}]*)\}", css_text)
    out = []
    for b in blocks[:2]:
        out.append(dict(re.findall(r"--([\w-]+):\s*(#[0-9a-fA-F]{3,6})", b)))
    light = out[0]
    dark = {**light, **out[1]} if len(out) > 1 else {}
    return {"light": light, "dark": dark}


def report(css_path):
    t = themes(Path(css_path).read_text())
    return {(theme, fg, bg): round(ratio(vals[fg], vals[bg]), 2) for theme, vals in t.items() for fg, bg in PAIRS if fg in vals and bg in vals}
