import uuid

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import func, select

from app.deps import DB, CurrentUser
from app.models import Tag
from app.models.document import document_tags
from app.schemas import TagCreate, TagOut

router = APIRouter(prefix="/tags", tags=["tags"])


@router.get("", response_model=list[TagOut])
async def list_tags(user: CurrentUser, db: DB) -> list[TagOut]:
    rows = (
        await db.execute(
            select(Tag, func.count(document_tags.c.document_id))
            .outerjoin(document_tags, document_tags.c.tag_id == Tag.id)
            .where(Tag.tenant_id == user.tenant_id)
            .group_by(Tag.id)
            .order_by(Tag.name)
        )
    ).all()
    return [
        TagOut(id=tag.id, name=tag.name, count=count) for tag, count in rows
    ]


@router.post("", response_model=TagOut, status_code=status.HTTP_201_CREATED)
async def create_tag(body: TagCreate, user: CurrentUser, db: DB) -> TagOut:
    existing = (
        await db.execute(
            select(Tag).where(Tag.tenant_id == user.tenant_id, Tag.name == body.name)
        )
    ).scalar_one_or_none()
    if existing is not None:
        return TagOut.model_validate(existing)
    tag = Tag(tenant_id=user.tenant_id, name=body.name)
    db.add(tag)
    await db.flush()
    return TagOut.model_validate(tag)


@router.delete("/{tag_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_tag(tag_id: uuid.UUID, user: CurrentUser, db: DB) -> None:
    tag = await db.get(Tag, tag_id)
    if tag is None or tag.tenant_id != user.tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Tag not found")
    await db.delete(tag)
