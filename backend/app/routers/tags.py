import uuid

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import func, select

from app.deps import DB, CurrentUser
from app.models import Tag
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
            select(Tag, func.count(document_tags.c.document_id))
            .outerjoin(document_tags, document_tags.c.tag_id == Tag.id)
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
    if body.parent_id is not None:
        await _get_owned(body.parent_id, user, db)
    tag = Tag(
        tenant_id=user.tenant_id,
        name=body.name,
        parent_id=body.parent_id,
        color=body.color,
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


def _hsl_to_hex(h: float, s: float, l: float) -> str:
    import colorsys

    r, g, b = colorsys.hls_to_rgb((h % 360) / 360, l / 100, s / 100)
    return "#{:02x}{:02x}{:02x}".format(int(r * 255), int(g * 255), int(b * 255))


@router.post("/auto-color")
async def auto_color_tags(user: CurrentUser, db: DB) -> dict:
    """Assign the whole tree a coherent palette: root tags get distinct
    hues (golden-angle spacing, so any count stays well separated) and
    every subtag is a lighter shade of its root's hue. Manual edits
    afterwards stick — this only runs when asked."""
    tags = (
        (
            await db.execute(
                select(Tag).where(Tag.tenant_id == user.tenant_id).order_by(Tag.name)
            )
        )
        .scalars()
        .all()
    )
    by_parent: dict = {}
    for tag in tags:
        by_parent.setdefault(tag.parent_id, []).append(tag)

    colored = 0

    def walk(nodes, hue, depth, start_light):
        nonlocal colored
        for i, node in enumerate(nodes):
            if depth == 0:
                node_hue = (i * 137.508) % 360
                light = 42.0
            else:
                node_hue = hue
                # Children: same hue, progressively lighter; siblings vary
                # slightly so adjacent chips stay tellable-apart.
                light = min(start_light + depth * 9 + (i % 4) * 4, 78.0)
            node.color = _hsl_to_hex(node_hue, 58, light)
            colored += 1
            walk(by_parent.get(node.id, []), node_hue, depth + 1, 42.0)

    walk(by_parent.get(None, []), 0.0, 0, 42.0)
    await db.flush()
    return {"colored": colored}


@router.delete("/unused")
async def delete_unused_tags(user: CurrentUser, db: DB) -> dict:
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
async def delete_tag(tag_id: uuid.UUID, user: CurrentUser, db: DB) -> None:
    """Delete a tag; its children are promoted to the root (parent SET NULL)."""
    tag = await _get_owned(tag_id, user, db)
    await db.delete(tag)
