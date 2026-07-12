"""RFC 6238 TOTP, dependency-free: 6 digits, 30-second steps, SHA-1 —
compatible with every mainstream authenticator app."""

import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote


def new_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def otpauth_url(secret: str, account: str, issuer: str) -> str:
    return (
        f"otpauth://totp/{quote(issuer)}:{quote(account)}"
        f"?secret={secret}&issuer={quote(issuer)}&algorithm=SHA1&digits=6&period=30"
    )


def _code_at(secret: str, timestamp: float) -> str:
    key = base64.b32decode(secret + "=" * (-len(secret) % 8))
    message = struct.pack(">Q", int(timestamp) // 30)
    digest = hmac.new(key, message, hashlib.sha1).digest()
    offset = digest[19] & 0x0F
    value = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(value % 1_000_000).zfill(6)


def verify(secret: str, code: str, window: int = 1) -> bool:
    """Accept the current step ± `window` steps of clock drift."""
    cleaned = code.strip().replace(" ", "")
    if not cleaned.isdigit() or len(cleaned) != 6:
        return False
    now = time.time()
    return any(
        hmac.compare_digest(_code_at(secret, now + step * 30), cleaned)
        for step in range(-window, window + 1)
    )
