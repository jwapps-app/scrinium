import asyncio
import re
import tempfile
import uuid
from datetime import date, timedelta
from pathlib import Path

from typing import Annotated

import aiofiles
from fastapi import APIRouter, Form, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy import and_, func, or_, select, text
from sqlalchemy import update as sqla_update

from app.deps import DB, AdminUser, CurrentUser
from app.models import Blob, Document, DocumentStatus, Job, JobStatus, Tag
from app.schemas import (
    BulkActionRequest,
    BulkActionResult,
    CopyTagsRequest,
    DocumentList,
    DocumentOut,
    DocumentUpdate,
    PageOpRequest,
    ReprocessRequest,
)
from datetime import datetime, timezone
from app.config import settings as app_settings
from app.models import Correspondent, CustomField, DocType, document_custom_values
from app.services import compress, deletion, intake, storage, thumbnails
from app.services import pages as pages_service
from app.services.intake import DuplicateDocument
from app.services.dates import extract_document_date
from app.services.app_state import (
    PROCESSING_PAUSED,
    get_flag,
    get_value,
    resolve_archive_dpi,
    set_flag,
)
from app.services.intake import ACCEPTED_SUFFIXES
from app.services.tag_tree import with_ancestors

router = APIRouter(prefix="/documents", tags=["documents"])

# Only these render inline; everything else is forced to download so a
# mistyped/evil content-type can never execute in the browser origin.
_INLINE_SAFE = {"application/pdf", "image/png", "image/jpeg", "image/gif", "image/webp"}



def _parse_ids(raw, field: str = "ids") -> list[uuid.UUID]:
    """Parse caller-supplied ids, answering 422 rather than a 500 from an
    uncaught ValueError."""
    out = []
    for value in raw or []:
        try:
            out.append(uuid.UUID(str(value)))
        except (ValueError, TypeError, AttributeError):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY, f"Invalid id in {field}"
            )
    return out


def _parse_id(value, field: str) -> uuid.UUID | None:
    if value in (None, ""):
        return None
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, f"Invalid {field}"
        )


def _light_document():
    """select(Document) without the heavy payload columns. text_content is
    megabytes of TOASTed OCR text per book —
    list/detail serialization never needs either, so loading them detoasts
    and ships huge data just to throw it away."""
    from sqlalchemy.orm import defer

    return select(Document).options(
        defer(Document.text_content)
    )


def _serve_blob(path, media_type: str, filename: str, disposition: str):
    inline = disposition != "attachment" and media_type in _INLINE_SAFE
    return FileResponse(
        path,
        media_type=media_type,
        filename=filename,
        content_disposition_type="inline" if inline else "attachment",
        headers={"X-Content-Type-Options": "nosniff"},
    )


def doc_out(
    doc: Document,
    progress: tuple[float, str | None] | None = None,
    custom_values: dict[str, str] | None = None,
    size_bytes: int | None = None,
) -> DocumentOut:
    out = DocumentOut.model_validate(doc)
    if size_bytes is not None:
        out.size_bytes = size_bytes
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


def _first_tag_name():
    from app.models import document_tags

    return (
        select(func.min(func.lower(Tag.name)))
        .select_from(document_tags.join(Tag, document_tags.c.tag_id == Tag.id))
        .where(document_tags.c.document_id == Document.id)
        .correlate(Document)
        .scalar_subquery()
    )


SORTS = {
    "newest": Document.created_at.desc(),
    "oldest": Document.created_at.asc(),
    "expires": Document.expires_on.asc().nulls_last(),
    "docdate": func.coalesce(Document.doc_date, func.date(Document.created_at)).desc(),
    "title": func.lower(Document.title).asc(),
    "updated": Document.updated_at.desc(),
    "tag": _first_tag_name().asc().nulls_last(),
    "correspondent": (
        select(func.lower(Correspondent.name))
        .where(Correspondent.id == Document.correspondent_id)
        .correlate(Document)
        .scalar_subquery()
        .asc()
        .nulls_last()
    ),
    "doctype": (
        select(func.lower(DocType.name))
        .where(DocType.id == Document.doc_type_id)
        .correlate(Document)
        .scalar_subquery()
        .asc()
        .nulls_last()
    ),
    "pages": Document.page_count.desc().nulls_last(),
    # Total on-disk footprint: original + archive blob. IN (not OR) so the
    # per-row subquery uses the blobs primary-key index.
    "size": (
        select(func.coalesce(func.sum(Blob.size_bytes), 0))
        .where(
            Blob.id.in_([Document.original_blob_id, Document.archive_blob_id])
        )
        .correlate(Document)
        .scalar_subquery()
        .desc()
    ),
    "dpi": Document.archive_dpi.desc().nulls_last(),
}


