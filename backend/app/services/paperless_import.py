"""Import a Paperless-ngx document export.

The user drops their export — the folder produced by Paperless's
`document_exporter` (or a zip of it) — into DATA_DIR/import/, then triggers
the import from Settings. Everything runs server-side, so there is no upload
size problem; on a NAS the folder is reachable over the same share as the
watch folder.

What carries over: originals, titles, created dates (→ document date), tags
(with colors), correspondents, document types, and notes. Originals are
ingested through the normal intake path, so dedup applies (re-running an
import skips everything already present) and OCR runs in `skip` mode —
Paperless originals that already carry a text layer stay untouched.

Format notes: the manifest is a Django fixture dump (list of
{model, pk, fields}). Documents reference their files via
`__exported_file_name__` keys. Tag colors are hex strings in current
exports and palette integers in old ones — both handled, unknown shapes
ignored. Every field read is defensive: a missing key degrades to "less
metadata", never a failed import.
"""

import asyncio
import json
import logging
import zipfile
from datetime import datetime
from pathlib import Path

from sqlalchemy import select

from app.config import settings
from app.database import SessionLocal
from app.models import Correspondent, DocType, Document, Tag
from app.services import intake
from app.services.app_state import set_value
from app.services.tag_tree import with_ancestors

logger = logging.getLogger(__name__)

IMPORT_STATUS = "paperless_import_status"

# Paperless 1.x stored a palette index instead of a hex color.
LEGACY_COLORS = {
    1: "#a6cee3", 2: "#1f78b4", 3: "#b2df8a", 4: "#33a02c", 5: "#fb9a99",
    6: "#e31a1c", 7: "#fdbf6f", 8: "#ff7f00", 9: "#cab2d6", 10: "#6a3d9a",
    11: "#b15928", 12: "#000000", 13: "#cccccc",
}


def import_root() -> Path:
    return Path(settings.data_dir) / "import"


def find_export(root: Path) -> Path | None:
    """Locate an export: a directory containing manifest.json, or a zip."""
    if (root / "manifest.json").exists():
        return root
    for child in sorted(root.iterdir()) if root.is_dir() else []:
        if child.is_dir() and (child / "manifest.json").exists():
            return child
        if child.suffix.lower() == ".zip":
            return child
    return None


def _extract_zip(zip_path: Path) -> Path:
    dest = zip_path.parent / f".extracted-{zip_path.stem}"
    if not (dest / "manifest.json").exists():
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(dest)
    # manifest may be at the top level or one folder down
    if (dest / "manifest.json").exists():
        return dest
    for child in dest.iterdir():
        if child.is_dir() and (child / "manifest.json").exists():
            return child
    raise FileNotFoundError("manifest.json not found inside the zip")


def _color_of(fields: dict) -> str | None:
    raw = fields.get("color", fields.get("colour"))
    if isinstance(raw, str) and raw.startswith("#"):
        return raw
    if isinstance(raw, int):
        return LEGACY_COLORS.get(raw)
    return None


def _parse_date(value) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


async def _status(state: str, **extra) -> None:
    async with SessionLocal() as session:
        await set_value(
            session, IMPORT_STATUS, json.dumps({"state": state, **extra})
        )
        await session.commit()


async def run_import(tenant_id) -> None:
    """The background import task. Progress lands in app_settings."""
    try:
        await _run_import(tenant_id)
    except Exception as exc:  # surface, never crash the API process
        logger.exception("paperless import failed")
        await _status("failed", error=str(exc)[:500])


