import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class JobStatus:
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class Job(Base):
    """Postgres-backed work queue; claimed with FOR UPDATE SKIP LOCKED."""

    __tablename__ = "jobs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(32), default="ingest")
    mode: Mapped[str] = mapped_column(String(16), default="skip")  # skip|redo|force
    status: Mapped[str] = mapped_column(
        String(16), default=JobStatus.QUEUED, index=True
    )
    attempts: Mapped[int] = mapped_column(default=0)
    pages_done: Mapped[int | None] = mapped_column(nullable=True)
    pages_total: Mapped[int | None] = mapped_column(nullable=True)
    # Which stage the counters describe: preparing | ocr | finishing
    phase: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # Stamped by the progress loop while running; a RUNNING job whose
    # heartbeat is stale was orphaned by a dead worker and gets requeued.
    heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
