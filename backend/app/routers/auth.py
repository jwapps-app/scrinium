import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import delete, func, select
from starlette.concurrency import run_in_threadpool

from app.config import settings
from app.deps import DB, AdminUser, CurrentUser
from app.models import RefreshToken, Tenant, User
from app.services.ratelimit import limit_account, rate_limit
from app.schemas import (
    AuthStatus,
    ChangePasswordRequest,
    LoginRequest,
    RefreshRequest,
    SetupRequest,
    TokenPair,
    TotpCodeRequest,
    TotpDisableRequest,
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


# How long a just-rotated refresh token is still answered. A client whose
# refresh response was lost on the wire retries with the token it still has;
# inside this window it gets the same successor back. Outside it, the second
# presentation is treated as what it most likely is: a copy in other hands.
REFRESH_REUSE_GRACE = timedelta(seconds=60)


async def _issue_pair(db, user: User) -> TokenPair:
    """A fresh access/refresh pair, the refresh half backed by a row it can
    be retired through."""
    now = datetime.now(timezone.utc)
    row = RefreshToken(
        user_id=user.id, expires_at=now + timedelta(days=settings.refresh_token_days)
    )
    db.add(row)
    await db.flush()
    return TokenPair(
        access_token=mint_access_token(user.id, user.token_version),
        refresh_token=mint_refresh_token(user.id, user.token_version, row.id),
    )


async def _prune_tokens(db, user: User) -> None:
    """Drop rows that can never be presented again — expired, or revoked long
    past the grace window. Done at sign-in, when there is a user to scope it
    to, rather than by yet another sweep."""
    now = datetime.now(timezone.utc)
    await db.execute(
        delete(RefreshToken).where(
            RefreshToken.user_id == user.id,
            (RefreshToken.expires_at < now)
            | (RefreshToken.revoked_at < now - timedelta(days=1)),
        )
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
    return await _issue_pair(db, user)


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
        step = totp_service.matching_step(user.totp_secret, body.totp)
        if step is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Bad one-time code")
        # Single use: a code observed once must not authenticate a second time
        # while it is still inside its validity window.
        if user.totp_last_step is not None and step <= user.totp_last_step:
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED, "That code has already been used"
            )
        user.totp_last_step = step
        await db.flush()
    await _prune_tokens(db, user)
    return await _issue_pair(db, user)


@router.post(
    "/refresh",
    response_model=TokenPair,
    dependencies=[Depends(rate_limit("refresh", 30, 60))],
)
async def refresh(body: RefreshRequest, db: DB) -> TokenPair:
    """Exchange a refresh token for a new pair, retiring the one presented.

    Single use, with one allowance: the exchange's response can be lost after
    the server has already rotated, and the client then retries with the
    token it still holds. For REFRESH_REUSE_GRACE after rotation that retry
    is answered with the same successor. After that a revoked token is
    refused — it is the shape a stolen copy takes.
    """
    decoded = decode_token(body.refresh_token, "refresh")
    if decoded is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid refresh token")
    user_id, token_version, jti = decoded
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "User not found")
    if token_version != user.token_version:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Session expired")
    if jti is None:
        # Issued before rotation existed. Honour it (its version and expiry
        # still stand) and hand back a pair that is in the scheme, so every
        # device migrates on its next refresh without being signed out.
        return await _issue_pair(db, user)

    row = await db.get(RefreshToken, jti)
    if row is None or row.user_id != user.id:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid refresh token")
    now = datetime.now(timezone.utc)
    if row.revoked_at is not None:
        if row.replaced_by is not None and now - row.revoked_at <= REFRESH_REUSE_GRACE:
            successor = await db.get(RefreshToken, row.replaced_by)
            if (
                successor is not None
                and successor.revoked_at is None
                and successor.expires_at > now
            ):
                return TokenPair(
                    access_token=mint_access_token(user.id, user.token_version),
                    refresh_token=mint_refresh_token(
                        user.id, user.token_version, successor.id
                    ),
                )
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "Refresh token already used"
        )
    if row.expires_at <= now:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Refresh token expired")

    successor = RefreshToken(
        user_id=user.id, expires_at=now + timedelta(days=settings.refresh_token_days)
    )
    db.add(successor)
    await db.flush()
    row.revoked_at = now
    row.replaced_by = successor.id
    await db.flush()
    return TokenPair(
        access_token=mint_access_token(user.id, user.token_version),
        refresh_token=mint_refresh_token(user.id, user.token_version, successor.id),
    )


@router.post(
    "/change-password", dependencies=[Depends(rate_limit("change-password", 10, 60))]
)
async def change_password(
    body: ChangePasswordRequest, user: CurrentUser, db: DB
) -> dict:
    # A stolen access token must not buy unlimited guesses at the password.
    limit_account("change-password", str(user.id), 10, 300)
    current = body.current_password
    new = body.new_password
    if not await run_in_threadpool(verify_password, current, user.password_hash):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Current password is wrong")
    user.password_hash = await run_in_threadpool(hash_password, new)
    # Invalidate every outstanding token (this device included) and hand the
    # caller a fresh pair so their current session continues seamlessly.
    user.token_version += 1
    await db.flush()
    fresh = await _issue_pair(db, user)
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
async def list_users(user: AdminUser, db: DB) -> list[dict]:
    """Owner-only, like the rest of account management: every account's
    address and role is not something each member needs to see."""
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
async def totp_enable(body: TotpCodeRequest, user: CurrentUser, db: DB) -> dict:
    limit_account("totp-enable", str(user.id), 10, 300)
    if not user.totp_secret:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Run setup first")
    step = totp_service.matching_step(user.totp_secret, body.code)
    if step is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "That code doesn't match")
    user.totp_last_step = step
    user.totp_enabled = True
    await db.flush()
    return {"enabled": True}


@router.post(
    "/totp/disable", dependencies=[Depends(rate_limit("totp-disable", 10, 60))]
)
async def totp_disable(body: TotpDisableRequest, user: CurrentUser, db: DB) -> dict:
    """Requires the password AND a current code — losing the phone means
    disabling via direct database access, which is the honest recovery
    story for a self-hosted single box."""
    # Otherwise a stolen session plus the password could strip 2FA by guessing
    # six digits without limit.
    limit_account("totp-disable", str(user.id), 10, 300)
    if not user.totp_enabled:
        return {"enabled": False}
    if not await run_in_threadpool(verify_password, body.password, user.password_hash):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Wrong password")
    step = totp_service.matching_step(user.totp_secret, body.code)
    if step is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "That code doesn't match")
    user.totp_enabled = False
    user.totp_secret = None
    user.totp_last_step = None
    await db.flush()
    return {"enabled": False}
