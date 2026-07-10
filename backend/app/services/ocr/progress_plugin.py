"""ocrmypdf plugin that reports stage progress to a file.

Loaded on every ocrmypdf run (both engines). ocrmypdf creates a progress bar
per stage; we classify each stage into a coarse phase and write JSON
(`{"phase": ..., "done": n, "total": t}`) to the path in
SCRINIUM_PROGRESS_FILE. The worker polls that file onto the job row so the
UI can show "Processing 43%" during recognition and "Finishing 61%" during
PDF/A assembly — without the phase split, the bar parks on a misleading
100% while Ghostscript rebuilds the whole document.
"""

import json
import os
from pathlib import Path

from ocrmypdf import hookimpl

# Recognition stages (tesseract engine / apple hOCR plugin).
OCR_DESCS = {"OCR", "hOCR", "Image processing"}
# Early page scan; everything else with a total counts as finishing
# (PDF/A conversion, Grafting hOCR to PDF, optimization…).
PREPARING_DESCS = {"Scanning contents"}


class FileProgressBar:
    def __init__(self, *, total=None, desc=None, unit=None, disable=False, **kwargs):
        self.total = total
        self.current = 0.0
        if desc in OCR_DESCS:
            self.phase = "ocr"
        elif desc in PREPARING_DESCS:
            self.phase = "preparing"
        else:
            self.phase = "finishing"
        target = os.environ.get("SCRINIUM_PROGRESS_FILE")
        self.path = Path(target) if target and total else None

    def _write(self):
        if self.path is None:
            return
        try:
            self.path.write_text(
                json.dumps(
                    {"phase": self.phase, "done": self.current, "total": self.total}
                )
            )
        except OSError:
            self.path = None  # never let progress reporting break OCR

    def __enter__(self):
        self._write()
        return self

    def __exit__(self, *args):
        return False

    def update(self, n=1, *, completed=None):
        if completed is not None:
            self.current = completed
        else:
            self.current += n
        self._write()


@hookimpl
def get_progressbar_class():
    return FileProgressBar
