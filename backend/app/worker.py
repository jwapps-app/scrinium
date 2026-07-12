"""Ingestion worker: polls the Postgres jobs queue and runs the OCR pipeline.

Runs as its own container (`python -m app.worker`) sharing the api image.
Jobs are claimed with FOR UPDATE SKIP LOCKED so multiple workers are safe.
"""

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, func, or_, select, text

from app.config import settings
from app.database import SessionLocal, engine
from app.models import Document, DocumentStatus, Job, JobStatus, Tenant
from app.services.app_state import PROCESSING_PAUSED, get_flag, set_value
from app.services import similarity
from app.services.deletion import purge_expired, sweep_upload_sessions
from app.services.export import run_export
from app.services.ingest import process_job
from app.services import push
from app.services.mail import poll_once as poll_mail_once
from app.services.watch import scan_once, sweep_retention

# Advisory locks so only one worker replica runs each background sweep;
# the job queue itself is already replica-safe (SKIP LOCKED).
WATCH_LOCK_KEY = 815551
MAIL_LOCK_KEY = 815552
PURGE_LOCK_KEY = 815553


async def _with_advisory_lock(key: int, coro_fn) -> None:
    async with engine.connect() as conn:
        locked = (
            await conn.execute(
                text("SELECT pg_try_advisory_lock(:key)"), {"key": key}
            )
        ).scalar()
        if not locked:
            return
        try:
            await coro_fn()
        finally:
            await conn.execute(
                text("SELECT pg_advisory_unlock(:key)"), {"key": key}
            )


async def scan_watch_exclusively() -> None:
    await _with_advisory_lock(WATCH_LOCK_KEY, scan_once)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("worker")


