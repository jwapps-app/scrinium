"""Per-page search vectors.

Why this exists: `ts_rank` scores from token positions, and Postgres records
positions only for roughly the first 16,383 words of a text. One vector for a
whole book therefore describes its opening pages. A 4.75 MB encyclopedia held
two positions for a word appearing twenty-eight times and ranked 82nd of 376
matches, while searching inside the same document found every occurrence.

A page is comfortably under that ceiling, so its positions are complete.
Summing the pages gives a score that reflects the real distribution — and it
demotes long volumes correctly, because a thousand-page book with two matching
pages scores below a short work on the subject.

Only the vector is stored. The text already lives on `documents.text_content`,
which is what snippets are drawn from.
"""

import uuid

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Document, DocumentPage

# pdftotext and the OCR paths both separate pages with a form feed, and the
# count matches page_count exactly across the library — so existing documents
# can be indexed from stored text without re-running OCR.
PAGE_BREAK = "\f"

# A page of dense print is a few thousand words; this only guards against a
# pathological "page" (a broken split, a text-only fallback that produced one
# enormous run) pushing a single vector back over the position ceiling.
MAX_PAGE_CHARS = 90_000


def split_pages(text: str | None) -> list[str]:
    """Stored text → one string per page, form feeds removed."""
    if not text:
        return []
    return [part[:MAX_PAGE_CHARS] for part in text.split(PAGE_BREAK)]


async def reindex_pages(session: AsyncSession, document: Document) -> int:
    """Replace a document's page vectors. Returns how many were written.

    Whole-document replacement rather than a diff: page numbering shifts
    whenever pages are rotated, deleted or extracted, so a partial update
    would leave vectors pointing at content that has moved.
    """
    await session.execute(
        delete(DocumentPage).where(DocumentPage.document_id == document.id)
    )
    pages = split_pages(document.text_content)
    written = 0
    for number, body in enumerate(pages, start=1):
        if not body.strip():
            continue  # blank page: nothing to match, no row worth keeping
        session.add(
            DocumentPage(
                document_id=document.id,
                page=number,
                # Computed by Postgres so the text never round-trips through
                # Python just to be thrown away.
                search_vector=func.to_tsvector("english", body),
            )
        )
        written += 1
    return written


async def documents_missing_pages(
    session: AsyncSession, limit: int
) -> list[uuid.UUID]:
    """Documents with text but no page vectors yet — the backfill work list."""
    rows = await session.execute(
        select(Document.id)
        .where(
            Document.deleted_at.is_(None),
            Document.text_content.is_not(None),
            ~select(DocumentPage.document_id)
            .where(DocumentPage.document_id == Document.id)
            .exists(),
        )
        .limit(limit)
    )
    return list(rows.scalars().all())
