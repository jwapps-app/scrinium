from sqlalchemy import Float, cast, func, select

from fastapi import APIRouter

from app.deps import DB, CurrentUser
from app.models import Document
from app.schemas import SearchResponse, SearchResult

router = APIRouter(prefix="/search", tags=["search"])


@router.get("", response_model=SearchResponse)
async def search(q: str, user: CurrentUser, db: DB, limit: int = 25) -> SearchResponse:
    q = q.strip()
    if not q:
        return SearchResponse(query=q, results=[])

    tsquery = func.websearch_to_tsquery("english", q)
    rank = cast(func.ts_rank(Document.search_vector, tsquery), Float)
    snippet = func.ts_headline(
        "english",
        func.coalesce(Document.text_content, ""),
        tsquery,
        # Plain markers, not HTML — document text is untrusted and the
        # frontend renders highlights itself.
        "StartSel=[[, StopSel=]], MaxWords=30, MinWords=15, MaxFragments=2",
    )
    rows = (
        await db.execute(
            select(Document.id, Document.title, Document.status, snippet, rank)
            .where(
                Document.tenant_id == user.tenant_id,
                Document.search_vector.op("@@")(tsquery),
            )
            .order_by(rank.desc())
            .limit(min(limit, 100))
        )
    ).all()
    return SearchResponse(
        query=q,
        results=[
            SearchResult(id=r[0], title=r[1], status=r[2], snippet=r[3], rank=r[4])
            for r in rows
        ],
    )
