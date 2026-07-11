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


def _fake_run(results):
    """subprocess.run stub yielding scripted (returncode, stderr) per call."""
    calls = []

    def run(cmd, **kwargs):
        code, stderr = results[min(len(calls), len(results) - 1)]
        calls.append(list(cmd))
        return SimpleNamespace(returncode=code, stderr=stderr, stdout="")

    return run, calls


def test_escalates_on_generic_failure(monkeypatch, tmp_path):
    run, calls = _fake_run([
        (7, "GPL Ghostscript: rangecheck in setscreen"),
        (7, "GPL Ghostscript: rangecheck in setscreen"),
        (0, ""),
    ])
    monkeypatch.setattr(T.subprocess, "run", run)
    T.run_ocrmypdf(BASE_CMD, tmp_path)  # should not raise
    assert len(calls) == 3
    assert "--force-ocr" in calls[2]


def test_unfixable_stops_immediately(monkeypatch, tmp_path):
    run, calls = _fake_run([(2, "input file is encrypted")])
    monkeypatch.setattr(T.subprocess, "run", run)
    with pytest.raises(T.OCRError):
        T.run_ocrmypdf(BASE_CMD, tmp_path)
    assert len(calls) == 1  # no pointless retries


def test_total_failure_raises_with_last_error(monkeypatch, tmp_path):
    run, calls = _fake_run([(4, "Pl_DCT::decompress: JPEG data is corrupt")])
    monkeypatch.setattr(T.subprocess, "run", run)
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
