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
