"""Ingestion worker: polls the Postgres jobs queue and runs the OCR pipeline.

Runs as its own container (`python -m app.worker`) sharing the api image.
Jobs are claimed with FOR UPDATE SKIP LOCKED so multiple workers are safe.
"""

import asyncio
import io
import logging
import signal
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, delete, func, or_, select, text, union
from sqlalchemy import true as sqla_true
from sqlalchemy import update as sqla_update

from app.config import settings
from app.database import SessionLocal, engine
from app.models import Document, DocumentStatus, Job, JobStatus, Tenant
from app.services.app_state import (
    PROCESSING_PAUSED,
    get_flag,
    get_value,
    resolve_archive_dpi,
    set_value,
)
from app.services import page_index, similarity
from app.services.compress import process_downsample_job
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
BACKFILL_LOCK_KEY = 815554
# Distinct keys: _with_advisory_lock is a no-op when the lock is held, so any
# two sweeps sharing a key means the slower one can mute the other indefinitely.
RECLAIM_LOCK_KEY = 815555
ORPHAN_LOCK_KEY = 815556
RETENTION_LOCK_KEY = 815557
VERIFY_LOCK_KEY = 815558
EXPIRY_LOCK_KEY = 815559
EXPORT_LOCK_KEY = 815560
PAGE_INDEX_LOCK_KEY = 815561


_backfill_done = False
_page_index_done = False


async def _backfill_text_length() -> None:
    """One-time gradual backfill of documents.text_length for docs OCR'd before
    the column existed — a batch at a time so it never detoasts the whole
    library at once. Once a pass updates 0 rows, back off to an hourly
    re-check instead of pinging the DB every 20s forever."""
    global _backfill_done
    async with SessionLocal() as session:
        result = await session.execute(
            text(
                "UPDATE documents SET text_length = length(text_content) "
                "WHERE id IN (SELECT id FROM documents "
                "WHERE text_length IS NULL AND text_content IS NOT NULL LIMIT 2000)"
            )
        )
        await session.commit()
        _backfill_done = (result.rowcount or 0) == 0


async def _backfill_page_index() -> None:
    """Gradually build per-page search vectors for documents OCR'd before the
    table existed.

    Needs no re-OCR: the stored text still carries its form-feed page breaks,
    and the count matches page_count exactly across the library.

    Deliberately small batches. There are ~9 GB of text across the library and
    tokenising it is real work; a handful of documents per pass keeps the
    database responsive while the queue is still running OCR.
    """
    global _page_index_done
    async with SessionLocal() as session:
        ids = await page_index.documents_missing_pages(session, limit=25)
        if not ids:
            _page_index_done = True
            return
        for doc_id in ids:
            document = await session.get(Document, doc_id)
            if document is None:
                continue
            await page_index.reindex_pages(session, document)
        await session.commit()
        logger.info("page index: %d document(s) indexed", len(ids))


async def _sweep_orphan_blobs() -> None:
    """Delete blob-store files that have no Blob row. Failure paths can leave
    them behind (bytes are written before the DB commit; a rollback drops the
    row but not the file), and nothing else ever looks at row-less files.
    Only files older than a day are touched, so in-flight stores are safe."""
    import os
    import uuid as _uuid
    from pathlib import Path

    from app.models import Blob
    from app.services import storage

    blob_root = Path(settings.data_dir) / "blobs"
    if not blob_root.exists():
        return
    cutoff = time.time() - 86400
    candidates: list[Path] = []
    for path in blob_root.glob("*/*/*"):
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                candidates.append(path)
        except OSError:
            continue
        if len(candidates) >= 5000:
            break  # bound one sweep; the next hour catches the rest
    if not candidates:
        return
    ids = []
    by_id = {}
    for path in candidates:
        try:
            bid = _uuid.UUID(path.name)
        except ValueError:
            continue
        ids.append(bid)
        by_id[bid] = path
    removed = 0
    async with SessionLocal() as session:
        known = {
            row
            for row in (
                await session.execute(select(Blob.id).where(Blob.id.in_(ids)))
            ).scalars()
        }
    for bid, path in by_id.items():
        if bid not in known:
            try:
                os.unlink(path)
                removed += 1
            except OSError:
                pass
    if removed:
        logger.info("orphan sweep removed %d row-less blob file(s)", removed)

    await _sweep_unreferenced_blob_rows()


