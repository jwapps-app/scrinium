"""Watched-folder consumer.

The worker polls WATCH_DIR for PDFs/images. Files are ingested through the
same intake path as uploads and then moved into dot-subfolders so nothing is
ever silently deleted:

    .consumed/    ingested successfully (original also lives in the blob store)
    .duplicates/  content hash already in the library
    .failed/      intake crashed; kept for inspection

Fail-soft by design: one bad file never stops the sweep, and the consumer
simply idles until Scrinium's first user/tenant exists.
"""

import logging
import time
from pathlib import Path

from sqlalchemy import select

from app.config import settings
from app.database import SessionLocal
from app.models import Tenant
from app.services import intake

logger = logging.getLogger(__name__)

# A file modified this recently may still be mid-copy; pick it up next sweep.
SETTLE_SECONDS = 3


def _move_into(path: Path, folder: Path) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / path.name
    if dest.exists():
        dest = folder / f"{int(time.time())}-{path.name}"
    path.rename(dest)


async def scan_once() -> int:
    """One sweep of the watch dir. Returns how many files were ingested."""
    if not settings.watch_dir:
        return 0
    watch = Path(settings.watch_dir)
    if not watch.is_dir():
        return 0

    candidates = [
        p
        for p in watch.iterdir()
        if p.is_file()
        and not p.name.startswith(".")
        and p.suffix.lower() in intake.ACCEPTED_SUFFIXES
    ]
    if not candidates:
        return 0

    consumed = 0
    async with SessionLocal() as session:
        tenant_id = (
            await session.execute(select(Tenant.id).order_by(Tenant.created_at))
        ).scalars().first()
        if tenant_id is None:
            return 0  # nobody has set up yet; leave files in place

        for path in sorted(candidates):
            if time.time() - path.stat().st_mtime < SETTLE_SECONDS:
                continue
            try:
                doc = await intake.ingest_file(session, tenant_id, path, path.name)
                await session.commit()
                _move_into(path, watch / ".consumed")
                consumed += 1
                logger.info("consumed %s as document %s", path.name, doc.id)
            except intake.DuplicateDocument as exc:
                await session.rollback()
                _move_into(path, watch / ".duplicates")
                logger.info("skipped duplicate %s (%s)", path.name, exc.existing_id)
            except Exception:
                await session.rollback()
                logger.exception("failed to consume %s", path.name)
                _move_into(path, watch / ".failed")
    return consumed
