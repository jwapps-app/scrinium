import re
import uuid

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select

from app.deps import DB, CurrentUser
from app.models import Rule
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
    rule = Rule(tenant_id=user.tenant_id, **body.model_dump())
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
    for key, value in updates.items():
        setattr(rule, key, value)
    await db.flush()
    return RuleOut.model_validate(rule)


@router.delete("/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_rule(rule_id: uuid.UUID, user: CurrentUser, db: DB) -> None:
    rule = await db.get(Rule, rule_id)
    if rule is None or rule.tenant_id != user.tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Rule not found")
    await db.delete(rule)
