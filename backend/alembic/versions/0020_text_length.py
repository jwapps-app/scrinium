"""Cache each document's OCR-text character count so the weak-OCR insight
doesn't recompute length(text_content) — which detoasts large text — on every
Insights load. Populated at OCR time and backfilled by the worker.

Revision ID: 0020
Revises: 0019
"""

import sqlalchemy as sa
from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("documents", sa.Column("text_length", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("documents", "text_length")
