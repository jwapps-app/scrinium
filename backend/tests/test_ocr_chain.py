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
