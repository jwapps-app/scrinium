"""Apple Vision provider — Option A (ocrmypdf engine plugin).

Runs the same ocrmypdf pipeline as the Tesseract path, but with the Vision
sidecar as the recognition engine (see apple_engine_plugin). Output has exact
PDF/A parity: searchable archive with an invisible Vision text layer.

Fallback contract: any sidecar failure — down at start, or dying mid-job —
falls back to Tesseract. A stopped sidecar never wedges ingestion.
"""

import logging
import subprocess
from pathlib import Path

import httpx

from app.config import settings
from app.services.ocr.base import OCRProvider, OCRResult
from app.services.ocr.tesseract import (
    IMAGE_SUFFIXES,
    MODE_FLAGS,
    PROGRESS_PLUGIN,
    OCRError,
    extract_text,
    process_with_fallbacks,
    rotate_flags,
)

logger = logging.getLogger(__name__)

PLUGIN_MODULE = "app.services.ocr.apple_engine_plugin"


class AppleVisionProvider:
    engine = "apple"

    def __init__(self, fallback: OCRProvider):
        self.fallback = fallback

    def sidecar_healthy(self) -> bool:
        try:
            resp = httpx.get(f"{settings.apple_ocr_url}/health", timeout=3)
            return resp.status_code == 200
        except httpx.HTTPError:
            return False

    def process(self, original: Path, workdir: Path, mode: str = "skip") -> OCRResult:
        if not self.sidecar_healthy():
            logger.info("Apple sidecar not reachable; falling back to Tesseract")
            return self.fallback.process(original, workdir, mode)
        try:
            return self._ocr_via_sidecar(original, workdir, mode)
        except Exception as exc:
            logger.warning(
                "Apple sidecar OCR failed (%s); falling back to Tesseract", exc
            )
            return self.fallback.process(original, workdir, mode)

    def _ocr_via_sidecar(self, original: Path, workdir: Path, mode: str) -> OCRResult:
        archive = workdir / "archive.pdf"
        # `python3 -m ocrmypdf` (not the console script) so the plugin module
        # resolves against the app package in the working directory.
        cmd = [
            "python3", "-m", "ocrmypdf",
            *MODE_FLAGS.get(mode, MODE_FLAGS["skip"]),
            "--output-type", "pdfa",
            "--pdf-renderer", "hocr",
            "--plugin", PLUGIN_MODULE,
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
