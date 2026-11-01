"""Move a SQLite database into PostgreSQL, once, when a clinic or a group outgrows SQLite.

    flask copy-to-postgres --to postgresql://user:password@host:5432/dental_os

It reads the SQLite file the app is set to use, makes the tables in the PostgreSQL database (which must be empty), copies every row
with its original id, sets the id counters so new rows carry on after the copied ones, and then counts rows in every table on both
sides. Nothing is changed in the SQLite file. Stop the app while you copy, then start it again with DATABASE_URL set.

Tables that point at themselves (a patient's guarantor, a lab remake) are copied in two passes so the order of rows cannot matter."""
import sqlite3

from .. import db as dbmod, dbpg


class CopyError(Exception):
    pass


def _order(src):
    """Table names with the ones that others point at first."""
    names = [r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'").fetchall()]
    deps = {n: {r[2] for r in src.execute(f"PRAGMA foreign_key_list({n})") if r[2] != n and r[2] in names} for n in names}
    out, left = [], set(names)
    while left:
        ready = sorted(n for n in left if not (deps[n] & left))
        if not ready:
            raise CopyError("The tables point at each other in a circle: %s" % ", ".join(sorted(left)))
        out.extend(ready)
        left -= set(ready)
    return out


def copy(src, dst, log=lambda msg: None):
    """src: a sqlite3 connection. dst: a dbpg.Connection with nothing in it yet. Returns {table: rows copied}."""
    if not isinstance(dst, dbpg.Connection):
        raise CopyError("The target must be a PostgreSQL connection.")
    dbmod.init_schema(dst)
    if dst.execute("SELECT COUNT(*) FROM clinics").fetchone()[0]:
        raise CopyError("The PostgreSQL database already has clinics in it. Use an empty database.")
    src.row_factory = sqlite3.Row
    counts = {}
    for table in _order(src):
        cols = [r[1] for r in src.execute(f"PRAGMA table_info({table})")]
        dst_cols = dst.table_columns(table)
        missing = [c for c in cols if c not in dst_cols]
        if missing:
            raise CopyError("%s has columns PostgreSQL does not: %s" % (table, ", ".join(missing)))
        selfref = {r[3] for r in src.execute(f"PRAGMA foreign_key_list({table})") if r[2] == table}
        rows = src.execute(f"SELECT {', '.join(cols)} FROM {table}").fetchall()
        marks = ", ".join("?" for _ in cols)
        insert = f"INSERT INTO {table}({', '.join(cols)}) VALUES ({marks})"
        later = []
        for row in rows:
            values = list(row)
            if selfref:
                held = {c: values[cols.index(c)] for c in selfref if values[cols.index(c)] is not None}
                for c in held:
                    values[cols.index(c)] = None
                if held:
                    later.append((row["id"], held))
            dst.execute(insert, values)
        for rid, held in later:
            for c, v in held.items():
                dst.execute(f"UPDATE {table} SET {c} = ? WHERE id = ?", (v, rid))
        if "id" in cols:
            dst.execute("SELECT setval(pg_get_serial_sequence(?, 'id'), COALESCE((SELECT MAX(id) FROM %s), 1), (SELECT MAX(id) FROM %s) IS NOT NULL)"
                        % (table, table), (table,))
        counts[table] = len(rows)
        log(f"{table}: {len(rows)}")
    dst.commit()
    for table, n in counts.items():
        got = dst.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        if got != n:
            raise CopyError("%s: copied %d rows but PostgreSQL holds %d" % (table, n, got))
    return counts
