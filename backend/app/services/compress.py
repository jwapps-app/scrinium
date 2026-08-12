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
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from pypdf import PdfReader
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Blob, Document, Job, JobStatus
from app.services import storage

logger = logging.getLogger(__name__)

# sRGB ICC profile shipped with the Ghostscript package — the OutputIntent a
# PDF/A file must carry. If it's ever missing we fall back to plain PDF.
ICC_PATH = "/usr/share/color/icc/ghostscript/srgb.icc"

# PDF/A OutputIntent prologue (pdfmarks) fed to Ghostscript ahead of the source.
_PDFA_DEF = """[/_objdef {{icc}} /type /stream /OBJ pdfmark
[{{icc}} <</N 3>> /PUT pdfmark
[{{icc}} ({icc}) (r) file /PUT pdfmark
[/_objdef {{oi}} /type /dict /OBJ pdfmark
[{{oi}} << /Type /OutputIntent /S /GTS_PDFA1 /DestOutputProfile {{icc}} \
/OutputConditionIdentifier (sRGB) >> /PUT pdfmark
[{{Catalog}} <</OutputIntents [ {{oi}} ]>> /PUT pdfmark
"""


# Ghostscript's ...ImageResolution is a *target*, not an exact clamp: the
# re-measured effective DPI (image pixels ÷ page inches, rounded to an integer
# by pdfimages) routinely lands a hair above it — e.g. a "300 DPI" downsample
# reads back as 301. A strict `dpi > cap` test then flags such a doc as still
# over the cap forever, and re-downsampling only reproduces 301. So treat any
# archive within this fraction of the cap as already-satisfied. 5% (300→315)
# absorbs the rounding while still catching genuine over-cap scans (400/600).
DPI_TOLERANCE = 0.05


def cap_threshold(target_dpi: int) -> int:
    """Highest measured DPI still considered "at the cap" for `target_dpi`."""
    return int(target_dpi * (1 + DPI_TOLERANCE))


def over_cap(dpi: int | None, target_dpi: int) -> bool:
    """True when an archive is meaningfully above the cap — i.e. worth
    downsampling — as opposed to sitting at the cap modulo rounding."""
    return bool(target_dpi > 0 and dpi is not None and dpi > cap_threshold(target_dpi))


def is_pdfa(pdf: Path) -> bool:
    """True when the PDF declares PDF/A conformance (XMP pdfaid:part)."""
    try:
        import pikepdf

        with pikepdf.open(pdf) as doc:
            return bool(doc.open_metadata().get("pdfaid:part"))
    except Exception:
        return False


# A page image is anything at least this many pixels. A real page scan — even a
# small receipt at 150 DPI — clears this easily (a letter page at 150 DPI is
# ~2 megapixels), while logos, icons, and inline figures fall below it. Keeps a
# 120×120 logo shown in a 0.1" box (which computes to ~1200 PPI) from being
# mistaken for the document's resolution.
MIN_PAGE_IMAGE_PX = 500_000


def max_image_dpi(pdf: Path, sample_pages: int = 2) -> int | None:
    """DPI of the dominant page image over the first `sample_pages` pages.

    Reports the highest DPI among *substantial* images only, ignoring small
    embedded graphics: a tiny sharp logo displayed in a small box computes to an
    enormous effective PPI (1000+), which a naive max would report as the whole
    document's resolution — making an already-lean file look like a high-DPI
    downsample candidate. Scans are uniform DPI across pages, so sampling the
    front is representative and cheap even on a 2,000-page book. Returns None for
    a PDF with no substantial raster image (born-digital / vector, or only
    decorative graphics) — nothing to downsample.
    """
    try:
        out = subprocess.run(
            ["pdfimages", "-list", "-f", "1", "-l", str(sample_pages), str(pdf)],
            capture_output=True, encoding="utf-8", errors="replace",
            timeout=120, stdin=subprocess.DEVNULL,
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
        try:
            if int(f[3]) * int(f[4]) < MIN_PAGE_IMAGE_PX:  # width × height
                continue
        except ValueError:
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
            capture_output=True, encoding="utf-8", errors="replace",
            timeout=120, stdin=subprocess.DEVNULL,
        )
        return bool((out.stdout or "").strip())
    except (subprocess.SubprocessError, OSError):
        return False


def _gs_downsample(src: Path, dst: Path, target_dpi: int, pdfa: bool) -> bool:
    """Run one Ghostscript pass. `pdfa` adds the PDF/A OutputIntent so the
    rebuilt archive keeps its archival conformance."""
    # DownsampleThreshold 1.0 → only resample images whose DPI exceeds the
    # target, so already-lean pages pass through untouched. Mono (bitonal) is
    # held at ≥300 so line art / text scans stay legible.
    mono_dpi = max(target_dpi, 300)
    cmd = [
        "gs", "-dBATCH", "-dNOPAUSE", "-dQUIET", "-dSAFER",
        "-sDEVICE=pdfwrite",
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
    ]
    inputs = [str(src)]
    with tempfile.TemporaryDirectory(prefix="gsdef-") as tmp:
        if pdfa:
            # SAFER blocks reads outside the workdir, so explicitly permit the
            # ICC profile the OutputIntent references.
            defps = Path(tmp) / "pdfa_def.ps"
            defps.write_text(_PDFA_DEF.format(icc=ICC_PATH))
            cmd += [
                f"--permit-file-read={ICC_PATH}",
                "-dPDFA=2", "-dPDFACompatibilityPolicy=1",
                "-sColorConversionStrategy=RGB",
            ]
            inputs = [str(defps), str(src)]
        else:
            cmd += ["-dCompatibilityLevel=1.7"]
        cmd += [f"-sOutputFile={dst}", *inputs]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, encoding="utf-8", errors="replace",
                timeout=1800, stdin=subprocess.DEVNULL,
            )
        except (subprocess.SubprocessError, OSError) as exc:
            logger.warning("ghostscript downsample failed to launch: %s", exc)
            return False
    if proc.returncode != 0 or not dst.exists():
        logger.warning(
            "ghostscript downsample (pdfa=%s) exited %s: %s",
            pdfa, proc.returncode, (proc.stderr or "")[:200],
        )
        return False
    return True


