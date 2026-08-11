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

from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Document, DocumentPage

# Pages are separated by a form feed (chr(12)) by both pdftotext and the OCR
# paths, and the count matches page_count exactly across the library — so
# existing documents index from stored text with no re-OCR. The split happens
# in SQL below; nothing here needs the character itself.

# A page of dense print is a few thousand words; this only guards against a
# pathological "page" (a broken split, a text-only fallback that produced one
# enormous run) pushing a single vector back over the position ceiling.
MAX_PAGE_CHARS = 90_000


async def reindex_pages(session: AsyncSession, document: Document) -> int:
    """Replace a document's page vectors. Returns how many were written.

    Whole-document replacement rather than a diff: page numbering shifts
    whenever pages are rotated, deleted or extracted, so a partial update
    would leave vectors pointing at content that has moved.

    Done as a single statement that splits the text inside Postgres. The
    obvious version — read the text, split in Python, add one row per page —
    ships every page back across the connection and spends minutes of
    event-loop time on a thousand-page book, long enough that the job's
    heartbeat goes stale while it runs.
    """
    # The statement below reads text_content straight from the table, and the
    # sessions here run with autoflush off — so a caller that has just assigned
    # new text (which is exactly what the ingest path does) would otherwise be
    # indexing whatever was stored before. For a freshly OCR'd document that is
    # NULL, and it would silently produce no pages at all.
    await session.flush()

    await session.execute(
        delete(DocumentPage).where(DocumentPage.document_id == document.id)
    )
    if not document.text_content:
        return 0
    result = await session.execute(
        text(
            """
            INSERT INTO document_pages (document_id, page, search_vector)
            SELECT d.id,
                   part.ordinality,
                   to_tsvector('english', left(part.body, :max_chars))
            FROM documents d,
                 unnest(string_to_array(d.text_content, chr(12)))
                     WITH ORDINALITY AS part(body, ordinality)
            WHERE d.id = :doc_id AND btrim(part.body) <> ''
            """
        ),
        {"doc_id": document.id, "max_chars": MAX_PAGE_CHARS},
    )
    return result.rowcount or 0


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
