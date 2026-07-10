"""Tag hierarchy helpers.

A tag may have a parent; applying a tag to a document also applies every
ancestor (Paperless semantics), so filters and counts need no recursive
queries — the chain is materialized on the document.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Tag


async def get_or_create_tag_path(
    session: AsyncSession, tenant_id: uuid.UUID, names: list[str]
) -> list[Tag]:
    """Resolve an ordered folder-style path of tag names to Tag rows.

    Newly created tags are parented to the previous path component, so
    dropping `Construction/Architecture/x.pdf` builds that hierarchy.
    Existing tags keep whatever parent they already have — a drop never
    silently rewires the tree.
    """
    tags: list[Tag] = []
    parent: Tag | None = None
    for name in names:
        tag = (
            await session.execute(
                select(Tag).where(Tag.tenant_id == tenant_id, Tag.name == name)
            )
        ).scalar_one_or_none()
        if tag is None:
            tag = Tag(
                tenant_id=tenant_id,
                name=name,
                parent_id=parent.id if parent else None,
            )
            session.add(tag)
            await session.flush()
        tags.append(tag)
        parent = tag
    return tags


async def with_ancestors(session: AsyncSession, tags: list[Tag]) -> list[Tag]:
    """Expand a tag list with every ancestor. Cycle-safe, deduplicated."""
    seen: dict[uuid.UUID, Tag] = {}
    for tag in tags:
        current: Tag | None = tag
        while current is not None and current.id not in seen:
            seen[current.id] = current
            if current.parent_id is None:
                break
            current = await session.get(Tag, current.parent_id)
    return list(seen.values())


async def is_descendant(
    session: AsyncSession, tag_id: uuid.UUID, candidate_ancestor_id: uuid.UUID
) -> bool:
    """True if candidate_ancestor_id appears in tag_id's ancestor chain —
    used to refuse re-parenting that would create a cycle."""
    current_id: uuid.UUID | None = tag_id
    hops = 0
    while current_id is not None and hops < 100:
        if current_id == candidate_ancestor_id:
            return True
        tag = await session.get(Tag, current_id)
        current_id = tag.parent_id if tag else None
        hops += 1
    return False
