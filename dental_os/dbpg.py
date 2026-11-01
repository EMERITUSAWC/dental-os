"""PostgreSQL support. The product was written for SQLite, and this file lets the same code run on PostgreSQL without changing a
line of the services. It does four things:

  1. Translates the few SQLite-only bits of SQL as each statement is sent: ? placeholders, datetime('now'), date(), strftime(),
     INSERT OR IGNORE, LIKE (SQLite's is not case-sensitive), COLLATE NOCASE, BEGIN IMMEDIATE, and two-argument MAX and MIN.
  2. Gives back rows that act like sqlite3.Row (by name or by position) and a cursor with lastrowid and rowcount.
  3. Turns schema.sql into PostgreSQL (ids that count up by themselves, BIGINT for whole numbers) and adds a few small SQL functions.
  4. Hands the connection to psycopg (version 3) if it is installed, or pg8000. Install one of them: pip install "psycopg[binary]".

Dates stay as text ('2026-10-27 14:30:00') exactly as in SQLite, so every comparison and sort behaves the same.

Setting up the database: create it with a plain binary sort order, as SQLite sorts, for example
    createdb -T template0 -E UTF8 --locale=C dental_os
and give the web app DATABASE=postgresql://user:password@host:5432/dental_os. Tables are created on first start.

Known differences from SQLite that the product handles for you: a failed statement in PostgreSQL cancels the whole transaction until
a rollback (the product already rolls back after an error); BEGIN IMMEDIATE becomes an advisory lock, so two people booking the
last slot still queue up; sums of whole numbers come back as integers."""
import decimal
import importlib
import os
import re
from functools import lru_cache
from urllib.parse import parse_qs, unquote, urlparse

LOCK_KEY = 727001                       # one lock for every "BEGIN IMMEDIATE" section: they are short and rare


class Error(Exception):
    pass


class IntegrityError(Error):
    """A rule of the database was broken. The message starts the way SQLite's does ("UNIQUE constraint failed: ...") because the
    product reads those words to give a friendly message."""


_LABELS = {"23505": "UNIQUE constraint failed", "23503": "FOREIGN KEY constraint failed", "23502": "NOT NULL constraint failed",
           "23514": "CHECK constraint failed"}


def _sqlstate(exc):
    code = getattr(exc, "sqlstate", None) or getattr(exc, "pgcode", None)
    if not code and exc.args and isinstance(exc.args[0], dict):
        code = exc.args[0].get("C")
    return code


def is_url(value):
    return str(value).startswith(("postgres://", "postgresql://"))


# ----------------------------------------------------------------------------- rows
class Row:
    """Like sqlite3.Row: row[0], row['name'], row.keys(), iteration over values, len, equality with tuples."""
    __slots__ = ("_names", "_values")

    def __init__(self, names, values):
        self._names = names
        self._values = values

    def __getitem__(self, key):
        if isinstance(key, (int, slice)):
            return self._values[key]
        try:
            return self._values[self._names.index(key)] if key in self._names else self._values[[n.lower() for n in self._names].index(str(key).lower())]
        except ValueError:
            raise IndexError("No item with that key")

    def keys(self):
        return list(self._names)

    def __iter__(self):
        return iter(self._values)

    def __len__(self):
        return len(self._values)

    def __eq__(self, other):
        return tuple(self._values) == (tuple(other._values) if isinstance(other, Row) else other)

    def __hash__(self):
        return hash(tuple(self._values))

    def __repr__(self):
        return "Row(%s)" % ", ".join("%s=%r" % kv for kv in zip(self._names, self._values))


def _plain(v):
    """SUM() of whole numbers comes back from PostgreSQL as a Decimal. SQLite gives an int. Keep it an int."""
    if isinstance(v, decimal.Decimal):
        return int(v) if v == v.to_integral_value() else float(v)
    return v


# ----------------------------------------------------------------------------- translating SQL
_STR = re.compile(r"'(?:[^']|'')*'")
_COMMENT = re.compile(r"--[^\n]*")


