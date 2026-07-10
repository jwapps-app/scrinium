import logging
import os
import subprocess
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


def pdfa_fallback_commands(cmd: list[str]) -> list[tuple[str, list[str]]]:
    """The remedy chain for PDF/A conversion failures.

    Some born-digital PDFs use print-industry color constructs (DeviceN /
    spot colors, overprint) that Ghostscript can't carry into strict PDF/A.
    Attempts, in order:
      1. as requested (strict PDF/A)
      2. + --color-conversion-strategy RGB  (normalize exotic color spaces,
         still PDF/A)
      3. --output-type pdf                  (plain searchable PDF; archival
         format sacrificed for that one document, search/text kept)
    """
    rgb = cmd + ["--color-conversion-strategy", "RGB"]
    plain = list(cmd)
    out_type = plain.index("--output-type")
    plain[out_type + 1] = "pdf"
    return [("pdfa", cmd), ("pdfa-rgb", rgb), ("plain-pdf", plain)]


def run_ocrmypdf(cmd: list[str], workdir: Path) -> None:
    """Run ocrmypdf with the PDF/A remedy chain. Fallbacks only trigger on
    Ghostscript/PDF-A-shaped failures — a corrupt input fails once, fast."""
    last_error = ""
    for label, attempt in pdfa_fallback_commands(cmd):
        result = subprocess.run(
            attempt,
            capture_output=True,
            text=True,
            timeout=7200,
            env=progress_env(workdir),
        )
        if result.returncode == 0:
            if label != "pdfa":
                logger.warning(
                    "PDF/A conversion needed fallback '%s' for %s",
                    label,
                    attempt[-2],
                )
            return
        last_error = result.stderr.strip()[:2000]
        gs_shaped = "Ghostscript" in last_error or "PDF/A" in last_error
        if not gs_shaped:
            break  # not a conversion problem; retrying won't help
        logger.warning("ocrmypdf attempt '%s' failed; trying next remedy", label)
    raise OCRError(f"ocrmypdf exited {result.returncode}: {last_error}")


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
            "--language", settings.ocr_languages,
            "--quiet",
        ]
        if original.suffix.lower() in IMAGE_SUFFIXES:
            cmd += ["--image-dpi", "300"]
        cmd += [str(original), str(archive)]

        run_ocrmypdf(cmd, workdir)

        return OCRResult(
            archive_path=archive,
            text=extract_text(archive),
            engine=self.engine,
        )
