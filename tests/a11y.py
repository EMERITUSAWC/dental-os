"""A plain checker for the accessibility rules every page must follow. Used by tests.test_v58.

It reads the HTML the server sends and reports:
  * a form field with no label (a <label for>, a wrapping <label>, aria-label or aria-labelledby, or a title);
  * an image with no alt text, and an inline drawing (svg) with no role="img"/aria-label or aria-hidden;
  * a button or link with nothing to read out;
  * a page with no <title>, no lang on <html>, or not exactly one <h1>;
  * an id used twice on one page (labels and screen readers find the wrong thing).
"""
from html.parser import HTMLParser

SILENT_INPUTS = {"hidden", "submit", "button", "reset", "image"}


class _Page(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.fields, self.labels_for, self.ids, self.problems = [], set(), {}, []
        self.label_depth = 0
        self.lang = None
        self.title = ""
        self.in_title = False
        self.h1 = 0
        self.open_clickables = []          # [tag, attrs, text so far, has labelled child]
        self.svg_depth = 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if "id" in a:
            self.ids[a["id"]] = self.ids.get(a["id"], 0) + 1
        if tag == "html":
            self.lang = a.get("lang")
        elif tag == "title":
            self.in_title = True
        elif tag == "h1":
            self.h1 += 1
        elif tag == "label":
            self.label_depth += 1
            if a.get("for"):
                self.labels_for.add(a["for"])
        elif tag in ("input", "select", "textarea"):
            kind = (a.get("type") or "text").lower() if tag == "input" else tag
            if kind not in SILENT_INPUTS:
                named = bool(a.get("aria-label") or a.get("aria-labelledby") or a.get("title")) or self.label_depth > 0
                self.fields.append((tag, a.get("name"), a.get("id"), named))
            elif kind == "image" and not a.get("alt"):
                self.problems.append(f"image button {a.get('name')} has no alt text")
        elif tag == "img":
            if "alt" not in a:
                self.problems.append(f"image {a.get('src', '')[:60]} has no alt text")
            for c in self.open_clickables:
                if a.get("alt"):
                    c[3] = True
        elif tag == "svg":
            self.svg_depth += 1
            if self.svg_depth == 1:
                ok = a.get("aria-hidden") == "true" or (a.get("role") == "img" and (a.get("aria-label") or a.get("aria-labelledby")))
                if not ok:
                    self.problems.append("a drawing (svg) needs role=\"img\" and aria-label, or aria-hidden=\"true\"")
                for c in self.open_clickables:
                    if a.get("aria-label"):
                        c[3] = True
        if tag in ("button", "a") and (tag == "button" or "href" in a):
            named = bool(a.get("aria-label") or a.get("aria-labelledby") or a.get("title"))
            self.open_clickables.append([tag, a, "", named])

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag == "svg":
            self.svg_depth -= 1

    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False
        elif tag == "label":
            self.label_depth = max(self.label_depth - 1, 0)
        elif tag == "svg":
            self.svg_depth = max(self.svg_depth - 1, 0)
        elif tag in ("button", "a"):
            for i in range(len(self.open_clickables) - 1, -1, -1):
                if self.open_clickables[i][0] == tag:
                    t, a, text, named = self.open_clickables.pop(i)
                    if not named and not text.strip():
                        self.problems.append(f"{t} {a.get('href') or a.get('name') or a.get('class') or ''} has nothing to read out")
                    break

    def handle_data(self, data):
        if self.in_title:
            self.title += data
        for c in self.open_clickables:
            c[2] += data


def check(html):
    p = _Page()
    p.feed(html)
    out = list(p.problems)
    for tag, name, fid, named in p.fields:
        if not named and not (fid and fid in p.labels_for):
            out.append(f"{tag} {name or fid or ''} has no label")
    if not p.title.strip():
        out.append("the page has no title")
    if not p.lang:
        out.append("<html> has no lang")
    if p.h1 != 1:
        out.append(f"the page has {p.h1} main headings (h1), not one")
    out += [f"id {i} is used {n} times" for i, n in p.ids.items() if n > 1]
    return out
