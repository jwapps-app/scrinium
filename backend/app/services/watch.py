"""Watched-folder consumer.

The worker polls WATCH_DIR for PDFs/images, including inside subfolders —
and each folder level becomes a tag: dropping `Taxes/2023/return.pdf` into
the watch dir ingests it tagged "Taxes" and "2023" (tags are created if
they don't exist, reused if they do). Files go through the same intake path
as uploads and are then moved into dot-subfolders, keeping their relative
folder structure, so nothing is ever silently deleted:

    .consumed/    ingested successfully (original also lives in the blob store)
    .duplicates/  content hash already in the library
    .failed/      intake crashed; kept for inspection

Emptied drop folders are pruned after each sweep. Fail-soft by design: one
bad file never stops the sweep, and the consumer simply idles until
Scrinium's first user/tenant exists.
"""

import asyncio
import logging
import os
import time
from pathlib import Path

from sqlalchemy import select

from app.config import settings
from app.database import SessionLocal
from app.models import Tenant
from app.services import intake, tag_tree

logger = logging.getLogger(__name__)

# A file modified this recently may still be mid-copy; pick it up next sweep.
SETTLE_SECONDS = 3

FILING_DIRS = (".consumed", ".duplicates", ".failed")


def _skip_part(part: str) -> bool:
    # "."  — our filing dirs, hidden files, AppleDouble (._*)
    # "@"  — Synology system dirs (@eaDir thumbnail metadata, @Recycle…)
    # "#"  — Synology #recycle / #snapshot
    return part.startswith((".", "@", "#"))


def _candidates(watch: Path) -> list[Path]:
    found = []
    for path in sorted(watch.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(watch)
        if any(_skip_part(part) for part in rel.parts):
            continue
        if path.suffix.lower() not in intake.ACCEPTED_SUFFIXES:
            continue
        found.append(path)
    return found


def _file_into(watch: Path, path: Path, folder_name: str) -> Path:
    """Move a processed file under watch/<folder_name>/, keeping its
    relative folder structure. Returns the destination."""
    rel = path.relative_to(watch)
    dest = watch / folder_name / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest = dest.with_name(f"{int(time.time())}-{dest.name}")
    path.rename(dest)
    return dest


def _prune_empty_dirs(watch: Path) -> None:
    """Remove drop folders that emptied out during the sweep."""
    for dirpath, _dirnames, _filenames in os.walk(watch, topdown=False):
        directory = Path(dirpath)
        if directory == watch:
            continue
        rel = directory.relative_to(watch)
        if any(_skip_part(part) for part in rel.parts):
            continue
        try:
            directory.rmdir()  # fails harmlessly unless empty
        except OSError:
            pass


def sweep_retention() -> int:
    """Delete filed copies in .consumed/ and .duplicates/ older than
    CONSUMED_RETENTION_DAYS. Opt-in: 0 (the default) keeps everything
    forever, preserving the never-delete convention. .failed/ is never
    swept — those need eyes. Returns how many files were removed."""
    days = settings.consumed_retention_days
    if not settings.watch_dir or days <= 0:
        return 0
    watch = Path(settings.watch_dir)
    cutoff = time.time() - days * 86400
    removed = 0
    for folder_name in (".consumed", ".duplicates"):
        folder = watch / folder_name
        if not folder.is_dir():
            continue
        for dirpath, _dirnames, filenames in os.walk(folder):
            for name in filenames:
                path = Path(dirpath) / name
                try:
                    if path.stat().st_mtime < cutoff:
                        path.unlink()
                        removed += 1
                except OSError:
                    continue
        # Prune emptied subfolders (never the filing root itself).
        for dirpath, _dirnames, _filenames in os.walk(folder, topdown=False):
            directory = Path(dirpath)
            if directory == folder:
                continue
            try:
                directory.rmdir()
            except OSError:
                pass
    if removed:
        logger.info(
            "retention sweep removed %d filed copies older than %dd", removed, days
        )
    return removed


async def scan_once() -> int:
    """One sweep of the watch dir. Returns how many files were ingested."""
    if not settings.watch_dir:
        return 0
    watch = Path(settings.watch_dir)
    if not watch.is_dir():
        return 0

    candidates = await asyncio.to_thread(
        lambda: [
            path
            for path in _candidates(watch)
            if time.time() - path.stat().st_mtime >= SETTLE_SECONDS
        ]
    )
    if not candidates:
        return 0
    # Cap per sweep: with a huge dump (tens of thousands of files), ingesting
    # everything in one pass would starve OCR jobs for hours. Batching lets
    # intake and processing interleave; the rest is picked up next sweep.
    candidates = candidates[: settings.watch_batch_size]

    consumed = 0
    async with SessionLocal() as session:
        tenant_id = (
            await session.execute(select(Tenant.id).order_by(Tenant.created_at))
        ).scalars().first()
        if tenant_id is None:
            return 0  # nobody has set up yet; leave files in place

        for path in candidates:
            folder_names = list(path.relative_to(watch).parts[:-1])
            try:
                tags = (
                    await tag_tree.get_or_create_tag_path(
                        session, tenant_id, folder_names
                    )
                    if folder_names
                    else None
                )
                created = await intake.ingest_with_split(
                    session, tenant_id, path, path.name, tags=tags
                )
                await session.commit()
                dest = _file_into(watch, path, ".consumed")
                # Remember the filing location so deleting the document can
                # clean up its consumed copy too. (Split segments share the
                # one source file; only a lone document claims it.)
                if len(created) == 1:
                    created[0].source_path = str(dest.relative_to(watch))
                await session.commit()
                consumed += 1
                logger.info(
                    "consumed %s as %d document(s)%s",
                    path.name,
                    len(created),
                    f" (tags: {', '.join(folder_names)})" if folder_names else "",
                )
            except intake.DuplicateDocument as exc:
                await session.rollback()
                _file_into(watch, path, ".duplicates")
                logger.info("skipped duplicate %s (%s)", path.name, exc.existing_id)
            except Exception:
                await session.rollback()
                logger.exception("failed to consume %s", path.name)
                _file_into(watch, path, ".failed")

    _prune_empty_dirs(watch)
    return consumed
