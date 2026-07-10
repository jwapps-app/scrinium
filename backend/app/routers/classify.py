import uuid

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select

from app.deps import DB, CurrentUser
from app.models import Document, Rule
from app.routers.documents import doc_out
from app.schemas import BulkClassifyResult, ClassifyResult
from app.services.classify import classify_document

router = APIRouter(tags=["classify"])


@router.post("/documents/{doc_id}/classify", response_model=ClassifyResult)
async def classify_one(doc_id: uuid.UUID, user: CurrentUser, db: DB) -> ClassifyResult:
    doc = await db.get(Document, doc_id)
    if doc is None or doc.tenant_id != user.tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Document not found")
    outcome = await classify_document(db, doc)
    await db.flush()
    await db.refresh(doc)
    return ClassifyResult(
        matched_rules=outcome.matched_rules,
        added_tags=outcome.added_tags,
        new_title=outcome.new_title,
        document=doc_out(doc),
    )


@router.post("/classify/run", response_model=BulkClassifyResult)
async def classify_all(user: CurrentUser, db: DB) -> BulkClassifyResult:
    rules = (
        await db.execute(
            select(Rule)
            .where(Rule.tenant_id == user.tenant_id, Rule.enabled)
            .order_by(Rule.priority, Rule.created_at)
        )
    ).scalars().all()
    docs = (
        await db.execute(select(Document).where(Document.tenant_id == user.tenant_id))
    ).scalars().all()

    changed = 0
    for doc in docs:
        outcome = await classify_document(db, doc, rules)
        if outcome.added_tags or outcome.new_title:
            changed += 1
    await db.flush()
    return BulkClassifyResult(documents_examined=len(docs), documents_changed=changed)
