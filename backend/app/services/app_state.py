"""Runtime operational flags, DB-backed so they survive restarts."""

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import AppSetting

PROCESSING_PAUSED = "processing_paused"
OCR_ENGINE_OVERRIDE = "ocr_engine_override"
ARCHIVE_MAX_DPI = "archive_max_dpi"
ARCHIVE_FORMAT = "archive_format"


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


async def resolve_archive_format(session: AsyncSession) -> str:
    """Active archive format: runtime override wins over the env default."""
    override = (await get_value(session, ARCHIVE_FORMAT)).strip()
    if override in ("pdfa", "pdf", "auto"):
        return override
    return settings.archive_format


def wants_pdfa(archive_format: str, original_dpi: int | None) -> bool:
    """Should this document's archive be PDF/A?

    Under `auto`, on whether the original carries real text. original_dpi of 0
    means it holds no raster images at all — born-digital, where embedded
    fonts are the thing worth preserving and the conversion has little to
    inflate. Anything above 0 is a scan: no text to protect, and a 4x bill.
    Unmeasured (None) keeps PDF/A, so an unknown is never quietly downgraded.
    """
    if archive_format == "pdfa":
        return True
    if archive_format == "pdf":
        return False
    return original_dpi is None or original_dpi == 0


async def resolve_archive_dpi(session: AsyncSession) -> int:
    """The active archive-DPI cap: runtime Settings override wins over the
    ARCHIVE_MAX_DPI env default. 0 means downsampling is disabled."""
    override = (await get_value(session, ARCHIVE_MAX_DPI)).strip()
    if override.isdigit():
        return int(override)
    return settings.archive_max_dpi