def _mask(sql):
    strings = []

    def hold(m):
        strings.append(m.group(0))
        return "\x00%d\x00" % (len(strings) - 1)

    return _STR.sub(hold, _COMMENT.sub("", sql)), strings


def _unmask(text, strings):
    return re.sub(r"\x00(\d+)\x00", lambda m: strings[int(m.group(1))].replace("%", "%%"), text)


def _plain_text(arg, strings):
    return re.sub(r"\x00(\d+)\x00", lambda m: strings[int(m.group(1))], arg).strip()


def _split_call(text, open_at):
    """text[open_at] is '('. Returns (list of top-level argument texts, index just after the closing ')')."""
    depth, args, start, i = 0, [], open_at + 1, open_at
    while i < len(text):
        c = text[i]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                args.append(text[start:i])
                return args, i + 1
        elif c == "," and depth == 1:
            args.append(text[start:i])
            start = i + 1
        i += 1
    raise Error("unbalanced brackets in: " + text[:120])


def _rewrite_calls(text, strings, name, fn):
    """Find every name( ... ) call, from the left, and let fn(args) give its replacement (or None to leave it)."""
    pat = re.compile(r"(?<![\w.])" + name + r"\s*\(", re.I)
    pos = 0
    while True:
        m = pat.search(text, pos)
        if not m:
            return text
        args, end = _split_call(text, m.end() - 1)
        new = fn([a.strip() for a in args], strings)
        if new is None:
            pos = m.end()
        else:
            text = text[:m.start()] + new + text[end:]
            pos = m.start() + 1


def _datetime(args, strings):
    plain = [_plain_text(a, strings) for a in args]
    if len(args) == 1 and plain[0].lower() == "'now'":
        return "dos_now()"
    if len(args) == 2 and plain[0].lower() == "'now'":
        return "dos_now_mod(%s)" % args[1]
    if len(args) == 2:
        return "dos_dt(%s, %s)" % (args[0], args[1])
    return None


def _strftime(args, strings):
    if len(args) != 2:
        return None
    fmt = _plain_text(args[0], strings).strip("'")
    if fmt == "%s":
        return "dos_epoch(%s)" % args[1]
    if fmt == "%m-%d":
        return "substr(%s, 6, 5)" % args[1]
    if fmt == "%Y-%m-%d":
        return "substr(%s, 1, 10)" % args[1]
    raise Error("strftime format not supported on PostgreSQL yet: " + fmt)


def _maxmin(word):
    def fn(args, strings):
        return None if len(args) < 2 else "%s(%s)" % ("GREATEST" if word == "max" else "LEAST", ", ".join(args))
    return fn


_ORDER = re.compile(r"\bORDER\s+BY\b", re.I)
_ORDER_END = re.compile(r"\b(LIMIT|OFFSET|UNION|INTERSECT|EXCEPT|FOR|FETCH|WINDOW)\b", re.I)


def _order_nulls(text):
    """SQLite sorts NULL as the smallest value: first when ascending, last when descending. PostgreSQL does the opposite,
    so say it out loud for every ORDER BY item that does not say it already."""
    for m in reversed(list(_ORDER.finditer(text))):
        depth, i, start, cuts = 0, m.end(), m.end(), []
        end = len(text)
        while i < len(text):
            c = text[i]
            if c == "(":
                depth += 1
            elif c == ")":
                if depth == 0:
                    end = i
                    break
                depth -= 1
            elif c == "," and depth == 0:
                cuts.append(i)
            elif depth == 0:
                kw = _ORDER_END.match(text, i)
                if kw and (i == 0 or not (text[i - 1].isalnum() or text[i - 1] == "_")):
                    end = i
                    break
            i += 1
        bounds = [start] + [c + 1 for c in cuts]
        ends = cuts + [end]
        pieces = []
        for b, e in zip(bounds, ends):
            item = text[b:e]
            if re.search(r"\bNULLS\s+(FIRST|LAST)\b", item, re.I) or not item.strip():
                pieces.append(item)
                continue
            body = item.rstrip()
            pad = item[len(body):]
            pieces.append(body + (" NULLS LAST" if re.search(r"\bDESC$", body, re.I) else " NULLS FIRST") + pad)
        text = text[:start] + ",".join(pieces) + text[end:]
    return text