async def _sweep_unreferenced_blob_rows() -> None:
    """Delete Blob rows (and their files) no document points at.

    The complement of the sweep above: the archive swap commits the new pointer
    before deleting the superseded row, so a crash in that window — or a job
    discarding its own result — leaves a blob with a live row and no referrer.
    Those are invisible to a row-less-file scan, so disk use only ever grew.
    Aged a day so nothing mid-store is touched.
    """
    from datetime import datetime, timedelta, timezone

    from app.models import Blob, Document
    from app.services import storage

    cutoff = datetime.now(timezone.utc) - timedelta(days=1)
    async with SessionLocal() as session:
        referenced = union(
            select(Document.original_blob_id).where(
                Document.original_blob_id.is_not(None)
            ),
            select(Document.archive_blob_id).where(
                Document.archive_blob_id.is_not(None)
            ),
            select(Document.thumbnail_blob_id).where(
                Document.thumbnail_blob_id.is_not(None)
            ),
        )
        stale = (
            await session.execute(
                select(Blob.id)
                .where(Blob.created_at < cutoff, Blob.id.not_in(referenced))
                .limit(2000)
            )
        ).scalars().all()
        if not stale:
            return
        await session.execute(delete(Blob).where(Blob.id.in_(stale)))
        await session.commit()
    for bid in stale:
        storage.delete_blob(bid)
    logger.info("orphan sweep removed %d unreferenced blob row(s)", len(stale))


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


# A job is abandoned after this many starts. Each attempt is counted when the
# job begins, so this bounds crash-loops (worker killed mid-job) as well as
# ordinary failures.
MAX_JOB_ATTEMPTS = 5

# Jobs this process is actively running. The reclaimer must never requeue one
# of them. A heartbeat can legitimately fall quiet mid-job — waiting on the
# document row lock after OCR is the obvious case, and it beats nothing while
# it waits — and requeuing our own live work spawns a duplicate that blocks on
# that same lock, falls quiet in turn, and gets reclaimed too. Five rounds of
# that and the document is abandoned, which is how the largest books in the
# library were failing after an hour of perfectly good OCR.
IN_FLIGHT: set = set()


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
        # attempts was incremented but never read, so a job that kills the
        # worker outright (rather than raising) was requeued by the startup
        # reclaim and crashed again — an unbounded loop in which nothing else in
        # the queue ever ran. Give up and flag the document instead.
        if job.attempts >= MAX_JOB_ATTEMPTS:
            logger.error(
                "job %s (document %s) has failed %d times; giving up",
                job.id, job.document_id, job.attempts,
            )
            job.status = JobStatus.FAILED
            job.error = f"abandoned after {job.attempts} attempts"
            job.finished_at = datetime.now(timezone.utc)
            document = await session.get(Document, job.document_id)
            if document is not None:
                document.status = DocumentStatus.FLAGGED
                document.error = (
                    "Processing was interrupted repeatedly and has been stopped. "
                    "Retry OCR to try again."
                )
            await session.commit()
            return True

        logger.info(
            "processing job %s (document %s, kind %s, mode %s)",
            job.id, job.document_id, job.kind, job.mode,
        )
        IN_FLIGHT.add(job.id)
        try:
            if job.kind == "downsample":
                target = await resolve_archive_dpi(session)
                await process_downsample_job(session, job, target)
            else:
                await process_job(session, job)
        finally:
            IN_FLIGHT.discard(job.id)
        return True


_paused_cache: tuple[float, bool] | None = None


