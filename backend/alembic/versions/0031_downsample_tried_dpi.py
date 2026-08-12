"""Record which DPI target a downsample was tried at.

The marker that stops a document being requeued for ever recorded only the
archive blob, not the target. So lowering the cap — 300 to 200, say — would
leave every document marked `not_smaller` at 300 permanently excluded, even
though a 200 DPI rebuild might shrink it easily. The setting would appear to
do nothing for 1,135 documents and nobody would know why.

Revision ID: 0031
Revises: 0030
"""

import sqlalchemy as sa
from alembic import op

revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "documents", sa.Column("downsample_tried_dpi", sa.Integer(), nullable=True)
    )
    # Everything already marked was tried at the cap in force at the time.
    op.execute(
        "UPDATE documents SET downsample_tried_dpi = archive_dpi "
        "WHERE downsample_tried_blob IS NOT NULL"
    )


def downgrade() -> None:
    op.drop_column("documents", "downsample_tried_dpi")
