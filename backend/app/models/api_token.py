import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class ApiToken(Base):
    """A long-lived credential for another program, made under Settings.

    The secret is shown once and only its hash is kept, so a copy of the
    database does not hand out working tokens. It resolves to the person who
    made it and does everything their session can — or, when read-only,
    only what a GET can — and it is revoked one at a time by name. It is
    not a session: signing out or changing the password leaves it alone,
    because the thing holding it is a service, not a browser.
    """

    __tablename__ = "api_tokens"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(255))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    # The last few characters of the secret, so a list of tokens can be told
    # apart from the one in a config file without revealing any of it.
    suffix: Mapped[str] = mapped_column(String(8))
    read_only: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
