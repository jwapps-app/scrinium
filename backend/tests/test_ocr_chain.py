from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.ocr import tesseract as T


BASE_CMD = [
    "python3", "-m", "ocrmypdf",
    "--skip-text",
    "--output-type", "pdfa",
    "--plugin", "p",
    "--language", "eng",
    "--quiet",
    "in.pdf", "out.pdf",
]


def test_rotate_flags_toggle(monkeypatch):
    monkeypatch.setattr(T.settings, "rotate_pages", True)
    monkeypatch.setattr(T.settings, "rotate_pages_threshold", 5.0)
    flags = T.rotate_flags()
    assert flags == ["--rotate-pages", "--rotate-pages-threshold", "5.0"]

    monkeypatch.setattr(T.settings, "rotate_pages", False)
    assert T.rotate_flags() == []


def test_rotate_flags_survive_force_raster_fallback(monkeypatch):
    # The rebuild-from-raster fallback drops mode flags but must keep rotation.
    monkeypatch.setattr(T.settings, "rotate_pages", True)
    cmd = BASE_CMD[:6] + T.rotate_flags() + BASE_CMD[6:]
    chain = T.pdfa_fallback_commands(cmd)
    force = dict(chain)["force-raster"]
    assert "--rotate-pages" in force


def test_remedy_chain_shape():
    chain = T.pdfa_fallback_commands(BASE_CMD)
    labels = [label for label, _ in chain]
    assert labels == ["pdfa", "pdfa-rgb", "force-raster"]

    _, rgb = chain[1]
    assert "--color-conversion-strategy" in rgb and "RGB" in rgb

    _, force = chain[2]
    assert "--force-ocr" in force
    assert "--continue-on-soft-render-error" in force
    # mode flags dropped (force-ocr is exclusive), output downgraded to pdf
    assert "--skip-text" not in force
    assert force[force.index("--output-type") + 1] == "pdf"


def test_remedy_chain_preserves_mode_on_first_attempts():
    chain = T.pdfa_fallback_commands(BASE_CMD)
    assert "--skip-text" in chain[0][1]
    assert "--skip-text" in chain[1][1]


def _fake_watched(results):
    """_run_watched stub yielding scripted (returncode, stderr) per call."""
    calls = []

    def watched(attempt, workdir):
        code, stderr = results[min(len(calls), len(results) - 1)]
        calls.append(list(attempt))
        return code, stderr

    return watched, calls


def test_escalates_on_generic_failure(monkeypatch, tmp_path):
    watched, calls = _fake_watched([
        (7, "GPL Ghostscript: rangecheck in setscreen"),
        (7, "GPL Ghostscript: rangecheck in setscreen"),
        (0, ""),
    ])
    monkeypatch.setattr(T, "_run_watched", watched)
    T.run_ocrmypdf(BASE_CMD, tmp_path)  # should not raise
    assert len(calls) == 3
    assert "--force-ocr" in calls[2]


def test_unfixable_stops_immediately(monkeypatch, tmp_path):
    watched, calls = _fake_watched([(2, "input file is encrypted")])
    monkeypatch.setattr(T, "_run_watched", watched)
    with pytest.raises(T.OCRError):
        T.run_ocrmypdf(BASE_CMD, tmp_path)
    assert len(calls) == 1  # no pointless retries


def test_total_failure_raises_with_last_error(monkeypatch, tmp_path):
    watched, calls = _fake_watched([(4, "Pl_DCT::decompress: JPEG data is corrupt")])
    monkeypatch.setattr(T, "_run_watched", watched)
    with pytest.raises(T.OCRError, match="JPEG data is corrupt"):
        T.run_ocrmypdf(BASE_CMD, tmp_path)
    assert len(calls) == 3  # walked the whole chain


def test_text_only_fallback_rescues(monkeypatch, tmp_path):
    """If ocrmypdf dies entirely, the text-only path still yields a result."""
    monkeypatch.setattr(
        T, "run_ocrmypdf",
        lambda cmd, wd: (_ for _ in ()).throw(T.OCRError("exited 7")),
    )
    monkeypatch.setattr(
        T, "text_only_fallback", lambda src, wd: "rescued text content"
    )
    result = T.process_with_fallbacks(
        BASE_CMD, Path("in.pdf"), tmp_path, tmp_path / "archive.pdf", "tesseract"
    )
    assert result.engine == "text-only"
    assert result.archive_path is None
    assert result.text == "rescued text content"


def test_text_only_fallback_empty_reraises(monkeypatch, tmp_path):
    monkeypatch.setattr(
        T, "run_ocrmypdf",
        lambda cmd, wd: (_ for _ in ()).throw(T.OCRError("exited 7")),
    )
    monkeypatch.setattr(T, "text_only_fallback", lambda src, wd: "   ")
    with pytest.raises(T.OCRError):
        T.process_with_fallbacks(
            BASE_CMD, Path("in.pdf"), tmp_path, tmp_path / "archive.pdf", "tesseract"
        )


def test_watchdog_kills_stalled_run(monkeypatch, tmp_path):
    from app.config import settings

    monkeypatch.setattr(settings, "ocr_stall_minutes", 0)  # stall instantly
    code, err = T._run_watched(["sleep", "60"], tmp_path)
    assert code == 137
    assert "no OCR progress" in err


