import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class Rule(Base):
    """A transparent classification rule: match text, assign a tag / title.

    No trained models, no pickle rot — rules are rows the user can read and
    edit, evaluated on demand, idempotent by construction.
    """

    __tablename__ = "rules"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), index=True
    )
    name: Mapped[str] = mapped_column(String(255))
    match_type: Mapped[str] = mapped_column(String(16), default="contains")  # contains|regex
    pattern: Mapped[str] = mapped_column(String(1024))
    tag_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tags.id", ondelete="SET NULL"), nullable=True
    )
    set_title: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    correspondent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("correspondents.id", ondelete="SET NULL"),
        nullable=True,
    )
    doc_type_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("doc_types.id", ondelete="SET NULL"), nullable=True
    )
    priority: Mapped[int] = mapped_column(default=100)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # Why the rule was auto-disabled (currently: pattern exceeded its match
    # budget). Surfaced in the UI so a disabled rule isn't a silent mystery.
    error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
