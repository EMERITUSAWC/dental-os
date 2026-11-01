"""One check for every "go back to" address that comes from a form or link.

Only a path on this same site is allowed. Browsers read a backslash as a
forward slash, so "/\\evil.example" would leave the site; that and any
control character are refused as well as "//", schemes and hosts.
"""
from urllib.parse import urlparse


def local_path(target):
    if not target or not isinstance(target, str):
        return None
    if not target.startswith("/") or target.startswith("//"):
        return None
    if "\\" in target or any(ord(c) < 32 or ord(c) == 127 for c in target):
        return None
    parts = urlparse(target)
    if parts.scheme or parts.netloc:
        return None
    return target