# --- Chunked uploads -------------------------------------------------------
# Reverse proxies/tunnels commonly cap request bodies (Cloudflare: 100 MB),
# so big files arrive as a session of ~32 MB parts the client PUTs one by
# one, then a complete call assembles and ingests. Stale sessions are swept
# by the worker after a day.


def _upload_session_dir(upload_id: uuid.UUID) -> Path:
    return Path(app_settings.data_dir) / "upload-sessions" / upload_id.hex


@router.post("/uploads")
async def create_upload_session(user: CurrentUser) -> dict:
    upload_id = uuid.uuid4()
    session_dir = _upload_session_dir(upload_id)
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / "owner").write_text(str(user.tenant_id))
    return {"upload_id": str(upload_id)}


def _owned_session_dir(upload_id: uuid.UUID, user) -> Path:
    session_dir = _upload_session_dir(upload_id)
    owner = session_dir / "owner"
    if not owner.exists() or owner.read_text() != str(user.tenant_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Upload session not found")
    return session_dir


@router.put("/uploads/{upload_id}/{index}")
async def upload_chunk(
    upload_id: uuid.UUID,
    index: int,
    request: Request,
    user: CurrentUser,
) -> dict:
    if index < 0 or index > 10000:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Bad chunk index")
    session_dir = _owned_session_dir(upload_id, user)
    # Bound a single session so a client can't fill the disk: 10001 parts ×
    # 64 MB ceiling ≈ well past any real document, and the assembled total
    # is re-checked below.
    existing = sum(p.stat().st_size for p in session_dir.glob("part-*"))
    if existing > 40 * 1024**3:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "Upload session too large"
        )
    part = session_dir / f"part-{index:05d}"
    written = 0
    async with aiofiles.open(part, "wb") as out:
        async for chunk in request.stream():
            written += len(chunk)
            if written > 128 * 1024 * 1024:
                await out.close()
                part.unlink(missing_ok=True)
                raise HTTPException(
                    status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "Chunk too large"
                )
            await out.write(chunk)
    return {"received": part.stat().st_size}


@router.post(
    "/uploads/{upload_id}/complete",
    response_model=DocumentOut,
    status_code=status.HTTP_201_CREATED,
)
async def complete_upload(
    upload_id: uuid.UUID, body: dict, user: CurrentUser, db: DB
) -> DocumentOut:
    session_dir = _owned_session_dir(upload_id, user)
    filename = (body.get("filename") or "upload.pdf").strip() or "upload.pdf"
    suffix = Path(filename).suffix.lower()
    if suffix not in ACCEPTED_SUFFIXES:
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            f"Unsupported file type {suffix or '(none)'}",
        )
    parts = sorted(session_dir.glob("part-*"))
    if not parts:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No chunks uploaded")
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        limit = app_settings.max_upload_mb * 1024 * 1024
        assembled = sum(p.stat().st_size for p in parts)
        if limit and assembled > limit:
            raise HTTPException(
                status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                f"Upload exceeds the {app_settings.max_upload_mb} MB limit",
            )
        async with aiofiles.open(tmp_path, "wb") as out:
            for part in parts:
                async with aiofiles.open(part, "rb") as src:
                    while chunk := await src.read(1024 * 1024):
                        await out.write(chunk)
        try:
            doc = await intake.ingest_file(
                db, user.tenant_id, tmp_path, filename
            )
        except intake.DuplicateDocument as exc:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"Duplicate of existing document {exc.existing_id}",
            ) from exc
        return doc_out(doc)
    finally:
        tmp_path.unlink(missing_ok=True)
        for part in parts:
            part.unlink(missing_ok=True)
        (session_dir / "owner").unlink(missing_ok=True)
        try:
            session_dir.rmdir()
        except OSError:
            pass


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
        # Enforce the configured ceiling while streaming. max_upload_mb was
        # declared but referenced nowhere, so the only bound was nginx's body
        # cap — absent for anything reaching the api container directly.
        limit = app_settings.max_upload_mb * 1024 * 1024
        written = 0
        async with aiofiles.open(tmp_path, "wb") as out:
            while chunk := await file.read(1024 * 1024):
                written += len(chunk)
                if limit and written > limit:
                    raise HTTPException(
                        status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        f"File exceeds the {app_settings.max_upload_mb} MB limit",
                    )
                await out.write(chunk)
        try:
            if ocr_text is None:
                created = await intake.ingest_with_split(
                    db, user.tenant_id, tmp_path, filename, mime=file.content_type
                )
                if not created:
                    raise HTTPException(
                        status.HTTP_409_CONFLICT,
                        "Every segment already exists in the library",
                    )
                doc = created[0]
            else:
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
    needs_review: bool = False,
    expiring: bool = False,
    non_pdfa: bool = False,
    title_q: str | None = None,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1)] = 50,
) -> DocumentList:
    conditions = [Document.tenant_id == user.tenant_id]
    if expiring:
        conditions.append(Document.expires_on.is_not(None))
        conditions.append(
            Document.expires_on <= date.today() + timedelta(days=60)
        )
    if needs_review:
        # Triage bucket: finished OCR with NO organization at all — no
        # tags, no correspondent, no type. Tagged docs (e.g. from folder
        # drops) count as filed; a books library isn't asked to invent
        # correspondents for everything.
        conditions.append(Document.status == DocumentStatus.READY)
        conditions.append(Document.correspondent_id.is_(None))
        conditions.append(Document.doc_type_id.is_(None))
        conditions.append(~Document.tags.any())
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
    if non_pdfa:
        conditions.append(Document.archive_pdfa.is_(False))
    if title_q and title_q.strip():
        # Title-only match for pickers (e.g. choosing a document to copy tags
        # from), deliberately not full-text: matching body text would bury the
        # title you actually typed under every document that mentions it.
        conditions.append(Document.title.ilike(f"%{title_q.strip()}%"))
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
            _light_document()
            .where(*conditions)
            .order_by(SORTS.get(sort, SORTS["newest"]))
            .offset(offset)
            .limit(min(limit, 200))
        )
    ).scalars().all()
    running = [d.id for d in docs if d.status == DocumentStatus.PROCESSING]
    progress = await _progress_map(db, running)
    sizes = await _size_map(db, docs)
    return DocumentList(
        items=[
            doc_out(d, progress.get(d.id), size_bytes=sizes.get(d.id))
            for d in docs
        ],
        total=total,
    )


