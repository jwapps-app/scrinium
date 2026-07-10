from fastapi import APIRouter, HTTPException, status
from sqlalchemy import func, select

from app.config import settings
from app.deps import DB
from app.models import Tenant, User
from app.schemas import (
    AuthStatus,
    LoginRequest,
    RefreshRequest,
    SetupRequest,
    TokenPair,
)
from app.security import (
    decode_token,
    hash_password,
    mint_access_token,
    mint_refresh_token,
    verify_password,
)

router = APIRouter(prefix="/auth", tags=["auth"])


def _token_pair(user: User) -> TokenPair:
    return TokenPair(
        access_token=mint_access_token(user.id),
        refresh_token=mint_refresh_token(user.id),
    )


@router.get("/status", response_model=AuthStatus)
async def auth_status(db: DB) -> AuthStatus:
    count = (await db.execute(select(func.count(User.id)))).scalar_one()
    return AuthStatus(needs_setup=count == 0)


@router.post("/setup", response_model=TokenPair)
async def setup(body: SetupRequest, db: DB) -> TokenPair:
    count = (await db.execute(select(func.count(User.id)))).scalar_one()
    if count > 0:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Already set up")
    tenant = Tenant(name=settings.app_name)
    db.add(tenant)
    await db.flush()
    user = User(
        tenant_id=tenant.id,
        email=body.email.lower(),
        password_hash=hash_password(body.password),
    )
    db.add(user)
    await db.flush()
    return _token_pair(user)


@router.post("/login", response_model=TokenPair)
async def login(body: LoginRequest, db: DB) -> TokenPair:
    user = (
        await db.execute(select(User).where(User.email == body.email.lower()))
    ).scalar_one_or_none()
    if user is None or not verify_password(body.password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid credentials")
    return _token_pair(user)


@router.post("/refresh", response_model=TokenPair)
async def refresh(body: RefreshRequest, db: DB) -> TokenPair:
    user_id = decode_token(body.refresh_token, "refresh")
    if user_id is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid refresh token")
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "User not found")
    return _token_pair(user)
