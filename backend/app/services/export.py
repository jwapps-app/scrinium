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


async def run_export(tenant_id) -> None:
    try:
        await _run_export(tenant_id)
    except Exception as exc:
        logger.exception("library export failed")
        await _status("failed", error=str(exc)[:500])


async def _run_export(tenant_id) -> None:
    dest_dir = Path(settings.data_dir) / "export"
    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    dest = dest_dir / f"library-export-{stamp}.zip"

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
        # (zip arcname, disk path) pairs collected while the session is open
        files: list[tuple[str, Path]] = []
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
                files.append((original_name, original_path))
            if archive_name:
                archive_path = storage.blob_file(doc.archive_blob_id)
                if archive_path.exists():
                    files.append((archive_name, archive_path))
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

    def build_zip() -> int:
        written = 0
        with zipfile.ZipFile(dest, "w", zipfile.ZIP_STORED) as zf:
            zf.writestr(
                "manifest.json",
                json.dumps(
                    {
                        "app": settings.app_name,
                        "exported_at": datetime.now(timezone.utc).isoformat(),
                        "documents": manifest,
                    },
                    indent=2,
                ),
            )
            for arcname, path in files:
                zf.write(path, arcname)
                written += 1
        return written

    written = await asyncio.to_thread(build_zip)
    size_mb = round(dest.stat().st_size / (1024 * 1024), 1)
    await _status(
        "done",
        total=total,
        files=written,
        path=str(dest),
        size_mb=size_mb,
    )
    logger.info("library export done: %d docs, %s (%.1f MB)", total, dest, size_mb)