async def claim_and_run() -> bool:
    """Claim one queued job and run it. Returns True if a job was processed."""
    async with SessionLocal() as session:
        result = await session.execute(
            select(Job)
            .where(Job.status == JobStatus.QUEUED)
            .order_by(Job.priority.desc(), Job.created_at)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        job = result.scalar_one_or_none()
        if job is None:
            await session.rollback()
            return False
        logger.info("processing job %s (document %s, mode %s)", job.id, job.document_id, job.mode)
        await process_job(session, job)
        return True


async def _is_paused() -> bool:
    try:
        async with SessionLocal() as session:
            return await get_flag(session, PROCESSING_PAUSED)
    except Exception:
        return False


async def processor_loop(slot: int) -> None:
    """One concurrent processing lane: claim and run jobs forever.

    N of these run per worker (WORKER_CONCURRENCY), so N documents process
    at once — filling the idle time each doc spends waiting on the OCR
    round-trip. SKIP LOCKED guarantees each lane grabs a distinct job.
    """
    while True:
        if await _is_paused():
            await asyncio.sleep(settings.worker_poll_seconds)
            continue
        try:
            worked = await claim_and_run()
        except Exception:
            logger.exception("job crashed in lane %s; continuing", slot)
            worked = False
        if not worked:
            await asyncio.sleep(settings.worker_poll_seconds)


async def maintenance_loop() -> None:
    """Single lane for the periodic sweeps (watch/mail/purge). Advisory
    locks keep these singular even across multiple worker replicas."""
    last_watch = last_mail = last_purge = 0.0
    last_reclaim = time.monotonic()
    last_pulse = 0.0
    prev_backlog: int | None = None
    while True:
        now = time.monotonic()
        paused = await _is_paused()

        # Watch + mail add NEW work, so they honor pause too.
        if not paused and now - last_watch >= settings.watch_poll_seconds:
            last_watch = now
            try:
                await scan_watch_exclusively()
            except Exception:
                logger.exception("watch sweep crashed; continuing")

        if (
            not paused
            and settings.mail_enabled()
            and now - last_mail >= settings.mail_poll_seconds
        ):
            last_mail = now
            try:
                await _with_advisory_lock(MAIL_LOCK_KEY, poll_mail_once)
            except Exception:
                logger.exception("mail poll crashed; continuing")

        if now - last_purge >= 3600:
            last_purge = now
            try:
                async def _purge():
                    async with SessionLocal() as session:
                        await purge_expired(session)
                await _with_advisory_lock(PURGE_LOCK_KEY, _purge)
            except Exception:
                logger.exception("trash purge crashed; continuing")
            try:
                async def _retention():
                    await asyncio.to_thread(sweep_retention)
                    await asyncio.to_thread(sweep_upload_sessions)
                await _with_advisory_lock(WATCH_LOCK_KEY, _retention)
            except Exception:
                logger.exception("retention sweep crashed; continuing")
            try:
                await _with_advisory_lock(MAIL_LOCK_KEY, _expiry_notice)
            except Exception:
                logger.exception("expiry notice crashed; continuing")
            try:
                await _with_advisory_lock(WATCH_LOCK_KEY, _verify_blobs)
            except Exception:
                logger.exception("integrity sweep crashed; continuing")
            if settings.export_every_days > 0:
                try:
                    await _with_advisory_lock(PURGE_LOCK_KEY, _scheduled_export)
                except Exception:
                    logger.exception("scheduled export crashed; continuing")

        # Liveness pulse + drain detection, every ~15s.
        if time.monotonic() - last_pulse >= 15:
            last_pulse = time.monotonic()
            try:
                async with SessionLocal() as session:
                    await set_value(
                        session, "worker_last_seen",
                        datetime.now(timezone.utc).isoformat(),
                    )
                    backlog = (
                        await session.execute(
                            select(func.count(Document.id)).where(
                                Document.status.in_(
                                    [DocumentStatus.PENDING, DocumentStatus.PROCESSING]
                                ),
                                Document.deleted_at.is_(None),
                            )
                        )
                    ).scalar_one()
                    # A real batch just finished — worth a ping. Small
                    # trickles (a couple of uploads) stay quiet.
                    if prev_backlog is not None and prev_backlog >= 10 and backlog == 0:
                        tenant_id = (
                            await session.execute(
                                select(Tenant.id).order_by(Tenant.created_at)
                            )
                        ).scalars().first()
                        if tenant_id is not None:
                            await push.notify_tenant(
                                session, tenant_id, settings.app_name,
                                "All caught up — the processing queue is empty.",
                                {},
                            )
                    prev_backlog = backlog
                    # Backfill content fingerprints for docs from before the
                    # near-duplicate feature; a few hundred per pulse until
                    # the corpus is covered.
                    stale = (
                        await session.execute(
                            select(Document)
                            .where(
                                Document.simhash.is_(None),
                                Document.text_content.is_not(None),
                            )
                            .limit(300)
                        )
                    ).scalars().all()
                    for doc in stale:
                        doc.simhash = similarity.simhash(doc.text_content or "")
                        if doc.simhash is None:
                            doc.simhash = 0  # too short to fingerprint; mark done
                    if stale:
                        logger.info("fingerprinted %d document(s)", len(stale))
                    # Backfill page counts for queued PDFs from before
                    # page-at-intake, so the pages-based ETA can see the
                    # whole backlog. Header reads only; small batches.
                    uncounted = (
                        await session.execute(
                            select(Document)
                            .where(
                                Document.page_count.is_(None),
                                Document.status.in_(
                                    [DocumentStatus.PENDING, DocumentStatus.PROCESSING]
                                ),
                                Document.deleted_at.is_(None),
                                Document.original_filename.ilike("%.pdf"),
                            )
                            .limit(100)
                        )
                    ).scalars().all()

                    def _count_pages(paths):
                        import pikepdf

                        from app.services import storage as _storage

                        results = []
                        for doc_id, blob_id in paths:
                            try:
                                with pikepdf.open(_storage.blob_file(blob_id)) as pdf:
                                    results.append((doc_id, len(pdf.pages)))
                            except Exception:
                                results.append((doc_id, 0))
                        return results

                    if uncounted:
                        counted = await asyncio.to_thread(
                            _count_pages,
                            [(d.id, d.original_blob_id) for d in uncounted],
                        )
                        by_id = {d.id: d for d in uncounted}
                        for doc_id, pages in counted:
                            if pages:
                                by_id[doc_id].page_count = pages
                        logger.info("page-counted %d queued document(s)", len(uncounted))
                    await session.commit()
            except Exception:
                logger.exception("liveness pulse crashed; continuing")

        if time.monotonic() - last_reclaim >= 300:
            last_reclaim = time.monotonic()
            try:
                await _with_advisory_lock(
                    PURGE_LOCK_KEY, lambda: reclaim_interrupted_jobs(180)
                )
            except Exception:
                logger.exception("stale-job reclaim crashed; continuing")

        await asyncio.sleep(1)


async def _verify_blobs(batch: int = 300) -> None:
    """Bit-rot watchdog: re-hash a batch of blobs against their stored
    sha256 each hour, oldest verification first — the whole store cycles
    every few days. Mismatches are surfaced in Settings health and logged
    loudly; they mean the bytes on disk are no longer the bytes ingested."""
    import hashlib
    import json as _json

    from app.models import Blob
    from app.services import storage as _storage
    from app.services.app_state import get_value

    async with SessionLocal() as session:
        blobs = (
            await session.execute(
                select(Blob)
                .order_by(Blob.verified_at.asc().nulls_first())
                .limit(batch)
            )
        ).scalars().all()
        if not blobs:
            return

        def check(items):
            bad = []
            for blob_id, sha in items:
                path = _storage.blob_file(blob_id)
                try:
                    digest = hashlib.sha256()
                    with open(path, "rb") as f:
                        for chunk in iter(lambda: f.read(1024 * 1024), b""):
                            digest.update(chunk)
                    if digest.hexdigest() != sha:
                        bad.append(str(blob_id))
                except OSError:
                    bad.append(str(blob_id))
            return bad

        bad = await asyncio.to_thread(
            check, [(b.id, b.sha256) for b in blobs]
        )
        now_ts = datetime.now(timezone.utc)
        bad_set = set(bad)
        for blob in blobs:
            if str(blob.id) not in bad_set:
                blob.verified_at = now_ts

        raw = await get_value(session, "integrity_status")
        try:
            state = _json.loads(raw) if raw else {}
        except ValueError:
            state = {}
        known_bad = set(state.get("corrupt", [])) | bad_set
        state = {
            "checked_at": now_ts.isoformat(),
            "corrupt": sorted(known_bad),
        }
        await set_value(session, "integrity_status", _json.dumps(state))
        await session.commit()
        if bad:
            logger.error("INTEGRITY: %d blob(s) failed sha256 verification: %s", len(bad), bad)


async def _expiry_notice() -> None:
    """Once a day: if documents lapse within 30 days, say so on the phone."""
    from datetime import date, timedelta as _td

    from app.services.app_state import get_value

    async with SessionLocal() as session:
        today = date.today().isoformat()
        if await get_value(session, "expiry_notice_date") == today:
            return
        count = (
            await session.execute(
                select(func.count(Document.id)).where(
                    Document.deleted_at.is_(None),
                    Document.expires_on.is_not(None),
                    Document.expires_on <= date.today() + _td(days=30),
                    Document.expires_on >= date.today(),
                )
            )
        ).scalar_one()
        await set_value(session, "expiry_notice_date", today)
        if count:
            tenant_id = (
                await session.execute(select(Tenant.id).order_by(Tenant.created_at))
            ).scalars().first()
            if tenant_id is not None:
                await push.notify_tenant(
                    session, tenant_id, settings.app_name,
                    f"{count} document{'s' if count != 1 else ''} expire within 30 days.",
                    {"expiring": True},
                )
        await session.commit()


async def _scheduled_export() -> None:
    """Kick a full-library export when the newest zip is older than the
    schedule; prune beyond EXPORT_KEEP. Skips while a big import runs
    (backlog would make the zip stale immediately)."""
    from pathlib import Path

    dest = Path(settings.data_dir) / "export"
    newest = 0.0
    exports = (
        sorted(dest.glob("library-export-*")) if dest.is_dir() else []
    )
    if exports:
        newest = max(e.stat().st_mtime for e in exports)
    if time.time() - newest < settings.export_every_days * 86400:
        return
    async with SessionLocal() as session:
        tenant_id = (
            await session.execute(select(Tenant.id).order_by(Tenant.created_at))
        ).scalars().first()
    if tenant_id is None:
        return
    logger.info("scheduled export starting")
    await run_export(tenant_id)
    keep = max(1, settings.export_keep)
    import shutil as _shutil

    exports = sorted(
        dest.glob("library-export-*"), key=lambda e: e.stat().st_mtime
    )
    # Multi-part zips of one run share a stamp; group by stamp so a "kept
    # export" means the whole run.
    runs: dict[str, list] = {}
    for e in exports:
        stamp_key = e.name.split("-part")[0]
        runs.setdefault(stamp_key, []).append(e)
    ordered = sorted(runs.values(), key=lambda group: group[0].stat().st_mtime)
    for group in ordered[:-keep]:
        for stale in group:
            if stale.is_dir():
                _shutil.rmtree(stale, ignore_errors=True)
            else:
                stale.unlink(missing_ok=True)
            logger.info("scheduled export pruned %s", stale.name)


async def reclaim_interrupted_jobs(stale_after_seconds: int | None = None) -> None:
    """Requeue jobs left RUNNING by a dead worker — redeploy, crash, or
    NAS reboot. Reprocessing is idempotent, so an interrupted doc simply
    starts over rather than stranding in 'processing' forever.

    Liveness comes from the heartbeat each running job stamps every ~15s:
    with `stale_after_seconds` set, only jobs whose heartbeat has gone quiet
    are reclaimed — safe even with multiple worker replicas, since live
    jobs on other replicas keep beating. Without it (startup), anything
    RUNNING with no fresh beat in the last 2 minutes is fair game.
    """
    threshold = datetime.now(timezone.utc) - timedelta(
        seconds=stale_after_seconds if stale_after_seconds is not None else 120
    )
    async with SessionLocal() as session:
        jobs = (
            await session.execute(
                select(Job).where(
                    Job.status == JobStatus.RUNNING,
                    or_(
                        Job.heartbeat_at < threshold,
                        # Not yet beating: stale only if it also STARTED
                        # before the window (a just-claimed job on another
                        # replica hasn't had time to beat).
                        and_(
                            Job.heartbeat_at.is_(None),
                            or_(Job.started_at.is_(None), Job.started_at < threshold),
                        ),
                    ),
                )
            )
        ).scalars().all()
        for job in jobs:
            job.status = JobStatus.QUEUED
            job.pages_done = None
            job.pages_total = None
            job.phase = None
            job.heartbeat_at = None
            doc = await session.get(Document, job.document_id)
            if doc is not None and doc.status == DocumentStatus.PROCESSING:
                doc.status = DocumentStatus.PENDING
        if jobs:
            await session.commit()
            logger.info("requeued %d interrupted job(s) from a prior run", len(jobs))


async def main() -> None:
    concurrency = max(1, settings.worker_concurrency)
    logger.info(
        "worker started (concurrency %d, watch dir: %s)",
        concurrency,
        settings.watch_dir or "disabled",
    )
    try:
        await reclaim_interrupted_jobs()
        await asyncio.gather(
            maintenance_loop(),
            *(processor_loop(i) for i in range(concurrency)),
        )
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
