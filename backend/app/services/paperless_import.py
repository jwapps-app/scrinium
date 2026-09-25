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
from datetime import date, datetime
from pathlib import Path

from sqlalchemy import select

from app.config import settings
from app.database import SessionLocal
from app.models import (
    Annotation,
    Correspondent,
    CustomField,
    DocType,
    Document,
    ReadingPosition,
    Tag,
    User,
    document_custom_values,
)
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


def _safe_member(source: Path, value) -> Path | None:
    """Resolve a manifest-referenced file, refusing anything that escapes
    the export directory (absolute paths, ../ traversal). The manifest is
    untrusted — it rides inside the imported zip."""
    if not value or not isinstance(value, str):
        return None
    candidate = (source / value).resolve()
    root = source.resolve()
    if candidate == root or root not in candidate.parents:
        return None
    return candidate


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


MAX_EXTRACT_BYTES = 100 * 1024**3  # 100 GB uncompressed ceiling


def _extract_zip(zip_path: Path) -> Path:
    dest = zip_path.parent / f".extracted-{zip_path.stem}"
    if not (dest / "manifest.json").exists():
        with zipfile.ZipFile(zip_path) as zf:
            total = sum(i.file_size for i in zf.infolist())
            if total > MAX_EXTRACT_BYTES:
                raise ValueError(
                    f"Export expands to {total // 1024**3} GB — refusing "
                    "(possible zip bomb; raise MAX_EXTRACT_BYTES if genuine)"
                )
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

    manifest = await asyncio.to_thread(
        lambda: json.loads((source / "manifest.json").read_text())
    )

    # A Scrinium export manifest is a dict with a "documents" list; a
    # Paperless export is a bare Django-fixture list. One Import button,
    # both formats.
    if isinstance(manifest, dict) and "documents" in manifest:
        await _restore_scrinium(tenant_id, source, manifest)
        return

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

        # Paperless pk → our row id. Ids only: a duplicate further down
        # rolls the session back, which expires every cached ORM object,
        # and the next touch of `corr.id` or a tag's parent would then need
        # a synchronous load the async session cannot do (MissingGreenlet).
        # Every document that shared that correspondent or tag then failed.
        tags_by_pk: dict = {}
        for pk, fields in tag_rows.items():
            name = (fields.get("name") or "").strip()
            if not name:
                continue
            tag = await get_or_create(Tag, name)
            color = _color_of(fields)
            if color and not tag.color:
                tag.color = color
            tags_by_pk[pk] = tag.id
        corr_by_pk: dict = {}
        for pk, fields in corr_rows.items():
            name = (fields.get("name") or "").strip()
            if name:
                corr_by_pk[pk] = (await get_or_create(Correspondent, name)).id
        type_by_pk: dict = {}
        for pk, fields in type_rows.items():
            name = (fields.get("name") or "").strip()
            if name:
                type_by_pk[pk] = (await get_or_create(DocType, name)).id
        await session.commit()

        for i, entry in enumerate(doc_rows):
            fields = entry.get("fields", {})
            file_name = entry.get("__exported_file_name__") or fields.get(
                "filename"
            )
            path = _safe_member(source, file_name)
            if path is None or not path.exists():
                failed += 1
                logger.warning("import: missing file for document pk %s", entry.get("pk"))
                continue
            title = (fields.get("title") or "").strip() or path.stem
            tag_ids = [
                tags_by_pk[pk] for pk in fields.get("tags", []) if pk in tags_by_pk
            ]
            try:
                # Re-queried per document so the objects are always live,
                # whatever a rollback did to the identity map in between.
                doc_tags = (
                    (
                        await session.execute(select(Tag).where(Tag.id.in_(tag_ids)))
                    ).scalars().all()
                    if tag_ids
                    else []
                )
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
                corr_id = corr_by_pk.get(fields.get("correspondent"))
                if corr_id:
                    doc.correspondent_id = corr_id
                type_id = type_by_pk.get(fields.get("document_type"))
                if type_id:
                    doc.doc_type_id = type_id
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


def _parse_day(value) -> date | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