def test_watchdog_spares_progressing_run(monkeypatch, tmp_path):
    from app.config import settings

    # Stall window is generous; the command writes progress and exits fine.
    monkeypatch.setattr(settings, "ocr_stall_minutes", 5)
    script = tmp_path / "work.sh"
    script.write_text(
        '#!/bin/sh\nfor i in 1 2 3; do echo "{\\"done\\": $i}" > "$SCRINIUM_PROGRESS_FILE"; sleep 1; done\nexit 0\n'
    )
    script.chmod(0o755)
    code, _ = T._run_watched([str(script)], tmp_path)
    assert code == 0


def test_stall_kill_skips_remedies_and_rescues(monkeypatch, tmp_path):
    """A stalled run must not be retried twice more; text-only still saves it."""
    from app.config import settings

    monkeypatch.setattr(settings, "ocr_stall_minutes", 0)
    calls = []

    def counting(attempt, wd):
        calls.append(attempt)
        return 137, "killed: no OCR progress for 0 minutes"

    monkeypatch.setattr(T, "_run_watched", counting)
    monkeypatch.setattr(T, "text_only_fallback", lambda s, w: "rescued")
    result = T.process_with_fallbacks(
        BASE_CMD, Path("in.pdf"), tmp_path, tmp_path / "a.pdf", "tesseract"
    )
    assert len(calls) == 1  # no pointless remedy retries after a stall
    assert result.engine == "text-only"


def test_ingest_uses_the_configured_ocr_mode():
    """New documents must inherit the configured mode. The old hardcoded
    "skip" meant ocrmypdf left any page that already carried text alone — and
    scanners routinely emit an empty or invisible text layer, so a legible scan
    could land with nothing searchable in it."""
    import inspect

    from app.config import settings
    from app.services import intake

    assert settings.ocr_mode == "redo"
    source = inspect.getsource(intake.ingest_file)
    assert 'mode=settings.ocr_mode' in source
    assert 'mode="skip"' not in source


def test_ocr_mode_rejects_an_unknown_value():
    """A typo in the env should fail at startup, not silently fall back."""
    import pytest
    from pydantic import ValidationError

    from app.config import Settings

    for good in ("skip", "redo", "force"):
        assert Settings(ocr_mode=good).ocr_mode == good
    with pytest.raises(ValidationError):
        Settings(ocr_mode="sometimes")


def test_fallback_keeps_page_order_when_run_in_parallel(monkeypatch, tmp_path):
    """Pages are OCR'd concurrently but must be stitched back in page order."""
    import time

    monkeypatch.setattr(T.settings, "ocr_jobs", 4)
    monkeypatch.setattr(T.settings, "ocr_fallback_minutes", 60)
    # Finish out of order: later pages return first.
    monkeypatch.setattr(
        T, "_tesseract_image",
        lambda p: (time.sleep(0.01 * (10 - int(p.stem[3:]))), f"[{p.stem}]")[1],
    )
    pages = [Path(f"pg-{i:03d}.png") for i in range(1, 11)]

    text = T._ocr_pages(pages, tmp_path)

    assert text == "\f".join(f"[pg-{i:03d}]" for i in range(1, 11))


def test_fallback_reports_progress_the_worker_can_read(monkeypatch, tmp_path):
    """Without this the bar sits on whatever the failed ocrmypdf run left
    behind, and a multi-hour fallback looks like a wedged job."""
    import json

    monkeypatch.setattr(T.settings, "ocr_jobs", 2)
    monkeypatch.setattr(T.settings, "ocr_fallback_minutes", 60)
    monkeypatch.setattr(T, "_tesseract_image", lambda p: "x")

    T._ocr_pages([Path(f"pg-{i}.png") for i in range(4)], tmp_path)

    report = json.loads((tmp_path / "progress").read_text())
    assert report == {"phase": "text-only", "done": 4, "total": 4}


def test_fallback_budget_keeps_partial_text(monkeypatch, tmp_path):
    """Expiring must not throw away the pages already read."""
    monkeypatch.setattr(T.settings, "ocr_jobs", 1)
    monkeypatch.setattr(T.settings, "ocr_fallback_minutes", 0)  # already expired
    monkeypatch.setattr(T, "_tesseract_image", lambda p: f"[{p.stem}]")
    pages = [Path(f"pg-{i:03d}.png") for i in range(1, 21)]

    text = T._ocr_pages(pages, tmp_path)

    assert text.startswith("[pg-001]")
    assert text.count("[pg-") == 1          # stopped after the first page
    assert text.endswith("\f" * 19)         # the rest stay as empty slots


def test_one_bad_page_does_not_sink_the_document(monkeypatch):
    """This is already the last-resort path; a page that times out is dropped."""
    import subprocess

    def explode(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="tesseract", timeout=600)

    monkeypatch.setattr(T.subprocess, "run", explode)
    assert T._tesseract_image(Path("pg-001.png")) == ""


def test_fallback_pages_are_pinned_to_one_openmp_thread(monkeypatch):
    """Tesseract spreads one page over ~2 cores by default, so running
    OCR_JOBS pages at once would cost double what the setting implies."""
    seen = {}

    def capture(cmd, **kwargs):
        seen.update(kwargs.get("env") or {})
        return SimpleNamespace(returncode=0, stdout="text")

    monkeypatch.setattr(T.subprocess, "run", capture)
    T._tesseract_image(Path("pg-001.png"))

    assert seen.get("OMP_THREAD_LIMIT") == "1"
    assert "PATH" in seen, "must extend the real environment, not replace it"
