"""The ingestion pipeline: original blob → OCR → archive blob + indexed text.

Fail-soft: any OCR failure flags the document (original is kept, error is
surfaced on the row for the UI) and the pipeline moves on.
"""

import asyncio
import json
import logging
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from pypdf import PdfReader
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Blob, Document, DocumentStatus, Job, JobStatus
from app.services import push, storage, thumbnails
from app.services.classify import classify_document
from app.services.dates import extract_document_date
from app.services.app_state import OCR_ENGINE_OVERRIDE, get_value
from app.services.ocr import get_provider

logger = logging.getLogger(__name__)


def _page_count(pdf: Path) -> int | None:
    try:
        return len(PdfReader(str(pdf)).pages)
    except Exception:
        return None


@dataclass
class IngestOutcome:
    # blob fields are None when the provider produced text only (no archive)
    blob_id: uuid.UUID | None
    sha256: str | None
    size_bytes: int | None
    text: str
    engine: str
    page_count: int | None
    thumb: tuple[uuid.UUID, str, int] | None = None  # (blob_id, sha256, size)


def _run_ocr(
    original: Path, suffix: str, mode: str, workdir: Path, engine: str | None = None
) -> IngestOutcome:
    provider = get_provider(engine)
    # Blob paths are opaque (no extension); providers dispatch on suffix,
    # so hand them a properly-named symlink to the untouched original.
    source = workdir / f"input{suffix.lower()}"
    source.symlink_to(original)
    result = provider.process(source, workdir, mode)

    thumb_dir = workdir / "thumbwork"
    thumb_dir.mkdir()
    thumb_path = thumbnails.make_thumbnail(result.archive_path or source, thumb_dir)
    thumb = storage.store_file(thumb_path) if thumb_path else None

    if result.archive_path is None:
        pages = _page_count(source) if suffix.lower() == ".pdf" else 1
        return IngestOutcome(
            None, None, None, result.text, result.engine, pages, thumb
        )
    pages = _page_count(result.archive_path)
    # Copy the archive into the blob store before the tempdir vanishes.
    blob_id, sha256, size = storage.store_file(result.archive_path)
    return IngestOutcome(
        blob_id, sha256, size, result.text, result.engine, pages, thumb
    )


async def _run_with_progress(
    session: AsyncSession,
    job: Job,
    original: Path,
    suffix: str,
    workdir: Path,
    engine: str | None = None,
) -> IngestOutcome:
    """Run OCR in a thread while mirroring the plugin's page counter (see
    ocr/progress_plugin.py) onto the job row so the UI can show a real bar."""
    progress_file = workdir / "progress"
    task = asyncio.create_task(
        asyncio.to_thread(_run_ocr, original, suffix, job.mode, workdir, engine)
    )
    last: tuple[str, int, int] | None = None
    last_beat = 0.0
    while True:
        done, _ = await asyncio.wait({task}, timeout=1.5)
        if done:
            break
        # Liveness signal for orphan recovery; cheap, so every ~15s is plenty.
        if time.monotonic() - last_beat >= 15:
            last_beat = time.monotonic()
            job.heartbeat_at = datetime.now(timezone.utc)
            await session.commit()
        try:
            report = json.loads(progress_file.read_text())
            snapshot = (
                str(report["phase"]),
                int(float(report["done"])),
                int(float(report["total"])),
            )
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if snapshot != last and snapshot[2] > 0:
            last = snapshot
            job.phase, job.pages_done, job.pages_total = snapshot
            await session.commit()
    return task.result()


async def process_job(session: AsyncSession, job: Job) -> None:
    document = await session.get(Document, job.document_id)
    if document is None:
        job.status = JobStatus.FAILED
        job.error = "document no longer exists"
        job.finished_at = datetime.now(timezone.utc)
        await session.commit()
        return

    document.status = DocumentStatus.PROCESSING
    job.status = JobStatus.RUNNING
    job.started_at = datetime.now(timezone.utc)
    job.attempts += 1
    await session.commit()

    original = storage.blob_file(document.original_blob_id)
    suffix = Path(document.original_filename).suffix
    # Runtime Settings toggle wins over the OCR_ENGINE env default.
    engine_override = await get_value(session, OCR_ENGINE_OVERRIDE)
    try:
        with tempfile.TemporaryDirectory(prefix="ingest-") as tmp:
            workdir = Path(tmp)
            outcome = await _run_with_progress(
                session, job, original, suffix, workdir, engine_override or None
            )
    except Exception as exc:
        logger.warning("OCR failed for document %s: %s", document.id, exc)
        document.status = DocumentStatus.FLAGGED
        document.error = str(exc)[:4000]
        job.status = JobStatus.FAILED
        job.error = str(exc)[:4000]
        job.finished_at = datetime.now(timezone.utc)
        await session.commit()
        await push.notify_tenant(
            session,
            document.tenant_id,
            settings.app_name,
            f"“{document.title}” needs attention — OCR failed.",
            {"document_id": str(document.id)},
        )
        return

    old_archive_id = None
    old_thumb_id = None
    if outcome.blob_id is not None:
        # A text-only pass (Apple Option B) keeps any existing archive.
        old_archive_id = document.archive_blob_id
        session.add(
            Blob(
                id=outcome.blob_id,
                sha256=outcome.sha256,
                size_bytes=outcome.size_bytes,
                mime_type="application/pdf",
            )
        )
        document.archive_blob_id = outcome.blob_id
    if outcome.thumb is not None:
        old_thumb_id = document.thumbnail_blob_id
        t_id, t_sha, t_size = outcome.thumb
        session.add(
            Blob(id=t_id, sha256=t_sha, size_bytes=t_size, mime_type="image/png")
        )
        document.thumbnail_blob_id = t_id
    document.text_content = outcome.text
    if document.doc_date is None:
        document.doc_date = extract_document_date(outcome.text)
    document.page_count = outcome.page_count
    document.ocr_engine = outcome.engine
    document.status = DocumentStatus.READY
    document.error = None
    job.status = JobStatus.DONE
    job.finished_at = datetime.now(timezone.utc)
    await session.commit()

    # Auto-classification: rules are deterministic and idempotent, so running
    # them on every fresh OCR is safe; before the push so the notification
    # carries any rule-set title.
    try:
        outcome = await classify_document(session, document)
        if outcome.matched_rules:
            logger.info(
                "auto-classified %s: %s", document.id, ", ".join(outcome.matched_rules)
            )
        await session.commit()
    except Exception:
        logger.exception("auto-classification failed; continuing")
        await session.rollback()

    for old_id in (old_archive_id, old_thumb_id):
        if old_id is None:
            continue
        old_blob = await session.get(Blob, old_id)
        if old_blob is not None:
            await session.delete(old_blob)
            await session.commit()
        storage.delete_blob(old_id)

    await push.notify_tenant(
        session,
        document.tenant_id,
        settings.app_name,
        f"“{document.title}” is ready to search.",
        {"document_id": str(document.id)},
    )
