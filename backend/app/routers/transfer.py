"""Import (Paperless-ngx) and full-library export. Both run as background
tasks in the API process; progress is polled from app_settings."""

import asyncio
import json
import logging

from fastapi import APIRouter, HTTPException, status

from app.database import SessionLocal
from app.deps import DB, AdminUser
from app.schemas import ExportRequest
from app.services.background import spawn
from app.services.app_state import get_value, set_value
from app.services.export import EXPORT_STATUS, run_export
from app.services.paperless_import import (
    IMPORT_STATUS,
    find_export,
    import_root,
    run_import,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["transfer"])

# The running flag lives in app_settings, but the task it describes lives in
# this process. Two starts in the same instant would both read "not running"
# before either had written "running", so the check-and-claim is done under
# this lock and the claim is committed before the task is spawned.
_start_lock = asyncio.Lock()

INTERRUPTED_MESSAGE = (
    "The server restarted while this was running. Start it again to finish."
)


def _parse(raw: str | None) -> dict:
    try:
        return json.loads(raw) if raw else {}
    except ValueError:
        return {}


async def _current(db, key) -> dict:
    return _parse(await get_value(db, key))


async def _claim(db, key: str, note: str) -> None:
    """Publish "running" now, in its own commit, so a concurrent start sees it
    before the background task has had a chance to write anything."""
    await set_value(db, key, json.dumps({"state": "running", "note": note}))
    await db.commit()


async def reset_interrupted_transfers() -> list[str]:
    """Turn any "running" import/export status into "interrupted".

    Called once at API startup. The tasks those statuses describe ran inside
    the previous process and died with it; without this the flag would stay
    set for good and every later start would be refused as a duplicate.
    Returns the keys it changed.
    """
    changed = []
    async with SessionLocal() as session:
        for key in (IMPORT_STATUS, EXPORT_STATUS):
            state = _parse(await get_value(session, key))
            if state.get("state") != "running":
                continue
            state.update(state="interrupted", error=INTERRUPTED_MESSAGE)
            await set_value(session, key, json.dumps(state))
            changed.append(key)
        await session.commit()
    if changed:
        logger.warning("marked interrupted at startup: %s", ", ".join(changed))
    return changed


@router.get("/import/paperless")
async def import_status(user: AdminUser, db: DB) -> dict:
    state = await _current(db, IMPORT_STATUS)
    root = import_root()
    found = find_export(root)
    return {
        "import_dir": str(root),
        "export_found": str(found) if found else None,
        "status": state,
    }


@router.post("/import/paperless")
async def start_import(user: AdminUser, db: DB) -> dict:
    async with _start_lock:
        state = await _current(db, IMPORT_STATUS)
        if state.get("state") == "running":
            raise HTTPException(status.HTTP_409_CONFLICT, "An import is already running")
        if find_export(import_root()) is None:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"No Paperless export found in {import_root()} — copy the export "
                "folder (or zip) there first",
            )
        await _claim(db, IMPORT_STATUS, "starting…")
        spawn(run_import(user.tenant_id))
    return {"started": True}


@router.get("/export")
async def export_status(user: AdminUser, db: DB) -> dict:
    return {"status": await _current(db, EXPORT_STATUS)}


@router.post("/export")
async def start_export(
    user: AdminUser, db: DB, body: ExportRequest | None = None
) -> dict:
    async with _start_lock:
        state = await _current(db, EXPORT_STATUS)
        if state.get("state") == "running":
            raise HTTPException(status.HTTP_409_CONFLICT, "An export is already running")
        body = body or ExportRequest()
        fmt = body.format or None
        if fmt not in (None, "folder", "zip"):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "format: folder|zip")
        await _claim(db, EXPORT_STATUS, "starting…")
        spawn(run_export(user.tenant_id, fmt, body.part_gb))
    return {"started": True}
