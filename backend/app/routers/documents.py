import asyncio
import re
import tempfile
import uuid
from datetime import date, timedelta
from pathlib import Path

from typing import Annotated

import aiofiles
from fastapi import APIRouter, Form, HTTPException, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy import func, select, text

from app.deps import DB, CurrentUser
from app.models import Blob, Document, DocumentStatus, Job, JobStatus, Tag
from app.schemas import (
    BulkActionRequest,
    BulkActionResult,
    DocumentList,
    DocumentOut,
    DocumentUpdate,
    ReprocessRequest,
)
from datetime import datetime, timezone
from app.config import settings as app_settings
from app.models import Correspondent, CustomField, DocType, document_custom_values
from app.services import deletion, intake, storage, thumbnails
from app.services.dates import extract_document_date
from app.services.app_state import PROCESSING_PAUSED, get_flag, set_flag
from app.services.intake import ACCEPTED_SUFFIXES
from app.services.tag_tree import with_ancestors

router = APIRouter(prefix="/documents", tags=["documents"])


def doc_out(
    doc: Document,
    progress: tuple[float, str | None] | None = None,
    custom_values: dict[str, str] | None = None,
) -> DocumentOut:
    out = DocumentOut.model_validate(doc)
    out.has_archive = doc.archive_blob_id is not None
    out.has_thumbnail = doc.thumbnail_blob_id is not None
    out.correspondent_name = doc.correspondent.name if doc.correspondent else None
    out.doc_type_name = doc.doc_type.name if doc.doc_type else None
    if progress is not None:
        out.progress, out.phase = progress
    if custom_values is not None:
        out.custom_values = custom_values
    return out


async def _progress_map(
    db, doc_ids: list[uuid.UUID]
) -> dict[uuid.UUID, tuple[float, str | None]]:
    """document_id -> (fraction complete, phase) for running OCR jobs."""
    if not doc_ids:
        return {}
    rows = (
        await db.execute(
            select(
                Job.document_id, Job.pages_done, Job.pages_total, Job.phase
            ).where(Job.document_id.in_(doc_ids), Job.status == JobStatus.RUNNING)
        )
    ).all()
    return {
        doc_id: (min(done / total, 1.0), phase)
        for doc_id, done, total, phase in rows
        if done is not None and total
    }


SORTS = {
    "newest": Document.created_at.desc(),
    "oldest": Document.created_at.asc(),
    "docdate": func.coalesce(Document.doc_date, func.date(Document.created_at)).desc(),
    "title": func.lower(Document.title).asc(),
    "updated": Document.updated_at.desc(),
}


@router.post("", response_model=DocumentOut, status_code=status.HTTP_201_CREATED)
async def upload(
    file: UploadFile,
    user: CurrentUser,
    db: DB,
    ocr_text: Annotated[str | None, Form()] = None,
    ocr_engine: Annotated[str | None, Form()] = None,
    page_count: Annotated[int | None, Form()] = None,
) -> DocumentOut:
    """Ingest a document.

    Capture-time OCR (three-tier model): when the iOS app scans, it runs
    Vision on-device and sends `ocr_text` alongside the file — the freshest
    image meets the best engine, and the server never re-OCRs it. Such
    documents go straight to `ready` with no ingest job.
    """
    filename = file.filename or "upload.pdf"
    suffix = Path(filename).suffix.lower()
    if suffix not in ACCEPTED_SUFFIXES:
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            f"Unsupported file type {suffix or '(none)'}",
        )

    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        async with aiofiles.open(tmp_path, "wb") as out:
            while chunk := await file.read(1024 * 1024):
                await out.write(chunk)
        try:
            doc = await intake.ingest_file(
                db,
                user.tenant_id,
                tmp_path,
                filename,
                mime=file.content_type,
                ocr_text=ocr_text,
                ocr_engine=ocr_engine,
                page_count=page_count,
            )
        except intake.DuplicateDocument as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc))
    finally:
        tmp_path.unlink(missing_ok=True)
    return doc_out(doc)


