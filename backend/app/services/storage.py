"""Opaque-key blob store.

Files live at <data_dir>/blobs/<aa>/<bb>/<uuid> where aa/bb are the first hex
pairs of the blob id. The human-readable name is metadata only, applied at
download time. Blobs are never renamed or mutated.
"""

import hashlib
import shutil
import uuid
from pathlib import Path

from app.config import settings


def _blob_path(blob_id: uuid.UUID) -> Path:
    hex_id = blob_id.hex
    return Path(settings.data_dir) / "blobs" / hex_id[:2] / hex_id[2:4] / hex_id


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def store_file(source: Path, blob_id: uuid.UUID | None = None) -> tuple[uuid.UUID, str, int]:
    """Copy a file into the store. Returns (blob_id, sha256, size_bytes)."""
    blob_id = blob_id or uuid.uuid4()
    dest = _blob_path(blob_id)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, dest)
    return blob_id, sha256_of(dest), dest.stat().st_size


def blob_file(blob_id: uuid.UUID) -> Path:
    return _blob_path(blob_id)


def delete_blob(blob_id: uuid.UUID) -> None:
    _blob_path(blob_id).unlink(missing_ok=True)
