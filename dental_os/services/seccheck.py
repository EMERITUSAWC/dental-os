"""Version 0.67: automatic code checks. Runs with nothing but Python, so it works on any server and in the tests.

It reads the code (it never runs it) and looks for the mistakes that most often turn into holes in a web app like this one:
running text as code, unsafe loading of saved objects, shell commands, switching off certificate checks, weak randomness for
secrets, SQL built from what a visitor sent, and templates that print unescaped text. Each place where one of these is used on
purpose is listed in ALLOWED with the reason, so a new one cannot slip in unnoticed.

Bandit and pip-audit are better at this and should also be run where the server can reach PyPI: see tools/security-scan.sh."""
import ast
import functools
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# (file relative to dental_os, rule): why it is fine.
ALLOWED = {
    ("services/twofactor.py", "weak-hash"): "SHA-1 inside HMAC is what RFC 6238 one-time codes use, and authenticator apps expect.",
    ("services/voice.py", "weak-hash"): "Twilio signs its requests with HMAC-SHA1; checking that signature needs the same.",
    ("db.py", "weak-hash"): "MD5 only names a throwaway test schema; nothing secret.",
}
SAFE_FILTER_OK = {"svg", "qr_svg", "link_svg", "'<br>'"}         # SVG pictures the app draws itself from its own data

BANNED_CALLS = {"eval": "code-from-text", "exec": "code-from-text", "compile": "code-from-text"}
BANNED_IMPORTS = {"pickle": "unsafe-load", "marshal": "unsafe-load", "shelve": "unsafe-load", "random": "weak-random",
                  "telnetlib": "plain-protocol", "ftplib": "plain-protocol"}
WEAK_HASHES = {"md5", "sha1"}
SQL_CALLS = {"execute", "executemany", "executescript"}


def _source_files():
    return sorted(p for p in ROOT.rglob("*.py") if "__pycache__" not in p.parts)


def _rel(path):
    return str(path.relative_to(ROOT)).replace("\\", "/")


def _uses_request(node):
    return any(isinstance(n, ast.Name) and n.id == "request" for n in ast.walk(node))


def check_python(path):
    out = []
    tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    rel = _rel(path)
    for n in ast.walk(tree):
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            names = [a.name.split(".")[0] for a in n.names] if isinstance(n, ast.Import) else [(n.module or "").split(".")[0]]
            for name in names:
                if name in BANNED_IMPORTS:
                    out.append((rel, n.lineno, BANNED_IMPORTS[name], f"imports {name}"))
        if not isinstance(n, ast.Call):
            continue
        fn = n.func
        name = fn.id if isinstance(fn, ast.Name) else fn.attr if isinstance(fn, ast.Attribute) else None
        if isinstance(fn, ast.Name) and name in BANNED_CALLS:
            out.append((rel, n.lineno, BANNED_CALLS[name], f"{name}()"))
        for kw in n.keywords:
            if kw.arg == "shell" and isinstance(kw.value, ast.Constant) and kw.value.value is True:
                out.append((rel, n.lineno, "shell", "shell=True"))
            if kw.arg == "verify" and isinstance(kw.value, ast.Constant) and kw.value.value is False:
                out.append((rel, n.lineno, "no-tls-check", "verify=False"))
            if kw.arg == "debug" and isinstance(kw.value, ast.Constant) and kw.value.value is True:
                out.append((rel, n.lineno, "debug", "debug=True"))
        if isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name):
            mod = fn.value.id
            if mod == "os" and name in ("system", "popen"):
                out.append((rel, n.lineno, "shell", f"os.{name}()"))
            if mod == "yaml" and name == "load":
                out.append((rel, n.lineno, "unsafe-load", "yaml.load()"))
            if mod == "hashlib" and name in WEAK_HASHES:
                out.append((rel, n.lineno, "weak-hash", f"hashlib.{name}()"))
            if mod == "ssl" and name == "_create_unverified_context":
                out.append((rel, n.lineno, "no-tls-check", "unverified TLS context"))
        if isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name) and fn.value.id == "hashlib" and name == "new":
            if n.args and isinstance(n.args[0], ast.Constant) and str(n.args[0].value).lower() in WEAK_HASHES:
                out.append((rel, n.lineno, "weak-hash", "hashlib.new()"))
        for a in n.args:
            if isinstance(a, ast.Attribute) and isinstance(a.value, ast.Name) and a.value.id == "hashlib" and a.attr in WEAK_HASHES:
                out.append((rel, n.lineno, "weak-hash", f"hashlib.{a.attr}"))
        if name in SQL_CALLS and n.args and not isinstance(n.args[0], ast.Constant) and _uses_request(n.args[0]):
            out.append((rel, n.lineno, "sql-from-request", "SQL text built from the request"))
        if name == "render_template_string" and n.args and not isinstance(n.args[0], ast.Constant):
            out.append((rel, n.lineno, "template-from-text", "render_template_string() with built text"))
        if name == "Markup" and n.args and not isinstance(n.args[0], ast.Constant):
            out.append((rel, n.lineno, "unescaped", "Markup() of built text"))
    return [f for f in out if (f[0], f[2]) not in ALLOWED]


SAFE = re.compile(r"\{\{\s*([^}|]+?)\s*\|\s*safe\b")


def check_templates():
    out = []
    for p in sorted((ROOT / "templates").glob("*.html")):
        text = p.read_text(encoding="utf-8")
        for i, line in enumerate(text.splitlines(), 1):
            for m in SAFE.finditer(line):
                if m.group(1).strip() not in SAFE_FILTER_OK:
                    out.append((f"templates/{p.name}", i, "unescaped", f"{m.group(1).strip()}|safe"))
            if re.search(r"autoescape\s+false", line):
                out.append((f"templates/{p.name}", i, "unescaped", "autoescape false"))
    return out


@functools.lru_cache(maxsize=1)
def _cached():
    return tuple(_scan())


def run():
    """Every finding, as (file, line, rule, what). The code does not change while the app runs, so it is read once."""
    return list(_cached())


def _scan():
    out = []
    for p in _source_files():
        out += check_python(p)
    return out + check_templates()


RULES = {
    "code-from-text": "Text run as Python code",
    "unsafe-load": "Loading saved objects in a way that can run code",
    "weak-random": "The random module (predictable); secrets is for anything secret",
    "plain-protocol": "A protocol that sends passwords in the clear",
    "shell": "A shell command",
    "no-tls-check": "Certificate checks switched off",
    "debug": "Debug mode, which lets a visitor run code",
    "weak-hash": "MD5 or SHA-1",
    "sql-from-request": "SQL built from what a visitor sent",
    "template-from-text": "A template built from text at run time",
    "unescaped": "Text printed without escaping",
}


def report():
    found = run()
    lines = [f"Automatic code checks on {len(_source_files())} Python files and the templates.", "",
             "Rules checked:"] + [f"  {k}: {v}" for k, v in RULES.items()]
    lines += ["", "Allowed on purpose:"] + [f"  {f} ({r}): {why}" for (f, r), why in sorted(ALLOWED.items())]
    lines += [f"  templates: |safe only on {', '.join(sorted(SAFE_FILTER_OK))}, pictures the app draws itself", ""]
    lines += [f"Findings: {len(found)}"] + [f"  {f}:{line} [{rule}] {what}" for f, line, rule, what in found]
    lines += ["", "Also run Bandit and pip-audit where the server can reach PyPI (tools/security-scan.sh)."]
    return "\n".join(lines) + "\n"
