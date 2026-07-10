"""Shared document intake: dedup, blob storage, document + job creation.

Used by the upload endpoint and the watched-folder consumer so every ingest
path behaves identically.
"""

import mimetypes
import uuid
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Blob, Document, DocumentStatus, Job
from app.services import storage
from app.services.classify import classify_document

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