@lru_cache(maxsize=4096)
def translate(sql):
    """One SQLite statement in, one PostgreSQL statement out. Returns the text with %s placeholders."""
    text, strings = _mask(sql)
    text = text.strip().rstrip(";")
    upper = text.upper()
    if re.fullmatch(r"BEGIN(\s+IMMEDIATE|\s+EXCLUSIVE|\s+DEFERRED)?", upper):
        return "SELECT pg_advisory_xact_lock(%d)" % LOCK_KEY
    ignore = False
    if re.match(r"INSERT\s+OR\s+IGNORE\s+INTO", upper):
        text = re.sub(r"^INSERT\s+OR\s+IGNORE\s+INTO", "INSERT INTO", text, flags=re.I)
        ignore = True
    elif re.match(r"INSERT\s+OR\s+REPLACE", upper):
        raise Error("INSERT OR REPLACE is not supported on PostgreSQL: use ON CONFLICT")
    text = _rewrite_calls(text, strings, "datetime", _datetime)
    text = _rewrite_calls(text, strings, "strftime", _strftime)
    text = _rewrite_calls(text, strings, "date", lambda a, s: "dos_date(%s)" % a[0] if len(a) == 1 else None)
    text = _rewrite_calls(text, strings, "max", _maxmin("max"))
    text = _rewrite_calls(text, strings, "min", _maxmin("min"))
    text = re.sub(r"\bIS\s+NOT\s+(?!NULL\b|TRUE\b|FALSE\b|DISTINCT\b)", "IS DISTINCT FROM ", text, flags=re.I)
    text = re.sub(r"\bIS\s+(?!NOT\b|NULL\b|TRUE\b|FALSE\b|DISTINCT\b)", "IS NOT DISTINCT FROM ", text, flags=re.I)
    text = _order_nulls(text)
    text = re.sub(r"\bLIKE\b", "ILIKE", text, flags=re.I)
    text = re.sub(r"\bCOLLATE\s+NOCASE\b", 'COLLATE "nocase"', text, flags=re.I)
    text = text.replace("%", "%%").replace("?", "%s")
    text = _unmask(text, strings)
    if ignore:
        text += " ON CONFLICT DO NOTHING"
    return text


def split_script(script):
    """Cut a script into statements at semicolons that are not inside quotes or comments."""
    text, strings = _mask(script)
    return [_unmask(part, strings).replace("%%", "%").strip() for part in text.split(";") if part.strip()]


def translate_ddl(sql):
    """Table and index definitions. Handles what schema.sql and COLUMN_UPGRADES use."""
    text, strings = _mask(sql)
    text = re.sub(r"^(\s*)id\s+INTEGER\s+PRIMARY\s+KEY\b", r"\1id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY", text, flags=re.M)
    text = re.sub(r"\bINTEGER\b", "BIGINT", text)
    text = re.sub(r"\bREAL\b", "DOUBLE PRECISION", text)
    text = re.sub(r"\bBLOB\b", "BYTEA", text)
    return translate(_unmask(text, strings).replace("%%", "%"))


