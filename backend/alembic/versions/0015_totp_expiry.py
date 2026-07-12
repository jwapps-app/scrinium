"""TOTP two-factor, document expiration dates, trigram search extension.

Revision ID: 0015
Revises: 0014
"""

import sqlalchemy as sa
from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("totp_secret", sa.String(64), nullable=True))
    op.add_column(
        "users",
        sa.Column("totp_enabled", sa.Boolean(), nullable=False, server_default="false"),
    )
    op.add_column("documents", sa.Column("expires_on", sa.Date(), nullable=True))
    op.create_index("ix_documents_expires_on", "documents", ["expires_on"])
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")


def downgrade() -> None:
    op.drop_index("ix_documents_expires_on")
    op.drop_column("documents", "expires_on")
    op.drop_column("users", "totp_enabled")
    op.drop_column("users", "totp_secret")
