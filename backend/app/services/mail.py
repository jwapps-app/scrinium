"""Email ingestion: poll an IMAP mailbox, consume PDF/image attachments.

Configured entirely by env (MAIL_HOST / MAIL_USERNAME / MAIL_PASSWORD,
optional MAIL_FOLDER, MAIL_POLL_SECONDS). Unseen messages are scanned;
accepted attachments enter the normal intake path with an "Email" tag and
the sender as correspondent (created on first sight); the message is then
marked seen. Duplicates dedup by content hash like every other path.
Fail-soft: one bad message never stops the poll.
"""

import email
import email.header
import email.utils
import imaplib
import logging
import tempfile
from pathlib import Path

from sqlalchemy import select

from app.config import settings
from app.database import SessionLocal
from app.models import AppSetting, Correspondent, Tenant
from app.services import intake, tag_tree

logger = logging.getLogger(__name__)

MAIL_STATUS_KEY = "mail_last_result"


def _decode(value: str | None) -> str:
    if not value:
        return ""
    parts = email.header.decode_header(value)
    out = ""
    for text, enc in parts:
        if isinstance(text, bytes):
            out += text.decode(enc or "utf-8", errors="replace")
        else:
            out += text
    return out


def _fetch_unseen() -> list[dict]:
    """Runs in a thread: returns [{filename, payload, sender_name, subject}]."""
    found: list[dict] = []
    with imaplib.IMAP4_SSL(settings.mail_host, settings.mail_port) as imap:
        imap.login(settings.mail_username, settings.mail_password)
        imap.select(settings.mail_folder)
        _, data = imap.search(None, "UNSEEN")
        uids = data[0].split()
        for uid in uids[:20]:  # cap per poll; the rest next time
            try:
                _, msg_data = imap.fetch(uid, "(RFC822)")
                message = email.message_from_bytes(msg_data[0][1])
                sender_name, sender_addr = email.utils.parseaddr(
                    _decode(message.get("From"))
                )
                subject = _decode(message.get("Subject"))
                for part in message.walk():
                    filename = part.get_filename()
                    if not filename:
                        continue
                    filename = _decode(filename)
                    if Path(filename).suffix.lower() not in intake.ACCEPTED_SUFFIXES:
                        continue
                    payload = part.get_payload(decode=True)
                    if payload:
                        found.append(
                            {
                                "filename": filename,
                                "payload": payload,
                                "sender": sender_name or sender_addr or "Unknown",
                                "subject": subject,
                            }
                        )
                imap.store(uid, "+FLAGS", "\\Seen")
            except Exception:
                logger.exception("failed reading mail uid %s; skipping", uid)
    return found


async def _record_status(session, text: str) -> None:
    row = await session.get(AppSetting, MAIL_STATUS_KEY)
    if row is None:
        session.add(AppSetting(key=MAIL_STATUS_KEY, value=text))
    else:
        row.value = text
    await session.commit()


async def poll_once() -> int:
    """One mailbox poll. Returns how many attachments were ingested."""
    if not settings.mail_enabled():
        return 0
    import asyncio

    ingested = 0
    async with SessionLocal() as session:
        tenant_id = (
            await session.execute(select(Tenant.id).order_by(Tenant.created_at))
        ).scalars().first()
        if tenant_id is None:
            return 0
        try:
            attachments = await asyncio.to_thread(_fetch_unseen)
        except Exception as exc:
            logger.warning("mail poll failed: %s", exc)
            await _record_status(session, f"error: {str(exc)[:200]}")
            return 0

        for item in attachments:
            try:
                with tempfile.NamedTemporaryFile(
                    suffix=Path(item["filename"]).suffix, delete=False
                ) as tmp:
                    tmp.write(item["payload"])
                    tmp_path = Path(tmp.name)
                try:
                    tags = await tag_tree.get_or_create_tag_path(
                        session, tenant_id, ["Email"]
                    )
                    doc = await intake.ingest_file(
                        session, tenant_id, tmp_path, item["filename"], tags=tags
                    )
                    correspondent = (
                        await session.execute(
                            select(Correspondent).where(
                                Correspondent.tenant_id == tenant_id,
                                Correspondent.name == item["sender"],
                            )
                        )
                    ).scalar_one_or_none()
                    if correspondent is None:
                        correspondent = Correspondent(
                            tenant_id=tenant_id, name=item["sender"]
                        )
                        session.add(correspondent)
                        await session.flush()
                    doc.correspondent_id = correspondent.id
                    await session.commit()
                    ingested += 1
                    logger.info(
                        "ingested mail attachment %s from %s",
                        item["filename"],
                        item["sender"],
                    )
                finally:
                    tmp_path.unlink(missing_ok=True)
            except intake.DuplicateDocument:
                await session.rollback()
                logger.info("mail attachment %s is a duplicate", item["filename"])
            except Exception:
                await session.rollback()
                logger.exception("failed ingesting mail attachment")
        await _record_status(session, "ok")
    return ingested
