import uuid
from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Column,
    Computed,
    Date,
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
    # Whether the archive is PDF/A-conformant. None = unknown/no archive;
    # False marks the plain PDFs that couldn't be made PDF/A.
    archive_pdfa: Mapped[bool | None] = mapped_column(nullable=True)
    status: Mapped[str] = mapped_column(
        String(32), default=DocumentStatus.PENDING, index=True
    )
    # For watched-folder ingests: where the consumed copy was filed
    # (relative to WATCH_DIR), so deleting the document cleans it up too.
    source_path: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    # The document's own date (letter date, invoice date…), extracted from
    # its text at ingest and editable by the user. Distinct from created_at,
    # which is only "when it entered the library".
    doc_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    correspondent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("correspondents.id", ondelete="SET NULL"),
        nullable=True,
    )
    doc_type_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("doc_types.id", ondelete="SET NULL"), nullable=True
    )
    # Soft delete: set = in the trash; purged for real after retention.
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    ocr_engine: Mapped[str | None] = mapped_column(String(32), nullable=True)
    page_count: Mapped[int | None] = mapped_column(nullable=True)
    # Freeform user notes, outside the OCR text.
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    # When this document lapses (policies, passports, certifications).
    expires_on: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    # 64-bit content fingerprint for near-duplicate detection.
    simhash: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
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
    correspondent = relationship("Correspondent", lazy="selectin")
    doc_type = relationship("DocType", lazy="selectin")

    __table_args__ = (
        Index("ix_documents_search_vector", "search_vector", postgresql_using="gin"),
    )
