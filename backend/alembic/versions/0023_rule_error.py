"""Record why a classification rule was auto-disabled.

A rule pattern that exceeds its match budget is now disabled rather than left
to stall every future document; this column says so in the UI instead of the
rule silently going quiet.

Revision ID: 0023
Revises: 0022
"""

import sqlalchemy as sa
from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("rules", sa.Column("error", sa.String(length=500), nullable=True))


def downgrade() -> None:
    op.drop_column("rules", "error")
