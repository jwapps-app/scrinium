"""Runtime operational flags, DB-backed so they survive restarts."""

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AppSetting

PROCESSING_PAUSED = "processing_paused"
OCR_ENGINE_OVERRIDE = "ocr_engine_override"


async def get_flag(session: AsyncSession, key: str, default: bool = False) -> bool:
    row = await session.get(AppSetting, key)
    if row is None:
        return default
    return row.value == "1"


async def set_flag(session: AsyncSession, key: str, value: bool) -> None:
    row = await session.get(AppSetting, key)
    if row is None:
        session.add(AppSetting(key=key, value="1" if value else "0"))
    else:
        row.value = "1" if value else "0"
    await session.flush()


async def get_value(session: AsyncSession, key: str, default: str = "") -> str:
    row = await session.get(AppSetting, key)
    return row.value if row is not None else default


async def set_value(session: AsyncSession, key: str, value: str) -> None:
    row = await session.get(AppSetting, key)
    if row is None:
        session.add(AppSetting(key=key, value=value))
    else:
        row.value = value
    await session.flush()
