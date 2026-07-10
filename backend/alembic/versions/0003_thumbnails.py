"""document thumbnails

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-09

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "documents",
        sa.Column(
            "thumbnail_blob_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("blobs.id"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("documents", "thumbnail_blob_id")
