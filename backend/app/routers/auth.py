import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from starlette.concurrency import run_in_threadpool

from app.config import settings
from app.deps import DB, AdminUser, CurrentUser
from app.models import Tenant, User
from app.services.ratelimit import limit_account, rate_limit
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

# A real bcrypt hash to verify against when the account doesn't exist, so
# the code path (and timing) matches a wrong-password attempt.
_DUMMY_HASH = hash_password("scrinium-timing-equalizer")


def _token_pair(user: User) -> TokenPair:
    return TokenPair(
        access_token=mint_access_token(user.id, user.token_version),
        refresh_token=mint_refresh_token(user.id, user.token_version),
    )


@router.get("/status", response_model=AuthStatus)
async def auth_status(db: DB) -> AuthStatus:
    count = (await db.execute(select(func.count(User.id)))).scalar_one()
    return AuthStatus(needs_setup=count == 0)


@router.post("/setup", response_model=TokenPair, dependencies=[Depends(rate_limit("setup", 5, 60))])
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
        password_hash=await run_in_threadpool(hash_password, body.password),
        is_admin=True,  # first user owns the library
    )
    db.add(user)
    await db.flush()
    return _token_pair(user)


@router.post("/login", response_model=TokenPair, dependencies=[Depends(rate_limit("login", 10, 60))])
async def login(body: LoginRequest, db: DB) -> TokenPair:
    # The IP-keyed dependency above is bypassable by anyone with a pool of
    # addresses; this window is per-account and is what actually bounds
    # password and one-time-code guessing against a given user.
    limit_account("login", body.email, 10, 300)
    user = (
        await db.execute(select(User).where(User.email == body.email.lower()))
    ).scalar_one_or_none()
    # Always run a bcrypt verify so a missing account costs the same as a
    # wrong password — no timing oracle for enumerating valid emails.
    password_ok = await run_in_threadpool(
        verify_password, body.password, user.password_hash if user else _DUMMY_HASH
    )
    if user is None or not password_ok:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid credentials")
    if user.totp_enabled:
        if not body.totp:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "totp_required")
        if not totp_service.verify(user.totp_secret, body.totp):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Bad one-time code")
    return _token_pair(user)


@router.post(
    "/refresh",
    response_model=TokenPair,
    dependencies=[Depends(rate_limit("refresh", 30, 60))],
)
async def refresh(body: RefreshRequest, db: DB) -> TokenPair:
    decoded = decode_token(body.refresh_token, "refresh")
    if decoded is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid refresh token")
    user_id, token_version = decoded
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "User not found")
    if token_version != user.token_version:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Session expired")
    return _token_pair(user)


@router.post(
    "/change-password", dependencies=[Depends(rate_limit("change-password", 10, 60))]
)
async def change_password(body: dict, user: CurrentUser, db: DB) -> dict:
    # A stolen access token must not buy unlimited guesses at the password.
    limit_account("change-password", str(user.id), 10, 300)
    current = body.get("current_password") or ""
    new = body.get("new_password") or ""
    if len(new) < 8:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "New password needs 8+ characters"
        )
    if not await run_in_threadpool(verify_password, current, user.password_hash):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Current password is wrong")
    user.password_hash = await run_in_threadpool(hash_password, new)
    # Invalidate every outstanding token (this device included) and hand the
    # caller a fresh pair so their current session continues seamlessly.
    user.token_version += 1
    await db.flush()
    fresh = _token_pair(user)
    return {
        "changed": True,
        "access_token": fresh.access_token,
        "refresh_token": fresh.refresh_token,
    }


@router.post("/logout")
async def logout(user: CurrentUser, db: DB) -> dict:
    """Revoke outstanding tokens for this account.

    Signing out used to be purely client-side — it dropped the tokens from
    local storage and left them valid on the server for up to the refresh
    window, so a copy taken beforehand kept working and could be renewed
    indefinitely. Bumping the token version invalidates every token for this
    user, which also makes this the "sign out everywhere" control after a lost
    device. Per-device revocation would need a sessions table; this is the
    coarse but honest version.
    """
    user.token_version += 1
    await db.flush()
    return {"signed_out": True}


@router.get("/me")
async def me(user: CurrentUser) -> dict:
    """Who am I — used by the UI to hide owner-only controls."""
    return {
        "id": str(user.id),
        "email": user.email,
        "is_admin": user.is_admin,
        "totp_enabled": user.totp_enabled,
    }


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
        {
            "id": str(u.id),
            "email": u.email,
            "is_me": u.id == user.id,
            "is_admin": u.is_admin,
        }
        for u in rows
    ]


@router.post("/users", status_code=status.HTTP_201_CREATED)
async def add_user(body: SetupRequest, user: AdminUser, db: DB) -> dict:
    email = body.email.lower()
    existing = (
        await db.execute(select(User).where(User.email == email))
    ).scalars().first()
    if existing is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "That email already has an account")
    new_user = User(
        tenant_id=user.tenant_id,
        email=email,
        password_hash=await run_in_threadpool(hash_password, body.password),
    )
    db.add(new_user)
    await db.flush()
    return {"id": str(new_user.id), "email": new_user.email}


@router.delete("/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_user(user_id: uuid.UUID, user: AdminUser, db: DB) -> None:
    target = await db.get(User, user_id)
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


@router.post(
    "/totp/enable", dependencies=[Depends(rate_limit("totp-enable", 10, 60))]
)
async def totp_enable(body: dict, user: CurrentUser, db: DB) -> dict:
    limit_account("totp-enable", str(user.id), 10, 300)
    if not user.totp_secret:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Run setup first")
    if not totp_service.verify(user.totp_secret, body.get("code") or ""):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "That code doesn't match")
    user.totp_enabled = True
    await db.flush()
    return {"enabled": True}


@router.post(
    "/totp/disable", dependencies=[Depends(rate_limit("totp-disable", 10, 60))]
)
async def totp_disable(body: dict, user: CurrentUser, db: DB) -> dict:
    """Requires the password AND a current code — losing the phone means
    disabling via direct database access, which is the honest recovery
    story for a self-hosted single box."""
    # Otherwise a stolen session plus the password could strip 2FA by guessing
    # six digits without limit.
    limit_account("totp-disable", str(user.id), 10, 300)
    if not user.totp_enabled:
        return {"enabled": False}
    if not await run_in_threadpool(
        verify_password, body.get("password") or "", user.password_hash
    ):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Wrong password")
    if not totp_service.verify(user.totp_secret, body.get("code") or ""):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "That code doesn't match")
    user.totp_enabled = False
    user.totp_secret = None
    await db.flush()
    return {"enabled": False}