async def _size_map(db, docs) -> dict:
    """document_id -> total on-disk bytes (original + archive blob)."""
    blob_ids = set()
    for d in docs:
        if d.original_blob_id:
            blob_ids.add(d.original_blob_id)
        if d.archive_blob_id:
            blob_ids.add(d.archive_blob_id)
    if not blob_ids:
        return {}
    rows = (
        await db.execute(
            select(Blob.id, Blob.size_bytes).where(Blob.id.in_(blob_ids))
        )
    ).all()
    by_blob = {bid: (sz or 0) for bid, sz in rows}
    out = {}
    for d in docs:
        out[d.id] = by_blob.get(d.original_blob_id, 0) + by_blob.get(
            d.archive_blob_id, 0
        )
    return out


_STATS_CACHE: dict = {}


@router.get("/stats")
async def library_stats(user: CurrentUser, db: DB) -> dict:
    """~8 aggregate queries, polled every few seconds by every open tab — a
    tiny TTL cache (STATS_CACHE_SECONDS, default 3s; 0 disables) means N
    pollers cost one computation while live progress stays honest."""
    import time as _time

    ttl = app_settings.stats_cache_seconds
    now = _time.monotonic()
    if ttl > 0:
        hit = _STATS_CACHE.get(user.tenant_id)
        if hit is not None and now - hit[0] < ttl:
            return hit[1]
    payload = await _compute_stats(user, db)
    if ttl > 0:
        for key in [k for k, v in _STATS_CACHE.items() if now - v[0] >= ttl]:
            _STATS_CACHE.pop(key, None)
        _STATS_CACHE[user.tenant_id] = (now, payload)
    return payload


