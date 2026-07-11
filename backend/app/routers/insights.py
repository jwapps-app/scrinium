"""Library statistics: what's in here, where it came from, what it costs."""

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter
from sqlalchemy import func, select, text

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
    }
