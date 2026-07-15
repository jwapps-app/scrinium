"""Track whether each document's archive is PDF/A conformant.

NULL = unknown / not yet measured (or no archive); TRUE = PDF/A; FALSE = a
plain PDF that couldn't be made PDF/A (surfaced as an indicator in the UI).

Revision ID: 0017
Revises: 0016
"""

import sqlalchemy as sa
from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "documents", sa.Column("archive_pdfa", sa.Boolean(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("documents", "archive_pdfa")
