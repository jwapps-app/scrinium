"""Store each archive's embedded-image DPI so it can be sorted, displayed, and
used to gauge reclaimable resolution. NULL until measured (at OCR, or when a
downsample/reclaim job checks the document).

Revision ID: 0019
Revises: 0018
"""

import sqlalchemy as sa
from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("documents", sa.Column("archive_dpi", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("documents", "archive_dpi")
