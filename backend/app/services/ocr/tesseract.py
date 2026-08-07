import json
import logging
import os
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from app.config import settings
from app.services.ocr.base import OCRResult

logger = logging.getLogger(__name__)

PROGRESS_PLUGIN = "app.services.ocr.progress_plugin"


def progress_env(workdir: Path) -> dict[str, str]:
    """Env for ocrmypdf subprocesses: page progress lands in the workdir."""
    return {**os.environ, "SCRINIUM_PROGRESS_FILE": str(workdir / "progress")}


def write_progress(workdir: Path, phase: str, done: int, total: int) -> None:
    """Write the same JSON ocrmypdf's plugin writes (see progress_plugin.py),
    for the stages that run in this process instead of in ocrmypdf."""
    try:
        (workdir / "progress").write_text(
            json.dumps({"phase": phase, "done": done, "total": total})
        )
    except OSError:
        pass  # never let progress reporting break OCR


IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}

MODE_FLAGS = {
    "skip": ["--skip-text"],
    "redo": ["--redo-ocr"],
    "force": ["--force-ocr"],
}


class OCRError(Exception):
    pass


ALL_MODE_FLAGS = {"--skip-text", "--redo-ocr", "--force-ocr"}


def rotate_flags() -> list[str]:
    """Per-page auto-rotation flags, shared by both engines. OSD detects each
    page's orientation and ocrmypdf rotates only those above the confidence
    threshold, leaving correctly-oriented pages untouched."""
    if not settings.rotate_pages:
        return []
    return [
        "--rotate-pages",
        "--rotate-pages-threshold",
        str(settings.rotate_pages_threshold),
    ]

# Signatures of failures no retry can fix — stop the chain immediately.
UNFIXABLE = ("encrypted", "password-protected", "usage:", "no such file")


def pdfa_fallback_commands(cmd: list[str]) -> list[tuple[str, list[str]]]:
    """Escalating remedy chain for ocrmypdf failures. Each attempt is more
    aggressive than the last:

      1. as requested (strict PDF/A, honoring skip/redo/force)
      2. + --color-conversion-strategy RGB — normalizes print-shop color
         spaces (DeviceN/spot) that Ghostscript won't carry into PDF/A
      3. --force-ocr --output-type pdf — rebuilds every page from a fresh
         raster, discarding bad halftone dictionaries (setscreen rangecheck)
         and corrupt embedded JPEGs; plain PDF, tolerating soft render errors

    A final Ghostscript-free text-only path (see text_only_fallback) runs
    only if all of these fail.
    """

    def set_output(base: list[str], value: str) -> list[str]:
        base = list(base)
        base[base.index("--output-type") + 1] = value
        return base

    rgb = cmd + ["--color-conversion-strategy", "RGB"]

    # Rebuild-from-raster: drop mode flags (force-ocr is exclusive) and
    # insert force-ocr right after `python3 -m ocrmypdf`.
    force = set_output([a for a in cmd if a not in ALL_MODE_FLAGS], "pdf")
    force = force[:3] + [
        "--force-ocr",
        "--continue-on-soft-render-error",
    ] + force[3:]

    return [("pdfa", cmd), ("pdfa-rgb", rgb), ("force-raster", force)]


def _run_watched(attempt: list[str], workdir: Path):
    """Run one ocrmypdf attempt under a stall watchdog instead of a fixed
    timeout: as long as the progress file keeps changing, the run may take
    as long as the book demands. Kill only after `ocr_stall_minutes` of
    silence (wedged), or the `ocr_max_hours` backstop."""
    progress_file = workdir / "progress"
    stall_limit = settings.ocr_stall_minutes * 60
    hard_limit = settings.ocr_max_hours * 3600
    stdout_path = workdir / ".ocr-stdout"
    stderr_path = workdir / ".ocr-stderr"

    with open(stdout_path, "wb") as out, open(stderr_path, "wb") as err:
        # Own process group: ocrmypdf spawns Ghostscript/Tesseract children,
        # and killing only the parent would orphan them as runaway zombies.
        proc = subprocess.Popen(
            attempt,
            stdout=out,
            stderr=err,
            stdin=subprocess.DEVNULL,
            env=progress_env(workdir),
            start_new_session=True,
        )
        started = time.monotonic()
        last_progress = started
        last_snapshot = None
        while True:
            code = proc.poll()
            if code is not None:
                break
            time.sleep(10)
            try:
                snapshot = progress_file.read_text()
            except OSError:
                snapshot = None
            if snapshot != last_snapshot:
                last_snapshot = snapshot
                last_progress = time.monotonic()
            now = time.monotonic()
            if now - last_progress > stall_limit or now - started > hard_limit:
                _kill_group(proc)
                reason = (
                    f"no OCR progress for {settings.ocr_stall_minutes} minutes"
                    if now - started <= hard_limit
                    else f"exceeded the {settings.ocr_max_hours}h ceiling"
                )
                return 137, f"killed: {reason}"
    stderr_text = stderr_path.read_text(errors="replace")
    return proc.returncode, stderr_text