@router.get("", response_model=DocumentList)
async def list_documents(
    user: CurrentUser,
    db: DB,
    status_filter: str | None = None,
    tag_id: uuid.UUID | None = None,
    correspondent_id: uuid.UUID | None = None,
    doc_type_id: uuid.UUID | None = None,
    engine: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    sort: str = "newest",
    offset: int = 0,
    limit: int = 50,
) -> DocumentList:
    conditions = [Document.tenant_id == user.tenant_id]
    if status_filter == "trash":
        conditions.append(Document.deleted_at.is_not(None))
    else:
        conditions.append(Document.deleted_at.is_(None))
        if status_filter:
            # Comma-separated, so the UI's "Processing" bucket (pending +
            # processing) is one query.
            conditions.append(Document.status.in_(status_filter.split(",")))
    if tag_id:
        conditions.append(Document.tags.any(Tag.id == tag_id))
    if correspondent_id:
        conditions.append(Document.correspondent_id == correspondent_id)
    if doc_type_id:
        conditions.append(Document.doc_type_id == doc_type_id)
    if engine:
        conditions.append(Document.ocr_engine == engine)
    # Date filters mean the document's own date, falling back to when it
    # was added for docs without one.
    effective_date = func.coalesce(Document.doc_date, func.date(Document.created_at))
    if date_from:
        conditions.append(effective_date >= date_from)
    if date_to:
        conditions.append(effective_date <= date_to)

    total = (
        await db.execute(select(func.count(Document.id)).where(*conditions))
    ).scalar_one()
    docs = (
        await db.execute(
            select(Document)
            .where(*conditions)
            .order_by(SORTS.get(sort, SORTS["newest"]))
            .offset(offset)
            .limit(min(limit, 200))
        )
    ).scalars().all()
    running = [d.id for d in docs if d.status == DocumentStatus.PROCESSING]
    progress = await _progress_map(db, running)
    return DocumentList(
        items=[doc_out(d, progress.get(d.id)) for d in docs], total=total
    )


@router.get("/stats")
async def library_stats(user: CurrentUser, db: DB) -> dict:
    counts = dict(
        (
            await db.execute(
                select(Document.status, func.count(Document.id))
                .where(
                    Document.tenant_id == user.tenant_id,
                    Document.deleted_at.is_(None),
                )
                .group_by(Document.status)
            )
        ).all()
    )
    trash_count = (
        await db.execute(
            select(func.count(Document.id)).where(
                Document.tenant_id == user.tenant_id,
                Document.deleted_at.is_not(None),
            )
        )
    ).scalar_one()
    recent_added = (
        await db.execute(
            select(Document.id, Document.title)
            .where(
                Document.tenant_id == user.tenant_id, Document.deleted_at.is_(None)
            )
            .order_by(Document.created_at.desc())
            .limit(5)
        )
    ).all()

    now = datetime.now(timezone.utc)

    # Every currently-running job → one live bar. Ordered by start time so
    # the bars keep stable slots instead of reshuffling as progress changes.
    running_rows = (
        await db.execute(
            select(Job, Document.title)
            .join(Document, Job.document_id == Document.id)
            .where(
                Job.status == JobStatus.RUNNING,
                Document.tenant_id == user.tenant_id,
            )
            .order_by(Job.started_at)
        )
    ).all()
    running = []
    for job, title in running_rows:
        prog = (
            min(job.pages_done / job.pages_total, 1.0)
            if job.pages_done and job.pages_total
            else 0.0
        )
        eta = None
        if job.started_at and job.pages_done and job.pages_total:
            elapsed = (now - job.started_at).total_seconds()
            per_page = elapsed / job.pages_done
            eta = int(max(0, (job.pages_total - job.pages_done) * per_page))
        running.append(
            {
                "id": str(job.document_id),
                "title": title,
                "progress": prog,
                "phase": job.phase,
                "eta_seconds": eta,
            }
        )

    remaining = counts.get(DocumentStatus.PENDING, 0) + counts.get(
        DocumentStatus.PROCESSING, 0
    )

    # Throughput-based queue ETA: measure documents actually completed in a
    # recent window, so parallelism and real per-doc cost are baked in.
    window_min = 5
    done_recent = (
        await db.execute(
            select(func.count(Job.id))
            .join(Document, Job.document_id == Document.id)
            .where(
                Job.status == JobStatus.DONE,
                Job.finished_at >= now - timedelta(minutes=window_min),
                Document.tenant_id == user.tenant_id,
            )
        )
    ).scalar_one()
    rate_per_min = done_recent / window_min
    queue_eta = (
        int(remaining / rate_per_min * 60) if rate_per_min > 0 and remaining else None
    )

    return {
        "total": sum(counts.values()),
        "ready": counts.get(DocumentStatus.READY, 0),
        "processing": remaining,
        "flagged": counts.get(DocumentStatus.FLAGGED, 0),
        "trash": trash_count,
        "recent": [{"id": str(r[0]), "title": r[1]} for r in recent_added],
        "paused": await get_flag(db, PROCESSING_PAUSED),
        "running": running,
        "running_count": len(running),
        "rate_per_min": round(rate_per_min, 2),
        "queue_eta_seconds": queue_eta,
    }


