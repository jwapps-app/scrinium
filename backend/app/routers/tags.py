import uuid

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import func, select

from app.deps import DB, AdminUser, CurrentUser
from app.models import Document, Tag
from app.models.document import document_tags
from app.schemas import TagCreate, TagOut, TagUpdate
from app.services.tag_tree import is_descendant

router = APIRouter(prefix="/tags", tags=["tags"])


def _tag_out(tag: Tag, count: int = 0) -> TagOut:
    return TagOut(
        id=tag.id, name=tag.name, parent_id=tag.parent_id, color=tag.color, count=count
    )


async def _get_owned(tag_id: uuid.UUID, user, db) -> Tag:
    tag = await db.get(Tag, tag_id)
    if tag is None or tag.tenant_id != user.tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Tag not found")
    return tag


@router.get("", response_model=list[TagOut])
async def list_tags(user: CurrentUser, db: DB) -> list[TagOut]:
    rows = (
        await db.execute(
            # Live documents only: correspondents and types already count
            # that way, and a tag reading "3" over an empty filtered list
            # was the trash showing through.
            select(Tag, func.count(Document.id))
            .outerjoin(document_tags, document_tags.c.tag_id == Tag.id)
            .outerjoin(
                Document,
                (Document.id == document_tags.c.document_id)
                & Document.deleted_at.is_(None),
            )
            .where(Tag.tenant_id == user.tenant_id)
            .group_by(Tag.id)
            .order_by(Tag.name)
        )
    ).all()
    return [_tag_out(tag, count) for tag, count in rows]


@router.post("", response_model=TagOut, status_code=status.HTTP_201_CREATED)
async def create_tag(body: TagCreate, user: CurrentUser, db: DB) -> TagOut:
    existing = (
        await db.execute(
            select(Tag).where(Tag.tenant_id == user.tenant_id, Tag.name == body.name)
        )
    ).scalar_one_or_none()
    if existing is not None:
        return _tag_out(existing)
    parent = None
    if body.parent_id is not None:
        parent = await _get_owned(body.parent_id, user, db)
    tag = Tag(
        tenant_id=user.tenant_id,
        name=body.name,
        parent_id=body.parent_id,
        color=body.color or await _auto_color(user, db, parent),
    )
    db.add(tag)
    await db.flush()
    return _tag_out(tag)


@router.patch("/{tag_id}", response_model=TagOut)
async def update_tag(
    tag_id: uuid.UUID, body: TagUpdate, user: CurrentUser, db: DB
) -> TagOut:
    tag = await _get_owned(tag_id, user, db)
    if body.name is not None and body.name != tag.name:
        clash = (
            await db.execute(
                select(Tag).where(
                    Tag.tenant_id == user.tenant_id,
                    Tag.name == body.name,
                    Tag.id != tag.id,
                )
            )
        ).scalars().first()
        if clash is not None:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"A tag named “{body.name}” already exists",
            )
        tag.name = body.name
    if body.clear_parent:
        tag.parent_id = None
    elif body.parent_id is not None:
        if body.parent_id == tag.id or await is_descendant(
            db, body.parent_id, tag.id
        ):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "That would make the tag its own ancestor",
            )
        await _get_owned(body.parent_id, user, db)
        tag.parent_id = body.parent_id
    if body.clear_color:
        tag.color = None
    elif body.color is not None:
        tag.color = body.color
    await db.flush()
    return _tag_out(tag)


async def _auto_color(user, db, parent) -> str:
    """A colour for a tag created without one.

    The UI never sends a colour, so tags were landing with none at all and
    stayed grey until someone ran the whole-tree recolour by hand — which was
    the only thing that assigned colours, and it overwrites every manual
    choice on the way past. A new tag gets one immediately instead: a shade of
    its parent's hue, or, for a root, the emptiest part of the wheel.
    """
    from app.services import palette

    if parent is not None:
        parent_hsl = palette.hex_to_hsl(parent.color or "")
        if parent_hsl is not None:
            # The colours in use, not how many there are: a count cannot avoid
            # a hue a sibling already holds.
            sibling_colors = (
                (
                    await db.execute(
                        select(Tag.color).where(
                            Tag.tenant_id == user.tenant_id,
                            Tag.parent_id == parent.id,
                            Tag.color.is_not(None),
                        )
                    )
                )
                .scalars()
                .all()
            )
            hues = [
                hsl[0]
                for hsl in (palette.hex_to_hsl(c) for c in sibling_colors)
                if hsl
            ]
            return palette.hsl_to_hex(*palette.child_hsl(parent_hsl, hues))

    # A root, or a parent that has no colour of its own to derive from.
    used = (
        (
            await db.execute(
                select(Tag.color).where(
                    Tag.tenant_id == user.tenant_id, Tag.color.is_not(None)
                )
            )
        )
        .scalars()
        .all()
    )
    hues = [hsl[0] for hsl in (palette.hex_to_hsl(c) for c in used) if hsl]
    return palette.hsl_to_hex(
        palette.next_root_hue(hues), palette.ROOT_SAT, palette.ROOT_LIGHT
    )


def _hsl_to_hex(h: float, s: float, l: float) -> str:
    from app.services.palette import hsl_to_hex

    return hsl_to_hex(h, s, l)


@router.post("/auto-color")
async def auto_color_tags(user: CurrentUser, db: DB) -> dict:
    """Palette for the whole tree — see services/palette.py. Manual edits
    afterwards stick; this only runs when asked."""
    from app.services.palette import assign_palette

    tags = (
        (
            await db.execute(select(Tag).where(Tag.tenant_id == user.tenant_id))
        )
        .scalars()
        .all()
    )
    palette = assign_palette([(t.id, t.parent_id, t.name.lower()) for t in tags])
    for tag in tags:
        h, sat, light = palette[tag.id]
        tag.color = _hsl_to_hex(h, sat, light)
    await db.flush()
    return {"colored": len(tags)}


@router.delete("/unused")
async def delete_unused_tags(user: AdminUser, db: DB) -> dict:
    """Delete every tag with zero documents. Runs repeatedly so emptied
    parent chains collapse bottom-up. Returns how many were removed."""
    removed = 0
    while True:
        unused = (
            await db.execute(
                select(Tag)
                .outerjoin(document_tags, document_tags.c.tag_id == Tag.id)
                .where(Tag.tenant_id == user.tenant_id)
                .group_by(Tag.id)
                .having(func.count(document_tags.c.document_id) == 0)
            )
        ).scalars().all()
        # Only leaves: a tag with children sticks around until they're gone.
        child_parents = {
            row[0]
            for row in (
                await db.execute(
                    select(Tag.parent_id).where(
                        Tag.tenant_id == user.tenant_id, Tag.parent_id.is_not(None)
                    )
                )
            ).all()
        }
        leaves = [t for t in unused if t.id not in child_parents]
        if not leaves:
            break
        for tag in leaves:
            await db.delete(tag)
        removed += len(leaves)
        await db.flush()
    return {"removed": removed}


@router.delete("/{tag_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_tag(tag_id: uuid.UUID, user: AdminUser, db: DB) -> None:
    """Delete a tag; its children are promoted to the root (parent SET NULL)."""
    tag = await _get_owned(tag_id, user, db)
    await db.delete(tag)
