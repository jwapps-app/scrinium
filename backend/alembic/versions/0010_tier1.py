"""document dates, correspondents, doc types, trash, saved views, custom fields

Revision ID: 0010
Revises: 0009
Create Date: 2026-07-11

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def _entity_table(name: str) -> None:
    op.create_table(
        name,
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id"),
            nullable=False,
            index=True,
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint("tenant_id", "name"),
    )


def upgrade() -> None:
    _entity_table("correspondents")
    _entity_table("doc_types")

    op.add_column("documents", sa.Column("doc_date", sa.Date(), nullable=True))
    op.create_index("ix_documents_doc_date", "documents", ["doc_date"])
    op.add_column(
        "documents",
        sa.Column(
            "correspondent_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("correspondents.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        "documents",
        sa.Column(
            "doc_type_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("doc_types.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        "documents", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_index("ix_documents_deleted_at", "documents", ["deleted_at"])

    op.add_column(
        "rules",
        sa.Column(
            "correspondent_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("correspondents.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        "rules",
        sa.Column(
            "doc_type_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("doc_types.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )

    op.create_table(
        "saved_views",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id"),
            nullable=False,
            index=True,
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("params", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )

    op.create_table(
        "custom_fields",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id"),
            nullable=False,
            index=True,
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint("tenant_id", "name"),
    )
    op.create_table(
        "document_custom_values",
        sa.Column(
            "document_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "field_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("custom_fields.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("value", sa.Text(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("document_custom_values")
    op.drop_table("custom_fields")
    op.drop_table("saved_views")
    op.drop_column("rules", "doc_type_id")
    op.drop_column("rules", "correspondent_id")
    op.drop_index("ix_documents_deleted_at", "documents")
    op.drop_column("documents", "deleted_at")
    op.drop_column("documents", "doc_type_id")
    op.drop_column("documents", "correspondent_id")
    op.drop_index("ix_documents_doc_date", "documents")
    op.drop_column("documents", "doc_date")
    op.drop_table("doc_types")
    op.drop_table("correspondents")
