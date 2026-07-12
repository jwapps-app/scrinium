from fastapi import APIRouter, HTTPException, status
from sqlalchemy import func, select

from app.config import settings
from app.deps import DB, CurrentUser
from app.models import Tenant, User
from app.schemas import (
    AuthStatus,
    LoginRequest,
    RefreshRequest,
    SetupRequest,
    TokenPair,
)
from app.services import totp as totp_service
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
    if user.totp_enabled:
        if not body.totp:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "totp_required")
        if not totp_service.verify(user.totp_secret, body.totp):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Bad one-time code")
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


@router.post("/change-password")
async def change_password(body: dict, user: CurrentUser, db: DB) -> dict:
    current = body.get("current_password") or ""
    new = body.get("new_password") or ""
    if len(new) < 8:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "New password needs 8+ characters"
        )
    if not verify_password(current, user.password_hash):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Current password is wrong")
    user.password_hash = hash_password(new)
    await db.flush()
    return {"changed": True}


@router.get("/users")
async def list_users(user: CurrentUser, db: DB) -> list[dict]:
    rows = (
        (
            await db.execute(
                select(User)
                .where(User.tenant_id == user.tenant_id)
                .order_by(User.created_at)
            )
        )
        .scalars()
        .all()
    )
    return [
        {"id": str(u.id), "email": u.email, "is_me": u.id == user.id} for u in rows
    ]


@router.post("/users", status_code=status.HTTP_201_CREATED)
async def add_user(body: SetupRequest, user: CurrentUser, db: DB) -> dict:
    email = body.email.lower()
    existing = (
        await db.execute(select(User).where(User.email == email))
    ).scalars().first()
    if existing is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "That email already has an account")
    new_user = User(
        tenant_id=user.tenant_id,
        email=email,
        password_hash=hash_password(body.password),
    )
    db.add(new_user)
    await db.flush()
    return {"id": str(new_user.id), "email": new_user.email}


@router.delete("/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_user(user_id: str, user: CurrentUser, db: DB) -> None:
    import uuid as _uuid

    target = await db.get(User, _uuid.UUID(user_id))
    if target is None or target.tenant_id != user.tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    if target.id == user.id:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "You can't remove your own account"
        )
    await db.delete(target)
    await db.flush()


@router.get("/totp")
async def totp_status(user: CurrentUser) -> dict:
    return {"enabled": user.totp_enabled}


@router.post("/totp/setup")
async def totp_setup(user: CurrentUser, db: DB) -> dict:
    """Mint a fresh secret (pending until a code proves the authenticator
    has it). Re-running before enabling just rotates the pending secret."""
    if user.totp_enabled:
        raise HTTPException(status.HTTP_409_CONFLICT, "Two-factor is already enabled")
    user.totp_secret = totp_service.new_secret()
    await db.flush()
    return {
        "secret": user.totp_secret,
        "otpauth_url": totp_service.otpauth_url(
            user.totp_secret, user.email, settings.app_name
        ),
    }


@router.post("/totp/enable")
async def totp_enable(body: dict, user: CurrentUser, db: DB) -> dict:
    if not user.totp_secret:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Run setup first")
    if not totp_service.verify(user.totp_secret, body.get("code") or ""):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "That code doesn't match")
    user.totp_enabled = True
    await db.flush()
    return {"enabled": True}


@router.post("/totp/disable")
async def totp_disable(body: dict, user: CurrentUser, db: DB) -> dict:
    """Requires the password AND a current code — losing the phone means
    disabling via direct database access, which is the honest recovery
    story for a self-hosted single box."""
    if not user.totp_enabled:
        return {"enabled": False}
    if not verify_password(body.get("password") or "", user.password_hash):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Wrong password")
    if not totp_service.verify(user.totp_secret, body.get("code") or ""):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "That code doesn't match")
    user.totp_enabled = False
    user.totp_secret = None
    await db.flush()
    return {"enabled": False}
