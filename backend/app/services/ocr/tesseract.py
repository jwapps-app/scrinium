import logging
import os
import subprocess
import time
from pathlib import Path

from app.config import settings
from app.services.ocr.base import OCRResult

logger = logging.getLogger(__name__)

PROGRESS_PLUGIN = "app.services.ocr.progress_plugin"


def progress_env(workdir: Path) -> dict[str, str]:
    """Env for ocrmypdf subprocesses: page progress lands in the workdir."""
    return {**os.environ, "SCRINIUM_PROGRESS_FILE": str(workdir / "progress")}

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
        proc = subprocess.Popen(
            attempt, stdout=out, stderr=err, env=progress_env(workdir)
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
                proc.kill()
                proc.wait(timeout=60)
                reason = (
                    f"no OCR progress for {settings.ocr_stall_minutes} minutes"
                    if now - started <= hard_limit
                    else f"exceeded the {settings.ocr_max_hours}h ceiling"
                )
                return 137, f"killed: {reason}"
    stderr_text = stderr_path.read_text(errors="replace")
    return proc.returncode, stderr_text


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
        logger.warning("ocrmypdf attempt '%s' failed; escalating", label)
    raise OCRError(f"ocrmypdf exited {last_code}: {last_error}")


def extract_text(pdf: Path) -> str:
    result = subprocess.run(
        ["pdftotext", "-layout", str(pdf), "-"],
        capture_output=True,
        text=True,
        timeout=300,
    )
    if result.returncode != 0:
        raise OCRError(f"pdftotext failed: {result.stderr.strip()[:2000]}")
    return result.stdout


def _tesseract_image(image: Path) -> str:
    result = subprocess.run(
        ["tesseract", str(image), "stdout", "-l", settings.ocr_languages],
        capture_output=True,
        text=True,
        timeout=600,
    )
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
        ["pdftoppm", "-r", "200", "-png", str(source), str(fb_dir / "pg")],
        capture_output=True,
        text=True,
        timeout=3600,
    )
    pages = sorted(fb_dir.glob("pg*.png"))
    if result.returncode != 0 and not pages:
        raise OCRError(
            f"poppler rasterize failed: {result.stderr.strip()[:1000]}"
        )
    return "\f".join(_tesseract_image(p) for p in pages)


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
        text = text_only_fallback(original, workdir)
        if text.strip():
            logger.warning(
                "%s: ocrmypdf failed (%s); stored text-only, original preserved",
                original.name,
                str(exc)[:200],
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
