"""Keys that let another program use one clinic's data. The key is shown once. Only a hash is kept, so a stolen database
does not hand out working keys. Keys are long random strings, so a plain SHA-256 is enough to store them."""
import hashlib
import secrets

from . import audit

PREFIX = "dos_"


class ApiKeyError(Exception):
    pass


def _hash(key):
    return hashlib.sha256(key.encode()).hexdigest()


def create(db, clinic, user_id, name, access):
    name = (name or "").strip()
    if not name or len(name) > 60:
        raise ApiKeyError("Give the key a name of up to 60 characters, for example 'Accounts export'.")
    if access not in ("read", "write", "fhir"):
        raise ApiKeyError("Pick read only or read and write.")
    fhir = 1 if access == "fhir" else 0                  # 0.64: a FHIR key reads clinical records and can change nothing
    key = PREFIX + secrets.token_urlsafe(32)
    cur = db.execute("INSERT INTO api_keys(clinic_id, name, key_prefix, key_hash, access, fhir, created_by) VALUES (?,?,?,?,?,?,?)",
                     (clinic["id"], name, key[:10], _hash(key), "read" if fhir else access, fhir, user_id))
    audit.log(db, clinic["id"], user_id, "apikey.create", "api_key", cur.lastrowid, access)
    return cur.lastrowid, key


def revoke(db, clinic, user_id, key_id):
    cur = db.execute("UPDATE api_keys SET revoked = 1 WHERE id = ? AND clinic_id = ?", (key_id, clinic["id"]))
    if not cur.rowcount:
        raise LookupError("key")
    audit.log(db, clinic["id"], user_id, "apikey.revoke", "api_key", key_id)


def list_keys(db, cid):
    return db.execute("SELECT * FROM api_keys WHERE clinic_id = ? ORDER BY revoked, id DESC", (cid,)).fetchall()


def authenticate(db, header):
    """The key row for an 'Authorization: Bearer ...' header, or None."""
    if not header or not header.lower().startswith("bearer "):
        return None
    key = header[7:].strip()
    if not key.startswith(PREFIX) or len(key) > 200:
        return None
    row = db.execute("SELECT * FROM api_keys WHERE key_hash = ? AND revoked = 0", (_hash(key),)).fetchone()
    if row and (row["last_used_at"] is None or db.execute("SELECT datetime('now', '-1 minutes') > ?", (row["last_used_at"],)).fetchone()[0]):
        db.execute("UPDATE api_keys SET last_used_at = datetime('now') WHERE id = ?", (row["id"],))
    return row
