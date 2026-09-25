"""Worker and ingest fixes from the September 2026 audit (F10, F24, F25,
F27, F28, F29, F32, F36, F37, F43, F44, F45)."""

import asyncio
import hashlib
import json
import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
import sqlalchemy as sa

from app.database import SessionLocal
from app.models import (
    Annotation,
    AppSetting,
    Blob,
    Document,
    DocumentStatus,
    Job,
    JobStatus,
    ReadingPosition,
    User,
)


async def _tenant_id() -> uuid.UUID:
    async with SessionLocal() as session:
        return (await session.execute(sa.select(User.tenant_id).limit(1))).scalar_one()


async def _make_document(
    tenant_id, *, status=DocumentStatus.PENDING, archive=False, title="audit"
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID | None]:
    """A document row with a (fake) original blob, optionally an archive blob."""
    async with SessionLocal() as session:
        original = Blob(id=uuid.uuid4(), sha256="a" * 64, size_bytes=1, mime_type="application/pdf")
        session.add(original)
        archive_id = None
        if archive:
            archive_blob = Blob(id=uuid.uuid4(), sha256="b" * 64, size_bytes=1, mime_type="application/pdf")
            session.add(archive_blob)
            archive_id = archive_blob.id
        await session.flush()
        doc = Document(
            tenant_id=tenant_id, title=title, original_filename="d.pdf",
            original_blob_id=original.id, archive_blob_id=archive_id, status=status,
        )
        session.add(doc)
        await session.commit()
        return doc.id, original.id, archive_id


