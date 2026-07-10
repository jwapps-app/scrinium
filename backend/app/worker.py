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
from app.services.ingest import process_job
from app.services.watch import scan_once

# Advisory lock so only one worker replica sweeps the watch folder at a
# time; the job queue itself is already replica-safe (SKIP LOCKED).
WATCH_LOCK_KEY = 815551


async def scan_watch_exclusively() -> None:
    async with engine.connect() as conn:
        locked = (
            await conn.execute(
                text("SELECT pg_try_advisory_lock(:key)"), {"key": WATCH_LOCK_KEY}
            )
        ).scalar()
        if not locked:
            return  # another replica is sweeping
        try:
            await scan_once()
        finally:
            await conn.execute(
                text("SELECT pg_advisory_unlock(:key)"), {"key": WATCH_LOCK_KEY}
            )

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
    try:
        while True:
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

            if not worked:
                await asyncio.sleep(settings.worker_poll_seconds)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