@router.post("/extract-dates")
async def extract_dates(user: CurrentUser, db: DB) -> dict:
    """Backfill document dates for existing docs that don't have one,
    from their already-stored text. Cheap — no OCR involved."""
    docs = (
        await db.execute(
            select(Document).where(
                Document.tenant_id == user.tenant_id,
                Document.doc_date.is_(None),
                Document.text_content.is_not(None),
                Document.deleted_at.is_(None),
            )
        )
    ).scalars().all()
    updated = 0
    for doc in docs:
        found = extract_document_date(doc.text_content)
        if found:
            doc.doc_date = found
            updated += 1
    await db.flush()
    return {"examined": len(docs), "dated": updated}


@router.post("/processing")
async def set_processing(body: dict, user: CurrentUser, db: DB) -> dict:
    """Pause/resume the worker queue. Pausing lets the current file finish
    and stops new claims and watch-folder sweeps — safe to restart the
    server or the Mac (Apple OCR host) without losing work or quality."""
    paused = bool(body.get("paused"))
    await set_flag(db, PROCESSING_PAUSED, paused)
    return {"paused": paused}


async def _get_owned(doc_id: uuid.UUID, user, db) -> Document:
    doc = await db.get(Document, doc_id)
    if doc is None or doc.tenant_id != user.tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Document not found")
    return doc


async def _custom_values(db, doc_id: uuid.UUID) -> dict[str, str]:
    rows = (
        await db.execute(
            select(
                document_custom_values.c.field_id, document_custom_values.c.value
            ).where(document_custom_values.c.document_id == doc_id)
        )
    ).all()
    return {str(field_id): value for field_id, value in rows}


@router.get("/{doc_id}", response_model=DocumentOut)
async def get_document(doc_id: uuid.UUID, user: CurrentUser, db: DB) -> DocumentOut:
    doc = await _get_owned(doc_id, user, db)
    progress = await _progress_map(db, [doc.id])
    return doc_out(doc, progress.get(doc.id), await _custom_values(db, doc.id))


@router.get("/{doc_id}/file")
async def download(
    doc_id: uuid.UUID,
    user: CurrentUser,
    db: DB,
    version: str = "archive",
    disposition: str = "inline",
) -> FileResponse:
    doc = await _get_owned(doc_id, user, db)
    if version == "archive" and doc.archive_blob_id is not None:
        blob_id = doc.archive_blob_id
        # Pretty name is applied only here, at download time.
        filename = f"{doc.title}.pdf"
        media_type = "application/pdf"
    else:
        blob_id = doc.original_blob_id
        filename = doc.original_filename
        blob = await db.get(Blob, blob_id)
        media_type = blob.mime_type if blob else "application/octet-stream"
    path = storage.blob_file(blob_id)
    if not path.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Blob missing from store")
    dispo = "attachment" if disposition == "attachment" else "inline"
    return FileResponse(
        path,
        media_type=media_type,
        filename=filename,
        content_disposition_type=dispo,
    )


@router.get("/{doc_id}/search")
async def search_within(
    doc_id: uuid.UUID, q: str, user: CurrentUser, db: DB
) -> dict:
    """Locate a query inside one document, page by page.

    Splits the stored text on the form-feed page separators and matches each
    page with the same tsvector/stemming pipeline as global search, so
    "blueberry" finds "BLUEBERRIES" here too. Returns the literal matched
    words (extracted from ts_headline markers) so the viewer can highlight
    the exact strings that appear on the page.
    """
    doc = await _get_owned(doc_id, user, db)
    q = q.strip()
    if not q or not doc.text_content:
        return {"query": q, "pages": [], "terms": []}

    rows = (
        await db.execute(
            text(
                """
                SELECT t.ord::int AS page,
                       ts_headline('english', t.page_text, query,
                           'StartSel=[[, StopSel=]], MaxFragments=4, MaxWords=10, MinWords=4'
                       ) AS snippet
                FROM documents d
                CROSS JOIN websearch_to_tsquery('english', :q) AS query
                CROSS JOIN LATERAL unnest(string_to_array(d.text_content, E'\f'))
                     WITH ORDINALITY AS t(page_text, ord)
                WHERE d.id = :doc_id
                  AND to_tsvector('english', t.page_text) @@ query
                ORDER BY t.ord
                """
            ),
            {"q": q, "doc_id": str(doc.id)},
        )
    ).all()

    marker = re.compile(r"\[\[(.+?)\]\]")
    pages = []
    all_terms: set[str] = set()
    for page, snippet in rows:
        terms = sorted({m.group(1) for m in marker.finditer(snippet or "")})
        all_terms.update(terms)
        pages.append({"page": page, "terms": terms, "snippet": snippet})
    return {"query": q, "pages": pages, "terms": sorted(all_terms)}


