"""Share links: time-limited public access to a single document.

The owner mints a token; anyone holding the URL can view metadata and
download the file for that one document until the link expires or is
revoked. The public endpoints authenticate by token alone — they must
never leak anything beyond the shared document.
"""

import secrets
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import FileResponse
from sqlalchemy import select

from app.deps import DB, CurrentUser
from app.models import Blob, Document, ShareLink
from app.schemas import ShareLinkCreate, ShareLinkOut
from app.services import storage

router = APIRouter(tags=["share"])


async def _owned_doc(doc_id: uuid.UUID, user, db) -> Document:
    doc = await db.get(Document, doc_id)
    if doc is None or doc.tenant_id != user.tenant_id or doc.deleted_at is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Document not found")
    return doc


def _link_out(link: ShareLink) -> ShareLinkOut:
    return ShareLinkOut(
        id=link.id,
        token=link.token,
        url_path=f"/share/{link.token}",
        expires_at=link.expires_at,
        created_at=link.created_at,
    )


@router.post("/documents/{doc_id}/share", response_model=ShareLinkOut)
async def create_share_link(
    doc_id: uuid.UUID, body: ShareLinkCreate, user: CurrentUser, db: DB
) -> ShareLinkOut:
    doc = await _owned_doc(doc_id, user, db)
    expires = (
        datetime.now(timezone.utc) + timedelta(days=body.days)
        if body.days > 0
        else None
    )
    link = ShareLink(
        token=secrets.token_urlsafe(24), document_id=doc.id, expires_at=expires
    )
    db.add(link)
    await db.flush()
    await db.refresh(link)
    return _link_out(link)


@router.get("/documents/{doc_id}/share", response_model=list[ShareLinkOut])
async def list_share_links(
    doc_id: uuid.UUID, user: CurrentUser, db: DB
) -> list[ShareLinkOut]:
    doc = await _owned_doc(doc_id, user, db)
    links = (
        (
            await db.execute(
                select(ShareLink)
                .where(ShareLink.document_id == doc.id)
                .order_by(ShareLink.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    now = datetime.now(timezone.utc)
    return [
        _link_out(l) for l in links if l.expires_at is None or l.expires_at > now
    ]


@router.delete("/documents/{doc_id}/share", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_share_links(doc_id: uuid.UUID, user: CurrentUser, db: DB) -> None:
    """Revoke every share link for the document."""
    doc = await _owned_doc(doc_id, user, db)
    links = (
        (await db.execute(select(ShareLink).where(ShareLink.document_id == doc.id)))
        .scalars()
        .all()
    )
    for link in links:
        await db.delete(link)
    await db.flush()


async def _shared_doc(token: str, db) -> Document:
    link = (
        (await db.execute(select(ShareLink).where(ShareLink.token == token)))
        .scalars()
        .first()
    )
    if link is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Link not found or expired")
    if link.expires_at is not None and link.expires_at < datetime.now(timezone.utc):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Link not found or expired")
    doc = await db.get(Document, link.document_id)
    if doc is None or doc.deleted_at is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Link not found or expired")
    return doc


@router.get("/share/{token}")
async def shared_document(token: str, db: DB) -> dict:
    """Public: minimal metadata for the shared document only."""
    doc = await _shared_doc(token, db)
    return {
        "title": doc.title,
        "page_count": doc.page_count,
        "has_archive": doc.archive_blob_id is not None,
    }


@router.get("/share/{token}/file")
async def shared_file(
    token: str, db: DB, disposition: str = "inline"
) -> FileResponse:
    """Public: the shared document's file (archive if available)."""
    doc = await _shared_doc(token, db)
    if doc.archive_blob_id is not None:
        blob_id = doc.archive_blob_id
        filename = f"{doc.title}.pdf"
        media_type = "application/pdf"
    else:
        blob_id = doc.original_blob_id
        filename = doc.original_filename
        blob = await db.get(Blob, blob_id)
        media_type = blob.mime_type if blob else "application/octet-stream"
    path = storage.blob_file(blob_id)
    if not path.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "File missing")
    dispo = "attachment" if disposition == "attachment" else "inline"
    return FileResponse(
        path, media_type=media_type, filename=filename, content_disposition_type=dispo
    )
