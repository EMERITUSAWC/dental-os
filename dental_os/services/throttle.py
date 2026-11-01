"""Slows down abuse of the public pages. Keys (an address, a clinic) are hashed with the app secret, so no raw address is stored."""
import hashlib
import hmac


def key(secret, *parts):
    return hmac.new(secret.encode(), "|".join(str(p) for p in parts).encode(), hashlib.sha256).hexdigest()[:32]


def allow(db, bucket, limit, minutes):
    """True if fewer than `limit` hits happened in the last `minutes`. A permitted call is counted."""
    n = db.execute("SELECT COUNT(*) FROM throttle WHERE bucket = ? AND at > datetime('now', ?)", (bucket, f"-{minutes} minutes")).fetchone()[0]
    if n >= limit:
        return False
    db.execute("INSERT INTO throttle(bucket) VALUES (?)", (bucket,))
    if n == 0:
        db.execute("DELETE FROM throttle WHERE at < datetime('now', '-2 days')")
    return True


def count(db, bucket, minutes):
    return db.execute("SELECT COUNT(*) FROM throttle WHERE bucket = ? AND at > datetime('now', ?)", (bucket, f"-{minutes} minutes")).fetchone()[0]


def record(db, bucket):
    db.execute("INSERT INTO throttle(bucket) VALUES (?)", (bucket,))
