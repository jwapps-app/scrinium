from fastapi import APIRouter, status
from sqlalchemy import delete

from app.deps import DB, CurrentUser
from app.models import DeviceToken
from app.schemas import DeviceRegister

router = APIRouter(prefix="/devices", tags=["devices"])


@router.post("", status_code=status.HTTP_204_NO_CONTENT)
async def register_device(body: DeviceRegister, user: CurrentUser, db: DB) -> None:
    """Idempotent upsert keyed on token; reassigns the token if the phone
    switched users."""
    device = await db.get(DeviceToken, body.token)
    if device is None:
        db.add(
            DeviceToken(
                token=body.token,
                user_id=user.id,
                platform=body.platform,
                environment=body.environment,
            )
        )
    else:
        device.user_id = user.id
        device.platform = body.platform
        device.environment = body.environment
    await db.flush()


@router.delete("/{token}", status_code=status.HTTP_204_NO_CONTENT)
async def unregister_device(token: str, user: CurrentUser, db: DB) -> None:
    await db.execute(
        delete(DeviceToken).where(
            DeviceToken.token == token, DeviceToken.user_id == user.id
        )
    )
