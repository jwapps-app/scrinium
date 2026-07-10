"""Push dispatch via the shared push-relay.

One seam — `notify_tenant()` — fans out to every registered device. The
relay holds the .p8 and talks to Apple; this app only ever POSTs
`{bundle_id, device_token, title, body, custom_data, sandbox}` with its
per-app key.

Standards applied (fleet recipe): fire-and-forget with try/except (a push
failure must never break the triggering action), per-token `sandbox` from
the stored environment, self-heal a mislabeled environment by retrying once
with the flag flipped, prune tokens on the full dead-token reason set, log
403 loudly (config error — won't fix itself). No presence-skip: send to
every token, the client suppresses foreground banners.
"""

import logging
import uuid

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import DeviceToken, User

logger = logging.getLogger(__name__)

DEAD_TOKEN_REASONS = {"BadDeviceToken", "Unregistered", "DeviceTokenNotForTopic"}


async def _post_notify(
    client: httpx.AsyncClient, token: str, sandbox: bool,
    title: str, body: str, custom_data: dict[str, str],
) -> httpx.Response:
    return await client.post(
        f"{settings.push_relay_url}/notify",
        headers={"X-API-Key": settings.push_relay_api_key},
        json={
            "bundle_id": settings.apns_bundle_id,
            "device_token": token,
            "title": title,
            "body": body,
            "custom_data": custom_data,
            "sandbox": sandbox,
        },
    )


def _failure_reason(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return ""
    return str(payload.get("reason") or payload.get("detail") or "")


async def _send_one(
    session: AsyncSession, client: httpx.AsyncClient, device: DeviceToken,
    title: str, body: str, custom_data: dict[str, str],
) -> None:
    sandbox = device.environment == "sandbox"
    response = await _post_notify(client, device.token, sandbox, title, body, custom_data)

    if response.status_code == 200:
        return
    if response.status_code == 403:
        logger.error(
            "push-relay rejected our API key for %s — check apps.keys / "
            "PUSH_RELAY_API_KEY (this will not fix itself)",
            settings.apns_bundle_id,
        )
        return

    reason = _failure_reason(response)
    if reason == "BadDeviceToken":
        # Possibly a mislabeled environment: retry flipped, self-heal if it works.
        retry = await _post_notify(
            client, device.token, not sandbox, title, body, custom_data
        )
        if retry.status_code == 200:
            device.environment = "production" if sandbox else "sandbox"
            logger.info(
                "self-healed environment for token …%s -> %s",
                device.token[-8:], device.environment,
            )
            return

    if reason in DEAD_TOKEN_REASONS:
        logger.info("pruning dead token …%s (%s)", device.token[-8:], reason)
        await session.delete(device)
        return

    logger.warning(
        "push to …%s failed: %s %s", device.token[-8:], response.status_code, reason
    )


async def notify_tenant(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    title: str,
    body: str,
    custom_data: dict[str, str],
) -> None:
    """Send an alert push to every device of every user in the tenant.
    Never raises; commits its own token-table changes."""
    if not settings.push_enabled():
        return
    try:
        devices = (
            await session.execute(
                select(DeviceToken)
                .join(User, DeviceToken.user_id == User.id)
                .where(User.tenant_id == tenant_id)
            )
        ).scalars().all()
        if not devices:
            return
        async with httpx.AsyncClient(timeout=10) as client:
            for device in devices:
                try:
                    await _send_one(session, client, device, title, body, custom_data)
                except httpx.HTTPError as exc:
                    logger.warning("push transport error: %s", exc)
        await session.commit()
    except Exception:
        logger.exception("push dispatch failed; continuing")
