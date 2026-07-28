"""Owner rights, so user management and global settings aren't open to every
account in the tenant.

Existing users are all promoted: on a single-user box that is the owner, and on
a shared one it preserves today's behaviour rather than silently locking anyone
out of settings they already had. New accounts default to non-admin.

Revision ID: 0024
Revises: 0023
"""

import sqlalchemy as sa
from alembic import op

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "is_admin", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )
    op.execute("UPDATE users SET is_admin = true")


def downgrade() -> None:
    op.drop_column("users", "is_admin")
