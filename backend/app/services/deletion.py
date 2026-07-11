"""Document deletion: soft delete to trash, restore, and permanent purge.

Deleting a document parks it in the trash (deleted_at set); blobs and files
stay untouched so restore is a one-field change. The purge — run manually
("delete forever") or by the worker's retention sweep — is what actually
removes blobs, disk files, and the consumed watch-folder copy.
"""

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Blob, Document
from app.services import storage

logger = logging.getLogger(__name__)


def _remove_consumed_copy(source_path: str) -> None:
    """Delete a document's filed copy under WATCH_DIR (e.g. .consumed/…)
    and prune any folders that emptied out. Best-effort."""
    if not settings.watch_dir:
        return
    watch = Path(settings.watch_dir)
    target = (watch / source_path).resolve()
    if not str(target).startswith(str(watch.resolve())):
        return  # never follow a path outside the watch dir
    target.unlink(missing_ok=True)
    parent = target.parent
    while parent != watch and parent.name != "":
        try:
            parent.rmdir()  # fails harmlessly unless empty
        except OSError:
            break
        parent = parent.parent


async def purge_document(db: AsyncSession, doc: Document) -> None:
    """Permanently remove a document: row, blobs, files, consumed copy."""
    blob_ids = [doc.original_blob_id]
    if doc.archive_blob_id is not None:
        blob_ids.append(doc.archive_blob_id)
    if doc.thumbnail_blob_id is not None:
        blob_ids.append(doc.thumbnail_blob_id)
    source_path = doc.source_path
    await db.delete(doc)
    await db.flush()
    for blob_id in blob_ids:
        blob = await db.get(Blob, blob_id)
        if blob is not None:
            await db.delete(blob)
        storage.delete_blob(blob_id)
    await db.flush()
    if source_path:
        _remove_consumed_copy(source_path)


async def purge_expired(db: AsyncSession, limit: int = 100) -> int:
    """Purge trashed documents past the retention window. Returns count."""
    cutoff = datetime.now(timezone.utc) - timedelta(
        days=settings.trash_retention_days
    )
    expired = (
        await db.execute(
            select(Document)
            .where(Document.deleted_at.is_not(None), Document.deleted_at < cutoff)
            .limit(limit)
        )
    ).scalars().all()
    for doc in expired:
        await purge_document(db, doc)
    if expired:
        await db.commit()
        logger.info("purged %d expired document(s) from trash", len(expired))
    return len(expired)
