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

from sqlalchemy import delete, func, select, text
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
    """Build page vectors for a document written before the trigger existed.

    Ongoing correctness is the database's job (migration 0028): a trigger on
    documents.text_content rebuilds these on every write, so no caller can
    store document text and forget to index it. That property is why the old
    generated column never had coverage gaps, and losing it briefly made
    anything writing text_content directly invisible to search.

    This exists only for the backfill, which has rows the trigger never saw.
    """
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
            # Strip form feeds too, not just spaces. A document whose OCR
            # produced nothing but page breaks yields no rows however often it
            # is indexed, so without this it comes back every sweep forever and
            # the backfill never reports itself finished.
            func.btrim(Document.text_content, " \t\r\n\f") != "",
            ~select(DocumentPage.document_id)
            .where(DocumentPage.document_id == Document.id)
            .exists(),
        )
        .limit(limit)
    )
    return list(rows.scalars().all())
