"""Import (Paperless-ngx) and full-library export. Both run as background
tasks in the API process; progress is polled from app_settings."""

import asyncio
import json

from fastapi import APIRouter, HTTPException, status

from app.deps import DB, AdminUser
from app.services.app_state import get_value
from app.services.export import EXPORT_STATUS, run_export
from app.services.paperless_import import (
    IMPORT_STATUS,
    find_export,
    import_root,
    run_import,
)

router = APIRouter(tags=["transfer"])


async def _current(db, key) -> dict:
    raw = await get_value(db, key)
    try:
        return json.loads(raw) if raw else {}
    except ValueError:
        return {}


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


# asyncio holds only weak refs to tasks — retain them so a background
# import/export can't be garbage-collected mid-run, and surface crashes.
_BACKGROUND_TASKS: set = set()


def _spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)


@router.post("/import/paperless")
async def start_import(user: AdminUser, db: DB) -> dict:
    state = await _current(db, IMPORT_STATUS)
    if state.get("state") == "running":
        raise HTTPException(status.HTTP_409_CONFLICT, "An import is already running")
    if find_export(import_root()) is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"No Paperless export found in {import_root()} — copy the export "
            "folder (or zip) there first",
        )
    _spawn(run_import(user.tenant_id))
    return {"started": True}


@router.get("/export")
async def export_status(user: AdminUser, db: DB) -> dict:
    return {"status": await _current(db, EXPORT_STATUS)}


@router.post("/export")
async def start_export(user: AdminUser, db: DB, body: dict | None = None) -> dict:
    state = await _current(db, EXPORT_STATUS)
    if state.get("state") == "running":
        raise HTTPException(status.HTTP_409_CONFLICT, "An export is already running")
    body = body or {}
    fmt = body.get("format") or None
    if fmt not in (None, "folder", "zip"):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "format: folder|zip")
    part_gb = body.get("part_gb")
    if part_gb is not None and not (1 <= int(part_gb) <= 500):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "part_gb: 1-500")
    _spawn(run_export(user.tenant_id, fmt, int(part_gb) if part_gb else None))
    return {"started": True}
