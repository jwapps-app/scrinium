"""Opaque-key blob store.

Files live at <data_dir>/blobs/<aa>/<bb>/<uuid> where aa/bb are the first hex
pairs of the blob id. The human-readable name is metadata only, applied at
download time. Blobs are never renamed or mutated.
"""

import hashlib
import logging
import os
import uuid
from pathlib import Path

from app.config import settings


logger = logging.getLogger(__name__)


def _blob_path(blob_id: uuid.UUID) -> Path:
    hex_id = blob_id.hex
    return Path(settings.data_dir) / "blobs" / hex_id[:2] / hex_id[2:4] / hex_id


def open_source(path: Path):
    """Open a file for reading without following a symlink at the last
    path component.

    The watch share is writable by less-privileged NAS accounts, and the
    scan-time symlink check is separated from the reads by database round
    trips — long enough for the checked file to be swapped for a link to
    something the container (running as root) can read. Refusing here, at
    the open itself, closes that window for the final component; O_NOFOLLOW
    raises ELOOP on a link instead of opening through it.
    """
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        return os.fdopen(fd, "rb")
    except Exception:
        os.close(fd)
        raise


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open_source(path) as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def store_file(source: Path, blob_id: uuid.UUID | None = None) -> tuple[uuid.UUID, str, int]:
    """Copy a file into the store, hashing while copying so the bytes are
    read exactly once. Returns (blob_id, sha256, size_bytes)."""
    blob_id = blob_id or uuid.uuid4()
    dest = _blob_path(blob_id)
    dest.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    size = 0
    with open_source(source) as src, open(dest, "wb") as out:
        while chunk := src.read(1024 * 1024):
            digest.update(chunk)
            out.write(chunk)
            size += len(chunk)
    # Owner-only. The store sits on a bind-mounted NAS share, so the default
    # 0644 meant every local account could read every original and archive
    # directly off the filesystem — bypassing auth, share links and trash.
    try:
        os.chmod(dest, 0o600)
        os.chmod(dest.parent, 0o700)
    except OSError:
        logger.debug("could not tighten permissions on %s", dest, exc_info=True)
    return blob_id, digest.hexdigest(), size


def blob_file(blob_id: uuid.UUID) -> Path:
    return _blob_path(blob_id)


def delete_blob(blob_id: uuid.UUID) -> None:
    _blob_path(blob_id).unlink(missing_ok=True)
