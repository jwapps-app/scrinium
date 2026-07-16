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


def _fetch_unseen() -> tuple[list[dict], list[bytes]]:
    """Runs in a thread. Returns (attachments, scanned_uids). Messages are NOT
    marked Seen here — that happens only after their attachments actually
    ingest, so a failed ingestion is retried on the next poll instead of the
    attachment being silently lost."""
    found: list[dict] = []
    scanned: list[bytes] = []
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
                                "uid": uid,
                                "filename": filename,
                                "payload": payload,
                                "sender": sender_name or sender_addr or "Unknown",
                                "subject": subject,
                            }
                        )
                scanned.append(uid)
            except Exception:
                logger.exception("failed reading mail uid %s; skipping", uid)
    return found, scanned


def _mark_seen(uids: list[bytes]) -> None:
    """Runs in a thread: flag fully-processed messages so they leave the
    unseen set."""
    if not uids:
        return
    with imaplib.IMAP4_SSL(settings.mail_host, settings.mail_port) as imap:
        imap.login(settings.mail_username, settings.mail_password)
        imap.select(settings.mail_folder)
        for uid in uids:
            imap.store(uid, "+FLAGS", "\\Seen")


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
            attachments, scanned = await asyncio.to_thread(_fetch_unseen)
        except Exception as exc:
            logger.warning("mail poll failed: %s", exc)
            await _record_status(session, f"error: {str(exc)[:200]}")
            return 0

        failed_uids: set[bytes] = set()
        for item in attachments:
            # Bind the temp path before writing so a failed write can still
            # be cleaned up (the old ordering leaked the file on error).
            tmp = tempfile.NamedTemporaryFile(
                suffix=Path(item["filename"]).suffix, delete=False
            )
            tmp_path = Path(tmp.name)
            try:
                try:
                    tmp.write(item["payload"])
                finally:
                    tmp.close()
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
            except intake.DuplicateDocument:
                await session.rollback()
                logger.info("mail attachment %s is a duplicate", item["filename"])
            except Exception:
                await session.rollback()
                failed_uids.add(item["uid"])
                logger.exception("failed ingesting mail attachment")
            finally:
                tmp_path.unlink(missing_ok=True)

        # Only fully-processed messages leave the unseen set; a message whose
        # attachment failed for a real reason is retried next poll.
        to_mark = [uid for uid in scanned if uid not in failed_uids]
        try:
            await asyncio.to_thread(_mark_seen, to_mark)
        except Exception:
            logger.exception("failed marking mail seen; will re-scan next poll")
        await _record_status(session, "ok")
    return ingested
