"""Correspondents, document types, saved views, and custom fields."""

import uuid

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import func, select

from app.deps import DB, AdminUser, CurrentUser
from app.models import Correspondent, CustomField, DocType, Document, SavedView
from app.schemas import (
    CustomFieldCreate,
    CustomFieldOut,
    NamedEntityCreate,
    NamedEntityOut,
    SavedViewCreate,
    SavedViewOut,
)

router = APIRouter(tags=["organize"])


def _entity_routes(prefix: str, model, count_column):
    sub = APIRouter(prefix=prefix)

    @sub.get("", response_model=list[NamedEntityOut])
    async def list_entities(user: CurrentUser, db: DB):
        rows = (
            await db.execute(
                select(model, func.count(Document.id))
                .outerjoin(
                    Document,
                    (count_column == model.id) & Document.deleted_at.is_(None),
                )
                .where(model.tenant_id == user.tenant_id)
                .group_by(model.id)
                .order_by(model.name)
            )
        ).all()
        return [
            NamedEntityOut(id=e.id, name=e.name, count=count) for e, count in rows
        ]

    @sub.post("", response_model=NamedEntityOut, status_code=status.HTTP_201_CREATED)
    async def create_entity(body: NamedEntityCreate, user: CurrentUser, db: DB):
        existing = (
            await db.execute(
                select(model).where(
                    model.tenant_id == user.tenant_id, model.name == body.name
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            return NamedEntityOut(id=existing.id, name=existing.name)
        entity = model(tenant_id=user.tenant_id, name=body.name)
        db.add(entity)
        await db.flush()
        return NamedEntityOut(id=entity.id, name=entity.name)

    @sub.patch("/{entity_id}", response_model=NamedEntityOut)
    async def rename_entity(
        entity_id: uuid.UUID, body: NamedEntityCreate, user: CurrentUser, db: DB
    ):
        entity = await db.get(model, entity_id)
        if entity is None or entity.tenant_id != user.tenant_id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
        entity.name = body.name
        await db.flush()
        return NamedEntityOut(id=entity.id, name=entity.name)

    @sub.delete("/{entity_id}", status_code=status.HTTP_204_NO_CONTENT)
    async def delete_entity(entity_id: uuid.UUID, user: AdminUser, db: DB):
        entity = await db.get(model, entity_id)
        if entity is None or entity.tenant_id != user.tenant_id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
        await db.delete(entity)  # documents keep working: FK is SET NULL

    return sub


router.include_router(
    _entity_routes("/correspondents", Correspondent, Document.correspondent_id)
)
router.include_router(_entity_routes("/doc-types", DocType, Document.doc_type_id))


@router.get("/views", response_model=list[SavedViewOut])
async def list_views(user: CurrentUser, db: DB):
    views = (
        await db.execute(
            select(SavedView)
            .where(SavedView.tenant_id == user.tenant_id)
            .order_by(SavedView.name)
        )
    ).scalars().all()
    return [SavedViewOut.model_validate(v) for v in views]


@router.post("/views", response_model=SavedViewOut, status_code=status.HTTP_201_CREATED)
async def create_view(body: SavedViewCreate, user: CurrentUser, db: DB):
    view = SavedView(tenant_id=user.tenant_id, name=body.name, params=body.params)
    db.add(view)
    await db.flush()
    return SavedViewOut.model_validate(view)


@router.delete("/views/{view_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_view(view_id: uuid.UUID, user: CurrentUser, db: DB):
    view = await db.get(SavedView, view_id)
    if view is None or view.tenant_id != user.tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    await db.delete(view)


@router.get("/custom-fields", response_model=list[CustomFieldOut])
async def list_fields(user: CurrentUser, db: DB):
    fields = (
        await db.execute(
            select(CustomField)
            .where(CustomField.tenant_id == user.tenant_id)
            .order_by(CustomField.name)
        )
    ).scalars().all()
    return [CustomFieldOut.model_validate(f) for f in fields]


@router.post(
    "/custom-fields", response_model=CustomFieldOut, status_code=status.HTTP_201_CREATED
)
async def create_field(body: CustomFieldCreate, user: CurrentUser, db: DB):
    existing = (
        await db.execute(
            select(CustomField).where(
                CustomField.tenant_id == user.tenant_id,
                CustomField.name == body.name,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return CustomFieldOut.model_validate(existing)
    field = CustomField(tenant_id=user.tenant_id, name=body.name, kind=body.kind)
    db.add(field)
    await db.flush()
    return CustomFieldOut.model_validate(field)


@router.delete("/custom-fields/{field_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_field(field_id: uuid.UUID, user: AdminUser, db: DB):
    field = await db.get(CustomField, field_id)
    if field is None or field.tenant_id != user.tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    await db.delete(field)  # values cascade
