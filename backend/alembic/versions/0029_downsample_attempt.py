"""Remember a downsample that could not improve an archive.

The eligible set was "archive above the cap", and a rebuild that Ghostscript
cannot make smaller leaves the archive — and therefore its measured DPI —
exactly as it was. So the document stayed eligible, was queued again, spent
another Ghostscript pass failing the same way, and stayed eligible. Nine
hundred and ninety-nine documents were in that loop, burning CPU with no
possible end: most are bitonal scans already compressed better than a
re-encode can manage.

The marker is the blob that was tried, not a flag. If a later re-OCR replaces
the archive, the id differs and the document qualifies again on its own —
which is the behaviour wanted, and one a boolean would have got wrong.

Revision ID: 0029
Revises: 0028
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "documents",
        sa.Column("downsample_tried_blob", UUID(as_uuid=True), nullable=True),
    )
    # Why it could not be improved: not_smaller, lost_text, page_mismatch,
    # unreadable. Recorded because "it did not work" and "it cannot work" want
    # different responses, and guessing between them wastes a day.
    op.add_column(
        "documents",
        sa.Column("downsample_note", sa.String(32), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("documents", "downsample_note")
    op.drop_column("documents", "downsample_tried_blob")
