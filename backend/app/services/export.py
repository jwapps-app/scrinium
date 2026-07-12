"""Full-library export: a zip with every original (and archive) plus a
manifest.json of all metadata. Written server-side into DATA_DIR/export —
no browser download of a 100 GB file; on a NAS it lands on the same share
as everything else. Your documents are never hostage to this app."""

import asyncio
import json
import logging
import re
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database import SessionLocal
from app.models import Document, document_custom_values
from app.services import storage
from app.services.app_state import set_value

logger = logging.getLogger(__name__)

EXPORT_STATUS = "library_export_status"

_UNSAFE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def sanitize(part: str) -> str:
    """A filesystem-safe path segment (Windows-safe too)."""
    cleaned = _UNSAFE.sub("-", part).strip(" .")
    return cleaned[:120] or "untitled"


def folder_for(doc, parents: dict) -> str:
    """Folder path from the document's most specific tag chain.

    Folder drops became tag hierarchies at ingest, so exporting by the
    deepest chain reconstructs the structure the files arrived in. Multiple
    unrelated tags: the longest chain wins, alphabetical on ties.
    """
    best: list[str] = []
    for tag in doc.tags:
        chain = [tag.name]
        parent_id = tag.parent_id
        seen = {tag.id}
        while parent_id and parent_id in parents and parent_id not in seen:
            seen.add(parent_id)
            name, parent_id_next = parents[parent_id]
            chain.append(name)
            parent_id = parent_id_next
        chain.reverse()
        if len(chain) > len(best) or (len(chain) == len(best) and chain < best):
            best = chain
    if not best:
        return "Untagged"
    return "/".join(sanitize(part) for part in best)


async def _status(state: str, **extra) -> None:
    async with SessionLocal() as session:
        await set_value(
            session, EXPORT_STATUS, json.dumps({"state": state, **extra})
        )
        await session.commit()


async def run_export(
    tenant_id, fmt: str | None = None, part_gb: int | None = None
) -> None:
    try:
        await _run_export(
            tenant_id,
            fmt or settings.export_format,
            part_gb or settings.export_part_gb,
        )
    except Exception as exc:
        logger.exception("library export failed")
        await _status("failed", error=str(exc)[:500])


def plan_parts(entries: list[tuple], cap_bytes: int) -> list[list[tuple]]:
    """Pack (folder, arcname, path, size) entries into parts ≤ cap, keeping
    folder subtrees together whenever they fit.

    Entries are grouped by their tag-path folder; folders sort
    depth-first-alphabetically, so a subtree's folders are contiguous and a
    greedy sweep keeps them in the same part when the subtree fits. A folder
    larger than the cap spans parts (paths identical, so unzipping all parts
    into one place still reassembles the tree exactly)."""
    by_folder: dict[str, list[tuple]] = {}
    for entry in entries:
        by_folder.setdefault(entry[0], []).append(entry)

    parts: list[list[tuple]] = [[]]
    part_size = 0

    def place(items: list[tuple], size: int) -> None:
        nonlocal part_size
        if part_size > 0 and part_size + size > cap_bytes:
            parts.append([])
            part_size = 0
        parts[-1].extend(items)
        part_size += size

    for folder in sorted(by_folder):
        items = by_folder[folder]
        folder_size = sum(e[3] for e in items)
        if folder_size <= cap_bytes:
            place(items, folder_size)
        else:
            # One folder bigger than a part: split by files, largest first
            # so the tail packs tight.
            for entry in sorted(items, key=lambda e: -e[3]):
                place([entry], entry[3])
    return [p for p in parts if p]