# ----------------------------------------------------------------------------- the functions the translated SQL calls
FUNCTIONS = [
    "CREATE OR REPLACE FUNCTION dos_now() RETURNS text LANGUAGE sql STABLE AS $$ SELECT to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS') $$",
    "CREATE OR REPLACE FUNCTION dos_now_mod(m text) RETURNS text LANGUAGE sql STABLE AS "
    "$$ SELECT to_char((now() AT TIME ZONE 'UTC') + m::interval, 'YYYY-MM-DD HH24:MI:SS') $$",
    "CREATE OR REPLACE FUNCTION dos_dt(base text, m text) RETURNS text LANGUAGE sql IMMUTABLE AS "
    "$$ SELECT to_char(base::timestamp + m::interval, 'YYYY-MM-DD HH24:MI:SS') $$",
    "CREATE OR REPLACE FUNCTION dos_date(x text) RETURNS text LANGUAGE sql IMMUTABLE AS $$ SELECT substr(x, 1, 10) $$",
    "CREATE OR REPLACE FUNCTION dos_epoch(x text) RETURNS bigint LANGUAGE sql IMMUTABLE AS "
    "$$ SELECT extract(epoch FROM x::timestamp AT TIME ZONE 'UTC')::bigint $$",
]


# ----------------------------------------------------------------------------- driver
def _driver():
    """Returns (module, kind). DENTAL_OS_PG_DRIVER can name a module with a DB-API connect(), used by the tests."""
    name = os.environ.get("DENTAL_OS_PG_DRIVER")
    if name:
        return importlib.import_module(name), "dbapi"
    try:
        import psycopg
        return psycopg, "psycopg"
    except ImportError:
        pass
    try:
        import pg8000.dbapi as pg8000
        return pg8000, "pg8000"
    except ImportError:
        raise Error('To use PostgreSQL install a driver: pip install "psycopg[binary]"')


def open_raw(url, schema=None):
    mod, kind = _driver()
    u = urlparse(url)
    db = (u.path or "/").lstrip("/")
    if kind == "psycopg":
        conn = mod.connect(url, autocommit=False, prepare_threshold=None)    # no named statements: safe behind a connection pooler
    elif kind == "pg8000":
        q = parse_qs(u.query)
        conn = mod.connect(user=unquote(u.username or ""), password=unquote(u.password or "") or None, host=u.hostname or "localhost",
                           port=u.port or 5432, database=db, ssl_context=True if q.get("sslmode", [""])[0] in ("require", "verify-full") else None)
    else:
        conn = mod.connect(host=u.hostname or "127.0.0.1", port=u.port or 5432, user=unquote(u.username or "postgres"), database=db,
                           password=unquote(u.password or "") or None)
    return conn


_IDTABLES = {}


class Cursor:
    def __init__(self, names=None, rows=None, rowcount=-1, lastrowid=None):
        self.description = [(n,) for n in names] if names else None
        self._rows = rows or []
        self._pos = 0
        self.rowcount = rowcount
        self.lastrowid = lastrowid

    def fetchone(self):
        if self._pos >= len(self._rows):
            return None
        self._pos += 1
        return self._rows[self._pos - 1]

    def fetchall(self):
        rest, self._pos = self._rows[self._pos:], len(self._rows)
        return rest

    def __iter__(self):
        return iter(self.fetchall())

    def close(self):
        pass


def _param(v):
    if isinstance(v, bool):
        return int(v)
    if hasattr(v, "isoformat") and not isinstance(v, str):
        return v.isoformat(" ") if hasattr(v, "hour") else v.isoformat()
    return v


