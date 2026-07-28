import re
import uuid

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select

from app.deps import DB, CurrentUser
from app.models import Correspondent, DocType, Rule, Tag
from app.schemas import RuleCreate, RuleOut, RuleUpdate

router = APIRouter(prefix="/rules", tags=["rules"])


def _validate_pattern(match_type: str, pattern: str) -> None:
    if match_type == "regex":
        try:
            re.compile(pattern)
        except re.error as exc:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY, f"Invalid regex: {exc}"
            )


async def _validate_targets(updates: dict, user: CurrentUser, db: DB) -> None:
    """Every id a rule assigns must belong to the caller's tenant.

    Without this a rule could reference another tenant's tag, and applying it
    disclosed that tag's name and its whole ancestor path through the document
    it was applied to — the same class of hole already closed on /documents/bulk.
    An unknown id also used to surface as a 500 from the FK, which doubled as an
    existence oracle for other tenants' ids.
    """
    for field, model in (
        ("tag_id", Tag),
        ("correspondent_id", Correspondent),
        ("doc_type_id", DocType),
    ):
        value = updates.get(field)
        if value is None:
            continue
        owned = (
            await db.execute(
                select(model.id).where(
                    model.id == value, model.tenant_id == user.tenant_id
                )
            )
        ).scalar_one_or_none()
        if owned is None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY, f"Unknown {field}"
            )


@router.get("", response_model=list[RuleOut])
async def list_rules(user: CurrentUser, db: DB) -> list[RuleOut]:
    rules = (
        await db.execute(
            select(Rule)
            .where(Rule.tenant_id == user.tenant_id)
            .order_by(Rule.priority, Rule.created_at)
        )
    ).scalars().all()
    return [RuleOut.model_validate(r) for r in rules]


@router.post("", response_model=RuleOut, status_code=status.HTTP_201_CREATED)
async def create_rule(body: RuleCreate, user: CurrentUser, db: DB) -> RuleOut:
    _validate_pattern(body.match_type, body.pattern)
    fields = body.model_dump()
    await _validate_targets(fields, user, db)
    rule = Rule(tenant_id=user.tenant_id, **fields)
    db.add(rule)
    await db.flush()
    return RuleOut.model_validate(rule)


@router.patch("/{rule_id}", response_model=RuleOut)
async def update_rule(
    rule_id: uuid.UUID, body: RuleUpdate, user: CurrentUser, db: DB
) -> RuleOut:
    rule = await db.get(Rule, rule_id)
    if rule is None or rule.tenant_id != user.tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Rule not found")
    updates = body.model_dump(exclude_unset=True)
    match_type = updates.get("match_type", rule.match_type)
    pattern = updates.get("pattern", rule.pattern)
    _validate_pattern(match_type, pattern)
    await _validate_targets(updates, user, db)
    for key, value in updates.items():
        setattr(rule, key, value)
    # Editing a rule clears any auto-disable note: the pattern the user just
    # supplied deserves a fresh trial rather than inheriting the old verdict.
    if "pattern" in updates or "match_type" in updates:
        rule.error = None
    await db.flush()
    return RuleOut.model_validate(rule)


@router.delete("/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_rule(rule_id: uuid.UUID, user: CurrentUser, db: DB) -> None:
    rule = await db.get(Rule, rule_id)
    if rule is None or rule.tenant_id != user.tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Rule not found")
    await db.delete(rule)