@router.get("/{doc_id}/thumbnail")
async def thumbnail(doc_id: uuid.UUID, user: CurrentUser, db: DB) -> FileResponse:
    doc = await _get_owned(doc_id, user, db)
    if doc.thumbnail_blob_id is None:
        # Lazy backfill for documents ingested before thumbnails existed.
        generated = await _generate_thumbnail(doc, db)
        if not generated:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "No thumbnail")
    path = storage.blob_file(doc.thumbnail_blob_id)
    if not path.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Thumbnail blob missing")
    return FileResponse(path, media_type="image/png")


async def _generate_thumbnail(doc: Document, db) -> bool:
    if doc.archive_blob_id is not None:
        source_blob, suffix = doc.archive_blob_id, ".pdf"
    else:
        source_blob = doc.original_blob_id
        suffix = Path(doc.original_filename).suffix.lower() or ".pdf"

    def render() -> tuple[uuid.UUID, str, int] | None:
        with tempfile.TemporaryDirectory(prefix="thumb-") as tmp:
            workdir = Path(tmp)
            source = workdir / f"input{suffix}"
            source.symlink_to(storage.blob_file(source_blob))
            out = thumbnails.make_thumbnail(source, workdir)
            return storage.store_file(out) if out else None

    stored = await asyncio.to_thread(render)
    if stored is None:
        return False
    t_id, t_sha, t_size = stored
    db.add(Blob(id=t_id, sha256=t_sha, size_bytes=t_size, mime_type="image/png"))
    doc.thumbnail_blob_id = t_id
    await db.flush()
    return True


@router.patch("/{doc_id}", response_model=DocumentOut)
async def update_document(
    doc_id: uuid.UUID, body: DocumentUpdate, user: CurrentUser, db: DB
) -> DocumentOut:
    doc = await _get_owned(doc_id, user, db)
    if body.title is not None:
        doc.title = body.title
    if body.clear_doc_date:
        doc.doc_date = None
    elif body.doc_date is not None:
        doc.doc_date = body.doc_date
    if body.clear_correspondent:
        doc.correspondent_id = None
    elif body.correspondent_id is not None:
        corr = await db.get(Correspondent, body.correspondent_id)
        if corr is None or corr.tenant_id != user.tenant_id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Correspondent not found")
        doc.correspondent_id = corr.id
    if body.clear_doc_type:
        doc.doc_type_id = None
    elif body.doc_type_id is not None:
        dtype = await db.get(DocType, body.doc_type_id)
        if dtype is None or dtype.tenant_id != user.tenant_id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Document type not found")
        doc.doc_type_id = dtype.id
    if body.custom_values is not None:
        for field_id, value in body.custom_values.items():
            field = await db.get(CustomField, field_id)
            if field is None or field.tenant_id != user.tenant_id:
                continue
            await db.execute(
                document_custom_values.delete().where(
                    document_custom_values.c.document_id == doc.id,
                    document_custom_values.c.field_id == field_id,
                )
            )
            if value.strip():
                await db.execute(
                    document_custom_values.insert().values(
                        document_id=doc.id, field_id=field_id, value=value.strip()
                    )
                )
    if body.tag_ids is not None:
        tags = (
            await db.execute(
                select(Tag).where(
                    Tag.tenant_id == user.tenant_id, Tag.id.in_(body.tag_ids)
                )
            )
        ).scalars().all()
        # A tag implies its ancestors (hierarchy semantics).
        doc.tags = await with_ancestors(db, list(tags))
    await db.flush()
    await db.refresh(doc)
    return doc_out(doc, custom_values=await _custom_values(db, doc.id))


@router.post("/{doc_id}/reprocess", response_model=DocumentOut)
async def reprocess(
    doc_id: uuid.UUID, body: ReprocessRequest, user: CurrentUser, db: DB
) -> DocumentOut:
    doc = await _get_owned(doc_id, user, db)
    doc.status = DocumentStatus.PENDING
    doc.error = None
    db.add(Job(document_id=doc.id, kind="ingest", mode=body.mode))
    await db.flush()
    await db.refresh(doc)
    return doc_out(doc)


