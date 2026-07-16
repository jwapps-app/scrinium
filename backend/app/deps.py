from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import User
from app.security import decode_token

bearer_scheme = HTTPBearer(auto_error=False)


def client_ip(request: Request) -> str:
    # Behind the Cloudflare Tunnel the socket peer is always Cloudflare.
    return request.headers.get("CF-Connecting-IP") or (
        request.client.host if request.client else ""
    )


async def get_current_user(
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Depends(bearer_scheme)
    ],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> User:
    if credentials is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")
    decoded = decode_token(credentials.credentials, "access")
    if decoded is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired token")
    user_id, token_version = decoded
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "User not found")
    if token_version != user.token_version:
        # Token predates a password change — force a fresh sign-in.
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Session expired")
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]
DB = Annotated[AsyncSession, Depends(get_db)]
