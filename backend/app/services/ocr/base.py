from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass
class OCRResult:
    """Outcome of one document's OCR pass.

    archive_path: the searchable PDF/A produced (separate from the original).
        None when the provider only extracts text for search (Apple Option B);
        the viewer/download then falls back to the original.
    text: full extracted text for indexing.
    engine: which recognition engine actually did the work.
    """

    archive_path: Path | None
    text: str
    engine: str


class OCRProvider(Protocol):
    def process(
        self, original: Path, workdir: Path, mode: str = "skip", pdfa: bool = True
    ) -> OCRResult:
        """Produce a searchable archive PDF + text from the original.

        pdfa: write the archive as PDF/A. Worth it for a born-digital
        document, where embedded fonts are what decays; on a scan there is no
        text to protect and Ghostscript's conversion costs ~4x the size.

        mode: "skip" (don't re-OCR pages with a text layer), "redo"
        (re-OCR replacing existing layers), "force" (rasterize + OCR all).
        Must never mutate `original`.
        """
        ...