async def _upload(client, auth, pdf_bytes, filename):
    resp = await client.post(
        "/api/documents", headers=auth,
        files={"file": (filename, pdf_bytes, "application/pdf")},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


# ---------------------------------------------------------------- F10 / F25


def test_a_result_is_current_only_when_both_blobs_are(monkeypatch):
    from app.services import ingest

    a, b = uuid.uuid4(), uuid.uuid4()
    doc = SimpleNamespace(original_blob_id=a, archive_blob_id=None)
    assert ingest._still_current(doc, a, None)
    # A page edit: new original, archive cleared. Archive alone reads as unchanged.
    doc.original_blob_id = b
    assert not ingest._still_current(doc, a, None)


async def test_a_page_edit_during_first_ocr_discards_the_stale_result(auth, monkeypatch):
    """The archive was None when the job began and None when it finished,
    but the original underneath had been replaced. The old check installed
    text recognised from pages the document no longer has."""
    from app.services import ingest, storage

    tenant_id = await _tenant_id()
    doc_id, original_id, _ = await _make_document(tenant_id)
    async with SessionLocal() as session:
        job = Job(document_id=doc_id, kind="ingest", mode="skip")
        session.add(job)
        await session.commit()
        job_id = job.id

    stale_blob = uuid.uuid4()
    replaced_original = uuid.uuid4()
    deleted: list = []
    monkeypatch.setattr(storage, "delete_blob", lambda bid: deleted.append(bid))

    async def fake_run(session, job, original, suffix, workdir, *rest):
        # Meanwhile: a page edit swaps the original and queues a force job.
        async with SessionLocal() as other:
            other.add(Blob(id=replaced_original, sha256="c" * 64, size_bytes=1, mime_type="application/pdf"))
            await other.flush()
            await other.execute(
                sa.update(Document).where(Document.id == doc_id).values(
                    original_blob_id=replaced_original, archive_blob_id=None
                )
            )
            await other.commit()
        return ingest.IngestOutcome(
            blob_id=stale_blob, sha256="d" * 64, size_bytes=5,
            text="text from the pre-edit pages", engine="stub", page_count=3,
        )

    monkeypatch.setattr(ingest, "_run_with_progress", fake_run)

    async with SessionLocal() as session:
        job = await session.get(Job, job_id)
        await ingest.process_job(session, job)

    async with SessionLocal() as session:
        job = await session.get(Job, job_id)
        doc = await session.get(Document, doc_id)
        assert job.status == JobStatus.DONE
        assert "superseded" in (job.error or "")
        assert doc.original_blob_id == replaced_original
        assert doc.archive_blob_id is None
        assert doc.text_content is None, "stale text must not be installed"
        assert doc.status != DocumentStatus.READY
    assert stale_blob in deleted, "the discarded archive must not leak"


async def test_bulk_reprocess_skips_documents_with_an_active_job(client, auth, pdf_factory):
    doc = await _upload(client, auth, pdf_factory(text=uuid.uuid4().hex), f"bulk-{uuid.uuid4().hex[:6]}.pdf")
    doc_id = uuid.UUID(doc["id"])
    # The upload queued an ingest job; it is still queued (no worker here).
    resp = await client.post(
        "/api/documents/bulk", headers=auth, json={"ids": [doc["id"]], "action": "reprocess"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["processed"] == 0 and resp.json()["skipped"] == 1

    async with SessionLocal() as session:
        await session.execute(
            sa.update(Job).where(Job.document_id == doc_id).values(status=JobStatus.DONE)
        )
        await session.commit()
    resp = await client.post(
        "/api/documents/bulk", headers=auth, json={"ids": [doc["id"]], "action": "reprocess"}
    )
    assert resp.json()["processed"] == 1
    async with SessionLocal() as session:
        active = (
            await session.execute(
                sa.select(sa.func.count(Job.id)).where(
                    Job.document_id == doc_id, Job.status == JobStatus.QUEUED
                )
            )
        ).scalar_one()
    assert active == 1


# ---------------------------------------------------------------- F24


async def test_the_cancel_marker_is_still_there_when_the_watchdog_polls(tmp_path, monkeypatch):
    """The marker used to be written and the workdir torn down in the same
    breath. The lane now waits for the OCR thread to acknowledge it."""
    from app.services import ingest
    from app.services.ocr import tesseract

    workdir = tmp_path / "work"
    workdir.mkdir()
    seen: dict = {}

    def fake_run_ocr(original, suffix, mode, wd, *rest):
        # Stand-in for the watchdog: poll on its own cadence, well after the
        # lane has been cancelled, and report what it found.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            time.sleep(0.05)
            if tesseract.cancel_requested(wd):
                seen["cancelled"] = True
                break
        seen["finished_at"] = time.monotonic()
        raise tesseract.OCRError("killed: job cancelled")

    monkeypatch.setattr(ingest, "_run_ocr", fake_run_ocr)

    class _Session:
        async def commit(self):
            pass

    job = SimpleNamespace(mode="skip", heartbeat_at=None, phase=None, pages_done=None, pages_total=None)
    lane = asyncio.create_task(
        ingest._run_with_progress(_Session(), job, tmp_path / "orig", ".pdf", workdir)
    )
    await asyncio.sleep(0.3)
    lane.cancel()
    with pytest.raises(asyncio.CancelledError):
        await lane
    raised_at = time.monotonic()

    assert seen.get("cancelled") is True
    assert seen["finished_at"] <= raised_at, "the lane must not raise before the thread stops"
    assert tesseract.cancel_requested(workdir), "the marker outlives the lane"
    # The wait comfortably covers the watchdog's own poll.
    assert tesseract.WATCHDOG_POLL_SECONDS * 2 < ingest.CANCEL_WAIT_SECONDS


# ---------------------------------------------------------------- F27


def _old_file(path: Path) -> None:
    path.write_bytes(b"x")
    old = time.time() - 3 * 86400
    os.utime(path, (old, old))


def test_orphan_scan_resumes_past_the_first_shards(tmp_path):
    from app import worker

    root = tmp_path / "blobs"
    (root / "00" / "00").mkdir(parents=True)
    (root / "ff" / "ff").mkdir(parents=True)
    legit = [uuid.UUID("0000" + uuid.uuid4().hex[4:]) for _ in range(5001)]
    for bid in legit:
        _old_file(root / "00" / "00" / bid.hex)
    orphan = uuid.UUID("ffff" + uuid.uuid4().hex[4:])
    _old_file(root / "ff" / "ff" / orphan.hex)
    cutoff = time.time() - 86400

    found, reached = worker._scan_blob_shards(root, cutoff, None, 5000)
    assert len(found) == 5001 and orphan not in found
    assert reached == "0000"
    # The old sweep started here again every hour. The cursor moves on.
    found, reached = worker._scan_blob_shards(root, cutoff, reached, 5000)
    assert orphan in found
    # The lap wrapped from the last shard back to the first and filled up there.
    assert reached == "0000"
    # A bound the tree fits under covers everything and clears the cursor.
    found, reached = worker._scan_blob_shards(root, cutoff, "0000", 10_000)
    assert len(found) == 5002 and reached is None
    # A cursor at (or past) the last shard starts from the top again.
    found, _ = worker._scan_blob_shards(root, cutoff, "ffff", 5000)
    assert len(found) == 5001 and orphan not in found


async def test_orphan_sweep_persists_its_cursor_and_reaches_the_end(auth, monkeypatch):
    from app import worker
    from app.config import settings
    from app.services.app_state import get_value

    root = Path(settings.data_dir) / "blobs"
    (root / "00" / "01").mkdir(parents=True, exist_ok=True)
    (root / "fe" / "fe").mkdir(parents=True, exist_ok=True)
    legit = [uuid.UUID("0001" + uuid.uuid4().hex[4:]) for _ in range(3)]
    async with SessionLocal() as session:
        for bid in legit:
            session.add(Blob(id=bid, sha256="e" * 64, size_bytes=1, mime_type="application/pdf"))
            _old_file(root / "00" / "01" / bid.hex)
        await session.execute(
            sa.delete(AppSetting).where(AppSetting.key == worker.ORPHAN_SWEEP_CURSOR)
        )
        await session.commit()
    orphan = uuid.UUID("fefe" + uuid.uuid4().hex[4:])
    orphan_path = root / "fe" / "fe" / orphan.hex
    _old_file(orphan_path)

    monkeypatch.setattr(worker, "ORPHAN_SCAN_LIMIT", 2)

    async def no_row_sweep():
        pass

    monkeypatch.setattr(worker, "_sweep_unreferenced_blob_rows", no_row_sweep)

    await worker._sweep_orphan_blobs()
    async with SessionLocal() as session:
        cursor = await get_value(session, worker.ORPHAN_SWEEP_CURSOR)
    assert cursor == "0001", cursor
    assert orphan_path.exists(), "not reached yet: the first shard filled the bound"
    for bid in legit:
        assert (root / "00" / "01" / bid.hex).exists()

    await worker._sweep_orphan_blobs()
    assert not orphan_path.exists(), "the second sweep resumed past the first shard"
    for bid in legit:
        assert (root / "00" / "01" / bid.hex).exists()


# ---------------------------------------------------------------- F28


async def test_integrity_check_moves_past_permanently_bad_blobs(auth, tmp_path, monkeypatch):
    from app.services import storage
    from app.services.app_state import get_value
    from app.worker import _verify_blobs

    real_blob_file = storage.blob_file
    good = tmp_path / "good.bin"
    good.write_bytes(b"sound bytes")
    rotten = tmp_path / "rotten.bin"
    rotten.write_bytes(b"not what was stored")
    good_sha = hashlib.sha256(good.read_bytes()).hexdigest()

    bad_ids = [uuid.uuid4(), uuid.uuid4()]
    good_id = uuid.uuid4()
    async with SessionLocal() as s:
        for bid in bad_ids:
            s.add(Blob(id=bid, sha256="0" * 64, size_bytes=1, mime_type="application/pdf"))
        s.add(Blob(id=good_id, sha256=good_sha, size_bytes=11, mime_type="application/pdf"))
        await s.commit()

    def fake_path(blob_id):
        if blob_id in bad_ids:
            return rotten
        if blob_id == good_id:
            return good
        return real_blob_file(blob_id)

    monkeypatch.setattr(storage, "blob_file", fake_path)
    await _verify_blobs(batch=10000)

    async with SessionLocal() as s:
        rows = {
            b.id: b for b in (
                await s.execute(sa.select(Blob).where(Blob.id.in_(bad_ids + [good_id])))
            ).scalars()
        }
        for bid in bad_ids:
            assert rows[bid].verified_at is not None, "a failed check still counts as checked"
            assert (rows[bid].verify_error or "").startswith("corrupt")
        assert rows[good_id].verify_error is None
        first_stamp = rows[bad_ids[0]].verified_at
        state = json.loads(await get_value(s, "integrity_status") or "{}")
        assert {str(b) for b in bad_ids} <= set(state["corrupt"])

    # A blob never checked sorts ahead of the bad ones now: a batch of one
    # picks it, not a bad blob for the second hour running.
    fresh_id = uuid.uuid4()
    async with SessionLocal() as s:
        s.add(Blob(id=fresh_id, sha256=good_sha, size_bytes=11, mime_type="application/pdf"))
        await s.commit()
    monkeypatch.setattr(storage, "blob_file", lambda bid: good if bid == fresh_id else fake_path(bid))
    await _verify_blobs(batch=1)
    async with SessionLocal() as s:
        fresh = await s.get(Blob, fresh_id)
        assert fresh.verified_at is not None and fresh.verify_error is None
        bad = await s.get(Blob, bad_ids[0])
        assert bad.verified_at == first_stamp, "the bad blob was not re-checked"
        assert bad.verify_error, "and its verdict is still on the row"
        state = json.loads(await get_value(s, "integrity_status") or "{}")
        assert str(bad_ids[0]) in state["corrupt"], "the failure is still reported"


# ---------------------------------------------------------------- F29


def test_unadvanced_heartbeats_are_the_dead_ones():
    from app import worker

    t0 = datetime.now(timezone.utc)
    a, b, c, d, gone = (uuid.uuid4() for _ in range(5))
    before = {a: t0, b: t0, c: None, gone: t0}
    after = {a: t0 + timedelta(seconds=15), b: t0, c: None, d: t0}
    dead = worker._unadvanced(before, after)
    assert set(dead) == {b, c}, "b never beat again, c never started beating"
    assert gone not in dead, "finished elsewhere meanwhile: not ours"
    assert d not in dead, "claimed after the first reading: not ours"


async def test_startup_reclaim_in_single_mode_spares_a_job_that_keeps_beating(auth, monkeypatch):
    from app import worker
    from app.config import settings

    tenant_id = await _tenant_id()
    live_doc, _, _ = await _make_document(tenant_id, status=DocumentStatus.PROCESSING)
    dead_doc, _, _ = await _make_document(tenant_id, status=DocumentStatus.PROCESSING)
    now = datetime.now(timezone.utc)
    async with SessionLocal() as session:
        live = Job(document_id=live_doc, status=JobStatus.RUNNING, heartbeat_at=now, started_at=now)
        dead = Job(document_id=dead_doc, status=JobStatus.RUNNING, heartbeat_at=now, started_at=now)
        session.add_all([live, dead])
        await session.commit()
        live_id, dead_id = live.id, dead.id

    readings = iter([
        {live_id: now, dead_id: now},
        {live_id: now + timedelta(seconds=15), dead_id: now},
    ])
    thresholds: list = []

    async def fake_running():
        return next(readings)

    async def fake_reclaim(stale_after_seconds=None):
        thresholds.append(stale_after_seconds)

    monkeypatch.setattr(worker, "_running_heartbeats", fake_running)
    monkeypatch.setattr(worker, "reclaim_interrupted_jobs", fake_reclaim)
    monkeypatch.setattr(worker, "STARTUP_WATCH_SECONDS", 0)
    monkeypatch.setattr(settings, "worker_single", True)

    await worker.reclaim_interrupted_jobs_at_startup()

    # Stale-by-heartbeat jobs went to the ordinary reclaim with a short window,
    # not the "everything is mine" zero the single mode used to pass.
    assert thresholds == [worker.STARTUP_STALE_SECONDS]
    assert 0 < worker.STARTUP_STALE_SECONDS < 60

    async with SessionLocal() as session:
        assert (await session.get(Job, live_id)).status == JobStatus.RUNNING
        dead_job = await session.get(Job, dead_id)
        assert dead_job.status == JobStatus.QUEUED
        assert dead_job.heartbeat_at is None
        assert (await session.get(Document, dead_doc)).status == DocumentStatus.PENDING
        assert (await session.get(Document, live_doc)).status == DocumentStatus.PROCESSING


def test_startup_windows_resume_own_jobs_within_a_minute():
    from app import worker

    # Two missed beats decide "stale"; the watch is a little over two beats.
    assert worker.STARTUP_STALE_SECONDS == 30
    assert 30 <= worker.STARTUP_WATCH_SECONDS <= 45


# ---------------------------------------------------------------- F32


def test_long_native_text_is_cut_into_indexable_pages(tmp_path):
    from app.services import page_index, textdocs

    lines = []
    n = 0
    while sum(len(line) for line in lines) < 200_000:
        n += 1
        lines.append(f"line {n} " + "lorem ipsum dolor sit amet " * 3 + "\n")
        if n % 12 == 0:
            lines.append("\n")  # paragraph break
    text = "".join(lines)
    source = tmp_path / "long.txt"
    source.write_text(text)

    out = textdocs.extract_text(source)
    chunks = out.split("\f")
    assert len(chunks) >= 3
    assert all(len(c) <= page_index.MAX_PAGE_CHARS for c in chunks)
    assert all(len(c) <= textdocs.CHUNK_HARD_CHARS for c in chunks)
    # Cuts fall at line ends, never through a line — and nothing is lost.
    for chunk in chunks[:-1]:
        assert chunk.endswith("\n")
    assert out.replace("\f", "") == text.strip()
    # Short text is untouched.
    assert "\f" not in textdocs._clean("a\n\nb")


def test_a_single_enormous_line_is_still_bounded():
    from app.services import page_index, textdocs

    out = textdocs._clean(("word " * 60_000).strip())
    assert all(len(c) <= page_index.MAX_PAGE_CHARS for c in out.split("\f"))
    assert out.replace("\f", "").replace(" ", "") == ("word" * 60_000)


# ---------------------------------------------------------------- F36


def test_a_rebuild_that_lost_more_than_the_tolerance_is_refused(tmp_path, monkeypatch):
    from app.services import compress

    src = tmp_path / "src.pdf"
    src.write_bytes(b"x" * 1000)
    dst = tmp_path / "dst.pdf"
    dst.write_bytes(b"x" * 500)
    monkeypatch.setattr(compress, "_page_count", lambda p: 40)

    lengths = {src: 10_000, dst: 9_800}
    monkeypatch.setattr(compress, "_has_text", lambda p: lengths[p])
    assert compress._acceptable(src, dst, 40, lengths[src]) is None

    lengths[dst] = 9_000  # 10% gone: one page of forty lost its layer
    assert compress._acceptable(src, dst, 40, lengths[src]) == "lost_text"
    assert compress.pdfa_is_better(src, dst) == "lost_text"

    monkeypatch.setattr(compress, "is_pdfa", lambda p: True)
    assert compress.pdfa_is_valid(src, dst) == "lost_text"
    lengths[dst] = 9_700
    assert compress.pdfa_is_valid(src, dst) is None

    # The bar is a documented constant, not a bool.
    assert 0.9 < compress.TEXT_KEEP_RATIO < 1.0
    assert "~1%" not in (compress.pdfa_is_better.__doc__ or "")
    assert "about 1%" not in (compress.pdfa_is_better.__doc__ or "")


def test_the_text_probe_returns_a_length_not_a_flag(tmp_path, monkeypatch):
    from app.services import compress

    calls: list = []

    class _Out:
        stdout = "  twelve chars  "

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return _Out()

    monkeypatch.setattr(compress.subprocess, "run", fake_run)
    assert compress._has_text(tmp_path / "x.pdf") == 12
    assert calls and calls[0][0] == "pdftotext"


# ---------------------------------------------------------------- F43


async def test_downsample_records_the_measured_dpi_not_the_target(auth, tmp_path, monkeypatch):
    from app.services import compress, storage

    tenant_id = await _tenant_id()
    doc_id, _, archive_id = await _make_document(tenant_id, status=DocumentStatus.READY, archive=True)
    async with SessionLocal() as session:
        job = Job(document_id=doc_id, kind="downsample")
        session.add(job)
        await session.commit()
        job_id = job.id

    archive_path = tmp_path / "archive.pdf"
    archive_path.write_bytes(b"%PDF-1.7 big")
    monkeypatch.setattr(storage, "blob_file", lambda bid: archive_path)
    # The archive is a 600 DPI mono scan; Ghostscript holds mono at 300 even
    # for a 150 target, and the output measures as such.
    monkeypatch.setattr(
        compress, "max_image_dpi", lambda path: 600 if path == archive_path else 300
    )

    def fake_downsample(src, dst, target, keep_pdfa=True):
        dst.write_bytes(b"%PDF-1.7")
        return "pdf", None

    monkeypatch.setattr(compress, "downsample_archive", fake_downsample)
    new_blob = uuid.uuid4()
    monkeypatch.setattr(storage, "store_file", lambda path: (new_blob, "f" * 64, 8))
    monkeypatch.setattr(storage, "delete_blob", lambda bid: None)

    async with SessionLocal() as session:
        job = await session.get(Job, job_id)
        await compress.process_downsample_job(session, job, 150)

    async with SessionLocal() as session:
        doc = await session.get(Document, doc_id)
        assert doc.archive_blob_id == new_blob
        assert doc.archive_dpi == 300, "what came out, not what was asked for"
        assert (await session.get(Job, job_id)).status == JobStatus.DONE


# ---------------------------------------------------------------- F44


def test_mid_job_apple_fallback_keeps_the_requested_format(tmp_path, monkeypatch):
    from app.services.ocr import apple

    received: dict = {}

    class _Fallback:
        def process(self, original, workdir, mode="skip", pdfa=True):
            received.update(mode=mode, pdfa=pdfa)
            return "fallback-result"

    provider = apple.AppleVisionProvider(fallback=_Fallback())
    monkeypatch.setattr(provider, "sidecar_healthy", lambda: True)

    def _dies(*a, **k):
        raise RuntimeError("sidecar went away mid-job")

    monkeypatch.setattr(provider, "_ocr_via_sidecar", _dies)
    assert provider.process(tmp_path / "in.pdf", tmp_path, "redo", False) == "fallback-result"
    assert received == {"mode": "redo", "pdfa": False}

    # And the down-at-start path, for parity.
    received.clear()
    monkeypatch.setattr(provider, "sidecar_healthy", lambda: False)
    provider.process(tmp_path / "in.pdf", tmp_path, "force", False)
    assert received == {"mode": "force", "pdfa": False}


def test_apple_provider_has_no_unused_imports():
    import inspect

    from app.services.ocr import apple

    source = inspect.getsource(apple)
    for name in ("subprocess", "OCRError", "extract_text"):
        assert name not in source, name


# ---------------------------------------------------------------- F45


def test_image_originals_are_scans_to_the_format_policy(tmp_path, monkeypatch):
    from PIL import Image

    from app.services import compress, ingest, storage, thumbnails
    from app.services.app_state import needs_measuring, wants_pdfa

    tagged = tmp_path / "tagged.png"
    Image.new("RGB", (40, 40)).save(tagged, dpi=(200, 200))
    untagged = tmp_path / "plain.png"
    Image.new("RGB", (40, 40)).save(untagged)
    assert ingest._image_dpi(tagged) == 200
    assert ingest._image_dpi(untagged) == ingest.IMAGE_DPI_ASSUMED == 300
    assert ingest._image_dpi(tmp_path / "missing.png") == 300

    # Whatever the tag, an image never reads as born-digital.
    for dpi in (ingest._image_dpi(tagged), ingest._image_dpi(untagged)):
        assert wants_pdfa("auto", dpi) is False
        assert needs_measuring("measured", dpi) is True

    class _Result:
        text = "scan text"
        engine = "stub"

        def __init__(self, archive):
            self.archive_path = archive

    seen: dict = {}

    class _Provider:
        def process(self, source, workdir, mode, pdfa):
            seen["pdfa"] = pdfa
            archive = workdir / "archive.pdf"
            archive.write_bytes(b"%PDF-1.7\n")
            return _Result(archive)

    monkeypatch.setattr(ingest, "get_provider", lambda engine: _Provider())
    monkeypatch.setattr(compress, "max_image_dpi", lambda path: 300)
    monkeypatch.setattr(compress, "over_cap", lambda dpi, cap: False)
    monkeypatch.setattr(compress, "is_pdfa", lambda path: False)
    monkeypatch.setattr(thumbnails, "make_thumbnail", lambda src, out: None)
    monkeypatch.setattr(ingest, "_page_count", lambda path: 1)
    monkeypatch.setattr(storage, "store_file", lambda path: (uuid.uuid4(), "sha", 1))

    original = tmp_path / "original-blob"
    original.write_bytes(tagged.read_bytes())
    workdir = tmp_path / "work"
    workdir.mkdir()
    outcome = ingest._run_ocr(original, ".png", "skip", workdir, None, 300, "auto")
    assert outcome.original_dpi == 200
    assert seen["pdfa"] is False, "an image upload takes the scan branch"
    assert outcome.archive_pdfa_wanted is False


# ---------------------------------------------------------------- F37


def test_page_numbers_shift_up_past_deleted_pages():
    from app.services.pages import PageEdit

    edit = PageEdit(total=6, removed={1, 4})
    assert edit.new_page(1) is None and edit.new_page(4) is None
    assert edit.new_page(2) == 1
    assert edit.new_page(3) == 2
    assert edit.new_page(5) == 3
    assert edit.new_page(6) == 4
    # A resume point on a deleted page lands on what took its place.
    assert edit.nearest_page(4) == 3
    assert edit.nearest_page(1) == 1
    assert edit.nearest_page(6) == 4
    # Deleting the last page: clamp to the new end.
    assert PageEdit(total=3, removed={3}).nearest_page(3) == 2
    # Rotation moves nothing.
    assert PageEdit(total=3, rotated={2}, degrees=90).new_page(3) == 3


def test_rects_turn_with_their_page():
    from app.services.pages import rotate_rect

    # A short wide highlight near the top-left of a portrait page.
    rect = {"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.05}
    cw = rotate_rect(rect, 90)
    # Top edge becomes the right edge: the highlight is now tall and on
    # the right, its old left end at the top.
    assert cw == {"x": 0.75, "y": 0.1, "w": 0.05, "h": 0.3}
    ccw = rotate_rect(rect, -90)
    assert ccw == {"x": 0.2, "y": 0.6, "w": 0.05, "h": 0.3}
    assert rotate_rect(rect, 270) == ccw
    half = rotate_rect(rect, 180)
    assert half == {"x": 0.6, "y": 0.75, "w": 0.3, "h": 0.05}
    # Four quarter turns come home; two half turns too.
    back = rect
    for _ in range(4):
        back = rotate_rect(back, 90)
    assert back == rect
    assert rotate_rect(half, 180) == rect
    assert rotate_rect(cw, -90) == rect
    # Everything stays on the page.
    for r in (cw, ccw, half):
        assert 0 <= r["x"] and r["x"] + r["w"] <= 1
        assert 0 <= r["y"] and r["y"] + r["h"] <= 1


async def test_page_edits_carry_highlights_and_resume_points(client, auth, pdf_factory):
    doc = await _upload(client, auth, pdf_factory(pages=3, text=uuid.uuid4().hex), f"pages-{uuid.uuid4().hex[:6]}.pdf")
    doc_id = doc["id"]
    rect = {"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.05}
    for page in (1, 3):
        resp = await client.post(
            f"/api/documents/{doc_id}/annotations", headers=auth,
            json={"page": page, "quote": f"quote on {page}", "rects": [rect]},
        )
        assert resp.status_code == 201, resp.text
    assert (await client.put(f"/api/documents/{doc_id}/position", headers=auth, json={"page": 3})).status_code == 200
    async with SessionLocal() as session:
        old_original = (
            await session.execute(sa.select(Document.original_blob_id).where(Document.id == uuid.UUID(doc_id)))
        ).scalar_one()

    resp = await client.post(
        f"/api/documents/{doc_id}/pages", headers=auth, json={"action": "delete", "pages": [1]}
    )
    assert resp.status_code == 200, resp.text

    live = (await client.get(f"/api/documents/{doc_id}/annotations", headers=auth)).json()
    assert [a["page"] for a in live] == [2], "page 1's highlight went; page 3's is now on page 2"
    assert live[0]["quote"] == "quote on 3"
    assert (await client.get(f"/api/documents/{doc_id}/position", headers=auth)).json()["page"] == 2

    async with SessionLocal() as session:
        snapshot = (
            await session.execute(
                sa.select(Document).where(Document.original_blob_id == old_original)
            )
        ).scalar_one()
        assert snapshot.id != uuid.UUID(doc_id) and snapshot.deleted_at is not None
        kept = (
            await session.execute(
                sa.select(Annotation.page).where(Annotation.document_id == snapshot.id).order_by(Annotation.page)
            )
        ).scalars().all()
        assert kept == [1, 3], "the snapshot carries the highlights as they were"
        pos = (
            await session.execute(
                sa.select(ReadingPosition.page).where(ReadingPosition.document_id == snapshot.id)
            )
        ).scalar_one()
        assert pos == 3

    # Now turn the page the surviving highlight sits on.
    resp = await client.post(
        f"/api/documents/{doc_id}/pages", headers=auth,
        json={"action": "rotate", "pages": [2], "degrees": 90},
    )
    assert resp.status_code == 200, resp.text
    live = (await client.get(f"/api/documents/{doc_id}/annotations", headers=auth)).json()
    assert len(live) == 1 and live[0]["page"] == 2
    assert live[0]["rects"] == [{"x": 0.75, "y": 0.1, "w": 0.05, "h": 0.3}]
