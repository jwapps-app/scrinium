import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class Blob(Base):
    """A stored file under an opaque key. Never renamed, never mutated."""

    __tablename__ = "blobs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    mime_type: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    # When the integrity sweep last re-hashed this blob, whatever it found.
    # Stamped on failures too: the sweep works oldest-first, and a blob that
    # never advanced kept its place at the front, so a few hundred bad files
    # were re-checked every hour while the rest of the store never was.
    verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Why the last check failed ("corrupt: …" or "unreadable: …"); None when
    # the bytes matched. The verdict lives here, next to the timestamp.
    verify_error: Mapped[str | None] = mapped_column(Text, nullable=True)
