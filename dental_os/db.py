import atexit
import hashlib
import os
import sqlite3
from pathlib import Path

from flask import current_app, g

from . import dbpg

SCHEMA = Path(__file__).with_name("schema.sql")
_TEST_SCHEMAS = set()


def is_postgres(path):
    return dbpg.is_url(path) or bool(os.environ.get("DENTAL_OS_TEST_PG") and str(path) != ":memory:")


def _drop_test_schemas():
    url = os.environ.get("DENTAL_OS_TEST_PG")
    if not url or not _TEST_SCHEMAS:
        return
    try:
        raw = dbpg.open_raw(url)
        cur = raw.cursor()
        for name in list(_TEST_SCHEMAS):
            cur.execute('DROP SCHEMA IF EXISTS "%s" CASCADE' % name)
        raw.commit()
        raw.close()
    except Exception:                                      # noqa: BLE001 - cleaning up a test server must never fail a test run
        pass


atexit.register(_drop_test_schemas)


def connect(path):
    test_url = os.environ.get("DENTAL_OS_TEST_PG")
    if dbpg.is_url(path):
        return dbpg.connect(str(path))
    if test_url and str(path) != ":memory:":
        # Test mode: every database file path gets its own schema on one PostgreSQL server, so the whole suite runs unchanged.
        schema = "t_" + hashlib.md5(str(path).encode()).hexdigest()[:20]
        if schema not in _TEST_SCHEMAS:
            raw = dbpg.open_raw(test_url)
            raw.cursor().execute('CREATE SCHEMA IF NOT EXISTS "%s"' % schema)
            raw.commit()
            raw.close()
            _TEST_SCHEMAS.add(schema)
        return dbpg.connect(test_url, schema)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if str(path) != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    return conn


