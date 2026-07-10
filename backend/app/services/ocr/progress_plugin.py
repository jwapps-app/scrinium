"""ocrmypdf plugin that reports page progress to a file.

Loaded on every ocrmypdf run (both engines). ocrmypdf constructs a progress
bar per stage; we only track the page-unit stages and write "done total" to
the path in SCRINIUM_PROGRESS_FILE. The worker polls that file and mirrors
it onto the job row for the UI.
"""

import os
from pathlib import Path

from ocrmypdf import hookimpl


class FileProgressBar:
    def __init__(self, *, total=None, desc=None, unit=None, disable=False, **kwargs):
        self.total = total
        self.unit = unit
        self.current = 0.0
        target = os.environ.get("SCRINIUM_PROGRESS_FILE")
        # Only the page-unit bars reflect OCR work worth showing.
        self.path = Path(target) if target and unit == "page" and total else None

    def _write(self):
        if self.path is None:
            return
        try:
            self.path.write_text(f"{self.current} {self.total}")
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