def _acceptable(src: Path, dst: Path, src_pages: int, src_had_text: bool) -> str | None:
    """None when the rebuild is usable, otherwise why it is not.

    The reason is worth keeping: "already as small as it gets" and "the rebuild
    dropped the text layer" are different problems, and the sweep should not be
    guessing between them.
    """
    if _page_count(dst) != src_pages:
        return "page_mismatch"
    if src_had_text and not _has_text(dst):
        return "lost_text"
    if dst.stat().st_size >= src.stat().st_size:
        return "not_smaller"
    return None


def downsample_archive(
    src: Path, dst: Path, target_dpi: int, keep_pdfa: bool = True
) -> tuple[str | None, str | None]:
    """Write a downsampled-to-`target_dpi` copy of `src` at `dst`.

    Returns (format, reason): the accepted format — "pdfa" or "pdf" — with no
    reason, or None with the reason nothing usable was produced. The caller
    keeps the original archive untouched in that case, and records the reason
    so the document is not queued to fail the same way again. Returned rather
    than stashed, because several of these run concurrently in worker threads.

    When `keep_pdfa`, a PDF/A pass is tried first so archival conformance is
    preserved; only if that can't produce a clean smaller file do we fall back
    to a plain-PDF pass (some color spaces / transparency won't convert).
    """
    src_pages = _page_count(src)
    if src_pages is None:
        return None, "unreadable"
    src_had_text = _has_text(src)

    attempts = []
    if keep_pdfa and Path(ICC_PATH).exists():
        attempts.append(True)
    attempts.append(False)

    reason = "unreadable"
    for pdfa in attempts:
        if _gs_downsample(src, dst, target_dpi, pdfa):
            reason = _acceptable(src, dst, src_pages, src_had_text)
            if reason is None:
                # A PDF/A pass that didn't actually stamp conformance still
                # counts as a valid smaller plain PDF — report it honestly.
                return ("pdfa" if (pdfa and is_pdfa(dst)) else "pdf"), None
        else:
            reason = "gs_failed"
        dst.unlink(missing_ok=True)  # clear a bad output before the next pass
    return None, reason


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

    # Baseline for the compare-and-swap after the (slow) Ghostscript pass.
    archive_at_start = document.archive_blob_id
    archive_path = storage.blob_file(document.archive_blob_id)
    dpi = await asyncio.to_thread(max_image_dpi, archive_path)
    # 0 (not None) for no-image archives, so they read as measured and don't
    # requeue forever as unmeasured candidates.
    document.archive_dpi = dpi or 0
    if not over_cap(dpi, target_dpi):
        # Already lean (or at the cap modulo rounding) — nothing to rebuild, but
        # record the archive's current PDF/A status so the indicator is
        # accurate across the whole library.
        document.archive_pdfa = await asyncio.to_thread(is_pdfa, archive_path)
        job.status = JobStatus.DONE
        job.finished_at = datetime.now(timezone.utc)
        await session.commit()
        return

    try:
        with tempfile.TemporaryDirectory(prefix="downsample-") as tmp:
            out = Path(tmp) / "archive.pdf"
            result, why = await _run_with_heartbeat(
                session, job, downsample_archive, archive_path, out, target_dpi
            )
            if result is None:
                # No usable win. Leave the archive alone, and record which blob
                # was tried and why it could not be improved — without that the
                # document stays over the cap, stays eligible, and is queued to
                # fail identically for ever. Keying on the blob means a later
                # re-OCR, which produces a different archive, qualifies again.
                logger.info(
                    "downsample: %s cannot be reduced (%s)", document.id, why
                )
                document.downsample_tried_blob = document.archive_blob_id
                document.downsample_tried_dpi = target_dpi
                document.downsample_note = why
                document.archive_pdfa = await asyncio.to_thread(is_pdfa, archive_path)
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

    # Lock the row and confirm the archive we downsampled is still the current
    # one. A concurrent re-OCR would otherwise have its fresh archive replaced
    # by this downsample of the bytes it superseded.
    await session.refresh(document, with_for_update=True)
    if document.archive_blob_id != archive_at_start:
        logger.warning(
            "document %s archive changed under downsample job %s; discarding",
            document.id,
            job.id,
        )
        storage.delete_blob(blob_id)
        job.status = JobStatus.DONE
        job.error = "superseded by a concurrent job"
        job.finished_at = datetime.now(timezone.utc)
        await session.commit()
        return

    old_archive_id = document.archive_blob_id
    session.add(
        Blob(id=blob_id, sha256=sha256, size_bytes=size, mime_type="application/pdf")
    )
    document.archive_blob_id = blob_id
    document.archive_pdfa = result == "pdfa"
    document.archive_dpi = target_dpi  # capped to the target
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
