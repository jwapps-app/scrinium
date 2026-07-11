from urllib.parse import urlparse

import httpx
from fastapi import APIRouter

from app.config import settings
from app.deps import DB, CurrentUser
from app.models import AppSetting

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
async def ocr_settings(user: CurrentUser) -> dict:
    sidecar = {"configured": bool(settings.apple_ocr_url), "healthy": None}
    if settings.apple_ocr_url:
        try:
            async with httpx.AsyncClient(timeout=3) as client:
                resp = await client.get(f"{settings.apple_ocr_url}/health")
            sidecar["healthy"] = resp.status_code == 200
        except httpx.HTTPError:
            sidecar["healthy"] = False
    return {
        "engine": settings.ocr_engine,
        "languages": settings.ocr_languages,
        "sidecar": sidecar,
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
