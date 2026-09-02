from sqlalchemy import func, select, text

from fastapi import APIRouter, HTTPException, status

from app.deps import DB, CurrentUser
import uuid

from app.models import Correspondent, Document, Tag
from app.schemas import SearchResponse, SearchResult

router = APIRouter(prefix="/search", tags=["search"])


# One pass over the page index, then a join — not a subquery per document.
#
# The previous shape correlated three subqueries on document_pages to each
# row of documents, and Postgres executed the `search_vector @@ query` GIN
# scan inside that per-document loop. For a term matching many pages the same
# 80,000-row index scan ran once for every document in the library: measured
# at 88 seconds for "steam engine" (3,783 matches), and the separate count
# query then did it all again. Aggregating the index once — sum of page
# ranks, page count, and the best page per document — took 451 ms on the
# same data. The count now rides along as a window function over the same
# match set, so it cannot disagree with the rows.
#
# The snippet is drawn from the best-matching page rather than the whole
# text. ts_headline over a full document detoasts and parses megabytes per
# result (175 ms each on this library); a page is a few thousand characters.
# Pages are split on the same form feed the index uses, so `best_page` lines
# up with split_part's 1-based numbering. A title-only match has no page, so
# it headlines the opening of the text instead.
_SEARCH_SQL = """
WITH q AS (
    SELECT websearch_to_tsquery('english', :q) AS query
),
hits AS (
    SELECT p.document_id,
           sum(ts_rank(p.search_vector, q.query)) AS page_score,
           count(*) AS pages_hit,
           (array_agg(p.page ORDER BY ts_rank(p.search_vector, q.query) DESC))[1]
               AS best_page
    FROM document_pages p, q
    WHERE p.search_vector @@ q.query
    GROUP BY p.document_id
),
matched AS (
    SELECT d.id, d.title, d.status, d.text_content,
           coalesce(h.pages_hit, 0) AS pages_hit,
           h.best_page,
           coalesce(h.page_score, 0) + ts_rank(d.title_vector, q.query) * 2.0 AS score
    FROM documents d
    CROSS JOIN q
    LEFT JOIN hits h ON h.document_id = d.id
    WHERE d.tenant_id = :tenant_id
      AND d.deleted_at IS NULL
      AND (h.document_id IS NOT NULL OR d.title_vector @@ q.query)
      {tag_clause}
),
page AS (
    SELECT id, title, status, text_content, pages_hit, best_page, score,
           count(*) OVER () AS total
    FROM matched
    ORDER BY score DESC, id
    LIMIT :limit OFFSET :offset
)
SELECT page.id, page.title, page.status, page.score, page.pages_hit, page.total,
       ts_headline(
           'english',
           CASE WHEN page.best_page IS NULL
                THEN left(coalesce(page.text_content, ''), 20000)
                ELSE split_part(coalesce(page.text_content, ''), chr(12), page.best_page)
           END,
           q.query,
           'StartSel=[[, StopSel=]], MaxWords=30, MinWords=15, MaxFragments=2'
       ) AS snippet
FROM page CROSS JOIN q
ORDER BY page.score DESC, page.id
"""

_TAG_CLAUSE = """
      AND EXISTS (
          SELECT 1 FROM document_tags dt
          WHERE dt.document_id = d.id AND dt.tag_id = :tag_id
      )
"""


@router.get("", response_model=SearchResponse)
async def search(
    q: str,
    user: CurrentUser,
    db: DB,
    limit: int = 25,
    offset: int = 0,
    tag_id: str | None = None,
) -> SearchResponse:
    q = q.strip()
    offset = max(0, offset)
    if not q:
        return SearchResponse(query=q, results=[])

    # Validate up front — a malformed tag_id should be a 422, not a 500.
    tag_uuid = None
    if tag_id:
        try:
            tag_uuid = uuid.UUID(tag_id)
        except ValueError:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY, "tag_id must be a UUID"
            )

    params = {
        "q": q,
        "tenant_id": user.tenant_id,
        "limit": min(limit, 100),
        "offset": offset,
    }
    if tag_uuid is not None:
        params["tag_id"] = tag_uuid
    sql = _SEARCH_SQL.format(tag_clause=_TAG_CLAUSE if tag_uuid is not None else "")
    rows = (await db.execute(text(sql), params)).all()
    total = int(rows[0].total) if rows else 0

    suggestions: list[str] = []
    if not rows and len(q) >= 3 and " " not in q:
        # Zero hits on a single word: offer close matches from titles,
        # tags, and correspondents (pg_trgm similarity — catches typos).
        seen = set()
        for column, table_filter in (
            (Document.title, Document.tenant_id == user.tenant_id),
            (Tag.name, Tag.tenant_id == user.tenant_id),
            (Correspondent.name, Correspondent.tenant_id == user.tenant_id),
        ):
            hits = (
                await db.execute(
                    select(column)
                    # word_similarity: the query against the closest word
                    # INSIDE the value — long titles still match a typo of
                    # one of their words.
                    .where(table_filter, func.word_similarity(q, column) > 0.4)
                    .order_by(func.word_similarity(q, column).desc())
                    .limit(3)
                )
            ).scalars().all()
            for hit in hits:
                # Suggest the specific similar word when the match is a
                # long title; otherwise the whole name.
                candidates = [w for w in hit.split() if func_sim(w, q) > 0.4] or [hit]
                for c in candidates[:1]:
                    key = c.lower()
                    if key not in seen and key != q.lower():
                        seen.add(key)
                        suggestions.append(c)
        suggestions = suggestions[:3]

    return SearchResponse(
        query=q,
        results=[
            SearchResult(
                id=r.id, title=r.title, status=r.status, snippet=r.snippet or "",
                rank=float(r.score or 0.0), pages_hit=int(r.pages_hit or 0),
            )
            for r in rows
        ],
        suggestions=suggestions,
        total=total,
        offset=offset,
    )


def func_sim(a: str, b: str) -> float:
    """Cheap client-side trigram similarity mirror for word extraction."""
    def grams(x):
        x = f"  {x.lower()} "
        return {x[i : i + 3] for i in range(len(x) - 2)}
    ga, gb = grams(a), grams(b)
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / len(ga | gb)
