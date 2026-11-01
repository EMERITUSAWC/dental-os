"""A tiny PostgreSQL client for TESTING ONLY. Do not use it in production.

Why it exists: the product talks to PostgreSQL through psycopg (or pg8000) when you install one of them. Where this was built, no
driver could be installed, but a PostgreSQL server could be run. This file speaks just enough of the server's simple-query
protocol (trust or cleartext password sign-in, text results) to run the product's whole test suite against a real server, so the
SQL itself is proven on PostgreSQL. It offers the small slice of the Python DB-API the product uses: connect, cursor, execute with
%s placeholders, fetchall, fetchone, description, rowcount, commit, rollback, close.

Values are put into the SQL text here, with strict quoting (standard_conforming_strings is on), because the simple-query protocol
has no placeholders. A real driver sends them separately. The product code never builds SQL with values in it, so this is only
test plumbing."""
import datetime
import decimal
import re
import socket
import struct

OID_BOOL, OID_BYTEA, OID_INT8, OID_INT2, OID_INT4 = 16, 17, 20, 21, 23
OID_FLOAT4, OID_FLOAT8, OID_NUMERIC = 700, 701, 1700


class Error(Exception):
    pass


class IntegrityError(Error):
    pass


class OperationalError(Error):
    pass


def quote(v):
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return repr(v)
    if isinstance(v, decimal.Decimal):
        return str(v)
    if isinstance(v, (bytes, bytearray, memoryview)):
        return "'\\x%s'::bytea" % bytes(v).hex()
    if isinstance(v, (datetime.date, datetime.datetime)):
        v = v.isoformat(" ")
    s = str(v)
    if "\x00" in s:
        raise Error("NUL byte in a text value")
    return "'" + s.replace("'", "''") + "'"


def interpolate(sql, params):
    """Replace %s with quoted values and %% with %, anywhere in the text (the way psycopg and pg8000 do)."""
    params = list(params or [])
    out, i, n = [], 0, len(sql)
    while i < n:
        c = sql[i]
        if c == "%" and i + 1 < n and sql[i + 1] == "s":
            if not params:
                raise Error("too few parameters for: " + sql[:120])
            out.append(quote(params.pop(0)))
            i += 1
        elif c == "%" and i + 1 < n and sql[i + 1] == "%":
            out.append("%")
            i += 1
        elif c == "%":
            raise Error("a lone % in: " + sql[:120])
        else:
            out.append(c)
        i += 1
    if params:
        raise Error("too many parameters for: " + sql[:120])
    return "".join(out)


def _convert(oid, raw):
    if raw is None:
        return None
    text = raw.decode("utf-8")
    if oid in (OID_INT2, OID_INT4, OID_INT8):
        return int(text)
    if oid in (OID_FLOAT4, OID_FLOAT8):
        return float(text)
    if oid == OID_NUMERIC:
        return decimal.Decimal(text)
    if oid == OID_BOOL:
        return text == "t"
    if oid == OID_BYTEA:
        return bytes.fromhex(text[2:]) if text.startswith("\\x") else text.encode()
    return text


def _type_of(v):
    """The type a real driver would send a Python value as (psycopg 3 picks the smallest whole-number type that fits)."""
    if v is None or isinstance(v, str):
        return ""                                        # sent as 'unknown': the server works the type out from the SQL
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, int):
        return "int2" if -32768 <= v <= 32767 else "int4" if -2**31 <= v < 2**31 else "int8" if -2**63 <= v < 2**63 else "numeric"
    if isinstance(v, float):
        return "float8"
    if isinstance(v, decimal.Decimal):
        return "numeric"
    if isinstance(v, (bytes, bytearray, memoryview)):
        return "bytea"
    return ""


def prepared(sql, params):
    """Turn %s into $1, $2 and run it as PREPARE / EXECUTE, so the server checks parameter types the way it does for a real driver
    (a bare NULL in 'SELECT %s' or a number compared with text fails here, as it would there)."""
    params = list(params or [])
    if not params:
        return interpolate(sql, ())
    n = [0]

    def num(m):
        if m.group(0) == "%%":
            return "%"
        n[0] += 1
        return "$%d" % n[0]

    body = re.sub(r"%[s%]", num, sql)
    if n[0] != len(params):
        raise Error("wrong number of parameters for: " + sql[:120])
    types = ", ".join(_type_of(p) or "unknown" for p in params)
    return "DEALLOCATE ALL; PREPARE dos_p(%s) AS %s; EXECUTE dos_p(%s); DEALLOCATE dos_p" % (types, body, ", ".join(quote(p) for p in params))


