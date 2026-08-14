"""Runtime operational flags, DB-backed so they survive restarts."""

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import AppSetting

PROCESSING_PAUSED = "processing_paused"
OCR_ENGINE_OVERRIDE = "ocr_engine_override"
ARCHIVE_MAX_DPI = "archive_max_dpi"
ARCHIVE_FORMAT = "archive_format"

# pdfa/pdf force the format. auto decides from the original alone. measured
# builds both for a scan and keeps whichever is genuinely smaller.
ARCHIVE_FORMATS = ("pdfa", "pdf", "auto", "measured")


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
    if override in ARCHIVE_FORMATS:
        return override
    return settings.archive_format


def wants_pdfa(archive_format: str, original_dpi: int | None) -> bool:
    """Should this document's archive be PDF/A?

    Under `auto` and `measured`, on whether the original carries real text.
    original_dpi of 0 means it holds no raster images at all — born-digital,
    where embedded fonts are the thing worth preserving and the conversion has
    little to inflate. Anything above 0 is a scan: no text to protect.

    For `measured` this is only the starting point. A scan is built as plain
    PDF and then measured against a PDF/A copy — see needs_measuring. A
    born-digital document is not measured at all, because there the choice is
    about preserving fonts, not about size.
    """
    if archive_format == "pdfa":
        return True
    if archive_format == "pdf":
        return False
    return original_dpi is None or original_dpi == 0


def needs_measuring(archive_format: str, original_dpi: int | None) -> bool:
    """True when both formats should be built and the smaller one kept.

    Only for scans, and only under `measured`. Guessing from the original's
    size does not work — measured across 358 documents, an original's density
    barely predicts which format wins, and at 400-800 KB/page it is a coin
    flip. So the rule is to stop guessing and weigh both.
    """
    return archive_format == "measured" and not wants_pdfa(archive_format, original_dpi)


async def resolve_archive_dpi(session: AsyncSession) -> int:
    """The active archive-DPI cap: runtime Settings override wins over the
    ARCHIVE_MAX_DPI env default. 0 means downsampling is disabled."""
    override = (await get_value(session, ARCHIVE_MAX_DPI)).strip()
    if override.isdigit():
        return int(override)
    return settings.archive_max_dpi