async def _compute_stats(user: CurrentUser, db: DB) -> dict:
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

    # Only genuinely-active jobs get a bar: a live lane stamps its heartbeat
    # every ~15s, so a fresh beat (or a just-claimed job not yet beating)
    # means real work. This hides orphaned RUNNING rows left by a container
    # restart — they freeze with a stale heartbeat and would otherwise show
    # as extra bars (">3 files at once") until the periodic reclaim requeues
    # them minutes later.
    alive = now - timedelta(seconds=60)
    running_rows = (
        await db.execute(
            select(Job, Document.title)
            .join(Document, Job.document_id == Document.id)
            .where(
                Job.status == JobStatus.RUNNING,
                Document.tenant_id == user.tenant_id,
                or_(
                    Job.heartbeat_at >= alive,
                    and_(Job.heartbeat_at.is_(None), Job.started_at >= alive),
                ),
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
        if (
            job.phase == "ocr"
            and job.started_at
            and job.pages_done
            and job.pages_total
        ):
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

    # Queue ETA in PAGES, not documents: a 1,000-page book and a receipt
    # process at nearly the same pages/minute, so the estimate stops
    # exploding to "142 days" while a run of giant books happens to be in
    # front (and stops collapsing when the receipts fly by). Page counts
    # are stamped at intake and backfilled by the worker, so remaining
    # work is measurable up front.
    window_min = 30
    done_count, pages_done_recent = (
        await db.execute(
            select(
                func.count(Job.id),
                func.coalesce(func.sum(Document.page_count), 0),
            )
            .join(Document, Job.document_id == Document.id)
            .where(
                Job.status == JobStatus.DONE,
                Job.finished_at >= now - timedelta(minutes=window_min),
                Document.tenant_id == user.tenant_id,
            )
        )
    ).one()
    rate_per_min = done_count / window_min

    known_pages, known_docs = (
        await db.execute(
            select(
                func.coalesce(func.sum(Document.page_count), 0),
                func.count(Document.id),
            ).where(
                Document.tenant_id == user.tenant_id,
                Document.deleted_at.is_(None),
                Document.status.in_(
                    [DocumentStatus.PENDING, DocumentStatus.PROCESSING]
                ),
                Document.page_count.is_not(None),
            )
        )
    ).one()
    unknown_docs = remaining - known_docs
    # Docs whose size we don't know yet count at the known average (or a
    # conservative 20 pages when nothing is known).
    avg_known = (known_pages / known_docs) if known_docs else 20.0
    remaining_pages = int(known_pages + unknown_docs * avg_known)
    # Credit pages already finished inside the currently running jobs.
    pages_in_flight_done = sum(
        job.pages_done or 0 for job, _title in running_rows
    )
    remaining_pages = max(0, remaining_pages - pages_in_flight_done)

    pages_per_min = pages_done_recent / window_min
    if pages_per_min > 0 and remaining_pages:
        queue_eta = int(remaining_pages / pages_per_min * 60)
    elif rate_per_min > 0 and remaining:
        queue_eta = int(remaining / rate_per_min * 60)
    else:
        queue_eta = None

    review_count = (
        await db.execute(
            select(func.count(Document.id)).where(
                Document.tenant_id == user.tenant_id,
                Document.deleted_at.is_(None),
                Document.status == DocumentStatus.READY,
                Document.correspondent_id.is_(None),
                Document.doc_type_id.is_(None),
                ~Document.tags.any(),
            )
        )
    ).scalar_one()

    expiring_count = (
        await db.execute(
            select(func.count(Document.id)).where(
                Document.tenant_id == user.tenant_id,
                Document.deleted_at.is_(None),
                Document.expires_on.is_not(None),
                Document.expires_on <= date.today() + timedelta(days=60),
            )
        )
    ).scalar_one()

    non_pdfa_count = (
        await db.execute(
            select(func.count(Document.id)).where(
                Document.tenant_id == user.tenant_id,
                Document.deleted_at.is_(None),
                Document.archive_pdfa.is_(False),
            )
        )
    ).scalar_one()

    # Current-wave progress: cumulative completed since the wave anchored,
    # over the wave's high-water size. Restart-proof (see worker pulse).
    base_raw = await get_value(db, "wave_baseline")
    wave_total = int(await get_value(db, "wave_total") or 0)
    if (base_raw or "").isdigit() and remaining > 0:
        wave_done = max(0, counts.get(DocumentStatus.READY, 0) - int(base_raw))
        wave_total = max(wave_total, wave_done + remaining)
    else:
        wave_done = 0

    return {
        "total": sum(counts.values()),
        "review": review_count,
        "expiring": expiring_count,
        "ready": counts.get(DocumentStatus.READY, 0),
        "processing": remaining,
        "flagged": counts.get(DocumentStatus.FLAGGED, 0),
        "trash": trash_count,
        "non_pdfa": non_pdfa_count,
        "recent": [{"id": str(r[0]), "title": r[1]} for r in recent_added],
        "paused": await get_flag(db, PROCESSING_PAUSED),
        "running": running,
        "running_count": len(running),
        # Real processing-lane count, so the UI can label/pad honestly rather
        # than latching a transient peak from just-reclaimed restart orphans.
        "concurrency": max(1, app_settings.worker_concurrency),
        "rate_per_min": round(rate_per_min, 2),
        "pages_per_min": round(pages_per_min, 1),
        "queue_pages_remaining": remaining_pages,
        "queue_eta_seconds": queue_eta,
        "wave_done": wave_done,
        "wave_total": wave_total,
    }


@router.post("/extract-dates")
async def extract_dates(user: CurrentUser, db: DB) -> dict:
    """Backfill document dates for existing docs that don't have one,
    from their already-stored text. Cheap — no OCR involved."""
    # Stream in batches — loading every undated document's full text at once
    # is a memory spike on a large library.
    examined = 0
    updated = 0
    last_id = None
    while True:
        q = (
            select(Document.id, Document.text_content)
            .where(
                Document.tenant_id == user.tenant_id,
                Document.doc_date.is_(None),
                Document.text_content.is_not(None),
                Document.deleted_at.is_(None),
            )
            .order_by(Document.id)
            .limit(500)
        )
        if last_id is not None:
            q = q.where(Document.id > last_id)
        rows = (await db.execute(q)).all()
        if not rows:
            break
        for doc_id, content in rows:
            examined += 1
            found = extract_document_date(content)
            if found:
                await db.execute(
                    sqla_update(Document)
                    .where(Document.id == doc_id)
                    .values(doc_date=found)
                )
                updated += 1
            last_id = doc_id
        await db.flush()
    return {"examined": examined, "dated": updated}


@router.post("/processing")
async def set_processing(body: dict, user: AdminUser, db: DB) -> dict:
    """Pause/resume the worker queue. Pausing lets the current file finish
    and stops new claims and watch-folder sweeps — safe to restart the
    server or the Mac (Apple OCR host) without losing work or quality."""
    paused = bool(body.get("paused"))
    await set_flag(db, PROCESSING_PAUSED, paused)
    _STATS_CACHE.pop(user.tenant_id, None)  # reflect the toggle immediately
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


@router.get("/upgradeable")
async def upgradeable_count(user: CurrentUser, db: DB) -> dict:
    """Documents that finished on Tesseract (helper down at the time) and
    could be re-OCR'd with Apple Vision."""
    # Exclude docs already queued/running for (re-)OCR — pressing Upgrade
    # queues them, so the count drops to 0 immediately and only ticks up
    # when a genuinely new Tesseract-processed doc appears later.
    active_jobs = select(Job.document_id).where(
        Job.status.in_([JobStatus.QUEUED, JobStatus.RUNNING])
    )
    count = (
        await db.execute(
            select(func.count(Document.id)).where(
                Document.tenant_id == user.tenant_id,
                Document.deleted_at.is_(None),
                Document.status == DocumentStatus.READY,
                Document.ocr_engine == "tesseract",
                ~Document.id.in_(active_jobs),
            )
        )
    ).scalar_one()
    return {"count": count, "apple_configured": bool(app_settings.apple_ocr_url)}


@router.post("/upgrade-ocr")
async def upgrade_ocr(user: AdminUser, db: DB) -> dict:
    """Queue every Tesseract-finished document for Apple re-OCR at LOW
    priority: fresh intake always claims first, so the upgrade fleet only
    runs when the queue is otherwise idle."""
    active_jobs = select(Job.document_id).where(
        Job.status.in_([JobStatus.QUEUED, JobStatus.RUNNING])
    )
    docs = (
        await db.execute(
            select(Document.id).where(
                Document.tenant_id == user.tenant_id,
                Document.deleted_at.is_(None),
                Document.status == DocumentStatus.READY,
                Document.ocr_engine == "tesseract",
                ~Document.id.in_(active_jobs),
            )
        )
    ).scalars().all()
    for doc_id in docs:
        db.add(Job(document_id=doc_id, kind="ingest", mode="redo", priority=10))
    await db.flush()
    return {"queued": len(docs)}


def _downsample_eligible(tenant_id, target_dpi: int):
    """READY docs with an archive, no active job, and either an unmeasured DPI
    (backlog to check) or a DPI above the cap. Docs already known to be at or
    below the cap — including every born-compressed new document — are excluded,
    so the count reflects real candidates, not the whole library."""
    active_jobs = select(Job.document_id).where(
        Job.status.in_([JobStatus.QUEUED, JobStatus.RUNNING])
    )
    return (
        Document.tenant_id == tenant_id,
        Document.deleted_at.is_(None),
        Document.status == DocumentStatus.READY,
        Document.archive_blob_id.is_not(None),
        ~Document.id.in_(active_jobs),
        or_(
            Document.archive_dpi.is_(None),
            # Above the cap by more than rounding slack. Ghostscript targets the
            # cap but re-measures a hair over (e.g. 301 for a 300 cap), so a
            # strict `> cap` would flag every downsampled doc forever.
            Document.archive_dpi > compress.cap_threshold(target_dpi),
        ),
    )


@router.get("/downsample-candidates")
async def downsample_candidates(user: CurrentUser, db: DB) -> dict:
    """Documents whose archive is unmeasured or above the cap — the real
    downsample candidates. Docs already at/below the cap (every born-compressed
    new document) are excluded, so this doesn't tick up on each new scan."""
    dpi = await resolve_archive_dpi(db)
    count = (
        await db.execute(
            select(func.count(Document.id)).where(
                *_downsample_eligible(user.tenant_id, dpi)
            )
        )
    ).scalar_one()
    non_pdfa = (
        await db.execute(
            select(func.count(Document.id)).where(
                Document.tenant_id == user.tenant_id,
                Document.deleted_at.is_(None),
                Document.archive_pdfa.is_(False),
            )
        )
    ).scalar_one()
    return {
        "count": count, "target_dpi": dpi, "enabled": dpi > 0, "non_pdfa": non_pdfa
    }


@router.post("/downsample-archives")
async def downsample_archives(
    user: AdminUser, db: DB, limit: Annotated[int, Query(ge=1)] = 1000
) -> dict:
    """Queue archive-downsample jobs at the LOWEST priority so they run behind
    fresh intake and OCR upgrades. Batched: queues up to `limit` documents per
    call and returns `remaining` still to do — call again until it hits 0."""
    dpi = await resolve_archive_dpi(db)
    if dpi <= 0:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Archive downsampling is disabled — set the DPI cap first.",
        )
    ids = (
        await db.execute(
            select(Document.id)
            .where(*_downsample_eligible(user.tenant_id, dpi))
            .limit(limit)
        )
    ).scalars().all()
    for doc_id in ids:
        db.add(Job(document_id=doc_id, kind="downsample", mode="skip", priority=5))
    await db.flush()
    # Recompute after flush: the jobs we just queued now count as active and
    # drop out of the eligible set, so this is the true remainder.
    remaining = (
        await db.execute(
            select(func.count(Document.id)).where(
                *_downsample_eligible(user.tenant_id, dpi)
            )
        )
    ).scalar_one()
    return {"queued": len(ids), "remaining": remaining, "target_dpi": dpi}


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
    return _serve_blob(path, media_type, filename, disposition)


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
    if body.notes is not None:
        doc.notes = body.notes.strip() or None

    if body.clear_expires:
        doc.expires_on = None
    elif body.expires_on is not None:
        doc.expires_on = body.expires_on

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
    # One job per document at a time. Two jobs on the same document run in
    # different lanes and both swap archive_blob_id at the end, so the loser's
    # work is discarded and its blob leaks — and with a downsample queued
    # alongside a re-OCR, the served archive ends up a downsample of the
    # *pre*-re-OCR bytes while text_content describes the new ones. The other
    # enqueue paths already exclude documents with an active job; this one
    # didn't.
    active = (
        await db.execute(
            select(Job.id).where(
                Job.document_id == doc.id,
                Job.status.in_([JobStatus.QUEUED, JobStatus.RUNNING]),
            )
        )
    ).first()
    if active is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "This document is already being processed."
        )
    doc.status = DocumentStatus.PENDING
    doc.error = None
    db.add(Job(document_id=doc.id, kind="ingest", mode=body.mode))
    await db.flush()
    await db.refresh(doc)
    return doc_out(doc)


@router.post("/{doc_id}/copy-tags", response_model=DocumentOut)
async def copy_tags(
    doc_id: uuid.UUID, body: CopyTagsRequest, user: CurrentUser, db: DB
) -> DocumentOut:
    """Mirror another document's tags onto this one.

    Additive by design: the document keeps every tag it already had, so
    copying from a second source stacks rather than silently dropping work.
    Ancestors are implied as everywhere else, so copying a leaf tag brings its
    parents along."""
    doc = await _get_owned(doc_id, user, db)
    if body.source_id != doc.id:
        source = await _get_owned(body.source_id, user, db)
        merged = {t.id: t for t in doc.tags}
        for tag in source.tags:
            merged.setdefault(tag.id, tag)
        doc.tags = await with_ancestors(db, list(merged.values()))
        await db.flush()
        await db.refresh(doc)
    return doc_out(doc, custom_values=await _custom_values(db, doc.id))


@router.get("/{doc_id}/related")
async def related_documents(doc_id: uuid.UUID, user: CurrentUser, db: DB) -> dict:
    """Documents sharing substantial content with this one (fingerprint
    proximity — catches excerpts, partial rescans, revisions)."""
    from app.services.similarity import hamming

    doc = await _get_owned(doc_id, user, db)
    if not doc.simhash:
        return {"related": []}
    rows = (
        await db.execute(
            select(Document.id, Document.title, Document.page_count, Document.simhash)
            .where(
                Document.tenant_id == user.tenant_id,
                Document.deleted_at.is_(None),
                Document.id != doc.id,
                Document.simhash.is_not(None),
                Document.simhash != 0,
            )
        )
    ).all()
    scored = []
    for rid, title, pages, h in rows:
        dist = hamming(doc.simhash, h)
        if dist <= 26:
            scored.append((dist, rid, title, pages))
    scored.sort()
    return {
        "related": [
            {
                "id": str(rid),
                "title": title,
                "page_count": pages,
                "similarity": round((1 - dist / 64) * 100),
            }
            for dist, rid, title, pages in scored[:5]
        ]
    }


@router.get("/{doc_id}/text")
async def document_text(doc_id: uuid.UUID, user: CurrentUser, db: DB) -> dict:
    """Extracted text — the reader view for text-native formats."""
    doc = await _get_owned(doc_id, user, db)
    return {"text": doc.text_content or "", "title": doc.title}


@router.post("/binder")
async def build_binder_pdf(body: dict, user: CurrentUser, db: DB):
    """One print-ready PDF: cover + contents + the selected documents (or
    everything under a tag). Searchable copies preferred."""
    from app.services import binder as binder_service

    ids = body.get("ids") or []
    tag_id = body.get("filter_tag_id")
    title = (body.get("title") or "").strip() or "Scrinium Binder"
    if ids:
        docs = [await _get_owned(i, user, db) for i in _parse_ids(ids)[:300]]
    elif tag_id:
        docs = (
            await db.execute(
                _light_document()
                .where(
                    Document.tenant_id == user.tenant_id,
                    Document.deleted_at.is_(None),
                    Document.tags.any(Tag.id == _parse_id(tag_id, "filter_tag_id")),
                )
                .order_by(func.lower(Document.title))
                .limit(300)
            )
        ).scalars().all()
    else:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "ids or filter_tag_id")

    sources = []
    total_bytes = 0
    for doc in docs:
        blob_id = doc.archive_blob_id or doc.original_blob_id
        path = storage.blob_file(blob_id)
        if not path.exists() or not (
            doc.archive_blob_id or doc.original_filename.lower().endswith(".pdf")
        ):
            continue  # binder is PDFs only; skip text-native/missing
        total_bytes += path.stat().st_size
        if total_bytes > 2 * 1024**3:
            raise HTTPException(
                status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                "Binder would exceed 2 GB — split it into volumes",
            )
        sources.append((doc.title, path))
    if not sources:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No printable PDFs in the selection")

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        out_path = Path(tmp.name)
    try:
        total_pages = await asyncio.to_thread(
            binder_service.build_binder, title, sources, out_path
        )
    except binder_service.BinderError as exc:
        out_path.unlink(missing_ok=True)
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    from starlette.background import BackgroundTask

    safe_name = re.sub(r"[^\w\- ]", "", title) or "binder"
    return FileResponse(
        out_path,
        media_type="application/pdf",
        filename=f"{safe_name}.pdf",
        content_disposition_type="attachment",
        headers={"X-Total-Pages": str(total_pages)},
        background=BackgroundTask(lambda: out_path.unlink(missing_ok=True)),
    )


