from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import ApiToken, User
from app.security import decode_token, hash_api_token, is_api_token

bearer_scheme = HTTPBearer(auto_error=False)

# Methods a read-only token may use. Everything the library exposes for
# reading is a GET; the two others are what a client sends around one.
_READ_METHODS = {"GET", "HEAD", "OPTIONS"}

# last_used_at is a courtesy for the token list, not an audit log, so it is
# written at most this often per token rather than on every request.
_LAST_USED_EVERY = timedelta(minutes=5)


async def _user_from_api_token(
    request: Request, bearer: str, db: AsyncSession
) -> User:
    """Resolve an API token to its creator. A plain 401 for anything that is
    not a live token; the detail is what a client will show, so it says what
    happened without saying which."""
    row = (
        await db.execute(select(ApiToken).where(ApiToken.token_hash == hash_api_token(bearer)))
    ).scalar_one_or_none()
    if row is None or row.revoked_at is not None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or revoked API token")
    user = await db.get(User, row.user_id)
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or revoked API token")
    if row.read_only and request.method not in _READ_METHODS:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "This API token is read-only."
        )
    now = datetime.now(timezone.utc)
    if row.last_used_at is None or now - row.last_used_at > _LAST_USED_EVERY:
        row.last_used_at = now
    request.state.api_token = row
    return user


async def get_current_user(
    request: Request,
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Depends(bearer_scheme)
    ],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> User:
    if credentials is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")
    bearer = credentials.credentials
    # The prefix decides which path a bearer takes; the JWT check below is
    # exactly what it was. A token made under Settings answered the one-time
    # code when it was made, so it is not asked again here.
    if is_api_token(bearer):
        return await _user_from_api_token(request, bearer, db)
    decoded = decode_token(bearer, "access")
    if decoded is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired token")
    user_id, token_version, _jti = decoded
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "User not found")
    if token_version != user.token_version:
        # Token predates a password change — force a fresh sign-in.
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Session expired")
    request.state.api_token = None
    return user


async def get_session_user(
    request: Request, user: Annotated[User, Depends(get_current_user)]
) -> User:
    """A person signed in, not a program holding a token.

    Managing tokens is the one thing a token must not be able to do: a
    leaked one could otherwise mint itself successors and revoke the others,
    and the person would learn of it only by being locked out."""
    if getattr(request.state, "api_token", None) is not None:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "Sign in to manage API tokens."
        )
    return user


async def get_admin_user(
    user: Annotated[User, Depends(get_current_user)],
) -> User:
    """Owner-only operations: managing accounts, changing settings that affect
    the whole box, and running imports/exports. Without this every account in
    the tenant could create co-owners or delete the owner, and any user could
    pause processing or lower the archive-DPI cap for everyone."""
    if not user.is_admin:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "This action is limited to the library owner."
        )
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]
SessionUser = Annotated[User, Depends(get_session_user)]
AdminUser = Annotated[User, Depends(get_admin_user)]
DB = Annotated[AsyncSession, Depends(get_db)]
