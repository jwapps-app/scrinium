"""Highlights & notes on documents, plus synced reading positions."""

import json
import re
import uuid

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import defer

from app.deps import DB, CurrentUser
from app.models import Annotation, Document, ReadingPosition
from app.schemas import AnnotationCreate, AnnotationUpdate, PositionRequest

_HEX = re.compile(r"^#[0-9a-fA-F]{3,8}$")


def _safe_color(raw) -> str | None:
    """Only a hex colour. The value lands in a CSS property client-side, so
    keep it to something that cannot carry a url() or other function."""
    value = (raw or "").strip()
    return value if _HEX.match(value) else None


router = APIRouter(tags=["annotations"])


async def _owned_doc(doc_id: uuid.UUID, user, db) -> Document:
    doc = await db.get(Document, doc_id, options=[defer(Document.text_content)])
    if doc is None or doc.tenant_id != user.tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Document not found")
    return doc


def _out(a: Annotation, title: str | None = None) -> dict:
    return {
        "id": str(a.id),
        "document_id": str(a.document_id),
        "document_title": title,
        "page": a.page,
        "quote": a.quote,
        "note": a.note,
        "rects": json.loads(a.rects),
        "color": a.color,
        "created_at": a.created_at.isoformat() if a.created_at else None,
    }


@router.get("/documents/{doc_id}/annotations")
async def list_annotations(doc_id: uuid.UUID, user: CurrentUser, db: DB) -> list[dict]:
    await _owned_doc(doc_id, user, db)
    rows = (
        (
            await db.execute(
                select(Annotation)
                .where(Annotation.document_id == doc_id)
                .order_by(Annotation.page, Annotation.created_at)
            )
        )
        .scalars()
        .all()
    )
    return [_out(a) for a in rows]


@router.post("/documents/{doc_id}/annotations", status_code=status.HTTP_201_CREATED)
async def create_annotation(
    doc_id: uuid.UUID, body: AnnotationCreate, user: CurrentUser, db: DB
) -> dict:
    await _owned_doc(doc_id, user, db)
    quote = body.quote.strip()
    if not quote:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "quote and page required")
    annotation = Annotation(
        tenant_id=user.tenant_id,
        document_id=doc_id,
        page=body.page,
        quote=quote[:2000],
        note=(body.note or "").strip() or None,
        rects=json.dumps(
            [
                {"x": round(r.x, 5), "y": round(r.y, 5), "w": round(r.w, 5), "h": round(r.h, 5)}
                for r in body.rects
            ]
        ),
        color=_safe_color(body.color),
    )
    db.add(annotation)
    await db.flush()
    await db.refresh(annotation)
    return _out(annotation)


@router.patch("/annotations/{annotation_id}")
async def update_annotation(
    annotation_id: uuid.UUID, body: AnnotationUpdate, user: CurrentUser, db: DB
) -> dict:
    annotation = await db.get(Annotation, annotation_id)
    if annotation is None or annotation.tenant_id != user.tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Annotation not found")
    if "note" in body.model_fields_set:
        annotation.note = (body.note or "").strip() or None
    await db.flush()
    return _out(annotation)


@router.delete("/annotations/{annotation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_annotation(
    annotation_id: uuid.UUID, user: CurrentUser, db: DB
) -> None:
    annotation = await db.get(Annotation, annotation_id)
    if annotation is None or annotation.tenant_id != user.tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Annotation not found")
    await db.delete(annotation)
    await db.flush()


@router.get("/annotations")
async def search_annotations(user: CurrentUser, db: DB, q: str = "") -> list[dict]:
    """All highlights across the library, optionally filtered — searching
    your own markings instead of 13k documents."""
    query = (
        select(Annotation, Document.title)
        .join(Document, Annotation.document_id == Document.id)
        .where(
            Annotation.tenant_id == user.tenant_id,
            Document.deleted_at.is_(None),
        )
        .order_by(Annotation.created_at.desc())
        .limit(200)
    )
    needle = q.strip()
    if needle:
        pattern = f"%{needle}%"
        query = query.where(
            Annotation.quote.ilike(pattern) | Annotation.note.ilike(pattern)
        )
    rows = (await db.execute(query)).all()
    return [_out(a, title) for a, title in rows]


# --- Reading positions (synced across devices) ------------------------------


@router.get("/documents/{doc_id}/position")
async def get_position(doc_id: uuid.UUID, user: CurrentUser, db: DB) -> dict:
    await _owned_doc(doc_id, user, db)
    row = await db.get(ReadingPosition, (user.id, doc_id))
    return {"page": row.page if row else None}


@router.put("/documents/{doc_id}/position")
async def save_position(
    doc_id: uuid.UUID, body: PositionRequest, user: CurrentUser, db: DB
) -> dict:
    await _owned_doc(doc_id, user, db)
    page = body.page
    row = await db.get(ReadingPosition, (user.id, doc_id))
    if row is None:
        db.add(ReadingPosition(user_id=user.id, document_id=doc_id, page=page))
    else:
        row.page = page
    await db.flush()
    return {"page": page}
