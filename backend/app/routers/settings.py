from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, HTTPException, status

from app.config import settings
from app.deps import DB, CurrentUser
from app.models import AppSetting
from app.services.app_state import (
    ARCHIVE_MAX_DPI,
    OCR_ENGINE_OVERRIDE,
    get_value,
    resolve_archive_dpi,
    set_value,
)

router = APIRouter(prefix="/settings", tags=["settings"])

HELPER_LABEL = "com.example.scrinium-ocr-helper"
HELPER_BIN = "/usr/local/bin/scrinium-ocr-helper"

PLIST_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{label}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{binary}</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>OCR_HELPER_PORT</key>
        <string>{port}</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
</dict>
</plist>
"""


def _helper_port() -> int:
    if settings.apple_ocr_url:
        parsed = urlparse(settings.apple_ocr_url)
        if parsed.port:
            return parsed.port
    return 9876


@router.get("/mail")
async def mail_settings(user: CurrentUser, db: DB) -> dict:
    status_row = await db.get(AppSetting, "mail_last_result")
    return {
        "configured": settings.mail_enabled(),
        "host": settings.mail_host or None,
        "folder": settings.mail_folder,
        "last_result": status_row.value if status_row else None,
    }


@router.get("/ocr")
async def ocr_settings(user: CurrentUser, db: DB) -> dict:
    sidecar = {"configured": bool(settings.apple_ocr_url), "healthy": None}
    if settings.apple_ocr_url:
        try:
            async with httpx.AsyncClient(timeout=3) as client:
                resp = await client.get(f"{settings.apple_ocr_url}/health")
            sidecar["healthy"] = resp.status_code == 200
        except httpx.HTTPError:
            sidecar["healthy"] = False
    override = await get_value(db, OCR_ENGINE_OVERRIDE)
    return {
        "engine": override or settings.ocr_engine,
        "engine_env": settings.ocr_engine,
        "engine_override": override,
        "languages": settings.ocr_languages,
        "sidecar": sidecar,
    }


@router.post("/ocr")
async def set_ocr_engine(body: dict, user: CurrentUser, db: DB) -> dict:
    """Runtime engine choice; empty string returns to the env default.
    Applies to jobs claimed from now on — no restart needed."""
    engine = (body.get("engine") or "").strip()
    if engine not in ("", "tesseract", "apple"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unknown engine")
    if engine == "apple" and not settings.apple_ocr_url:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "APPLE_OCR_URL is not configured — set it in the stack env first",
        )
    await set_value(db, OCR_ENGINE_OVERRIDE, engine)
    return {"engine": engine or settings.ocr_engine, "engine_override": engine}


@router.get("/archive-dpi")
async def archive_dpi_settings(user: CurrentUser, db: DB) -> dict:
    override = await get_value(db, ARCHIVE_MAX_DPI)
    return {
        "dpi": await resolve_archive_dpi(db),
        "dpi_env": settings.archive_max_dpi,
        "dpi_override": override,
    }


@router.post("/archive-dpi")
async def set_archive_dpi(body: dict, user: CurrentUser, db: DB) -> dict:
    """Runtime cap on archive image DPI. 0 disables downsampling; empty string
    returns to the env default. Applies to OCR and downsample jobs from now on."""
    raw = body.get("dpi")
    if raw in (None, ""):
        await set_value(db, ARCHIVE_MAX_DPI, "")
        return {"dpi": await resolve_archive_dpi(db), "dpi_override": ""}
    try:
        dpi = int(raw)
    except (TypeError, ValueError):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "DPI must be a whole number")
    if dpi < 0 or dpi > 1200:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "DPI must be between 0 and 1200")
    if 0 < dpi < 150:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Below 150 DPI degrades legibility and OCR — use 0 to disable instead",
        )
    await set_value(db, ARCHIVE_MAX_DPI, str(dpi))
    return {"dpi": dpi, "dpi_override": str(dpi)}


@router.get("/health")
async def system_health(user: CurrentUser, db: DB) -> dict:
    """One-glance operational status for the Settings page."""
    import shutil
    from datetime import datetime, timezone

    from sqlalchemy import func, select

    from app.models import Document, DocumentStatus, Job, JobStatus

    backlog = (
        await db.execute(
            select(func.count(Document.id)).where(
                Document.status.in_(
                    [DocumentStatus.PENDING, DocumentStatus.PROCESSING]
                ),
                Document.deleted_at.is_(None),
            )
        )
    ).scalar_one()
    running = (
        await db.execute(
            select(func.count(Job.id)).where(Job.status == JobStatus.RUNNING)
        )
    ).scalar_one()

    last_seen_raw = await get_value(db, "worker_last_seen")
    worker_alive = None
    if last_seen_raw:
        try:
            seen = datetime.fromisoformat(last_seen_raw)
            worker_alive = (
                datetime.now(timezone.utc) - seen
            ).total_seconds() < 120
        except ValueError:
            pass
    if not worker_alive:
        # The pulse can lag during heavy sweeps; a job actively stamping
        # its heartbeat is equally good proof of life.
        from datetime import timedelta as _td

        beating = (
            await db.execute(
                select(func.count(Job.id)).where(
                    Job.status == JobStatus.RUNNING,
                    Job.heartbeat_at
                    >= datetime.now(timezone.utc) - _td(seconds=120),
                )
            )
        ).scalar_one()
        if beating:
            worker_alive = True

    try:
        usage = shutil.disk_usage(settings.data_dir)
        disk = {
            "total_gb": round(usage.total / 1024**3, 1),
            "free_gb": round(usage.free / 1024**3, 1),
        }
    except OSError:
        disk = None

    import json as _json

    from app.models import Blob

    verified, total_blobs = (
        await db.execute(
            select(
                func.count(Blob.id).filter(Blob.verified_at.is_not(None)),
                func.count(Blob.id),
            )
        )
    ).one()
    integrity_raw = await get_value(db, "integrity_status")
    try:
        integrity = _json.loads(integrity_raw) if integrity_raw else {}
    except ValueError:
        integrity = {}

    return {
        "queue": backlog,
        "running": running,
        "worker_alive": worker_alive,
        "worker_last_seen": last_seen_raw or None,
        "disk": disk,
        "integrity": {
            "verified": verified,
            "total": total_blobs,
            "corrupt": integrity.get("corrupt", []),
        },
    }


@router.get("/sidecar-setup")
async def sidecar_setup(user: CurrentUser) -> dict:
    """Everything the guided setup checklist needs, with the port baked in.

    The UI can automate up to the macOS security boundary; the user runs the
    commands themselves (Gatekeeper / launchd registration can't be done
    remotely).
    """
    port = _helper_port()
    plist_path = f"~/Library/LaunchAgents/{HELPER_LABEL}.plist"
    return {
        "port": port,
        "configured": bool(settings.apple_ocr_url),
        "build_commands": (
            "# run from wherever you cloned the repo — e.g. cd ~/development/scrinium\n"
            "cd sidecar\n"
            "swift build -c release\n"
            f"sudo cp .build/release/scrinium-ocr-helper {HELPER_BIN}"
        ),
        "plist": PLIST_TEMPLATE.format(
            label=HELPER_LABEL, binary=HELPER_BIN, port=port
        ),
        "plist_path": plist_path,
        "load_commands": (
            f"mv ~/Downloads/{HELPER_LABEL}.plist ~/Library/LaunchAgents/\n"
            f"launchctl load {plist_path}\n"
            f"sleep 2 && curl http://localhost:{port}/health"
        ),
        "server_env": (
            "OCR_ENGINE=apple\n"
            f"APPLE_OCR_URL=http://host.docker.internal:{port}"
        ),
    }
