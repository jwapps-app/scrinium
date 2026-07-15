"""Archive downsampling — shrink an OCR archive's images to a target DPI.

Space-only optimization applied to the *archive* blob (the OCR'd, served copy),
never the original. Ghostscript downsamples raster images above the target;
vector text is preserved, so the in-viewer text layer stays intact and search
(which reads the DB's text_content, not the PDF) is entirely unaffected.

Fail-soft throughout: if the rebuilt PDF isn't valid, changes page count, loses
its text layer, or isn't actually smaller, we discard it and keep the existing
archive. A bad archive is always recoverable by re-OCR from the pristine
original, so nothing here is destructive.
"""

import asyncio
import logging
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from pypdf import PdfReader
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Blob, Document, Job, JobStatus
from app.services import storage

logger = logging.getLogger(__name__)


def max_image_dpi(pdf: Path, sample_pages: int = 2) -> int | None:
    """Highest embedded-image DPI over the first `sample_pages` pages.

    Scans are uniform DPI across pages, so sampling the front is representative
    and stays cheap even on a 2,000-page book. Returns None for a PDF with no
    raster images (born-digital / vector) — nothing to downsample.
    """
    try:
        out = subprocess.run(
            ["pdfimages", "-list", "-f", "1", "-l", str(sample_pages), str(pdf)],
            capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if out.returncode != 0:
        return None
    mx = 0
    # Columns: page num type w h color comp bpc enc interp object ID x-ppi y-ppi …
    for line in out.stdout.splitlines()[2:]:
        f = line.split()
        if len(f) < 14 or f[2] not in ("image", "stencil"):
            continue
        for idx in (12, 13):  # x-ppi, y-ppi
            try:
                mx = max(mx, int(round(float(f[idx]))))
            except ValueError:
                pass
    return mx or None


def _page_count(pdf: Path) -> int | None:
    try:
        return len(PdfReader(str(pdf)).pages)
    except Exception:
        return None


def _has_text(pdf: Path) -> bool:
    try:
        out = subprocess.run(
            ["pdftotext", "-q", str(pdf), "-"],
            capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL,
        )
        return bool((out.stdout or "").strip())
    except (subprocess.SubprocessError, OSError):
        return False


def downsample_archive(src: Path, dst: Path, target_dpi: int) -> bool:
    """Write a downsampled-to-`target_dpi` copy of `src` at `dst`.

    Only images above the target are resampled; anything already at or below is
    left untouched. Returns True only when the result is a valid PDF with the
    same page count, a preserved text layer (when the source had one), and a
    genuinely smaller file. Otherwise returns False — keep the original archive.
    """
    src_pages = _page_count(src)
    if src_pages is None:
        return False
    src_had_text = _has_text(src)

    # DownsampleThreshold 1.0 → only resample images whose DPI exceeds the
    # target, so already-lean pages pass through untouched. Mono (bitonal) is
    # held at ≥300 so line art / text scans stay legible.
    mono_dpi = max(target_dpi, 300)
    cmd = [
        "gs", "-dBATCH", "-dNOPAUSE", "-dQUIET", "-dSAFER",
        "-sDEVICE=pdfwrite", "-dCompatibilityLevel=1.7",
        "-dDetectDuplicateImages=true",
        "-dColorImageDownsampleType=/Average",
        "-dGrayImageDownsampleType=/Average",
        "-dMonoImageDownsampleType=/Subsample",
        "-dDownsampleColorImages=true",
        f"-dColorImageResolution={target_dpi}",
        "-dColorImageDownsampleThreshold=1.0",
        "-dDownsampleGrayImages=true",
        f"-dGrayImageResolution={target_dpi}",
        "-dGrayImageDownsampleThreshold=1.0",
        "-dDownsampleMonoImages=true",
        f"-dMonoImageResolution={mono_dpi}",
        "-dMonoImageDownsampleThreshold=1.0",
        f"-sOutputFile={dst}", str(src),
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=1800,
            stdin=subprocess.DEVNULL,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        logger.warning("ghostscript downsample failed to launch: %s", exc)
        return False
    if proc.returncode != 0 or not dst.exists():
        logger.warning(
            "ghostscript downsample exited %s: %s",
            proc.returncode, (proc.stderr or "")[:200],
        )
        return False

    if _page_count(dst) != src_pages:
        logger.warning("downsample changed page count; keeping original archive")
        return False
    if src_had_text and not _has_text(dst):
        logger.warning("downsample dropped the text layer; keeping original archive")
        return False
    if dst.stat().st_size >= src.stat().st_size:
        return False  # no win — leave the archive as it was
    return True


async def _run_with_heartbeat(session: AsyncSession, job: Job, fn, *args):
    """Run a blocking fn in a thread, stamping the job heartbeat every ~15s so
    the interrupted-job reclaimer doesn't mistake a long Ghostscript run for a
    dead lane."""
    task = asyncio.create_task(asyncio.to_thread(fn, *args))
    last_beat = 0.0
    while True:
        done, _ = await asyncio.wait({task}, timeout=1.5)
        if done:
            break
        if time.monotonic() - last_beat >= 15:
            last_beat = time.monotonic()
            job.heartbeat_at = datetime.now(timezone.utc)
            await session.commit()
    return task.result()


async def process_downsample_job(
    session: AsyncSession, job: Job, target_dpi: int
) -> None:
    """Downsample one document's archive in place. The document stays READY the
    whole time (the old archive keeps serving until the swap), so a library-wide
    backfill never pulls docs out of the completed view."""
    document = await session.get(Document, job.document_id)
    if document is None:
        job.status = JobStatus.FAILED
        job.error = "document no longer exists"
        job.finished_at = datetime.now(timezone.utc)
        await session.commit()
        return

    job.status = JobStatus.RUNNING
    job.phase = "finishing"
    job.started_at = datetime.now(timezone.utc)
    job.attempts += 1
    await session.commit()

    # Nothing to do: downsampling disabled, text-only docs, or an archive
    # already at/below target.
    if target_dpi <= 0 or document.archive_blob_id is None:
        job.status = JobStatus.DONE
        job.finished_at = datetime.now(timezone.utc)
        await session.commit()
        return

    archive_path = storage.blob_file(document.archive_blob_id)
    dpi = await asyncio.to_thread(max_image_dpi, archive_path)
    if dpi is None or dpi <= target_dpi:
        job.status = JobStatus.DONE
        job.finished_at = datetime.now(timezone.utc)
        await session.commit()
        return

    try:
        import tempfile

        with tempfile.TemporaryDirectory(prefix="downsample-") as tmp:
            out = Path(tmp) / "archive.pdf"
            ok = await _run_with_heartbeat(
                session, job, downsample_archive, archive_path, out, target_dpi
            )
            if not ok:
                # No usable win; leave the archive untouched but mark done so we
                # don't re-attempt it every sweep.
                job.status = JobStatus.DONE
                job.finished_at = datetime.now(timezone.utc)
                await session.commit()
                return
            blob_id, sha256, size = await asyncio.to_thread(storage.store_file, out)
    except Exception as exc:
        logger.warning("downsample failed for document %s: %s", document.id, exc)
        job.status = JobStatus.FAILED
        job.error = str(exc)[:4000]
        job.finished_at = datetime.now(timezone.utc)
        await session.commit()
        return

    old_archive_id = document.archive_blob_id
    session.add(
        Blob(id=blob_id, sha256=sha256, size_bytes=size, mime_type="application/pdf")
    )
    document.archive_blob_id = blob_id
    job.status = JobStatus.DONE
    job.finished_at = datetime.now(timezone.utc)
    await session.commit()

    # Free the superseded full-resolution archive (blobs aren't deduped, so no
    # other document can be relying on it).
    old_blob = await session.get(Blob, old_archive_id)
    if old_blob is not None:
        await session.delete(old_blob)
        await session.commit()
    storage.delete_blob(old_archive_id)
