import os
import time
import uuid
from datetime import date
from pathlib import Path

from app.config import settings
from app.services import storage
from app.services.paperless_import import _color_of, _parse_date, find_export
from app.services.separators import _is_separator_value, split_on_separators
from app.services.watch import _skip_part, sweep_retention


# --- separators -------------------------------------------------------------

def test_separator_value_matches_paperless_convention():
    assert _is_separator_value(b"PATCHT")
    assert _is_separator_value(b"PATCH-T")
    assert _is_separator_value(b"patch t")


def test_separator_value_rejects_other_codes():
    assert not _is_separator_value(b"ASN00042")
    assert not _is_separator_value(b"")
    assert not _is_separator_value(b"PATCHT-EXTRA")


def test_split_disabled_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "split_on_separators", False)
    assert split_on_separators(tmp_path / "x.pdf") is None


def test_split_ignores_non_pdf(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "split_on_separators", True)
    assert split_on_separators(tmp_path / "x.png") is None


# --- paperless import parsing ------------------------------------------------

def test_color_hex_passthrough():
    assert _color_of({"color": "#a6cee3"}) == "#a6cee3"


def test_color_legacy_palette_int():
    assert _color_of({"colour": 5}) == "#fb9a99"


def test_color_garbage_ignored():
    assert _color_of({"color": 999}) is None
    assert _color_of({}) is None


def test_parse_date_iso_z():
    parsed = _parse_date("2019-04-02T00:00:00Z")
    assert parsed is not None and parsed.date() == date(2019, 4, 2)


def test_parse_date_garbage():
    assert _parse_date("not a date") is None
    assert _parse_date(None) is None


def test_find_export_locates_folder_and_zip(tmp_path):
    assert find_export(tmp_path) is None
    nested = tmp_path / "export"
    nested.mkdir()
    (nested / "manifest.json").write_text("[]")
    assert find_export(tmp_path) == nested
    # manifest at the root wins
    (tmp_path / "manifest.json").write_text("[]")
    assert find_export(tmp_path) == tmp_path


# --- watch folder -------------------------------------------------------------

def test_skip_parts_cover_synology_junk():
    assert _skip_part(".consumed")
    assert _skip_part("@eaDir")
    assert _skip_part("#recycle")
    assert _skip_part("._appledouble")
    assert not _skip_part("Taxes")


def test_sweep_retention_opt_in_and_selective(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "watch_dir", str(tmp_path))
    old_time = time.time() - 40 * 86400
    layout = {
        ".consumed/sub/old.pdf": old_time,
        ".consumed/sub/new.pdf": None,
        ".duplicates/old.pdf": old_time,
        ".failed/old.pdf": old_time,  # never swept
    }
    for rel, mtime in layout.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x")
        if mtime:
            os.utime(p, (mtime, mtime))

    # default 0 = disabled
    monkeypatch.setattr(settings, "consumed_retention_days", 0)
    assert sweep_retention() == 0

    monkeypatch.setattr(settings, "consumed_retention_days", 30)
    assert sweep_retention() == 2
    assert not (tmp_path / ".consumed/sub/old.pdf").exists()
    assert (tmp_path / ".consumed/sub/new.pdf").exists()
    assert not (tmp_path / ".duplicates/old.pdf").exists()
    assert (tmp_path / ".failed/old.pdf").exists()


# --- blob storage --------------------------------------------------------------

def test_blob_roundtrip(tmp_path):
    src = tmp_path / "in.bin"
    src.write_bytes(b"scrinium blob test")
    blob_id, sha, size = storage.store_file(src)
    stored = storage.blob_file(blob_id)
    assert stored.exists()
    assert size == len(b"scrinium blob test")
    assert sha == storage.sha256_of(src)
    # opaque fan-out layout aa/bb/<hex>, no extension
    assert stored.parent.parent.name == blob_id.hex[:2]
    assert stored.suffix == ""
    storage.delete_blob(blob_id)
    assert not stored.exists()
    storage.delete_blob(uuid.uuid4())  # missing blob: no error
