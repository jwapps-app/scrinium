import uuid

from fastapi import APIRouter, HTTPException, status

from app.deps import DB, CurrentUser
from app.models import Document
from app.routers.documents import doc_out
from app.schemas import ClassifyResult
from app.services.background import spawn
from app.services.classify import (
    classify_document,
    classify_status,
    run_classify_all,
)

router = APIRouter(tags=["classify"])


@router.post("/documents/{doc_id}/classify", response_model=ClassifyResult)
async def classify_one(doc_id: uuid.UUID, user: CurrentUser, db: DB) -> ClassifyResult:
    # The full row on purpose: classification reads the text.
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


@router.get("/classify/run")
async def classify_all_status(user: CurrentUser, db: DB) -> dict:
    """Progress of the library-wide pass: state, examined, changed, total."""
    return await classify_status(db)


@router.post("/classify/run")
async def classify_all(user: CurrentUser, db: DB) -> dict:
    """Start a pass over every live document, in the background.

    It reads the whole library's OCR text, which on a real library is
    gigabytes and many minutes; a request that waited for it timed out at
    the proxy while the work carried on invisibly. Poll GET for progress.
    """
    state = await classify_status(db)
    if state.get("state") == "running":
        raise HTTPException(
            status.HTTP_409_CONFLICT, "A classification pass is already running"
        )
    spawn(run_classify_all(user.tenant_id))
    return {"started": True}