async def _run_export(tenant_id, fmt: str = "folder", part_gb: int = 10) -> None:
    dest_dir = Path(settings.data_dir) / "export"
    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")

    async with SessionLocal() as session:
        docs = (
            (
                await session.execute(
                    select(Document)
                    .where(
                        Document.tenant_id == tenant_id,
                        Document.deleted_at.is_(None),
                    )
                    .options(
                        selectinload(Document.tags),
                        selectinload(Document.correspondent),
                        selectinload(Document.doc_type),
                    )
                    .order_by(Document.created_at)
                )
            )
            .scalars()
            .all()
        )
        custom_rows = (
            await session.execute(
                select(document_custom_values).where(
                    document_custom_values.c.document_id.in_(
                        [d.id for d in docs]
                    )
                    if docs
                    else False
                )
            )
        ).all()
        custom_by_doc: dict = {}
        for row in custom_rows:
            custom_by_doc.setdefault(row.document_id, {})[str(row.field_id)] = row.value

        # id → (name, parent_id) for every tag, to walk chains cheaply.
        from app.models import Tag

        parents = {
            t.id: (t.name, t.parent_id)
            for t in (
                await session.execute(
                    select(Tag).where(Tag.tenant_id == tenant_id)
                )
            ).scalars()
        }
        used_names: dict[str, int] = {}

        total = len(docs)
        await _status("running", done=0, total=total)

        manifest = []
        # (folder, arcname, disk path, size) collected while the session is open
        files: list[tuple] = []
        for doc in docs:
            suffix = Path(doc.original_filename).suffix.lower() or ".bin"
            folder = folder_for(doc, parents)
            base = sanitize(doc.title)
            # De-collide titles within a folder: "x", "x (2)", "x (3)"…
            key = f"{folder}/{base}".lower()
            used_names[key] = used_names.get(key, 0) + 1
            if used_names[key] > 1:
                base = f"{base} ({used_names[key]})"
            original_name = f"originals/{folder}/{base}{suffix}"
            archive_name = (
                f"searchable/{folder}/{base}.pdf"
                if doc.archive_blob_id is not None
                else None
            )
            original_path = storage.blob_file(doc.original_blob_id)
            if original_path.exists():
                files.append(
                    (folder, original_name, original_path, original_path.stat().st_size)
                )
            if archive_name:
                archive_path = storage.blob_file(doc.archive_blob_id)
                if archive_path.exists():
                    files.append(
                        (folder, archive_name, archive_path, archive_path.stat().st_size)
                    )
                else:
                    archive_name = None
            manifest.append(
                {
                    "id": str(doc.id),
                    "title": doc.title,
                    "original_filename": doc.original_filename,
                    "original": original_name,
                    "archive": archive_name,
                    "status": str(doc.status),
                    "ocr_engine": doc.ocr_engine,
                    "page_count": doc.page_count,
                    "doc_date": doc.doc_date.isoformat() if doc.doc_date else None,
                    "created_at": doc.created_at.isoformat(),
                    "correspondent": doc.correspondent.name if doc.correspondent else None,
                    "doc_type": doc.doc_type.name if doc.doc_type else None,
                    "tags": [t.name for t in doc.tags],
                    "notes": doc.notes,
                    "custom_values": custom_by_doc.get(doc.id, {}),
                }
            )

    manifest_bytes = json.dumps(
        {
            "app": settings.app_name,
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "documents": manifest,
        },
        indent=2,
    )

    if fmt == "zip":
        cap = max(1, part_gb) * 1024**3
        part_plans = plan_parts(files, cap)

        def build_zips() -> tuple[int, list[str], float]:
            written = 0
            paths = []
            total_bytes = 0
            for i, part in enumerate(part_plans, start=1):
                suffix_name = (
                    f"library-export-{stamp}.zip"
                    if len(part_plans) == 1
                    else f"library-export-{stamp}-part{i:02d}.zip"
                )
                dest = dest_dir / suffix_name
                with zipfile.ZipFile(dest, "w", zipfile.ZIP_STORED) as zf:
                    if i == 1:
                        zf.writestr("manifest.json", manifest_bytes)
                    for _folder, arcname, path, _size in part:
                        zf.write(path, arcname)
                        written += 1
                total_bytes += dest.stat().st_size
                paths.append(str(dest))
            return written, paths, total_bytes / 1024**2

        written, paths, size_mb = await asyncio.to_thread(build_zips)
        await _status(
            "done",
            total=total,
            files=written,
            parts=len(paths),
            path=paths[0] if len(paths) == 1 else f"{len(paths)} parts in {dest_dir}",
            size_mb=round(size_mb, 1),
        )
        logger.info(
            "library export done: %d docs, %d zip part(s), %.1f MB",
            total, len(paths), size_mb,
        )
        return

    # Folder format: a real browsable tree. Hardlink when possible (blobs
    # and export share a volume → instant and zero extra disk), copy as
    # fallback.
    import os
    import shutil

    root = dest_dir / f"library-export-{stamp}"

    def build_tree() -> tuple[int, int]:
        written = linked = 0
        root.mkdir(parents=True, exist_ok=True)
        (root / "manifest.json").write_text(manifest_bytes)
        for _folder, arcname, path, _size in files:
            dest = root / arcname
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists():
                continue
            try:
                os.link(path, dest)
                linked += 1
            except OSError:
                shutil.copyfile(path, dest)
            written += 1
        return written, linked

    written, linked = await asyncio.to_thread(build_tree)
    await _status(
        "done",
        total=total,
        files=written,
        hardlinked=linked,
        path=str(root),
    )
    logger.info(
        "library export done: %d docs → %s (%d/%d hardlinked)",
        total, root, linked, written,
    )
