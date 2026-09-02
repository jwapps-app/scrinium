"""Refresh tokens become single-use.

A refresh token was a bearer secret good for thirty days however often it
was exchanged. The exchange handed back a new pair and left the old one
valid, so a copy taken once could be renewed indefinitely until the user
happened to change their password. This table gives each issued token a row
to retire: a refresh revokes it and records its successor, and a token
presented after revocation is honoured only within a short grace window, and
only with that same successor — a lost response is not a stolen token.

Tokens issued before this carry no id and are accepted until they expire,
then rotated into the scheme on first use, so no device is signed out by
the deploy.

Revision ID: 0033
Revises: 0032
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "refresh_tokens",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("replaced_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index("ix_refresh_tokens_user_id", "refresh_tokens", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_refresh_tokens_user_id", table_name="refresh_tokens")
    op.drop_table("refresh_tokens")
