"""Shared document intake: dedup, blob storage, document + job creation.

Used by the upload endpoint and the watched-folder consumer so every ingest
path behaves identically.
"""

import logging
import mimetypes
import uuid
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Blob, Document, DocumentStatus, Job, Tag
from app.services import storage
from app.services.classify import classify_document
from app.services.dates import extract_document_date
from app.services.tag_tree import with_ancestors



logger = logging.getLogger(__name__)

ACCEPTED_SUFFIXES = {
    ".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp",
}


class DuplicateDocument(Exception):
    def __init__(self, existing_id: uuid.UUID):
        self.existing_id = existing_id
        super().__init__(f"Duplicate of existing document {existing_id}")


async def ingest_file(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    source: Path,
    filename: str,
    mime: str | None = None,
    ocr_text: str | None = None,
    ocr_engine: str | None = None,
    page_count: int | None = None,
    tags: list[Tag] | None = None,
) -> Document:
    """Store `source` as a new document. Raises DuplicateDocument on a
    content-hash match within the tenant. Does not commit."""
    sha256 = storage.sha256_of(source)
    existing = (
        (
            await session.execute(
                select(Document)
                .join(Blob, Document.original_blob_id == Blob.id)
                .where(Document.tenant_id == tenant_id, Blob.sha256 == sha256)
            )
        )
        .scalars()
        .first()
    )
    if existing is not None:
        raise DuplicateDocument(existing.id)

    blob_id, _, size = storage.store_file(source)
    mime = mime or mimetypes.guess_type(filename)[0] or "application/octet-stream"
    session.add(Blob(id=blob_id, sha256=sha256, size_bytes=size, mime_type=mime))

    # Capture-time OCR (iOS app): text arrives with the file, no server OCR.
    captured = ocr_text is not None and ocr_text.strip() != ""
    doc = Document(
        tenant_id=tenant_id,
        title=Path(filename).stem,
        original_filename=filename,
        original_blob_id=blob_id,
        status=DocumentStatus.READY if captured else DocumentStatus.PENDING,
    )
    if captured:
        doc.text_content = ocr_text
        doc.ocr_engine = ocr_engine or "apple"
        doc.page_count = page_count
        doc.doc_date = extract_document_date(ocr_text)
    if tags:
        doc.tags = await with_ancestors(session, list(tags))
    session.add(doc)
    await session.flush()
    if not captured:
        session.add(Job(document_id=doc.id, kind="ingest", mode="skip"))
        await session.flush()
    else:
        # Captured docs skip the worker entirely, so classify here.
        await classify_document(session, doc)
        await session.flush()
    await session.refresh(doc)
    return doc


async def ingest_with_split(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    source: Path,
    filename: str,
    **kwargs,
) -> list[Document]:
    """Ingest, splitting on separator barcode pages first when enabled.

    Returns the created documents (one, unless separators split the file).
    Duplicate segments are skipped individually — a re-scanned stack only
    adds what's new. Raises DuplicateDocument only for an unsplit single
    duplicate, preserving the plain-ingest contract."""
    from app.services.separators import split_on_separators

    segments = None
    try:
        segments = split_on_separators(source)
    except Exception:  # detection is best-effort, never blocks intake
        logger.exception("separator detection failed for %s", filename)
    if not segments:
        return [await ingest_file(session, tenant_id, source, filename, **kwargs)]

    stem = Path(filename).stem
    created: list[Document] = []
    for i, segment in enumerate(segments, start=1):
        try:
            created.append(
                await ingest_file(
                    session,
                    tenant_id,
                    segment,
                    f"{stem} ({i} of {len(segments)}).pdf",
                    **kwargs,
                )
            )
        except DuplicateDocument:
            continue
        finally:
            segment.unlink(missing_ok=True)
    try:
        segments[0].parent.rmdir()
    except OSError:
        pass
    return created