async def _run_import(tenant_id) -> None:
    root = import_root()
    source = find_export(root)
    if source is None:
        await _status(
            "failed",
            error=f"No export found — put the Paperless export folder or zip in {root}",
        )
        return
    if source.suffix.lower() == ".zip":
        await _status("running", note="extracting zip…")
        source = await asyncio.to_thread(_extract_zip, source)

    manifest = json.loads((source / "manifest.json").read_text())

    tag_rows = {}
    corr_rows = {}
    type_rows = {}
    doc_rows = []
    notes: dict[int, list[str]] = {}
    for entry in manifest:
        model = entry.get("model", "")
        fields = entry.get("fields", {})
        if model == "documents.tag":
            tag_rows[entry["pk"]] = fields
        elif model == "documents.correspondent":
            corr_rows[entry["pk"]] = fields
        elif model == "documents.documenttype":
            type_rows[entry["pk"]] = fields
        elif model == "documents.note":
            doc_pk = fields.get("document")
            if doc_pk is not None and fields.get("note"):
                notes.setdefault(doc_pk, []).append(str(fields["note"]))
        elif model == "documents.document":
            doc_rows.append(entry)

    total = len(doc_rows)
    imported = skipped = failed = 0
    await _status("running", done=0, total=total)

    async with SessionLocal() as session:
        # Entities first, idempotent by name.
        async def get_or_create(model, name, **extra):
            row = (
                await session.execute(
                    select(model).where(
                        model.tenant_id == tenant_id, model.name == name
                    )
                )
            ).scalars().first()
            if row is None:
                row = model(tenant_id=tenant_id, name=name, **extra)
                session.add(row)
                await session.flush()
            return row

        tags_by_pk = {}
        for pk, fields in tag_rows.items():
            name = (fields.get("name") or "").strip()
            if not name:
                continue
            tag = await get_or_create(Tag, name)
            color = _color_of(fields)
            if color and not tag.color:
                tag.color = color
            tags_by_pk[pk] = tag
        corr_by_pk = {}
        for pk, fields in corr_rows.items():
            name = (fields.get("name") or "").strip()
            if name:
                corr_by_pk[pk] = await get_or_create(Correspondent, name)
        type_by_pk = {}
        for pk, fields in type_rows.items():
            name = (fields.get("name") or "").strip()
            if name:
                type_by_pk[pk] = await get_or_create(DocType, name)
        await session.commit()

        for i, entry in enumerate(doc_rows):
            fields = entry.get("fields", {})
            file_name = entry.get("__exported_file_name__") or fields.get(
                "filename"
            )
            path = (source / file_name) if file_name else None
            if path is None or not path.exists():
                failed += 1
                logger.warning("import: missing file for document pk %s", entry.get("pk"))
                continue
            title = (fields.get("title") or "").strip() or path.stem
            doc_tags = [
                tags_by_pk[pk] for pk in fields.get("tags", []) if pk in tags_by_pk
            ]
            try:
                doc = await intake.ingest_file(
                    session,
                    tenant_id,
                    path,
                    f"{title}{path.suffix.lower()}",
                    tags=doc_tags,
                )
                doc.title = title
                created = _parse_date(fields.get("created"))
                if created:
                    doc.doc_date = created.date()
                corr = corr_by_pk.get(fields.get("correspondent"))
                if corr:
                    doc.correspondent_id = corr.id
                dtype = type_by_pk.get(fields.get("document_type"))
                if dtype:
                    doc.doc_type_id = dtype.id
                doc_notes = notes.get(entry.get("pk"))
                if doc_notes:
                    doc.notes = "\n\n".join(doc_notes)
                await session.commit()
                imported += 1
            except intake.DuplicateDocument:
                await session.rollback()
                skipped += 1
            except Exception:
                await session.rollback()
                failed += 1
                logger.exception("import: document pk %s failed", entry.get("pk"))
            if (i + 1) % 10 == 0 or i + 1 == total:
                await _status(
                    "running", done=i + 1, total=total,
                    imported=imported, skipped=skipped, failed=failed,
                )

    await _status(
        "done", total=total, imported=imported, skipped=skipped, failed=failed
    )
    logger.info(
        "paperless import finished: %d imported, %d duplicates skipped, %d failed",
        imported, skipped, failed,
    )