def _kill_group(proc: subprocess.Popen) -> None:
    """Kill the whole process group (ocrmypdf + its gs/tesseract children) and
    reap without letting an unkillable child crash the watchdog."""
    import os
    import signal

    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        proc.kill()
    try:
        proc.wait(timeout=60)
    except subprocess.TimeoutExpired:
        logger.warning("killed OCR process did not reap within 60s; continuing")


def run_ocrmypdf(cmd: list[str], workdir: Path) -> None:
    """Run ocrmypdf, escalating through the remedy chain. Raises OCRError
    if every attempt fails (the caller then tries text-only extraction)."""
    last_error = ""
    last_code = 0
    for label, attempt in pdfa_fallback_commands(cmd):
        returncode, stderr_text = _run_watched(attempt, workdir)
        if returncode == 0:
            if label != "pdfa":
                logger.warning("ocrmypdf succeeded via fallback '%s'", label)
            return
        last_error = stderr_text.strip()[:2000]
        last_code = returncode
        if any(sig in last_error.lower() for sig in UNFIXABLE):
            break  # no escalation can fix this
        if returncode == 137 and "killed:" in last_error:
            # A stalled run won't behave differently under the remedies.
            break
        # Log why, not just that. Only the final attempt's error reaches the
        # OCRError, so without this the reason the cheap attempts fail is
        # never recorded anywhere — and if they fail for every document, the
        # whole library silently runs in the most expensive mode.
        logger.warning(
            "ocrmypdf attempt '%s' failed (exit %d): %s; escalating",
            label,
            returncode,
            last_error.replace("\n", " ")[:400] or "(no stderr)",
        )
    raise OCRError(f"ocrmypdf exited {last_code}: {last_error}")


def extract_text(pdf: Path) -> str:
    result = subprocess.run(
        ["pdftotext", "-layout", str(pdf), "-"],
        capture_output=True,
        stdin=subprocess.DEVNULL,
        # A PDF's existing text layer may be in a legacy encoding (Latin-1 /
        # CP1252); pdftotext passes those bytes through verbatim, so decode
        # fail-soft rather than letting one bad byte flunk the whole document.
        encoding="utf-8",
        errors="replace",
        timeout=300,
    )
    if result.returncode != 0:
        raise OCRError(f"pdftotext failed: {result.stderr.strip()[:2000]}")
    return result.stdout


def _tesseract_image(image: Path) -> str:
    try:
        result = subprocess.run(
            ["tesseract", str(image), "stdout", "-l", settings.ocr_languages],
            capture_output=True,
            stdin=subprocess.DEVNULL,
            encoding="utf-8",
            errors="replace",
            timeout=600,
            # One OpenMP thread per page. Tesseract otherwise spreads a single
            # page over ~2 cores, so OCR_JOBS pages at once quietly costs twice
            # that — ocrmypdf pins the same limit when it runs pages in
            # parallel. Without it WORKER_CONCURRENCY x OCR_JOBS understates
            # the real load and the box ends up oversubscribed.
            env={**os.environ, "OMP_THREAD_LIMIT": "1"},
        )
    except subprocess.SubprocessError as exc:
        # One bad page must not sink a 900-page book: this is already the
        # last-resort path, so drop the page and keep the rest.
        logger.warning("tesseract failed on %s: %s", image.name, str(exc)[:200])
        return ""
    return result.stdout if result.returncode == 0 else ""


