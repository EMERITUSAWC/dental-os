"""Questions about the database's own shape, answered for SQLite or for PostgreSQL (when the suite runs with DENTAL_OS_TEST_PG)."""
import os
import unittest

from dental_os import dbpg

ON_PG = bool(os.environ.get("DENTAL_OS_TEST_PG"))
sqlite_only = unittest.skipIf(ON_PG, "builds an old SQLite file by hand: not meaningful on PostgreSQL")


def _pg(conn):
    return isinstance(conn, dbpg.Connection)


def tables(conn):
    if _pg(conn):
        rows = conn.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = COALESCE(current_schema(), 'public') "
                            "AND table_type = 'BASE TABLE'").fetchall()
        return sorted(r[0] for r in rows)
    return sorted(r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'").fetchall())


def has_table(conn, name):
    return name in tables(conn)


def column_types(conn, table):
    """{column: upper-case declared type}. PostgreSQL's bigint stands in for SQLite's INTEGER, and text for TEXT."""
    if _pg(conn):
        rows = conn.execute("SELECT column_name, data_type FROM information_schema.columns WHERE table_schema = COALESCE(current_schema(), 'public') "
                            "AND table_name = ?", (table,)).fetchall()
        kinds = {"bigint": "INTEGER", "integer": "INTEGER", "smallint": "INTEGER", "text": "TEXT", "character varying": "TEXT"}
        return {r[0]: kinds.get(r[1], r[1].upper()) for r in rows}
    return {r[1]: (r[2] or "").upper() for r in conn.execute(f"PRAGMA table_info({table})")}


def columns(conn, table):
    return set(column_types(conn, table))


def foreign_targets(conn, table):
    if _pg(conn):
        rows = conn.execute("SELECT c.confrelid::regclass::text FROM pg_constraint c WHERE c.contype = 'f' AND c.conrelid = ?::regclass", (table,)).fetchall()
        return {r[0].split(".")[-1].strip('"') for r in rows}
    return {r[2] for r in conn.execute(f"PRAGMA foreign_key_list({table})")}


def drop_table(conn, table):
    conn.execute(f"DROP TABLE {table}" + (" CASCADE" if _pg(conn) else ""))
