"""Record whether PDF/A was the intent, not just whether it happened.

`archive_pdfa is false` used to mean one thing: the conversion was supposed to
produce PDF/A and did not — a force-rastered fallback, something to fix. The
counter built on it was a worklist.

ARCHIVE_FORMAT=auto breaks that. A scan is now deliberately archived as plain
PDF, so the same false would accumulate for ever and the worklist would fill
with documents that are exactly as intended. Storing the intent keeps the two
apart: shortfall is wanted-and-didn't-get, which is still worth showing.

Existing rows are all `true`. Every archive predating the setting was made
when PDF/A was unconditional, so any that is not PDF/A genuinely fell short —
which is precisely what the counter should still report.

Revision ID: 0032
Revises: 0031
"""

import sqlalchemy as sa
from alembic import op

revision = "0032"
down_revision = "0031"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "documents",
        sa.Column(
            "archive_pdfa_wanted",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
    )


def downgrade() -> None:
    op.drop_column("documents", "archive_pdfa_wanted")
