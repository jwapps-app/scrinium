"""job phase label

Revision ID: 0006
Revises: 0005
Create Date: 2026-07-10

"""
from alembic import op
import sqlalchemy as sa

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("phase", sa.String(16), nullable=True))


def downgrade() -> None:
    op.drop_column("jobs", "phase")
