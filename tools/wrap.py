"""Wrap the plain English text in a template with _("...") so it can be translated.
Usage: wrap.py [--write] file.html ...   (dry run by default). Prints the strings found and anything needing a manual look."""
import html, re, sys

JINJA = re.compile(r"(\{\{.*?\}\}|\{%.*?%\}|\{#.*?#\})", re.S)
SKIP_BLOCK = re.compile(r"(<script.*?</script>|<style.*?</style>|<!--.*?-->)", re.S | re.I)
ATTRS = ("placeholder", "title", "aria-label", "alt")
HAS_WORD = re.compile(r"[A-Za-z]{2,}")

def norm(s):
    return re.sub(r"\s+", " ", html.unescape(s)).strip()

def quote(key):
    if '"' not in key:
        return '"' + key + '"'
    if "'" not in key:
        return "'" + key + "'"
    return None

def wrap_text(raw, found, manual, ctx):
    """raw is text with no jinja and no tags. Returns the text with the words wrapped."""
    if not HAS_WORD.search(raw):
        return raw
    key = norm(raw)
    q = quote(key)
    if q is None or "\\" in key:
        manual.append(("quote", key))
        return raw
    lead = raw[: len(raw) - len(raw.lstrip())]
    trail = raw[len(raw.rstrip()):]
    found.append(key)
    return f'{lead}{{{{ _({q}) }}}}{trail}'

def process(src):
    found, manual = [], []
    # protect blocks we must not touch
    keep = []
    def hold(m):
        keep.append(m.group(0)); return f"\x01{len(keep)-1}\x01"
    src = SKIP_BLOCK.sub(hold, src)
    # protect jinja
    jin = []
    def jhold(m):
        jin.append(m.group(0)); return f"\x02{len(jin)-1}\x02"
    # title block text
    def title(m):
        key = norm(m.group(2))
        if not HAS_WORD.search(key) or "\x02" in m.group(2):
            if HAS_WORD.search(key): manual.append(("title", key))
            return m.group(0)
        q = quote(key); found.append(key)
        return f'{m.group(1)}{{{{ _({q}) }}}}{m.group(3)}'
    src = re.sub(r"(\{% block title %\})(.*?)(\{% endblock %\})", title, src, flags=re.S)
    # jinja expression strings that look like English: report only
    src_for_report = src
    for m in JINJA.finditer(src):
        t = m.group(0)
        if t.startswith("{{") and "_(" in t: continue
        for s in re.findall(r"""['"]([A-Z][A-Za-z ,.'!?:()-]{3,})['"]""", t):
            if not s.isupper(): manual.append(("in-expression", s))
    # plain-English literals at the start of a ternary, after else, or after or, inside {{ }}
    def expr(m):
        t = m.group(0)
        if "_(" in t: return t
        def w(mm):
            key = mm.group(2)
            if "\\" in key or "'" in key or '"' in key or not HAS_WORD.search(key): return mm.group(0)
            found.append(key)
            return f"{mm.group(1)}_('{key}')"
        t = re.sub(r"^(\{\{\s*)'([A-Z][^'{}]*)'(?=\s+if\b)", w, t)
        t = re.sub(r"(\belse\s+)'([A-Z][^'{}]*)'", w, t)
        t = re.sub(r"(\bor\s+)'([A-Z][^'{}]*)'", w, t)
        return t
    src = re.sub(r"\{\{.*?\}\}", expr, src, flags=re.S)
    src = JINJA.sub(jhold, src)
    # attributes with plain text
    def attr(m):
        name, q, val = m.group(1), m.group(2), m.group(3)
        if "\x02" in val or not HAS_WORD.search(val): return m.group(0)
        key = norm(val)
        if "'" in key or '"' in key or "\\" in key:
            manual.append(("attr-quote", key)); return m.group(0)
        found.append(key)
        return f'{name}={q}{{{{ _(\'{key}\') }}}}{q}'
    src = re.sub(r"\b(" + "|".join(ATTRS) + r")=([\"'])(.*?)\2", attr, src)
    # text nodes
    def node(m):
        text = m.group(1)
        if "\x02" not in text:
            return ">" + wrap_text(text, found, manual, "") + "<"
        # mixed: wrap each literal fragment around the jinja tokens
        parts = re.split(r"(\x02\d+\x02)", text)
        out = []
        for p in parts:
            if p.startswith("\x02"): out.append(p)
            else: out.append(wrap_text(p, found, manual, "mixed"))
        return ">" + "".join(out) + "<"
    src = re.sub(r">([^<>]+)<", node, src)
    # leading/trailing text outside tags next to jinja blocks is left alone (reported if wordy)
    for m in re.finditer(r"(?:^|\})([^<>{}\x01\x02]{3,})(?=\{%|\{\{|$)", src):
        pass
    src = re.sub(r"\x02(\d+)\x02", lambda m: jin[int(m.group(1))], src)
    src = re.sub(r"\x01(\d+)\x01", lambda m: keep[int(m.group(1))], src)
    return src, found, manual

if __name__ == "__main__":
    write = "--write" in sys.argv
    allfound = []
    for path in [a for a in sys.argv[1:] if not a.startswith("--")]:
        s = open(path).read()
        out, found, manual = process(s)
        allfound += found
        print(f"## {path}: {len(found)} strings; manual: {len(manual)}")
        for kind, t in manual: print(f"   MANUAL {kind}: {t}")
        if write and out != s: open(path, "w").write(out)
    if "--list" in sys.argv:
        for k in sorted(set(allfound)): print(repr(k))
