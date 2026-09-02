import uuid
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt

from app.config import settings

ALGORITHM = "HS256"


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), password_hash.encode())
    except ValueError:
        return False


def _mint(
    user_id: uuid.UUID,
    token_type: str,
    lifetime: timedelta,
    version: int,
    jti: uuid.UUID | None = None,
) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "type": token_type,
        "ver": version,
        "iat": now,
        "exp": now + lifetime,
    }
    if jti is not None:
        payload["jti"] = str(jti)
    return jwt.encode(payload, settings.secret_key, algorithm=ALGORITHM)


def mint_access_token(user_id: uuid.UUID, version: int = 0) -> str:
    return _mint(
        user_id, "access", timedelta(minutes=settings.access_token_minutes), version
    )


def mint_refresh_token(
    user_id: uuid.UUID, version: int = 0, jti: uuid.UUID | None = None
) -> str:
    """`jti` names the refresh_tokens row this token stands for, so using it
    can retire it. Tokens from before rotation carry none."""
    return _mint(
        user_id, "refresh", timedelta(days=settings.refresh_token_days), version, jti
    )


def decode_token(
    token: str, expected_type: str
) -> tuple[uuid.UUID, int, uuid.UUID | None] | None:
    """Returns (user_id, token_version, jti) — the caller compares the version
    to the user row so a password change invalidates older tokens. Tokens
    minted before versioning carry no "ver" and read as version 0; access
    tokens, and refresh tokens from before rotation, carry no jti."""
    try:
        payload = jwt.decode(
            token,
            settings.secret_key,
            algorithms=[ALGORITHM],
            # exp is only verified when present by default: a token minted
            # without it would never expire.
            options={"require": ["exp", "sub", "type"]},
        )
    except jwt.PyJWTError:
        return None
    if payload.get("type") != expected_type:
        return None
    try:
        raw_jti = payload.get("jti")
        jti = uuid.UUID(str(raw_jti)) if raw_jti else None
        return uuid.UUID(payload["sub"]), int(payload.get("ver", 0)), jti
    except (KeyError, ValueError):
        return None
