"""Repair what wrap.py cannot do: a phrase that has a {{ variable }} inside it. Splits it so each English part is wrapped on its own:
    _("Notes, {{ p.first_name }}")   ->   {{ _("Notes,") }} {{ p.first_name }}
Usage: fixjinja.py file.html ..."""
import re
import sys

CALL = re.compile(r"\{\{\s*(_d?)\((\"|')((?:(?!\2).)*?\{\{.*?\}\}.*?)\2\)\s*\}\}", re.S)
PART = re.compile(r"(\{\{.*?\}\})", re.S)


def split(m):
    fn, q, text = m.group(1), m.group(2), m.group(3)
    out = []
    for piece in PART.split(text):
        if piece.startswith("{{"):
            out.append(piece)
        elif re.search(r"[A-Za-z]{2,}", piece):
            lead = piece[:len(piece) - len(piece.lstrip())]
            trail = piece[len(piece.rstrip()):]
            core = piece.strip()
            out.append(f"{lead}{{{{ {fn}({q}{core}{q}) }}}}{trail}")
        else:
            out.append(piece)
    return "".join(out)


for path in sys.argv[1:]:
    s = open(path).read()
    new = CALL.sub(split, s)
    if new != s:
        open(path, "w").write(new)
        print("fixed", path)
