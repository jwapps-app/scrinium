"""ocrmypdf engine plugin backed by the Apple Vision sidecar (Option A).

Invoked as `python3 -m ocrmypdf --plugin app.services.ocr.apple_engine_plugin`.
ocrmypdf keeps 100% of the PDF plumbing (deskew, PDF/A, text-layer grafting);
Vision replaces Tesseract as the recognition engine in the middle, mirroring
how ocrmypdf-easyocr works.

Coordinate mapping (the known fiddly bit): Vision bboxes are normalized 0-1
with a bottom-left origin; hOCR wants pixels with a top-left origin, so
top = (1 - y_max) * height and bottom = (1 - y_min) * height.
"""

import html
from argparse import Namespace
from pathlib import Path

import httpx
from ocrmypdf import hookimpl
from ocrmypdf.pluginspec import OcrEngine, OrientationConfidence

from app.config import settings

PAGE_TIMEOUT_S = 120

HOCR_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.0 Transitional//EN"
  "http://www.w3.org/TR/xhtml1/DTD/xhtml1-transitional.dtd">
<html xmlns="http://www.w3.org/1999/xhtml">
 <head>
  <title></title>
  <meta http-equiv="Content-Type" content="text/html;charset=utf-8" />
  <meta name="ocr-system" content="apple-vision" />
  <meta name="ocr-capabilities" content="ocr_page ocr_par ocr_line ocrx_word" />
 </head>
 <body>
  <div class="ocr_page" id="page_1" title="bbox 0 0 {width} {height}">
   <p class="ocr_par" id="par_1" title="bbox 0 0 {width} {height}">
{lines}
   </p>
  </div>
 </body>
</html>
"""


def _recognize(input_file: Path) -> dict:
    resp = httpx.post(
        f"{settings.apple_ocr_url}/ocr",
        content=input_file.read_bytes(),
        headers={"Content-Type": "image/png"},
        timeout=PAGE_TIMEOUT_S,
    )
    resp.raise_for_status()
    return resp.json()


def _to_hocr(result: dict) -> str:
    width, height = result["width"], result["height"]
    lines = []
    for i, block in enumerate(result.get("blocks", [])):
        x0, y0, x1, y1 = block["bbox"]
        left, right = round(x0 * width), round(x1 * width)
        top, bottom = round((1 - y1) * height), round((1 - y0) * height)
        conf = round(block.get("confidence", 1.0) * 100)
        text = html.escape(block["text"])
        # Vision reports whole lines, not words; emit each line as a single
        # ocrx_word spanning the line box. Positioning is line-accurate.
        lines.append(
            f'    <span class="ocr_line" id="line_{i}"'
            f' title="bbox {left} {top} {right} {bottom}">'
            f'<span class="ocrx_word" id="word_{i}"'
            f' title="bbox {left} {top} {right} {bottom}; x_wconf {conf}">'
            f"{text}</span></span>"
        )
    return HOCR_TEMPLATE.format(width=width, height=height, lines="\n".join(lines))


class AppleVisionEngine(OcrEngine):
    @staticmethod
    def version() -> str:
        return "1.0"

    @staticmethod
    def creator_tag(options: Namespace) -> str:
        return "Apple Vision sidecar 1.0"

    def __str__(self):
        return "Apple Vision sidecar 1.0"

    @staticmethod
    def languages(options: Namespace) -> set[str]:
        # Vision autodetects; accept whatever was requested.
        return set(options.languages)

    @staticmethod
    def get_orientation(input_file: Path, options: Namespace) -> OrientationConfidence:
        # Vision doesn't expose page orientation, so borrow Tesseract's OSD —
        # the same detector ocrmypdf uses for its built-in engine — so that
        # --rotate-pages can straighten sideways scans before Vision reads
        # them. Detection is Tesseract; recognition stays Apple Vision.
        try:
            from ocrmypdf._exec.tesseract import get_orientation as _osd

            return _osd(input_file, engine_mode=None, timeout=30.0)
        except Exception:
            return OrientationConfidence(angle=0, confidence=0.0)

    @staticmethod
    def generate_hocr(
        input_file: Path, output_hocr: Path, output_text: Path, options: Namespace
    ) -> None:
        result = _recognize(input_file)
        output_hocr.write_text(_to_hocr(result), encoding="utf-8")
        output_text.write_text(
            "\n".join(b["text"] for b in result.get("blocks", [])), encoding="utf-8"
        )

    @staticmethod
    def generate_pdf(
        input_file: Path, output_pdf: Path, output_text: Path, options: Namespace
    ) -> None:
        raise NotImplementedError("use the hocr renderer")


@hookimpl
def get_ocr_engine():
    return AppleVisionEngine()
