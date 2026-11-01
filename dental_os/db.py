import sqlite3
from pathlib import Path

from flask import current_app, g

SCHEMA = Path(__file__).with_name("schema.sql")


def connect(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if str(path) != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    return conn


def ensure_column(conn, table, column, ddl):
    """Add a column to an existing table if it is not there yet. Lets older databases pick up new fields."""
    cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


# New columns on tables that already existed in an earlier version: (table, column, definition).
COLUMN_UPGRADES = [
    ("users", "must_change_password", "INTEGER NOT NULL DEFAULT 0"),
    ("payments", "purpose", "TEXT NOT NULL DEFAULT 'treatment'"),       # 'membership' fees stay out of the treatment balance
    ("payments", "plan_id", "INTEGER"),                                  # a deposit held against a treatment plan
    ("payments", "claim_id", "INTEGER"),                                 # a payment from a payer, matched to a claim
    ("clinics", "tax_name", "TEXT"),                                     # for example VAT, GST, NHIL. Empty means no tax on invoices
    ("clinics", "tax_rate_bp", "INTEGER NOT NULL DEFAULT 0"),            # basis points: 1500 = 15%
    ("clinics", "tax_inclusive", "INTEGER NOT NULL DEFAULT 1"),          # 1: prices already include tax
    ("clinics", "tax_number", "TEXT"),
    ("clinics", "invoice_footer", "TEXT"),                               # for example bank details
    ("messages", "read_at", "TEXT"),                                     # for replies from patients: when staff read them
    ("clinics", "org_id", "INTEGER"),                                    # the group this clinic belongs to, if any
    ("procedure_codes", "tax_rate_id", "INTEGER"),                       # empty: the clinic's default tax rate applies
    ("users", "totp_secret", "TEXT"),                                    # encrypted. Empty until two-step login is being set up
    ("users", "totp_enabled", "INTEGER NOT NULL DEFAULT 0"),
    ("users", "totp_last_step", "INTEGER NOT NULL DEFAULT 0"),           # the last 30-second step used, so a code works once
    ("clinics", "require_2fa", "INTEGER NOT NULL DEFAULT 0"),            # 1: every staff member must use two-step login
    ("patients", "anonymised_on", "TEXT"),                               # set when a manager removed the person's identity details
    ("clinics", "language", "TEXT NOT NULL DEFAULT 'en'"),               # language of menus and patient documents: en or fr
    ("users", "language", "TEXT"),                                       # a member of staff's own choice. Empty means the clinic's
    ("patients", "language", "TEXT"),                                    # the language for this patient's messages and documents
    ("clinics", "retention_years", "INTEGER NOT NULL DEFAULT 10"),       # a starting value only. The clinic's lawyer decides the real period
    ("invoice_lines", "tax_name", "TEXT"),
    ("invoice_lines", "tax_rate_bp", "INTEGER NOT NULL DEFAULT 0"),
]


def init_schema(conn):
    conn.executescript(SCHEMA.read_text())
    for table, column, ddl in COLUMN_UPGRADES:
        ensure_column(conn, table, column, ddl)
    conn.commit()


def get_db():
    if "db" not in g:
        g.db = connect(current_app.config["DATABASE"])
    return g.db


def close_db(exc=None):
    conn = g.pop("db", None)
    if conn is not None:
        if exc is not None:
            conn.rollback()
        conn.close()
