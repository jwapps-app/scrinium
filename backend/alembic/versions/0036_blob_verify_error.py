"""Record the integrity sweep's verdict on the blob row.

verified_at used to advance only when the sha256 matched, and the sweep
picks the oldest verified_at first. Blobs that fail therefore never move,
and once a batch's worth of them sat at the front the same 300 were
re-hashed every hour while the rest of the store went unchecked. From here
verified_at means "last checked" and the failure, if any, is kept beside it.

Revision ID: 0036
Revises: 0035
"""

import sqlalchemy as sa
from alembic import op

revision = "0036"
down_revision = "0035"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("blobs", sa.Column("verify_error", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("blobs", "verify_error")
