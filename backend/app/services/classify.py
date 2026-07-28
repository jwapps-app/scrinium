"""Transparent, on-demand classification.

Evaluates the tenant's rules against a document's text (title + original
filename + OCR text). Idempotent: applying twice yields the same state.
Rules are ordered by priority (lower first); the first matching rule with a
`set_title` wins the title, every matching rule's tag is added.
"""

import asyncio
import logging
from dataclasses import dataclass, field

import regex
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Document, Rule, Tag
from app.services.tag_tree import with_ancestors

logger = logging.getLogger(__name__)


class RuleTooSlow(Exception):
    """A rule pattern exceeded its matching budget."""


@dataclass
class ClassifyOutcome:
    matched_rules: list[str] = field(default_factory=list)
    added_tags: list[str] = field(default_factory=list)
    new_title: str | None = None
    set_correspondent: bool = False
    set_doc_type: bool = False


def rule_matches(rule: Rule, text: str) -> bool:
    """Does this rule match? Raises RuleTooSlow past the time budget.

    Matching runs on the `regex` module rather than stdlib `re` because it
    honours a wall-clock timeout. A pattern that compiles can still take
    exponential time on ordinary text — `(\\s*\\w+)+$` is the classic shape and
    a plausible thing to write by hand — and an unbounded match here would
    block a whole worker lane or the API event loop until the container died.
    """
    if rule.match_type == "regex":
        try:
            return (
                regex.search(
                    rule.pattern,
                    text,
                    regex.IGNORECASE,
                    timeout=settings.rule_match_timeout,
                )
                is not None
            )
        except TimeoutError as exc:
            raise RuleTooSlow(
                f"pattern took longer than {settings.rule_match_timeout}s to match"
            ) from exc
        except regex.error:
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
        try:
            # In a thread: matching is CPU-bound, and on a book-length text even
            # a well-behaved pattern would stall the event loop (API) or the
            # worker's heartbeat while it ran.
            matched = await asyncio.to_thread(rule_matches, rule, text)
        except RuleTooSlow as exc:
            # Fail soft, like an OCR failure: disable the offending rule, record
            # why, and carry on with the rest. Leaving it enabled would re-stall
            # on every future document and, since rules persist, would wedge
            # ingestion again after every restart.
            logger.warning("disabling rule %s (%s): %s", rule.id, rule.name, exc)
            rule.enabled = False
            rule.error = str(exc)
            continue
        if not matched:
            continue
        outcome.matched_rules.append(rule.name)
        if rule.tag_id is not None and rule.tag_id not in existing_tag_ids:
            # Scope the lookup to the document's tenant: rule targets are
            # validated on write, but a rule predating that check (or a tag
            # moved since) must never pull in another tenant's tag here.
            tag = (
                await session.execute(
                    select(Tag).where(
                        Tag.id == rule.tag_id,
                        Tag.tenant_id == document.tenant_id,
                    )
                )
            ).scalar_one_or_none()
            if tag is not None:
                # A tag implies its ancestors (hierarchy semantics).
                for applied in await with_ancestors(session, [tag]):
                    if applied.id not in existing_tag_ids:
                        document.tags.append(applied)
                        existing_tag_ids.add(applied.id)
                        outcome.added_tags.append(applied.name)
        if rule.set_title and title_candidate is None:
            title_candidate = rule.set_title
        # Correspondent/type: first matching rule wins, and never stomps a
        # value that's already set (manually or by an earlier run).
        if rule.correspondent_id and document.correspondent_id is None:
            document.correspondent_id = rule.correspondent_id
            outcome.set_correspondent = True
        if rule.doc_type_id and document.doc_type_id is None:
            document.doc_type_id = rule.doc_type_id
            outcome.set_doc_type = True

    # Report a title change only when it actually changes something, so
    # reclassifying is a true no-op the second time.
    if title_candidate and title_candidate != document.title:
        document.title = title_candidate
        outcome.new_title = title_candidate
    return outcome