def ensure_column(conn, table, column, ddl):
    """Add a column to an existing table if it is not there yet. Lets older databases pick up new fields."""
    if isinstance(conn, dbpg.Connection):
        cols = conn.table_columns(table)
        ddl = dbpg.translate_ddl(ddl)
    else:
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
    ("messages", "provider_id", "TEXT"),                                 # WhatsApp's own number for a message sent through the API (the wamid)
    ("messages", "error", "TEXT"),                                       # why WhatsApp could not deliver it
    ("clinics", "voice_perio", "INTEGER NOT NULL DEFAULT 0"),            # 1: the gum chart screen offers hands-free entry by voice
    ("clinics", "voice_cloud", "INTEGER NOT NULL DEFAULT 0"),
    ("clinics", "voice_notes", "INTEGER NOT NULL DEFAULT 0"),
    ("claims", "edi_ack", "TEXT"),
    ("api_keys", "fhir", "INTEGER NOT NULL DEFAULT 0"),                  # 0.64: 1 lets the key read clinical records through FHIR                                       # 0.63: the latest acknowledgement from the clearinghouse or payer            # 0.61: dictation in clinical notes, on the device only            # 1: voice may use the browser maker's online speech service when the device cannot listen itself
    ("clinics", "tooth_notation", "TEXT NOT NULL DEFAULT 'fdi'"),        # how teeth are shown and typed: fdi, universal or palmer. Stored as FDI
    ("appointments", "type_id", "INTEGER"),                              # the appointment type it was booked as, if any
    ("appointments", "arrived_at", "TEXT"),                              # checked in at the desk, clinic time
    ("appointments", "seated_at", "TEXT"),                               # taken into the surgery
    ("appointments", "dismissed_at", "TEXT"),                            # left the surgery
    ("clinics", "address", "TEXT"),                                      # printed on letters and prescriptions
    ("clinics", "email", "TEXT"),
    ("clinics", "bp_amber", "TEXT NOT NULL DEFAULT '160/100'"),          # blood pressure at or above this: defer planned treatment and refer
    ("clinics", "bp_red", "TEXT NOT NULL DEFAULT '180/110'"),            # at or above this: no treatment without a doctor's advice
    ("providers", "reg_body", "TEXT"),                                   # for example GDC, Medical and Dental Council
    ("providers", "reg_number", "TEXT"),
    ("providers", "qualifications", "TEXT"),
    ("patients", "referrer_id", "INTEGER"),                              # who sent this patient to us
    ("patients", "referral_source", "TEXT"),                             # how they heard of us, when no referrer is named
    ("patient_cover", "cover_rank", "INTEGER NOT NULL DEFAULT 1"),            # 1: the main cover. 2: a second payer, who pays after the first
    ("patient_cover", "deductible_p", "INTEGER NOT NULL DEFAULT 0"),     # the excess the patient pays first each benefit year
    ("patient_cover", "deductible_on_preventive", "INTEGER NOT NULL DEFAULT 0"),  # 0: check-ups, cleaning and X-rays skip the excess
    ("patient_cover", "year_start_month", "INTEGER NOT NULL DEFAULT 1"), # the month the benefit year starts, 1 = January
    ("patient_cover", "ortho_max_p", "INTEGER"),                         # a lifetime limit for orthodontics, if the payer has one
    ("patient_cover", "waiting_from", "TEXT"),                           # waiting periods count from this date (default: the cover start)
    ("patient_cover", "verified_on", "TEXT"),                            # when someone last checked the cover with the payer
    ("patient_cover", "verified_by", "INTEGER"),
    ("patient_cover", "verify_note", "TEXT"),
    ("claim_items", "deductible_p", "INTEGER NOT NULL DEFAULT 0"),       # the part of this line the patient pays as excess
    ("claim_items", "note", "TEXT"),                                     # why the payer is expected to pay less, for example a waiting period
    ("payments", "payer_payment_id", "INTEGER"),                         # the payer's payment this was part of
    ("clinics", "export_account", "TEXT"),                               # sales account code for the accountant's invoice export
    ("clinics", "export_tax_type", "TEXT"),                              # the tax type name used in the clinic's accounting package
    ("procedure_codes", "cdt_code", "TEXT"),                             # the clinic's own CDT code for US claims, from its CDT licence
    ("providers", "npi", "TEXT"),                                        # US National Provider Identifier, for claims
    ("payers", "edi_payer_id", "TEXT"),                                  # the payer's ID at the clinic's clearinghouse
    ("patient_cover", "relationship", "TEXT"),                           # who holds the policy: self, spouse, child, other
    ("patient_cover", "holder_first", "TEXT"),
    ("patient_cover", "holder_last", "TEXT"),
    ("patient_cover", "holder_dob", "TEXT"),
    ("patient_cover", "holder_gender", "TEXT"),
    ("patient_cover", "group_no", "TEXT"),
    ("patient_cover", "holder_address", "TEXT"),
    ("patient_cover", "holder_city", "TEXT"),
    ("patient_cover", "holder_state", "TEXT"),
    ("patient_cover", "holder_zip", "TEXT"),
    ("providers", "nhs_performer_no", "TEXT"),                           # England: the dentist's NHS performer number
    ("procedure_codes", "nhs_class", "TEXT"),                            # England: which NHS band this procedure belongs to, empty if not NHS work
    ("users", "perm_group_id", "INTEGER"),                                # a permission group that narrows what the role allows
    ("pay_accounts", "allow_save", "INTEGER NOT NULL DEFAULT 0"),        # 1: patients may choose to keep their card on file
    ("pay_links", "save_card", "INTEGER NOT NULL DEFAULT 0"),            # the patient asked to keep the card used on this link
    ("pay_links", "auto_ok", "INTEGER NOT NULL DEFAULT 0"),              # and to have monthly fees charged to it
    ("pay_links", "consent_text", "TEXT"),
    ("pay_links", "consent_at", "TEXT"),
    ("membership_charges", "auto_tried", "INTEGER NOT NULL DEFAULT 0"),  # the saved card was tried for this fee
    ("patients", "prefers_sms", "INTEGER NOT NULL DEFAULT 0"),           # 1: send messages by SMS rather than WhatsApp
    ("ortho_charges", "auto_tried", "INTEGER NOT NULL DEFAULT 0"),       # the saved card was tried for this ortho amount
    ("patients", "fee_schedule_id", "INTEGER"),                          # empty: the standard prices
    ("patients", "merged_into", "INTEGER"),                              # set when this record was a duplicate, merged into that one
    ("procedures", "dx_code", "TEXT"),                                   # 0.52: the ICD-10 diagnosis, for claims that ask for one
    ("claim_items", "dx_code", "TEXT"),                                  # the diagnosis as sent on the claim
    ("claims", "batch_id", "INTEGER"),                                   # the monthly batch it was sent in
    ("payers", "needs_dx", "INTEGER NOT NULL DEFAULT 0"),                # 1: every claim line must carry a diagnosis
    ("clinics", "plan_state", "TEXT NOT NULL DEFAULT 'active'"),         # 0.53: trial, active, past_due, suspended, cancelled
    ("clinics", "trial_ends_on", "TEXT"),
    ("clinics", "state_reason", "TEXT"),                                 # why the service paused or closed the clinic, shown to its manager
    ("clinics", "plan_id", "INTEGER"),                                   # 0.54: the plan it pays for
    ("clinics", "paid_until", "TEXT"),                                   # paid up to this date; the next bill starts here
]


def init_schema(conn):
    conn.executescript(SCHEMA.read_text())
    for table, column, ddl in COLUMN_UPGRADES:
        ensure_column(conn, table, column, ddl)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_provider ON messages(clinic_id, provider_id)")
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