@router.post("/download-zip")
async def download_zip(body: dict, user: CurrentUser, db: DB):
    """Zip of selected documents (ids) or everything under a tag
    (filter_tag_id) — searchable copies when available, pretty names,
    tag-path folders for tag downloads."""
    from app.services.export import folder_for, sanitize

    ids = body.get("ids") or []
    tag_id = body.get("filter_tag_id")
    if ids:
        docs = [await _get_owned(i, user, db) for i in _parse_ids(ids)[:200]]
    elif tag_id:
        docs = (
            await db.execute(
                _light_document()
                .where(
                    Document.tenant_id == user.tenant_id,
                    Document.deleted_at.is_(None),
                    Document.tags.any(Tag.id == _parse_id(tag_id, "filter_tag_id")),
                )
                .limit(1000)
            )
        ).scalars().all()
    else:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "ids or filter_tag_id")
    if not docs:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Nothing to download")

    parents = {}
    if tag_id:
        all_tags = (
            await db.execute(select(Tag).where(Tag.tenant_id == user.tenant_id))
        ).scalars().all()
        parents = {t.id: (t.name, t.parent_id) for t in all_tags}

    total_bytes = 0
    entries = []
    used: dict[str, int] = {}
    for doc in docs:
        blob_id = doc.archive_blob_id or doc.original_blob_id
        path = storage.blob_file(blob_id)
        if not path.exists():
            continue
        total_bytes += path.stat().st_size
        if total_bytes > 4 * 1024**3:
            raise HTTPException(
                status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                "Selection exceeds 4 GB — use the library export instead",
            )
        ext = ".pdf" if doc.archive_blob_id else (Path(doc.original_filename).suffix.lower() or ".bin")
        base = sanitize(doc.title)
        folder = folder_for(doc, parents) if tag_id else ""
        key = f"{folder}/{base}".lower()
        used[key] = used.get(key, 0) + 1
        if used[key] > 1:
            base = f"{base} ({used[key]})"
        arc = f"{folder}/{base}{ext}" if folder else f"{base}{ext}"
        entries.append((arc, path))
    if not entries:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Files missing from store")

    import zipfile as _zipfile

    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
        zip_path = Path(tmp.name)
    with _zipfile.ZipFile(zip_path, "w", _zipfile.ZIP_STORED) as zf:
        for arc, path in entries:
            zf.write(path, arc)

    from starlette.background import BackgroundTask

    return FileResponse(
        zip_path,
        media_type="application/zip",
        filename="documents.zip",
        content_disposition_type="attachment",
        background=BackgroundTask(lambda: zip_path.unlink(missing_ok=True)),
    )


