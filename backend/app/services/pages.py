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

import asyncio
import json
import logging
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import pikepdf
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Annotation,
    Blob,
    Document,
    DocumentStatus,
    Job,
    ReadingPosition,
    Tag,
)
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


@dataclass
class PageEdit:
    """How an edit moved the pages, so what points at them can follow.

    `total` is the page count before the edit. `removed` are 1-based pages
    taken out; `rotated` are pages turned by `degrees` (clockwise-positive,
    as pikepdf and /Rotate have it)."""

    total: int
    removed: set[int] = field(default_factory=set)
    rotated: set[int] = field(default_factory=set)
    degrees: int = 0

    def new_page(self, page: int) -> int | None:
        """Where an old page number lands: None when the page was deleted,
        otherwise shifted up by however many earlier pages went."""
        if page in self.removed:
            return None
        return page - sum(1 for r in self.removed if r < page)

    def nearest_page(self, page: int) -> int:
        """For a resume point: the page itself when it survived, else the
        one that now sits where it was, clamped to the new extent."""
        remaining = max(1, self.total - len(self.removed))
        moved = page - sum(1 for r in self.removed if r < page)
        return min(max(moved, 1), remaining)


def rotate_rect(rect: dict, degrees: int) -> dict:
    """Turn a page-fraction rectangle with its page.

    Rects are stored as fractions of the rendered page with a top-left
    origin (x right, y down — see PdfViewer). Rotating the page clockwise by
    90° sends the old top edge to the right edge: a point (x, y) lands at
    (1 - y, x) on the new page, whose width is the old height."""
    x, y, w, h = rect["x"], rect["y"], rect["w"], rect["h"]
    turn = degrees % 360
    if turn == 90:
        out = {"x": 1 - (y + h), "y": x, "w": h, "h": w}
    elif turn == 180:
        out = {"x": 1 - (x + w), "y": 1 - (y + h), "w": w, "h": h}
    elif turn == 270:
        out = {"x": y, "y": 1 - (x + w), "w": h, "h": w}
    else:
        out = {"x": x, "y": y, "w": w, "h": h}
    return {k: round(v, 5) for k, v in out.items()}


async def _carry_user_data(
    session: AsyncSession, doc: Document, snapshot: Document, edit: PageEdit
) -> None:
    """Move highlights and resume points across an edit.

    The snapshot gets a copy of each as it was, so restoring it brings the
    highlights back where they were drawn. On the live document, highlights
    on deleted pages go (their text is gone), the rest follow their page to
    its new number, and rects on rotated pages are turned with the page.
    Left alone, they stayed at the old numbers and coordinates and drew over
    whatever had moved into their place."""
    annotations = (
        await session.execute(
            select(Annotation).where(Annotation.document_id == doc.id)
        )
    ).scalars().all()
    for a in annotations:
        session.add(
            Annotation(
                tenant_id=a.tenant_id,
                document_id=snapshot.id,
                page=a.page,
                quote=a.quote,
                note=a.note,
                rects=a.rects,
                color=a.color,
                created_at=a.created_at,
            )
        )
        target = edit.new_page(a.page)
        if target is None:
            await session.delete(a)
            continue
        if a.page in edit.rotated and edit.degrees % 360:
            try:
                rects = json.loads(a.rects)
                a.rects = json.dumps([rotate_rect(r, edit.degrees) for r in rects])
            except (ValueError, KeyError, TypeError):
                logger.warning("annotation %s has unreadable rects; left as is", a.id)
        a.page = target

    positions = (
        await session.execute(
            select(ReadingPosition).where(ReadingPosition.document_id == doc.id)
        )
    ).scalars().all()
    for pos in positions:
        session.add(
            ReadingPosition(user_id=pos.user_id, document_id=snapshot.id, page=pos.page)
        )
        pos.page = edit.nearest_page(pos.page)


async def _replace_original(
    session: AsyncSession, doc: Document, edited: Path, edit: PageEdit
) -> None:
    """Swap in a new original blob and re-queue OCR.

    The pre-edit version isn't destroyed: its blobs are handed to a snapshot
    document that goes straight to the trash — restorable for the retention
    window, purged automatically after. Undo for the one destructive edit in
    the app, at zero disk cost (the blobs just change owner). Highlights and
    resume points are copied to the snapshot and remapped on the live row."""
    # Hashing and copying a whole book is file work; on the event loop it
    # froze every other request for the duration.
    new_id, sha256, size = await asyncio.to_thread(storage.store_file, edited)
    # The row arrives with text_content deferred (ownership checks do not
    # need it); the snapshot does, so load it explicitly before reading it.
    await session.refresh(doc, attribute_names=["text_content"])
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
    await session.flush()  # the snapshot needs its id before rows can point at it
    await _carry_user_data(session, doc, snapshot, edit)
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


def _edit_copy(
    doc: Document, pages: list[int], degrees: int | None
) -> tuple[Path, list[int], int]:
    """Write an edited copy of the original: rotated when `degrees` is given,
    otherwise with the selected pages removed. Runs in a thread — pikepdf is
    pure CPU and disk, and a 700-page save takes seconds. Returns the copy,
    the pages touched, and the page count before the edit."""
    with _open_source(doc) as pdf:
        total = len(pdf.pages)
        selected = _check_pages(pages, total)
        if degrees is not None:
            for number in selected:
                pdf.pages[number - 1].rotate(degrees, relative=True)
        else:
            if len(selected) >= len(pdf.pages):
                raise PageOpError("Cannot delete every page — trash the document instead")
            for number in reversed(selected):
                del pdf.pages[number - 1]
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            edited = Path(tmp.name)
        pdf.save(edited)
    return edited, selected, total


async def rotate_pages(
    session: AsyncSession, doc: Document, pages: list[int], degrees: int
) -> None:
    if degrees not in (90, -90, 180):
        raise PageOpError("Rotation must be 90, -90, or 180 degrees")
    edited, selected, total = await asyncio.to_thread(_edit_copy, doc, pages, degrees)
    try:
        await _replace_original(
            session, doc, edited,
            PageEdit(total=total, rotated=set(selected), degrees=degrees),
        )
    finally:
        edited.unlink(missing_ok=True)
    logger.info("rotated %d page(s) of %s by %d°", len(selected), doc.id, degrees)


async def delete_pages(
    session: AsyncSession, doc: Document, pages: list[int]
) -> None:
    edited, selected, total = await asyncio.to_thread(_edit_copy, doc, pages, None)
    try:
        await _replace_original(
            session, doc, edited, PageEdit(total=total, removed=set(selected))
        )
    finally:
        edited.unlink(missing_ok=True)
    logger.info("deleted %d page(s) from %s", len(selected), doc.id)


def _extract_copy(doc: Document, pages: list[int]) -> tuple[Path, list[int]]:
    with _open_source(doc) as pdf:
        selected = _check_pages(pages, len(pdf.pages))
        out = pikepdf.new()
        for number in selected:
            out.pages.append(pdf.pages[number - 1])
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            extracted = Path(tmp.name)
        out.save(extracted)
    return extracted, selected


async def extract_pages(
    session: AsyncSession,
    doc: Document,
    pages: list[int],
    title: str | None,
) -> Document:
    """Copy the selected pages into a brand-new document (source untouched)."""
    extracted, selected = await asyncio.to_thread(_extract_copy, doc, pages)

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