@router.delete("/{doc_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_document(doc_id: uuid.UUID, user: CurrentUser, db: DB) -> None:
    """Soft delete: the document moves to the trash and purges for real
    after the retention window (or explicitly via /purge)."""
    doc = await _get_owned(doc_id, user, db)
    doc.deleted_at = datetime.now(timezone.utc)
    await db.flush()


@router.post("/{doc_id}/restore", response_model=DocumentOut)
async def restore_document(
    doc_id: uuid.UUID, user: CurrentUser, db: DB
) -> DocumentOut:
    doc = await _get_owned(doc_id, user, db)
    doc.deleted_at = None
    await db.flush()
    await db.refresh(doc)
    return doc_out(doc)


@router.delete("/{doc_id}/purge", status_code=status.HTTP_204_NO_CONTENT)
async def purge_document(doc_id: uuid.UUID, user: CurrentUser, db: DB) -> None:
    doc = await _get_owned(doc_id, user, db)
    await deletion.purge_document(db, doc)


@router.post("/bulk", response_model=BulkActionResult)
async def bulk_action(
    body: BulkActionRequest, user: CurrentUser, db: DB
) -> BulkActionResult:
    """Apply one action to many documents. Unknown/foreign ids are skipped.

    With `filter_tag_id`, acts on every document carrying that tag, 500 per
    call — the response's `remaining` says how many are left; call again
    until it reaches zero."""
    remaining = 0
    heavy = body.action in ("delete", "purge")
    if body.filter_trash:
        base = select(Document).where(
            Document.tenant_id == user.tenant_id, Document.deleted_at.is_not(None)
        )
        count_where = [
            Document.tenant_id == user.tenant_id, Document.deleted_at.is_not(None)
        ]
    elif body.filter_tag_id is not None:
        base = select(Document).where(
            Document.tenant_id == user.tenant_id,
            Document.tags.any(Tag.id == body.filter_tag_id),
            Document.deleted_at.is_(None),
        )
        count_where = [
            Document.tenant_id == user.tenant_id,
            Document.tags.any(Tag.id == body.filter_tag_id),
            Document.deleted_at.is_(None),
        ]
    if body.filter_trash or body.filter_tag_id is not None:
        if heavy:
            # Purges do file IO — chunk them; caller repeats while remaining.
            docs = (await db.execute(base.limit(500))).scalars().all()
            total = (
                await db.execute(select(func.count(Document.id)).where(*count_where))
            ).scalar_one()
            remaining = max(total - len(docs), 0)
        else:
            # Row-only actions are cheap; one pass over the whole filter.
            docs = (await db.execute(base)).scalars().all()
    elif body.ids:
        docs = (
            await db.execute(
                select(Document).where(
                    Document.tenant_id == user.tenant_id, Document.id.in_(body.ids)
                )
            )
        ).scalars().all()
    else:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "Provide ids or filter_tag_id"
        )

    tags: list[Tag] = []
    if body.action in ("add_tags", "remove_tags") and body.tag_ids:
        tags = (
            await db.execute(
                select(Tag).where(
                    Tag.tenant_id == user.tenant_id, Tag.id.in_(body.tag_ids)
                )
            )
        ).scalars().all()

    processed = 0
    for doc in docs:
        if body.action == "reprocess":
            if doc.deleted_at is not None:
                continue
            doc.status = DocumentStatus.PENDING
            doc.error = None
            db.add(Job(document_id=doc.id, kind="ingest", mode=body.mode))
        elif body.action == "delete":
            doc.deleted_at = datetime.now(timezone.utc)
        elif body.action == "restore":
            doc.deleted_at = None
        elif body.action == "purge":
            await deletion.purge_document(db, doc)
        elif body.action == "add_tags":
            expanded = await with_ancestors(db, tags)
            existing = {t.id for t in doc.tags}
            for tag in expanded:
                if tag.id not in existing:
                    doc.tags.append(tag)
        elif body.action == "remove_tags":
            remove = {t.id for t in tags}
            doc.tags = [t for t in doc.tags if t.id not in remove]
        processed += 1
    await db.flush()
    skipped = len(body.ids) - processed if body.ids else 0
    return BulkActionResult(
        processed=processed, skipped=max(skipped, 0), remaining=remaining
    )
