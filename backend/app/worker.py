"""Ingestion worker: polls the Postgres jobs queue and runs the OCR pipeline.

Runs as its own container (`python -m app.worker`) sharing the api image.
Jobs are claimed with FOR UPDATE SKIP LOCKED so multiple workers are safe.
"""

import asyncio
import logging
import time

from sqlalchemy import select, text

from app.config import settings
from app.database import SessionLocal, engine
from app.models import Job, JobStatus
from app.services.app_state import PROCESSING_PAUSED, get_flag
from app.services.deletion import purge_expired
from app.services.ingest import process_job
from app.services.mail import poll_once as poll_mail_once
from app.services.watch import scan_once

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


async def main() -> None:
    logger.info(
        "worker started (poll every %ss, watch dir: %s)",
        settings.worker_poll_seconds,
        settings.watch_dir or "disabled",
    )
    last_watch_scan = 0.0
    last_mail_poll = 0.0
    last_purge = 0.0
    was_paused = False
    try:
        while True:
            # Pause gates NEW work only — an in-flight job always finishes.
            # DB-backed, so pausing survives restarts until resumed.
            try:
                async with SessionLocal() as session:
                    paused = await get_flag(session, PROCESSING_PAUSED)
            except Exception:
                paused = False
            if paused != was_paused:
                logger.info("processing %s", "paused" if paused else "resumed")
                was_paused = paused
            if paused:
                await asyncio.sleep(settings.worker_poll_seconds)
                continue

            try:
                worked = await claim_and_run()
            except Exception:
                logger.exception("job crashed; continuing")
                worked = False

            if time.monotonic() - last_watch_scan >= settings.watch_poll_seconds:
                last_watch_scan = time.monotonic()
                try:
                    await scan_watch_exclusively()
                except Exception:
                    logger.exception("watch sweep crashed; continuing")

            if settings.mail_enabled() and (
                time.monotonic() - last_mail_poll >= settings.mail_poll_seconds
            ):
                last_mail_poll = time.monotonic()
                try:
                    await _with_advisory_lock(MAIL_LOCK_KEY, poll_mail_once)
                except Exception:
                    logger.exception("mail poll crashed; continuing")

            if time.monotonic() - last_purge >= 3600:
                last_purge = time.monotonic()
                try:
                    async def _purge():
                        async with SessionLocal() as session:
                            await purge_expired(session)
                    await _with_advisory_lock(PURGE_LOCK_KEY, _purge)
                except Exception:
                    logger.exception("trash purge crashed; continuing")

            if not worked:
                await asyncio.sleep(settings.worker_poll_seconds)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