async def _is_paused() -> bool:
    """The pause flag, cached ~3s: every lane checks it per job and the
    maintenance loop per tick, and it changes rarely — no need to open a
    session and hit AppSetting for each check."""
    global _paused_cache
    now = time.monotonic()
    if _paused_cache is not None and now - _paused_cache[0] < 3.0:
        return _paused_cache[1]
    try:
        async with SessionLocal() as session:
            value = await get_flag(session, PROCESSING_PAUSED)
    except Exception:
        return False
    _paused_cache = (now, value)
    return value


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


async def pulse_loop() -> None:
    """Liveness + recovery on their own lane: the worker_last_seen heartbeat,
    wave progress, drain notification, small backfills, and interrupted-job
    reclaim. Kept separate from maintenance_loop so heavy sweeps (exports,
    purges) can't starve the heartbeat or delay job recovery."""
    prev_backlog: int | None = None
    last_reclaim = time.monotonic()
    while True:
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
                # Current-wave progress, anchored to the cumulative
                # completed count so it NEVER moves backward on a restart
                # (done = ready_now - baseline; both are durable facts, so
                # 10-of-100 stays 10-of-100 across a container bounce).
                ready_now = (
                    await session.execute(
                        select(func.count(Document.id)).where(
                            Document.status == DocumentStatus.READY,
                            Document.deleted_at.is_(None),
                        )
                    )
                ).scalar_one()
                base_raw = await get_value(session, "wave_baseline")
                total_raw = await get_value(session, "wave_total")
                wave_total = int(total_raw) if (total_raw or "").isdigit() else 0
                if backlog == 0:
                    # Wave finished — clear the anchor; the next batch
                    # re-anchors at whatever's already completed.
                    await set_value(session, "wave_baseline", "")
                    await set_value(session, "wave_total", "0")
                else:
                    baseline = (
                        int(base_raw) if (base_raw or "").isdigit() else ready_now
                    )
                    done = max(0, ready_now - baseline)
                    wave_total = max(wave_total, done + backlog)
                    await set_value(session, "wave_baseline", str(baseline))
                    await set_value(session, "wave_total", str(wave_total))
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
                # near-duplicate feature. Fetch only (id, text) and hash in a
                # thread — simhash over full book text is CPU work that must
                # not sit on the event loop blocking heartbeats.
                stale = (
                    await session.execute(
                        select(Document.id, Document.text_content)
                        .where(
                            Document.simhash.is_(None),
                            Document.text_content.is_not(None),
                        )
                        .limit(300)
                    )
                ).all()
                if stale:
                    def _hash_batch(rows):
                        return [
                            (doc_id, similarity.simhash(text or "") or 0)
                            for doc_id, text in rows
                        ]

                    hashed = await asyncio.to_thread(_hash_batch, list(stale))
                    for doc_id, h in hashed:
                        await session.execute(
                            sqla_update(Document)
                            .where(Document.id == doc_id)
                            .values(simhash=h)
                        )
                    logger.info("fingerprinted %d document(s)", len(hashed))
                # Backfill page counts for queued PDFs from before
                # page-at-intake, so the pages-based ETA can see the
                # whole backlog. Header reads only; small batches.
                uncounted = (
                    await session.execute(
                        select(Document.id, Document.original_blob_id)
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
                ).all()

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
                    counted = await asyncio.to_thread(_count_pages, list(uncounted))
                    for doc_id, pages in counted:
                        if pages:
                            await session.execute(
                                sqla_update(Document)
                                .where(Document.id == doc_id)
                                .values(page_count=pages)
                            )
                    logger.info("page-counted %d queued document(s)", len(uncounted))
                await session.commit()
        except Exception:
            logger.exception("liveness pulse crashed; continuing")

        if time.monotonic() - last_reclaim >= 300:
            last_reclaim = time.monotonic()
            try:
                await _with_advisory_lock(
                    RECLAIM_LOCK_KEY, lambda: reclaim_interrupted_jobs(180)
                )
            except Exception:
                logger.exception("stale-job reclaim crashed; continuing")
        await asyncio.sleep(15)


async def maintenance_loop() -> None:
    """Single lane for the periodic sweeps (watch/mail/purge). Advisory
    locks keep these singular even across multiple worker replicas."""
    last_watch = last_mail = last_purge = last_backfill = 0.0
    last_page_index = 0.0
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

        if now - last_backfill >= (3600 if _backfill_done else 20):
            last_backfill = now
            try:
                await _with_advisory_lock(BACKFILL_LOCK_KEY, _backfill_text_length)
            except Exception:
                logger.exception("text-length backfill crashed; continuing")

        if now - last_page_index >= (3600 if _page_index_done else 15):
            last_page_index = now
            try:
                await _with_advisory_lock(
                    PAGE_INDEX_LOCK_KEY, _backfill_page_index
                )
            except Exception:
                logger.exception("page-index backfill crashed; continuing")

        if now - last_purge >= 3600:
            last_purge = now
            try:
                await _with_advisory_lock(ORPHAN_LOCK_KEY, _sweep_orphan_blobs)
            except Exception:
                logger.exception("orphan blob sweep crashed; continuing")
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
                await _with_advisory_lock(RETENTION_LOCK_KEY, _retention)
            except Exception:
                logger.exception("retention sweep crashed; continuing")
            try:
                await _with_advisory_lock(EXPIRY_LOCK_KEY, _expiry_notice)
            except Exception:
                logger.exception("expiry notice crashed; continuing")
            try:
                await _with_advisory_lock(VERIFY_LOCK_KEY, _verify_blobs)
            except Exception:
                logger.exception("integrity sweep crashed; continuing")
            if settings.export_every_days > 0:
                try:
                    await _with_advisory_lock(EXPORT_LOCK_KEY, _scheduled_export)
                except Exception:
                    logger.exception("scheduled export crashed; continuing")

        # (Liveness pulse and stale-job reclaim run in their own pulse_loop so
        # a long sweep here — a full-library export, a big purge — can never
        # make the worker read as dead or delay interrupted-job recovery.)

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
            """Separate 'the bytes changed' from 'I could not read the file'.

            Conflating them is actively misleading: a dropped mount, a dead
            disk or a permission change makes every blob in the batch
            unreadable at once, which reads as mass corruption when it is an
            availability problem that resolves itself when storage returns.
            """
            corrupt, unreadable = [], []
            for blob_id, sha in items:
                path = _storage.blob_file(blob_id)
                try:
                    digest = hashlib.sha256()
                    with open(path, "rb") as f:
                        for chunk in iter(lambda: f.read(1024 * 1024), b""):
                            digest.update(chunk)
                except OSError as exc:
                    unreadable.append((str(blob_id), str(exc)))
                    continue
                if digest.hexdigest() != sha:
                    corrupt.append(str(blob_id))
            return corrupt, unreadable

        corrupt, unreadable = await asyncio.to_thread(
            check, [(b.id, b.sha256) for b in blobs]
        )
        now_ts = datetime.now(timezone.utc)
        bad_set = set(corrupt)
        unreadable_ids = {bid for bid, _ in unreadable}
        for blob in blobs:
            key = str(blob.id)
            if key not in bad_set and key not in unreadable_ids:
                blob.verified_at = now_ts

        raw = await get_value(session, "integrity_status")
        try:
            state = _json.loads(raw) if raw else {}
        except ValueError:
            state = {}
        # Self-healing: a blob re-checked in this pass and now good drops off
        # the list. The old union kept every id forever, so one transient
        # storage fault branded blobs permanently with no way to clear them.
        rechecked = {str(b.id) for b in blobs}
        known_bad = (set(state.get("corrupt", [])) - rechecked) | bad_set
        state = {
            "checked_at": now_ts.isoformat(),
            "corrupt": sorted(known_bad),
            # Current-pass snapshot, deliberately not accumulated: this is a
            # statement about storage right now, and it clears by itself.
            "unreadable": sorted(unreadable_ids),
            "unreadable_reason": unreadable[0][1] if unreadable else None,
        }
        await set_value(session, "integrity_status", _json.dumps(state))
        await session.commit()
        if corrupt:
            logger.error(
                "INTEGRITY: %d blob(s) failed sha256 verification: %s",
                len(corrupt), corrupt,
            )
        if unreadable:
            logger.error(
                "STORAGE: %d blob(s) could not be read (%s) — this is an "
                "availability problem, not corruption",
                len(unreadable), unreadable[0][1],
            )


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
        # A job this process is running is normally alive whatever its
        # heartbeat says — the post-OCR stretch legitimately goes quiet for
        # longer than the ordinary window. But the exclusion needs a ceiling.
        # Unconditional, it protects a genuinely hung coroutine forever: the
        # job holds its worker slot, never completes, and is never retried,
        # which is worse than the reclaim storm it was added to stop. Past
        # `wedged`, believe the heartbeat and take the job back.
        mine = set(IN_FLIGHT)
        wedged = datetime.now(timezone.utc) - timedelta(
            minutes=settings.ocr_stall_minutes
        )
        jobs = (
            await session.execute(
                select(Job).where(
                    Job.status == JobStatus.RUNNING,
                    or_(Job.id.not_in(mine), Job.heartbeat_at < wedged)
                    if mine
                    else sqla_true(),
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
            for job in jobs:
                if job.id in mine:
                    logger.error(
                        "job %s was still held by this worker but had not "
                        "beaten in %d minutes; took it back. Something in the "
                        "post-OCR path is not returning.",
                        job.id,
                        settings.ocr_stall_minutes,
                    )


def _install_task_dumper() -> None:
    """SIGUSR1 → log the stack of every asyncio task.

    A coroutine suspended on an await has no thread, so py-spy, /proc and the
    thread list all show nothing for it: the one job that is stuck is the one
    thing you cannot see. That cost several wrong diagnoses of a hang whose
    only symptom was a heartbeat that stopped.

        docker kill -s USR1 scrinium-worker-1 && docker logs --tail 200 ...

    Cheap, off unless signalled, and the only way to find out what a wedged
    job is actually waiting on.
    """

    def dump() -> None:
        tasks = asyncio.all_tasks()
        logger.error("=== asyncio task dump: %d task(s) ===", len(tasks))
        for task in tasks:
            buf = io.StringIO()
            try:
                task.print_stack(file=buf, limit=12)
            except Exception as exc:  # never let a diagnostic take the worker down
                buf.write(f"<could not read stack: {exc}>")
            logger.error("task %r\n%s", task.get_name(), buf.getvalue())
        logger.error("=== end task dump ===")

    try:
        asyncio.get_running_loop().add_signal_handler(signal.SIGUSR1, dump)
    except (NotImplementedError, RuntimeError):
        logger.warning("SIGUSR1 task dump unavailable on this platform")


async def main() -> None:
    concurrency = max(1, settings.worker_concurrency)
    logger.info(
        "worker started (concurrency %d, watch dir: %s)",
        concurrency,
        settings.watch_dir or "disabled",
    )
    _install_task_dumper()
    # Everything blocking goes through asyncio.to_thread, which uses the loop's
    # default executor. Python sizes that at min(32, cpu+4) — 12 here — and an
    # OCR run holds its thread for as long as the document takes. Saturate it
    # and the next to_thread waits for a slot with no timeout, invisible to
    # every liveness check. Size it so job work and the maintenance sweeps
    # never compete.
    pool = ThreadPoolExecutor(
        max_workers=settings.thread_pool_size(), thread_name_prefix="scrinium"
    )
    asyncio.get_running_loop().set_default_executor(pool)
    logger.info("thread pool: %d workers", settings.thread_pool_size())
    try:
        # Single container → reclaim every RUNNING job at startup (all are
        # orphans; this worker owns none yet). Replicas → heartbeat-only.
        await reclaim_interrupted_jobs(0 if settings.worker_single else None)
        await asyncio.gather(
            pulse_loop(),
            maintenance_loop(),
            *(processor_loop(i) for i in range(concurrency)),
        )
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
