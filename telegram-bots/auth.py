"""Password hashing compatible with the Node dashboard (`src/lib/auth.ts`).

Node uses `crypto.scryptSync(plain, saltHexString, 64)` with the default
parameters N=16384, r=8, p=1. Critically, the salt is the *hex string itself*
(its UTF-8 bytes), NOT the decoded 16 raw bytes. We mirror that exactly so a
credential minted here logs in on the dashboard.
"""
from __future__ import annotations

import hashlib
import secrets

_KEYLEN = 64
_N = 16384
_R = 8
_P = 1
_MAXMEM = 64 * 1024 * 1024


def _scrypt(plain: str, salt_hex: str) -> str:
    return hashlib.scrypt(
        plain.encode("utf-8"),
        salt=salt_hex.encode("utf-8"),
        n=_N,
        r=_R,
        p=_P,
        dklen=_KEYLEN,
        maxmem=_MAXMEM,
    ).hex()


def hash_password(plain: str) -> tuple[str, str]:
    """Return (hash_hex, salt_hex) — same shape as Node's hashPassword."""
    salt_hex = secrets.token_hex(16)
    return _scrypt(plain, salt_hex), salt_hex


def verify_password(plain: str, hash_hex: str, salt_hex: str) -> bool:
    try:
        return secrets.compare_digest(_scrypt(plain, salt_hex), hash_hex)
    except Exception:
        return False
