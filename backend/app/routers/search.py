from sqlalchemy import Float, cast, func, select

from fastapi import APIRouter, HTTPException, status

from app.deps import DB, CurrentUser
import uuid

from app.models import Correspondent, Document, DocumentPage, Tag
from app.schemas import SearchResponse, SearchResult

router = APIRouter(prefix="/search", tags=["search"])


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

    tsquery = func.websearch_to_tsquery("english", q)
    snippet = func.ts_headline(
        "english",
        func.coalesce(Document.text_content, ""),
        tsquery,
        # Plain markers, not HTML — document text is untrusted and the
        # frontend renders highlights itself.
        "StartSel=[[, StopSel=]], MaxWords=30, MinWords=15, MaxFragments=2",
    )
    # Matching and ranking both come from the per-page index. The old
    # whole-document vector is gone: a tsvector caps at 1 MB, its size tracks
    # distinct lexemes rather than characters, and an encyclopedia therefore
    # blew straight past the ceiling — failing not just its indexing but the
    # entire UPDATE that wrote its OCR text, which took the ingest down with
    # it. A page is small enough that no such limit is in reach.
    #
    # It ranks better too. Postgres records token positions only for roughly
    # the first 16,383 words and ts_rank reads positions, so one vector over a
    # whole book described its opening pages: a 4.75 MB encyclopedia scored as
    # though a word appearing twenty-eight times appeared twice, and sat 82nd
    # of 376 matches. Summed pages reflect the real distribution, and demote
    # long volumes honestly — a thousand-page book with two matching pages
    # ranks below a short work on the subject.
    page_match = (
        select(DocumentPage.document_id)
        .where(
            DocumentPage.document_id == Document.id,
            DocumentPage.search_vector.op("@@")(tsquery),
        )
        .correlate(Document)
        .exists()
    )
    matches = [
        Document.tenant_id == user.tenant_id,
        Document.deleted_at.is_(None),
        page_match,
        # Scoped search: restrict to the active tag filter.
        *([Document.tags.any(Tag.id == tag_uuid)] if tag_uuid else []),
    ]

    page_score = (
        select(
            func.coalesce(func.sum(func.ts_rank(DocumentPage.search_vector, tsquery)), 0.0)
        )
        .where(
            DocumentPage.document_id == Document.id,
            DocumentPage.search_vector.op("@@")(tsquery),
        )
        .correlate(Document)
        .scalar_subquery()
    )
    pages_hit = (
        select(func.count())
        .select_from(DocumentPage)
        .where(
            DocumentPage.document_id == Document.id,
            DocumentPage.search_vector.op("@@")(tsquery),
        )
        .correlate(Document)
        .scalar_subquery()
    )
    score = cast(page_score, Float)

    rows = (
        await db.execute(
            select(
                Document.id, Document.title, Document.status, snippet,
                score, pages_hit,
            )
            .where(*matches)
            # id breaks ties, so paging can't repeat or skip a row when two
            # documents score identically.
            .order_by(score.desc(), Document.id)
            .limit(min(limit, 100))
            .offset(offset)
        )
    ).all()
    # The count comes from the same predicate, so "showing 25 of 376" is
    # honest even when the page is the last one.
    total = (
        await db.execute(
            select(func.count()).select_from(Document).where(*matches)
        )
    ).scalar_one()
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
                id=r[0], title=r[1], status=r[2], snippet=r[3],
                rank=r[4], pages_hit=r[5],
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
