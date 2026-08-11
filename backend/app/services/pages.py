"""User-initiated page operations: rotate, delete pages, extract to new doc.

Extract is purely additive — the source document is untouched and the chosen
pages become a NEW document through the normal intake path (dedup, tags,
classification, OCR queue).

Rotate and delete-pages are deliberate edits to the document itself: they
write a NEW original blob and re-queue OCR, while the pre-edit blobs move to
a trashed snapshot document — undoable via Restore for the trash retention
window, auto-purged after. The "originals are never mutated" rule holds:
the system never touches an original; a user reshaping their document is
the sanctioned exception, and even then the old bytes survive in the trash.
"""

import logging
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pikepdf
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Blob, Document, DocumentStatus, Job, Tag
from app.services import storage
from app.services.intake import ingest_file

logger = logging.getLogger(__name__)


class PageOpError(Exception):
    """Invalid page operation (bad page numbers, not a PDF, …)."""


def _open_source(doc: Document) -> pikepdf.Pdf:
    source = storage.blob_file(doc.original_blob_id)
    if not source.exists():
        raise PageOpError("Original file is missing from the blob store")
    try:
        return pikepdf.open(source)
    except pikepdf.PdfError as exc:
        raise PageOpError(f"Not an editable PDF: {exc}") from exc


def _check_pages(pages: list[int], total: int) -> list[int]:
    if not pages:
        raise PageOpError("No pages selected")
    unique = sorted(set(pages))
    if unique[0] < 1 or unique[-1] > total:
        raise PageOpError(f"Page numbers must be between 1 and {total}")
    return unique


async def _replace_original(
    session: AsyncSession, doc: Document, edited: Path
) -> None:
    """Swap in a new original blob and re-queue OCR.

    The pre-edit version isn't destroyed: its blobs are handed to a snapshot
    document that goes straight to the trash — restorable for the retention
    window, purged automatically after. Undo for the one destructive edit in
    the app, at zero disk cost (the blobs just change owner)."""
    new_id, sha256, size = storage.store_file(edited)
    session.add(
        Blob(id=new_id, sha256=sha256, size_bytes=size, mime_type="application/pdf")
    )
    snapshot = Document(
        tenant_id=doc.tenant_id,
        title=f"{doc.title} (before page edit)",
        original_filename=doc.original_filename,
        original_blob_id=doc.original_blob_id,
        archive_blob_id=doc.archive_blob_id,
        thumbnail_blob_id=doc.thumbnail_blob_id,
        status=DocumentStatus.READY,
        text_content=doc.text_content,
        ocr_engine=doc.ocr_engine,
        page_count=doc.page_count,
        deleted_at=datetime.now(timezone.utc),
    )
    session.add(snapshot)
    doc.original_blob_id = new_id
    doc.archive_blob_id = None
    doc.thumbnail_blob_id = None
    doc.status = DocumentStatus.PENDING
    doc.text_content = None
    # The page vectors go with it: the trigger on text_content clears them,
    # so a search cannot point at content that has moved.
    # Force: the copied text layer (if any) no longer matches the new layout.
    session.add(Job(document_id=doc.id, kind="ingest", mode="force"))
    await session.flush()


async def rotate_pages(
    session: AsyncSession, doc: Document, pages: list[int], degrees: int
) -> None:
    if degrees not in (90, -90, 180):
        raise PageOpError("Rotation must be 90, -90, or 180 degrees")
    with _open_source(doc) as pdf:
        selected = _check_pages(pages, len(pdf.pages))
        for number in selected:
            pdf.pages[number - 1].rotate(degrees, relative=True)
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            edited = Path(tmp.name)
        pdf.save(edited)
    try:
        await _replace_original(session, doc, edited)
    finally:
        edited.unlink(missing_ok=True)
    logger.info("rotated %d page(s) of %s by %d°", len(selected), doc.id, degrees)


async def delete_pages(
    session: AsyncSession, doc: Document, pages: list[int]
) -> None:
    with _open_source(doc) as pdf:
        selected = _check_pages(pages, len(pdf.pages))
        if len(selected) >= len(pdf.pages):
            raise PageOpError("Cannot delete every page — trash the document instead")
        for number in reversed(selected):
            del pdf.pages[number - 1]
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            edited = Path(tmp.name)
        pdf.save(edited)
    try:
        await _replace_original(session, doc, edited)
    finally:
        edited.unlink(missing_ok=True)
    logger.info("deleted %d page(s) from %s", len(selected), doc.id)


async def extract_pages(
    session: AsyncSession,
    doc: Document,
    pages: list[int],
    title: str | None,
) -> Document:
    """Copy the selected pages into a brand-new document (source untouched)."""
    with _open_source(doc) as pdf:
        selected = _check_pages(pages, len(pdf.pages))
        out = pikepdf.new()
        for number in selected:
            out.pages.append(pdf.pages[number - 1])
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            extracted = Path(tmp.name)
        out.save(extracted)

    name = (title or "").strip() or f"{doc.title} p{selected[0]}-{selected[-1]}"
    tags: list[Tag] = list(doc.tags)
    try:
        new_doc = await ingest_file(
            session,
            tenant_id=doc.tenant_id,
            source=extracted,
            filename=f"{name}.pdf",
            mime="application/pdf",
            tags=tags,
        )
    finally:
        extracted.unlink(missing_ok=True)
    logger.info(
        "extracted %d page(s) of %s into new document %s", len(selected), doc.id, new_doc.id
    )
    return new_doc