@router.post("/merge")
async def merge_documents(body: dict, user: CurrentUser, db: DB) -> dict:
    """Concatenate 2+ PDF documents into a new one (in the given order);
    the sources move to the trash. The inverse of extract/split."""
    import pikepdf

    ids = body.get("ids") or []
    if len(ids) < 2 or len(ids) > 50:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Pick 2-50 documents")
    docs = []
    for raw in ids:
        docs.append(await _get_owned(_parse_id(raw, "id"), user, db))
    for doc in docs:
        if not doc.original_filename.lower().endswith(".pdf"):
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"“{doc.title}” isn't a PDF — merge only combines PDFs",
            )
    title = (body.get("title") or "").strip() or f"{docs[0].title} (merged)"
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        merged_path = Path(tmp.name)
    try:
        merged = pikepdf.new()
        for doc in docs:
            source = storage.blob_file(doc.original_blob_id)
            if not source.exists():
                raise HTTPException(
                    status.HTTP_404_NOT_FOUND, f"File missing for “{doc.title}”"
                )
            with pikepdf.open(source) as src:
                for page in src.pages:
                    merged.pages.append(page)
        merged.save(merged_path)
        try:
            new_doc = await intake.ingest_file(
                db, user.tenant_id, merged_path, f"{title}.pdf",
                mime="application/pdf",
            )
        except DuplicateDocument as dup:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "An identical merged document already exists",
            ) from dup
        for doc in docs:
            doc.deleted_at = datetime.now(timezone.utc)
        await db.flush()
        return {"new_document_id": str(new_doc.id)}
    finally:
        merged_path.unlink(missing_ok=True)


