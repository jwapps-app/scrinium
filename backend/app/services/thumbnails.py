"""First-page thumbnail generation (PNG, longest side ~480px).

PDFs render via poppler's pdftoppm; images downscale via Pillow. Returns None
rather than raising — a missing thumbnail must never fail ingestion.
"""

import logging
import subprocess
from pathlib import Path

from PIL import Image

logger = logging.getLogger(__name__)

THUMB_SIZE = 480
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}


def make_thumbnail(source: Path, workdir: Path) -> Path | None:
    """Render a thumbnail for a PDF or image file into workdir."""
    out = workdir / "thumb.png"
    try:
        if source.suffix.lower() in IMAGE_SUFFIXES:
            with Image.open(source) as img:
                img.thumbnail((THUMB_SIZE, THUMB_SIZE))
                img.convert("RGB").save(out, "PNG")
            return out

        prefix = workdir / "thumbsrc"
        result = subprocess.run(
            [
                "pdftoppm", "-png", "-f", "1", "-l", "1",
                "-scale-to", str(THUMB_SIZE), str(source), str(prefix),
            ],
            capture_output=True,
            stdin=subprocess.DEVNULL,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
        if result.returncode != 0:
            logger.warning("thumbnail render failed: %s", result.stderr.strip()[:500])
            return None
        pages = sorted(workdir.glob("thumbsrc*.png"))
        if not pages:
            return None
        pages[0].rename(out)
        return out
    except Exception as exc:
        logger.warning("thumbnail generation failed for %s: %s", source.name, exc)
        return None