class Connection:
    row_factory = None                    # accepted and ignored: rows are always Row

    def __init__(self, raw, schema=None):
        self.raw = raw
        self.schema = schema
        self.in_transaction = False       # like sqlite3: true once this connection has changed something (or taken the lock) and not yet committed
        if schema:
            self._exec('SET search_path TO "%s", public' % schema.replace('"', ""))
            raw.commit()

    def _exec(self, sql, params=(), safe=False):
        """Run one statement. With safe=True a failed statement is undone on its own and the transaction carries on, which is how
        SQLite behaves (PostgreSQL would otherwise refuse everything until a rollback)."""
        cur = self.raw.cursor()
        if safe:
            cur.execute("SAVEPOINT dos_stmt")
        try:
            cur.execute(sql, params)
        except Exception as exc:                             # noqa: BLE001 - driver errors differ; mapped below
            if safe:
                self.raw.cursor().execute("ROLLBACK TO SAVEPOINT dos_stmt")
            code = _sqlstate(exc)
            if code and code.startswith("23"):
                detail = getattr(exc, "detail", None) or str(exc)
                raise IntegrityError("%s: %s" % (_LABELS.get(code, "constraint failed"), detail)) from exc
            raise
        if safe:
            self.raw.cursor().execute("RELEASE SAVEPOINT dos_stmt")
        return cur

    def _id_tables(self):
        key = self.schema or "public"
        if key not in _IDTABLES:
            cur = self._exec("SELECT table_name FROM information_schema.columns WHERE table_schema = %s AND column_name = 'id'", (key,))
            _IDTABLES[key] = {r[0] for r in cur.fetchall()}
            self.raw.commit()
        return _IDTABLES[key]

    def execute(self, sql, params=()):
        stripped = sql.lstrip()
        head = stripped[:6].upper()
        if head == "PRAGMA":
            return Cursor()
        params = [_param(p) for p in (params or ())]
        pg = translate(sql)
        returning = False
        if head == "INSERT" and " RETURNING " not in pg.upper():
            m = re.match(r"INSERT\s+INTO\s+([A-Za-z_][\w]*)", pg, flags=re.I)
            if m and m.group(1).lower() in self._id_tables():
                pg += " RETURNING id"
                returning = True
        cur = self._exec(pg, params, safe=head in ("INSERT", "UPDATE", "DELETE"))
        if head in ("INSERT", "UPDATE", "DELETE") or re.match(r"BEGIN", stripped, flags=re.I):
            self.in_transaction = True
        names = [d[0] for d in cur.description] if cur.description else None
        if names is not None:
            rows = [Row(names, tuple(_plain(v) for v in r)) for r in cur.fetchall()]
            if returning:
                return Cursor(None, [], cur.rowcount, rows[0][0] if rows else None)
            return Cursor(names, rows, cur.rowcount)
        return Cursor(None, [], cur.rowcount)

    def executemany(self, sql, seq):
        total = 0
        for params in seq:
            total += max(self.execute(sql, params).rowcount, 0)
        return Cursor(None, [], total)

    def executescript(self, script):
        """Run a schema script. A table that points at one not made yet waits for a second pass."""
        pending = [translate_ddl(s) for s in split_script(script)]
        for fn in FUNCTIONS:
            self._exec(fn)
        try:
            self._exec("CREATE COLLATION nocase (provider = icu, locale = 'und-u-ks-level2', deterministic = false)")
        except Exception as exc:                                    # noqa: BLE001 - already there is fine, anything else is not
            self.raw.rollback()
            if "already exists" not in str(exc):
                raise Error("This PostgreSQL has no ICU support, which the case-blind user names need: %s" % exc)
        self.raw.commit()
        for _ in range(8):
            failed = []
            for stmt in pending:
                try:
                    self._exec(stmt)
                    self.raw.commit()
                except Exception as exc:                       # noqa: BLE001 - driver errors differ; rolled back and retried below
                    self.raw.rollback()
                    failed.append((stmt, exc))
            if not failed:
                return
            if len(failed) == len(pending):
                raise Error("Could not create the tables. First error: %s: %s" % (failed[0][0][:80], failed[0][1]))
            pending = [f[0] for f in failed]

    def commit(self):
        self.raw.commit()
        self.in_transaction = False

    def rollback(self):
        self.raw.rollback()
        self.in_transaction = False

    def close(self):
        try:
            self.raw.rollback()
        finally:
            self.raw.close()

    def table_columns(self, table):
        cur = self._exec("SELECT column_name FROM information_schema.columns WHERE table_schema = COALESCE(%s, 'public') AND table_name = %s",
                         (self.schema, table))
        cols = {r[0] for r in cur.fetchall()}
        self.raw.commit()
        return cols


def connect(url, schema=None):
    return Connection(open_raw(url, schema), schema)
