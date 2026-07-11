"""Library statistics: what's in here, where it came from, what it costs."""

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter
from sqlalchemy import case, func, select, text

from app.services.similarity import find_near_duplicates

from app.deps import DB, CurrentUser
from app.models import (
    Blob,
    Correspondent,
    DocType,
    Document,
    Tag,
    document_tags,
)

router = APIRouter(tags=["insights"])


@router.get("/insights")
async def insights(user: CurrentUser, db: DB) -> dict:
    live = (
        Document.tenant_id == user.tenant_id,
        Document.deleted_at.is_(None),
    )

    totals = (
        await db.execute(
            select(
                func.count(Document.id),
                func.coalesce(func.sum(Document.page_count), 0),
            ).where(*live)
        )
    ).one()

    storage_bytes = (
        await db.execute(
            select(func.coalesce(func.sum(Blob.size_bytes), 0)).where(
                Blob.id.in_(
                    select(Document.original_blob_id).where(*live)
                )
                | Blob.id.in_(
                    select(Document.archive_blob_id).where(
                        *live, Document.archive_blob_id.is_not(None)
                    )
                )
            )
        )
    ).scalar_one()

    # Documents added per month, last 12 months (calendar months).
    start = (datetime.now(timezone.utc).replace(day=1) - timedelta(days=366)).replace(
        day=1
    )
    monthly_rows = (
        await db.execute(
            select(
                func.to_char(func.date_trunc("month", Document.created_at), "YYYY-MM"),
                func.count(Document.id),
            )
            .where(*live, Document.created_at >= start)
            .group_by(text("1"))
            .order_by(text("1"))
        )
    ).all()

    async def top(entity, join_col, limit=8):
        rows = (
            await db.execute(
                select(entity.name, func.count(Document.id))
                .join(Document, join_col == entity.id)
                .where(*live)
                .group_by(entity.id)
                .order_by(func.count(Document.id).desc())
                .limit(limit)
            )
        ).all()
        return [{"name": n, "count": c} for n, c in rows]

    top_tags_rows = (
        await db.execute(
            select(Tag.name, Tag.color, func.count(document_tags.c.document_id))
            .join(document_tags, document_tags.c.tag_id == Tag.id)
            .join(Document, Document.id == document_tags.c.document_id)
            .where(*live)
            .group_by(Tag.id)
            .order_by(func.count(document_tags.c.document_id).desc())
            .limit(10)
        )
    ).all()

    engines = (
        await db.execute(
            select(
                func.coalesce(Document.ocr_engine, "unprocessed"),
                func.count(Document.id),
            )
            .where(*live)
            .group_by(text("1"))
            .order_by(func.count(Document.id).desc())
        )
    ).all()

    # Suspiciously little text per page: candidates for a better scan or
    # a re-OCR (OCR "succeeded" but yielded almost nothing).
    low_yield_rows = (
        await db.execute(
            select(
                Document.id,
                Document.title,
                Document.page_count,
                func.length(Document.text_content),
            )
            .where(
                *live,
                Document.status == "ready",
                Document.page_count > 0,
                Document.text_content.is_not(None),
                func.length(Document.text_content)
                < Document.page_count * 150,
            )
            .order_by(
                (func.length(Document.text_content) / Document.page_count)
            )
            .limit(10)
        )
    ).all()

    return {
        "documents": totals[0],
        "pages": int(totals[1]),
        "storage_bytes": int(storage_bytes),
        "monthly": [{"month": m, "count": c} for m, c in monthly_rows],
        "correspondents": await top(Correspondent, Document.correspondent_id),
        "doc_types": await top(DocType, Document.doc_type_id),
        "tags": [
            {"name": n, "color": col, "count": c} for n, col, c in top_tags_rows
        ],
        "engines": [{"name": n, "count": c} for n, c in engines],
        "low_yield": [
            {
                "id": str(i),
                "title": t,
                "pages": p,
                "chars_per_page": round(l / p) if p else 0,
            }
            for i, t, p, l in low_yield_rows
        ],
    }


@router.get("/insights/duplicates")
async def possible_duplicates(user: CurrentUser, db: DB) -> dict:
    """Near-duplicate pairs by content fingerprint — the same document
    scanned twice, not byte-identical (that's blocked at ingest)."""
    rows = (
        await db.execute(
            select(Document.id, Document.simhash).where(
                Document.tenant_id == user.tenant_id,
                Document.deleted_at.is_(None),
                Document.simhash.is_not(None),
                Document.simhash != 0,
            )
        )
    ).all()
    pending = (
        await db.execute(
            select(func.count(Document.id)).where(
                Document.tenant_id == user.tenant_id,
                Document.deleted_at.is_(None),
                Document.simhash.is_(None),
                Document.text_content.is_not(None),
            )
        )
    ).scalar_one()

    pairs = find_near_duplicates([(r[0], r[1]) for r in rows])[:50]
    ids = {i for a, b, _ in pairs for i in (a, b)}
    docs = {}
    if ids:
        for doc in (
            await db.execute(select(Document).where(Document.id.in_(ids)))
        ).scalars():
            docs[doc.id] = {
                "id": str(doc.id),
                "title": doc.title,
                "page_count": doc.page_count,
                "created_at": doc.created_at.isoformat(),
            }
    return {
        "fingerprinted": len(rows),
        "pending_fingerprint": pending,
        "pairs": [
            {
                "a": docs[a],
                "b": docs[b],
                "similarity": round((1 - dist / 64) * 100),
            }
            for a, b, dist in pairs
            if a in docs and b in docs
        ],
    }
