"""Shared document intake: dedup, blob storage, document + job creation.

Used by the upload endpoint and the watched-folder consumer so every ingest
path behaves identically. Heavy file work (hashing, copying gigabyte books)
runs in threads — the event loop also carries heartbeats and progress
commits, and blocking it makes the whole worker look dead.
"""

import asyncio
import logging
import mimetypes
import uuid
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Blob, Document, DocumentStatus, Job, Tag
from app.services import similarity, storage
from app.services.classify import classify_document
from app.services.dates import extract_document_date
from app.services.tag_tree import with_ancestors



logger = logging.getLogger(__name__)

ACCEPTED_SUFFIXES = {
    ".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp",
    # Text-native formats: no OCR — text extracted directly at intake.
    ".txt", ".md", ".epub", ".docx", ".xlsx", ".pptx", ".odt",
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
    sha256 = await asyncio.to_thread(storage.sha256_of, source)
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

    blob_id, _, size = await asyncio.to_thread(storage.store_file, source)

    # Page count up front for PDFs (a header read, even on huge files): the
    # queue ETA estimates in pages, so pending work must be measurable.
    known_pages = page_count
    if known_pages is None and Path(filename).suffix.lower() == ".pdf":
        def _count():
            try:
                import pikepdf

                with pikepdf.open(source) as pdf:
                    return len(pdf.pages)
            except Exception:
                return None

        known_pages = await asyncio.to_thread(_count)
    mime = mime or mimetypes.guess_type(filename)[0] or "application/octet-stream"
    session.add(Blob(id=blob_id, sha256=sha256, size_bytes=size, mime_type=mime))

    # Capture-time OCR (iOS app): text arrives with the file, no server OCR.
    # Text-native formats likewise skip OCR — their text extracts directly.
    if ocr_text is None:
        from app.services import textdocs

        native = await asyncio.to_thread(textdocs.extract_text, source)
        if native and native.strip():
            ocr_text = native
            ocr_engine = ocr_engine or "native"
    captured = ocr_text is not None and ocr_text.strip() != ""
    doc = Document(
        tenant_id=tenant_id,
        # Both columns are varchar(1024) and nothing trimmed them, so an
        # over-long name failed on flush — after the blob had been written,
        # leaking a full copy and 500ing the upload.
        title=Path(filename).stem[:1024] or "untitled",
        original_filename=filename[:1024],
        original_blob_id=blob_id,
        status=DocumentStatus.READY if captured else DocumentStatus.PENDING,
        page_count=known_pages,
    )
    if captured:
        doc.text_content = ocr_text
        doc.text_length = len(ocr_text or "")
        doc.simhash = similarity.simhash(ocr_text)
        doc.ocr_engine = ocr_engine or "apple"
        doc.doc_date = extract_document_date(ocr_text)
    if tags:
        doc.tags = await with_ancestors(session, list(tags))
    else:
        # Initialize the collection: classify_document reads doc.tags, and
        # an unloaded lazy collection cannot load outside a greenlet.
        doc.tags = []
    session.add(doc)
    await session.flush()
    if not captured:
        session.add(Job(document_id=doc.id, kind="ingest", mode=settings.ocr_mode))
        await session.flush()
    else:
        # Captured docs skip the worker entirely, so classify here. Page
        # vectors are the database's job (0028) and need no help.
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
        segments = await asyncio.to_thread(split_on_separators, source)
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
