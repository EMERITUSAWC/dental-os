import re, sys, json
sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent.parent))
from pathlib import Path
from dental_os.services import i18n_fr
MARKED = re.compile(r"""\b_d?\(\s*(?:"((?:[^"\\]|\\.)*)"|'((?:[^'\\]|\\.)*)')""")
found = {}
for f in sorted(Path('./dental_os/templates').glob('*.html')):
    for m in MARKED.finditer(f.read_text()):
        k = m.group(1) if m.group(1) is not None else m.group(2)
        if k not in i18n_fr.FR:
            found.setdefault(k, f.name)
for k, f in found.items():
    print(json.dumps(k, ensure_ascii=False), f)
print(len(found), file=sys.stderr)
