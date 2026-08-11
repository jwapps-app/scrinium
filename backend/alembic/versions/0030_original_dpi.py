"""Measure the original's resolution, not just the archive's.

DPI has only ever been measured on the archive, so "can this document look
better?" was unanswerable. An archive sitting at the 300 cap might have come
from a 600 DPI scan that was downsampled — where a higher-resolution rebuild
would recover real detail — or from a 300 DPI scan that was never touched,
where it would only produce a larger file with nothing extra in it. Identical
in the database, opposite answers.

The original's native resolution is the ceiling on any rebuild: no setting
creates detail the scanner did not capture.

Revision ID: 0030
Revises: 0029
"""

import sqlalchemy as sa
from alembic import op

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # NULL = not yet measured (a worker sweep backfills). 0 = measured and has
    # no raster images at all, e.g. a born-digital PDF, which is a real answer
    # and must not read as "unmeasured" or it requeues for ever.
    op.add_column("documents", sa.Column("original_dpi", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("documents", "original_dpi")