class Cursor:
    def __init__(self, conn):
        self.conn = conn
        self.description = None
        self.rowcount = -1
        self._rows = []
        self._pos = 0

    def execute(self, sql, params=None):
        self.description, self._rows, self._pos = None, [], 0
        res = self.conn._run(prepared(sql, params))
        self.description, self._rows, self.rowcount = res
        return self

    def fetchone(self):
        if self._pos >= len(self._rows):
            return None
        row = self._rows[self._pos]
        self._pos += 1
        return row

    def fetchall(self):
        rows, self._pos = self._rows[self._pos:], len(self._rows)
        return rows

    def close(self):
        pass


class Connection:
    def __init__(self, host, port, user, database, password=None):
        self.sock = socket.create_connection((host, port), timeout=60)
        self.in_tx = False
        params = b"user\0" + user.encode() + b"\0database\0" + database.encode() + b"\0client_encoding\0UTF8\0\0"
        body = struct.pack("!i", 196608) + params
        self.sock.sendall(struct.pack("!i", len(body) + 4) + body)
        while True:
            kind, data = self._read()
            if kind == b"R":
                code = struct.unpack("!i", data[:4])[0]
                if code == 3:
                    pw = (password or "").encode() + b"\0"
                    self.sock.sendall(b"p" + struct.pack("!i", len(pw) + 4) + pw)
                elif code != 0:
                    raise OperationalError("this test client only supports trust and cleartext sign-in (server asked for %d)" % code)
            elif kind == b"E":
                raise OperationalError(self._error(data))
            elif kind == b"Z":
                break

    def _read(self):
        head = self._recv(5)
        kind, length = head[:1], struct.unpack("!i", head[1:])[0]
        return kind, self._recv(length - 4)

    def _recv(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise OperationalError("the server closed the connection")
            buf += chunk
        return buf

    @staticmethod
    def _error(data):
        fields = {}
        for part in data.split(b"\0"):
            if part:
                fields[part[:1].decode()] = part[1:].decode("utf-8", "replace")
        return fields

    def _run(self, sql):
        if not self.in_tx and sql.strip().upper() not in ("COMMIT", "ROLLBACK"):
            self._simple("BEGIN")
            self.in_tx = True
        return self._simple(sql)

    def _simple(self, sql):
        q = sql.encode("utf-8") + b"\0"
        self.sock.sendall(b"Q" + struct.pack("!i", len(q) + 4) + q)
        desc, rows, count, err = None, [], -1, None
        oids = []
        while True:
            kind, data = self._read()
            if kind == b"T":
                n = struct.unpack("!h", data[:2])[0]
                pos, desc, oids = 2, [], []
                for _ in range(n):
                    end = data.index(b"\0", pos)
                    name = data[pos:end].decode()
                    oid = struct.unpack("!i", data[end + 7:end + 11])[0]
                    desc.append((name, oid))
                    oids.append(oid)
                    pos = end + 19
            elif kind == b"D":
                n = struct.unpack("!h", data[:2])[0]
                pos, row = 2, []
                for i in range(n):
                    ln = struct.unpack("!i", data[pos:pos + 4])[0]
                    pos += 4
                    if ln == -1:
                        row.append(None)
                    else:
                        row.append(_convert(oids[i], data[pos:pos + ln]))
                        pos += ln
                rows.append(tuple(row))
            elif kind == b"C":
                tag = data.rstrip(b"\0").decode().split()
                if tag and tag[-1].isdigit():
                    count = int(tag[-1])
            elif kind == b"E":
                err = self._error(data)
            elif kind == b"Z":
                break
        if err:
            self.in_tx = False if sql.strip().upper() in ("COMMIT", "ROLLBACK") else self.in_tx
            exc = IntegrityError if err.get("C", "").startswith("23") else Error
            e = exc(err.get("M", "error"))
            e.sqlstate = err.get("C")
            e.detail = err.get("D")
            raise e
        return ([(d[0],) for d in desc] if desc else None), rows, count

    def cursor(self):
        return Cursor(self)

    def commit(self):
        if self.in_tx:
            self._simple("COMMIT")
            self.in_tx = False

    def rollback(self):
        if self.in_tx:
            try:
                self._simple("ROLLBACK")
            finally:
                self.in_tx = False

    def close(self):
        try:
            self.sock.sendall(b"X" + struct.pack("!i", 4))
        except OSError:
            pass
        self.sock.close()


def connect(host="127.0.0.1", port=5432, user="postgres", database="postgres", password=None):
    return Connection(host, port, user, database, password)