async def _restore_definitions(session, tenant_id, manifest: dict) -> dict:
    """Tags (colour, parent) and custom field definitions from a version-2
    manifest. Returns field name → id. Existing rows keep what they have;
    only gaps are filled, so a restore into a live library never rewires
    someone's tag tree or recolours it."""
    field_ids: dict = {}
    for spec in manifest.get("custom_fields") or []:
        name = (spec.get("name") or "").strip() if isinstance(spec, dict) else ""
        if not name:
            continue
        row = (
            await session.execute(
                select(CustomField).where(
                    CustomField.tenant_id == tenant_id, CustomField.name == name
                )
            )
        ).scalars().first()
        if row is None:
            row = CustomField(
                tenant_id=tenant_id, name=name, kind=str(spec.get("kind") or "text")[:16]
            )
            session.add(row)
            await session.flush()
        field_ids[name] = row.id

    specs = [t for t in manifest.get("tags") or [] if isinstance(t, dict) and t.get("name")]
    tag_ids: dict = {}
    for spec in specs:
        row = (
            await session.execute(
                select(Tag).where(Tag.tenant_id == tenant_id, Tag.name == spec["name"])
            )
        ).scalars().first()
        if row is None:
            row = Tag(tenant_id=tenant_id, name=spec["name"])
            session.add(row)
            await session.flush()
        if spec.get("color") and not row.color:
            row.color = str(spec["color"])[:16]
        tag_ids[spec["name"]] = row.id
    # Parents in a second pass, once every name has an id.
    for spec in specs:
        parent = spec.get("parent")
        if not parent or parent not in tag_ids or parent == spec["name"]:
            continue
        row = await session.get(Tag, tag_ids[spec["name"]])
        if row is not None and row.parent_id is None:
            row.parent_id = tag_ids[parent]
    await session.flush()
    return field_ids


async def _restore_extras(session, tenant_id, doc, entry: dict, field_ids: dict) -> None:
    """The per-document metadata a version-2 manifest adds: expiry, the
    original creation time, custom values by field name, annotations and
    reading positions."""
    expires = _parse_day(entry.get("expires_on"))
    if expires:
        doc.expires_on = expires
    created_at = _parse_date(entry.get("created_at"))
    if created_at:
        # The server default stamped "now" at ingest; the original is the
        # date that is meaningful in a restored library.
        doc.created_at = created_at

    values = entry.get("custom_values") or {}
    if isinstance(values, dict):
        for name, value in values.items():
            if value is None:
                continue
            field_id = field_ids.get(name)
            if field_id is None:
                # A value whose definition the manifest lacks: a text field
                # keeps the value visible rather than dropping it. Looked up
                # rather than cached — this document's transaction may still
                # roll back, taking a freshly created definition with it.
                row = (
                    await session.execute(
                        select(CustomField).where(
                            CustomField.tenant_id == tenant_id,
                            CustomField.name == str(name)[:255],
                        )
                    )
                ).scalars().first()
                if row is None:
                    row = CustomField(tenant_id=tenant_id, name=str(name)[:255], kind="text")
                    session.add(row)
                    await session.flush()
                field_id = row.id
            await session.execute(
                document_custom_values.insert().values(
                    document_id=doc.id, field_id=field_id, value=str(value)
                )
            )

    for item in entry.get("annotations") or []:
        if not isinstance(item, dict) or item.get("quote") is None:
            continue
        rects = item.get("rects")
        session.add(
            Annotation(
                tenant_id=tenant_id,
                document_id=doc.id,
                page=int(item.get("page") or 1),
                quote=str(item["quote"]),
                note=item.get("note"),
                rects=rects if isinstance(rects, str) else json.dumps(rects or []),
                color=item.get("color"),
            )
        )

    positions = entry.get("reading_positions") or []
    if positions:
        users = {
            u.email: u.id
            for u in (
                await session.execute(select(User).where(User.tenant_id == tenant_id))
            ).scalars()
        }
        # The importing account is the fallback when the exported user does
        # not exist here: only the owner can run a restore.
        owner_id = (
            await session.execute(
                select(User.id)
                .where(User.tenant_id == tenant_id, User.is_admin.is_(True))
                .order_by(User.created_at)
                .limit(1)
            )
        ).scalar_one_or_none()
        seen: set = set()
        for item in positions:
            if not isinstance(item, dict) or item.get("page") is None:
                continue
            user_id = users.get(item.get("user")) or owner_id
            if user_id is None or user_id in seen:
                continue
            seen.add(user_id)
            session.add(
                ReadingPosition(user_id=user_id, document_id=doc.id, page=int(item["page"]))
            )


