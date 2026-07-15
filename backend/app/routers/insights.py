"""Library statistics: what's in here, where it came from, what it costs."""

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter
from sqlalchemy import case, func, select, text

from app.services.similarity import find_near_duplicates

from app.deps import DB, CurrentUser
from app.models import (
    DismissedDuplicate,
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
                Document.weak_ocr_dismissed.is_(False),
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


def _weak_ocr_conditions(user):
    """Ready docs whose OCR yielded suspiciously little text per page and that
    haven't been dismissed as fine-as-is."""
    return (
        Document.tenant_id == user.tenant_id,
        Document.deleted_at.is_(None),
        Document.status == "ready",
        Document.weak_ocr_dismissed.is_(False),
        Document.page_count > 0,
        Document.text_content.is_not(None),
        func.length(Document.text_content) < Document.page_count * 150,
    )


@router.get("/insights/weak-ocr")
async def weak_ocr(user: CurrentUser, db: DB, limit: int = 200) -> dict:
    """The full weak-OCR worklist for the review flow (worst first)."""
    cond = _weak_ocr_conditions(user)
    total = (
        await db.execute(select(func.count(Document.id)).where(*cond))
    ).scalar_one()
    rows = (
        await db.execute(
            select(
                Document.id,
                Document.title,
                Document.page_count,
                func.length(Document.text_content),
                Document.ocr_engine,
            )
            .where(*cond)
            .order_by(func.length(Document.text_content) / Document.page_count)
            .limit(limit)
        )
    ).all()
    return {
        "count": total,
        "items": [
            {
                "id": str(i),
                "title": t,
                "pages": p,
                "chars_per_page": round(l / p) if p else 0,
                "engine": eng,
            }
            for i, t, p, l, eng in rows
        ],
    }


@router.post("/insights/weak-ocr/dismiss")
async def dismiss_weak_ocr(body: dict, user: CurrentUser, db: DB) -> dict:
    """Mark a document's scan as acceptable so it leaves the weak-OCR list."""
    import uuid as _uuid

    from fastapi import HTTPException, status as http_status

    try:
        doc_id = _uuid.UUID(str(body.get("id")))
    except (ValueError, TypeError):
        raise HTTPException(http_status.HTTP_422_UNPROCESSABLE_ENTITY, "id required")
    doc = (
        await db.execute(
            select(Document).where(
                Document.id == doc_id, Document.tenant_id == user.tenant_id
            )
        )
    ).scalar_one_or_none()
    if doc is None:
        raise HTTPException(http_status.HTTP_404_NOT_FOUND, "Document not found")
    doc.weak_ocr_dismissed = True
    await db.flush()
    return {"dismissed": True}


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

    dismissed = {
        frozenset((row.doc_a, row.doc_b))
        for row in (
            await db.execute(
                select(DismissedDuplicate).where(
                    DismissedDuplicate.tenant_id == user.tenant_id
                )
            )
        ).scalars()
    }
    pairs = [
        p for p in find_near_duplicates([(r[0], r[1]) for r in rows])
        if frozenset((p[0], p[1])) not in dismissed
    ][:50]
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


@router.post("/insights/duplicates/dismiss")
async def dismiss_duplicate(body: dict, user: CurrentUser, db: DB) -> dict:
    """Mark a pair as "not a duplicate" so the report stops resurfacing it."""
    import uuid as _uuid

    try:
        a = _uuid.UUID(str(body.get("a")))
        b = _uuid.UUID(str(body.get("b")))
    except (ValueError, TypeError):
        from fastapi import HTTPException, status as http_status

        raise HTTPException(http_status.HTTP_422_UNPROCESSABLE_ENTITY, "a and b required")
    lo, hi = sorted((a, b))
    existing = await db.get(DismissedDuplicate, (user.tenant_id, lo, hi))
    if existing is None:
        db.add(DismissedDuplicate(tenant_id=user.tenant_id, doc_a=lo, doc_b=hi))
        await db.flush()
    return {"dismissed": True}
