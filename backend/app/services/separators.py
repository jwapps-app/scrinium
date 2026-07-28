"""Barcode separator sheets: split a scanned stack into documents.

Opt-in (`SPLIT_ON_SEPARATORS=1`). When a multi-page PDF arrives, each page
is rasterized at low DPI and scanned for a barcode/QR whose text matches
`SEPARATOR_BARCODE` (default PATCHT — the same convention Paperless uses,
so existing separator sheets keep working). Pages between separators become
independent documents; the separator pages themselves are dropped.

Detection cost is one low-res raster pass per multi-page PDF, so the
feature stays off unless asked for.
"""

import logging
import re
import subprocess
import tempfile
from pathlib import Path

import pikepdf

from app.config import settings

logger = logging.getLogger(__name__)

_NORMALIZE = re.compile(r"[^A-Z0-9]")


def _is_separator_value(data: bytes) -> bool:
    try:
        text = data.decode("utf-8", "ignore")
    except Exception:
        return False
    wanted = _NORMALIZE.sub("", settings.separator_barcode.upper())
    return _NORMALIZE.sub("", text.upper()) == wanted


def _separator_pages(pdf_path: Path, workdir: Path) -> list[int]:
    """1-based page numbers that carry a separator code."""
    from pyzbar.pyzbar import decode
    from PIL import Image

    raster_dir = workdir / "sepscan"
    raster_dir.mkdir(exist_ok=True)
    result = subprocess.run(
        [
            "pdftoppm",
            "-r", "120",
            # Same MediaBox ceiling as the OCR fallback: a barcode scan does not
            # need more than this, and it bounds a hostile page size.
            "-scale-to-x", "1600",
            "-scale-to-y", "2100",
            "-gray", "-png",
            str(pdf_path),
            str(raster_dir / "pg"),
        ],
        capture_output=True,
        timeout=1800,
    )
    if result.returncode != 0:
        return []
    hits = []
    for image_path in sorted(raster_dir.glob("pg*.png")):
        page_no = int(image_path.stem.split("-")[-1])
        with Image.open(image_path) as img:
            for symbol in decode(img):
                if _is_separator_value(symbol.data):
                    hits.append(page_no)
                    break
        image_path.unlink(missing_ok=True)
    return hits


def split_on_separators(pdf_path: Path) -> list[Path] | None:
    """If enabled and the PDF contains separator pages, return the segment
    files (in a temp dir the caller consumes); otherwise None."""
    if not settings.split_on_separators:
        return None
    if pdf_path.suffix.lower() != ".pdf":
        return None
    try:
        with pikepdf.open(pdf_path) as pdf:
            total = len(pdf.pages)
    except pikepdf.PdfError:
        return None
    if total < 2:
        return None

    with tempfile.TemporaryDirectory(prefix="sepscan-") as tmp:
        separators = _separator_pages(pdf_path, Path(tmp))
    if not separators:
        return None

    # Build page runs between separators, dropping the separator pages.
    runs: list[list[int]] = []
    current: list[int] = []
    sep_set = set(separators)
    for page in range(1, total + 1):
        if page in sep_set:
            if current:
                runs.append(current)
                current = []
        else:
            current.append(page)
    if current:
        runs.append(current)
    if len(runs) <= 1:
        # Separator at the edge only — nothing actually splits.
        if runs and len(runs[0]) < total:
            pass  # separator pages still dropped below
        else:
            return None

    out_dir = Path(tempfile.mkdtemp(prefix="split-"))
    segments = []
    with pikepdf.open(pdf_path) as pdf:
        for i, run in enumerate(runs, start=1):
            segment = pikepdf.new()
            for page in run:
                segment.pages.append(pdf.pages[page - 1])
            dest = out_dir / f"{pdf_path.stem}-part{i:02d}.pdf"
            segment.save(dest)
            segments.append(dest)
    logger.info(
        "separator split: %s → %d segment(s) (%d separator page(s) dropped)",
        pdf_path.name, len(segments), len(separators),
    )
    return segments