async def _restore_scrinium(tenant_id, source: Path, manifest: dict) -> None:
    """Restore from our own export: originals re-ingested with their
    titles, dates, tags, correspondents, types, and notes. Text is pulled
    from the exported searchable copy when present, so restored documents
    are searchable immediately without re-OCR (archives themselves can be
    rebuilt later with Re-OCR). Duplicates skip — re-running is safe.

    A version-2 manifest (see export.MANIFEST_VERSION) also carries tag
    colours and parents, custom field definitions and values, expiry
    dates, original creation times, annotations and reading positions;
    all of it is restored. A manifest without a version key is the older
    shape and restores exactly as it always did."""
    import subprocess

    from app.models import Correspondent, DocType, Document
    from sqlalchemy import select as _select

    docs = manifest.get("documents", [])
    total = len(docs)
    imported = skipped = failed = 0
    version = manifest.get("version") or 1
    await _status("running", done=0, total=total, note="restoring Scrinium export")

    async with SessionLocal() as session:
        async def get_or_create(model, name):
            row = (
                await session.execute(
                    _select(model).where(
                        model.tenant_id == tenant_id, model.name == name
                    )
                )
            ).scalars().first()
            if row is None:
                row = model(tenant_id=tenant_id, name=name)
                session.add(row)
                await session.flush()
            return row

        field_ids: dict = {}
        if version >= 2:
            field_ids = await _restore_definitions(session, tenant_id, manifest)
            await session.commit()

        for i, entry in enumerate(docs):
            rel = entry.get("original")
            path = _safe_member(source, rel)
            if path is None or not path.exists():
                failed += 1
                continue
            try:
                tag_objs = []
                for tag_name in entry.get("tags") or []:
                    from app.models import Tag as _Tag

                    tag_objs.append(await get_or_create(_Tag, tag_name))

                text = None
                pages = entry.get("page_count")
                archive_path = _safe_member(source, entry.get("archive"))
                if archive_path and archive_path.exists():
                    # In a thread: this is a subprocess with a five-minute
                    # timeout, run once per document, inside the API process.
                    # Inline it froze every request for as long as each
                    # pdftotext took, for the whole length of a restore.
                    result = await asyncio.to_thread(
                        subprocess.run,
                        ["pdftotext", "-layout", str(archive_path), "-"],
                        capture_output=True, encoding="utf-8", errors="replace",
                        timeout=300,
                    )
                    if result.returncode == 0 and result.stdout.strip():
                        text = result.stdout

                title = entry.get("title") or path.stem
                doc = await intake.ingest_file(
                    session, tenant_id, path,
                    entry.get("original_filename") or path.name,
                    ocr_text=text,
                    ocr_engine=entry.get("ocr_engine") if text else None,
                    page_count=pages if text else None,
                    tags=tag_objs or None,
                )
                doc.title = title
                if entry.get("doc_date"):
                    doc.doc_date = datetime.fromisoformat(entry["doc_date"]).date()
                if entry.get("notes"):
                    doc.notes = entry["notes"]
                if entry.get("correspondent"):
                    doc.correspondent_id = (
                        await get_or_create(Correspondent, entry["correspondent"])
                    ).id
                if entry.get("doc_type"):
                    doc.doc_type_id = (
                        await get_or_create(DocType, entry["doc_type"])
                    ).id
                if version >= 2:
                    await _restore_extras(session, tenant_id, doc, entry, field_ids)
                await session.commit()
                imported += 1
            except intake.DuplicateDocument:
                await session.rollback()
                skipped += 1
            except Exception:
                await session.rollback()
                failed += 1
                logger.exception("restore: %s failed", rel)
            if (i + 1) % 10 == 0 or i + 1 == total:
                await _status(
                    "running", done=i + 1, total=total,
                    imported=imported, skipped=skipped, failed=failed,
                )

    await _status(
        "done", total=total, imported=imported, skipped=skipped, failed=failed
    )
    logger.info(
        "scrinium restore finished: %d imported, %d skipped, %d failed",
        imported, skipped, failed,
    )
