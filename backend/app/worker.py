"""Ingestion worker: polls the Postgres jobs queue and runs the OCR pipeline.

Runs as its own container (`python -m app.worker`) sharing the api image.
Jobs are claimed with FOR UPDATE SKIP LOCKED so multiple workers are safe.
"""

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, or_, select, text

from app.config import settings
from app.database import SessionLocal, engine
from app.models import Document, DocumentStatus, Job, JobStatus
from app.services.app_state import PROCESSING_PAUSED, get_flag
from app.services.deletion import purge_expired, sweep_upload_sessions
from app.services.ingest import process_job
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
            .order_by(Job.created_at)
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

        if time.monotonic() - last_reclaim >= 300:
            last_reclaim = time.monotonic()
            try:
                await _with_advisory_lock(
                    PURGE_LOCK_KEY, lambda: reclaim_interrupted_jobs(180)
                )
            except Exception:
                logger.exception("stale-job reclaim crashed; continuing")

        await asyncio.sleep(1)


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
