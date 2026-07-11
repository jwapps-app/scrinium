import uuid
from datetime import datetime

from sqlalchemy import (
    Column,
    Computed,
    DateTime,
    ForeignKey,
    Index,
    String,
    Table,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import TSVECTOR, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class DocumentStatus:
    PENDING = "pending"        # uploaded, job queued
    PROCESSING = "processing"  # worker is on it
    READY = "ready"            # archive + text indexed
    FLAGGED = "flagged"        # OCR failed; original kept, needs attention


document_tags = Table(
    "document_tags",
    Base.metadata,
    Column(
        "document_id",
        UUID(as_uuid=True),
        ForeignKey("documents.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "tag_id",
        UUID(as_uuid=True),
        ForeignKey("tags.id", ondelete="CASCADE"),
        primary_key=True,
    ),
)


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), index=True
    )
    title: Mapped[str] = mapped_column(String(1024))
    original_filename: Mapped[str] = mapped_column(String(1024))
    original_blob_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("blobs.id")
    )
    archive_blob_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("blobs.id"), nullable=True
    )
    thumbnail_blob_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("blobs.id"), nullable=True
    )
    status: Mapped[str] = mapped_column(
        String(32), default=DocumentStatus.PENDING, index=True
    )
    # For watched-folder ingests: where the consumed copy was filed
    # (relative to WATCH_DIR), so deleting the document cleans it up too.
    source_path: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    ocr_engine: Mapped[str | None] = mapped_column(String(32), nullable=True)
    page_count: Mapped[int | None] = mapped_column(nullable=True)
    text_content: Mapped[str | None] = mapped_column(Text, nullable=True)
    search_vector = mapped_column(
        TSVECTOR,
        Computed(
            "to_tsvector('english', coalesce(title, '') || ' ' || coalesce(text_content, ''))",
            persisted=True,
        ),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    original_blob = relationship("Blob", foreign_keys=[original_blob_id])
    archive_blob = relationship("Blob", foreign_keys=[archive_blob_id])
    tags = relationship("Tag", secondary=document_tags, lazy="selectin")

    __table_args__ = (
        Index("ix_documents_search_vector", "search_vector", postgresql_using="gin"),
    )