def text_only_fallback(source: Path, workdir: Path) -> str:
    """Last resort when the whole ocrmypdf/Ghostscript pipeline fails.

    Uses only poppler (pdftotext/pdftoppm) and Tesseract — never Ghostscript —
    so documents that trip Ghostscript (bad halftones, corrupt JPEGs) still
    become searchable. Produces text only; no archive PDF, so the viewer
    falls back to the untouched original.
    """
    if source.suffix.lower() in IMAGE_SUFFIXES:
        return _tesseract_image(source)

    # A usable existing text layer? Cheapest win.
    try:
        existing = extract_text(source)
        if len(existing.strip()) > 100:
            return existing
    except Exception:
        pass

    # Rasterize with poppler (not Ghostscript) and OCR each page.
    fb_dir = workdir / "textfallback"
    fb_dir.mkdir(exist_ok=True)
    result = subprocess.run(
        [
            "pdftoppm",
            "-r", "200",
            # Ceiling on the rasterized size: a hostile MediaBox would otherwise
            # turn a fixed DPI into gigapixel pages. 200 DPI on US Letter is
            # 1700x2200, so this only clamps the pathological cases.
            "-scale-to-x", "2600",
            "-scale-to-y", "3400",
            "-png",
            str(source),
            str(fb_dir / "pg"),
        ],
        capture_output=True,
        stdin=subprocess.DEVNULL,
        encoding="utf-8",
        errors="replace",
        timeout=3600,
    )
    pages = sorted(fb_dir.glob("pg*.png"))
    if result.returncode != 0 and not pages:
        raise OCRError(
            f"poppler rasterize failed: {result.stderr.strip()[:1000]}"
        )
    return _ocr_pages(pages, workdir)


def _ocr_pages(pages: list[Path], workdir: Path) -> str:
    """OCR rasterized pages, reporting progress and honoring a wall clock.

    Done page-at-a-time and silently, this is where a big book disappears: the
    progress file still holds whatever the failed ocrmypdf run last wrote, so
    the bar parks at some arbitrary percentage for hours while a worker slot
    stays occupied and nothing is logged until the very end. Report the real
    phase, use the page parallelism ocrmypdf would have used, and give up with
    partial text rather than never giving up at all.
    """
    total = len(pages)
    texts = [""] * total
    deadline = time.monotonic() + settings.ocr_fallback_minutes * 60
    write_progress(workdir, "text-only", 0, total)
    pool = ThreadPoolExecutor(max_workers=max(1, settings.ocr_jobs))
    try:
        pending = {pool.submit(_tesseract_image, p): i for i, p in enumerate(pages)}
        for done, future in enumerate(as_completed(pending), start=1):
            texts[pending[future]] = future.result()
            write_progress(workdir, "text-only", done, total)
            if time.monotonic() > deadline:
                logger.warning(
                    "text-only fallback stopped at %d/%d pages after %d minutes; "
                    "keeping the text read so far",
                    done,
                    total,
                    settings.ocr_fallback_minutes,
                )
                break
    finally:
        # Pages not yet started are dropped; ones already running end on their
        # own timeout. Either way this call does not block the worker.
        pool.shutdown(wait=False, cancel_futures=True)
    return "\f".join(texts)


def process_with_fallbacks(
    cmd: list[str], original: Path, workdir: Path, archive: Path, engine: str
) -> OCRResult:
    """Run the ocrmypdf remedy chain; on total failure, fall back to
    text-only extraction so the document is never a searchable dead end."""
    try:
        run_ocrmypdf(cmd, workdir)
        return OCRResult(
            archive_path=archive, text=extract_text(archive), engine=engine
        )
    except OCRError as exc:
        # Logged on the way in, not on the way out: on a long document the
        # fallback runs for hours, and a line that only appears afterwards is
        # no help to anyone wondering what the worker is doing.
        logger.warning(
            "%s: ocrmypdf failed (%s); starting text-only fallback",
            original.name,
            str(exc)[:200],
        )
        text = text_only_fallback(original, workdir)
        if text.strip():
            logger.warning(
                "%s: stored text-only, original preserved", original.name
            )
            return OCRResult(archive_path=None, text=text, engine="text-only")
        raise


class TesseractProvider:
    """Runs ocrmypdf (Tesseract engine) in-container as a subprocess."""

    engine = "tesseract"

    def process(self, original: Path, workdir: Path, mode: str = "skip") -> OCRResult:
        archive = workdir / "archive.pdf"
        cmd = [
            "python3", "-m", "ocrmypdf",
            *MODE_FLAGS.get(mode, MODE_FLAGS["skip"]),
            "--output-type", "pdfa",
            "--plugin", PROGRESS_PLUGIN,
            "--jobs", str(settings.ocr_jobs),
            "--language", settings.ocr_languages,
            "--quiet",
            *rotate_flags(),
        ]
        if original.suffix.lower() in IMAGE_SUFFIXES:
            cmd += ["--image-dpi", "300"]
        cmd += [str(original), str(archive)]

        return process_with_fallbacks(cmd, original, workdir, archive, self.engine)
