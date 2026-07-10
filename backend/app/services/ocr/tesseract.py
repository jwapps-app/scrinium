import os
import subprocess
from pathlib import Path

from app.config import settings
from app.services.ocr.base import OCRResult

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

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=7200,
            env=progress_env(workdir),
        )
        if result.returncode != 0:
            raise OCRError(
                f"ocrmypdf exited {result.returncode}: {result.stderr.strip()[:2000]}"
            )

        return OCRResult(
            archive_path=archive,
            text=extract_text(archive),
            engine=self.engine,
        )
