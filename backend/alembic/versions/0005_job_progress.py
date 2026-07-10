"""job page progress

Revision ID: 0005
Revises: 0004
Create Date: 2026-07-10

"""
from alembic import op
import sqlalchemy as sa

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("pages_done", sa.Integer(), nullable=True))
    op.add_column("jobs", sa.Column("pages_total", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("jobs", "pages_total")
    op.drop_column("jobs", "pages_done")
