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
    # Batched by id keyset: loading every document's full OCR text at once is
    # a memory spike on a large library, and trashed docs shouldn't reclassify.
    examined = 0
    changed = 0
    last_id = None
    while True:
        q = (
            select(Document)
            .where(
                Document.tenant_id == user.tenant_id,
                Document.deleted_at.is_(None),
            )
            .order_by(Document.id)
            .limit(200)
        )
        if last_id is not None:
            q = q.where(Document.id > last_id)
        docs = (await db.execute(q)).scalars().all()
        if not docs:
            break
        for doc in docs:
            outcome = await classify_document(db, doc, rules)
            if outcome.added_tags or outcome.new_title:
                changed += 1
            last_id = doc.id
        examined += len(docs)
        await db.flush()
        # Release the batch's loaded text before the next one.
        db.expunge_all()
    return BulkClassifyResult(documents_examined=examined, documents_changed=changed)
