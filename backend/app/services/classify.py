"""Transparent, on-demand classification.

Evaluates the tenant's rules against a document's text (title + original
filename + OCR text). Idempotent: applying twice yields the same state.
Rules are ordered by priority (lower first); the first matching rule with a
`set_title` wins the title, every matching rule's tag is added.
"""

import re
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Document, Rule, Tag


@dataclass
class ClassifyOutcome:
    matched_rules: list[str] = field(default_factory=list)
    added_tags: list[str] = field(default_factory=list)
    new_title: str | None = None


def rule_matches(rule: Rule, text: str) -> bool:
    if rule.match_type == "regex":
        try:
            return re.search(rule.pattern, text, re.IGNORECASE) is not None
        except re.error:
            return False
    return rule.pattern.lower() in text.lower()


async def classify_document(
    session: AsyncSession, document: Document, rules: list[Rule] | None = None
) -> ClassifyOutcome:
    if rules is None:
        rules = (
            (
                await session.execute(
                    select(Rule)
                    .where(Rule.tenant_id == document.tenant_id, Rule.enabled)
                    .order_by(Rule.priority, Rule.created_at)
                )
            )
            .scalars()
            .all()
        )

    text = "\n".join(
        filter(None, [document.title, document.original_filename, document.text_content])
    )
    outcome = ClassifyOutcome()
    existing_tag_ids = {t.id for t in document.tags}
    title_candidate: str | None = None

    for rule in rules:
        if not rule_matches(rule, text):
            continue
        outcome.matched_rules.append(rule.name)
        if rule.tag_id is not None and rule.tag_id not in existing_tag_ids:
            tag = await session.get(Tag, rule.tag_id)
            if tag is not None:
                document.tags.append(tag)
                existing_tag_ids.add(tag.id)
                outcome.added_tags.append(tag.name)
        if rule.set_title and title_candidate is None:
            title_candidate = rule.set_title

    # Report a title change only when it actually changes something, so
    # reclassifying is a true no-op the second time.
    if title_candidate and title_candidate != document.title:
        document.title = title_candidate
        outcome.new_title = title_candidate
    return outcome
