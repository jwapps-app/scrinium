"""track watched-folder filing path per document

Revision ID: 0009
Revises: 0008
Create Date: 2026-07-10

"""
from alembic import op
import sqlalchemy as sa

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "documents", sa.Column("source_path", sa.String(2048), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("documents", "source_path")