@router.post("/{doc_id}/pages")
async def page_operation(
    doc_id: uuid.UUID, body: PageOpRequest, user: CurrentUser, db: DB
) -> dict:
    """Rotate/delete pages in place (new original, re-OCR queued) or
    extract the selection into a brand-new document (source untouched)."""
    doc = await _get_owned(doc_id, user, db)
    try:
        if body.action == "rotate":
            await pages_service.rotate_pages(db, doc, body.pages, body.degrees)
        elif body.action == "delete":
            await pages_service.delete_pages(db, doc, body.pages)
        else:  # extract
            try:
                new_doc = await pages_service.extract_pages(
                    db, doc, body.pages, body.title
                )
            except DuplicateDocument as dup:
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    "Those pages already exist as a separate document",
                ) from dup
            return {"new_document_id": str(new_doc.id)}
    except pages_service.PageOpError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    await db.refresh(doc)
    return {"document": doc_out(doc).model_dump(mode="json")}


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
async def purge_document(doc_id: uuid.UUID, user: AdminUser, db: DB) -> None:
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
    # Purge destroys the file for good; every other bulk action is reversible
    # (trash can be restored, tags re-applied), so members keep those.
    if body.action == "purge" and not user.is_admin:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Permanently deleting documents is limited to the library owner.",
        )
    heavy = body.action in ("delete", "purge")
    if body.filter_trash:
        base = _light_document().where(
            Document.tenant_id == user.tenant_id, Document.deleted_at.is_not(None)
        )
        count_where = [
            Document.tenant_id == user.tenant_id, Document.deleted_at.is_not(None)
        ]
    elif body.filter_tag_id is not None:
        base = _light_document().where(
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
                _light_document().where(
                    Document.tenant_id == user.tenant_id, Document.id.in_(body.ids)
                )
            )
        ).scalars().all()
    else:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "Provide ids or filter_tag_id"
        )

    # Validate FK targets belong to this tenant before assigning them.
    if body.action == "set_correspondent" and body.correspondent_id is not None:
        owned = await db.get(Correspondent, body.correspondent_id)
        if owned is None or owned.tenant_id != user.tenant_id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Correspondent not found")
    if body.action == "set_doc_type" and body.doc_type_id is not None:
        owned = await db.get(DocType, body.doc_type_id)
        if owned is None or owned.tenant_id != user.tenant_id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Document type not found")

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
        elif body.action == "set_correspondent":
            doc.correspondent_id = body.correspondent_id  # None clears
        elif body.action == "set_doc_type":
            doc.doc_type_id = body.doc_type_id  # None clears
        processed += 1
    await db.flush()
    skipped = len(body.ids) - processed if body.ids else 0
    return BulkActionResult(
        processed=processed, skipped=max(skipped, 0), remaining=remaining
    )
