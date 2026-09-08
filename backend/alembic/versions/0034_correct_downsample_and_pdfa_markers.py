"""Correct two markers that kept finished work on the to-do lists.

Reclaim space reported 592 archives to check. 475 of them had already been
tried and recorded as "not smaller" — the state that is supposed to remove
them — but 0031 backfilled the target they were tried at from the archive's
own resolution. An archive that could not be shrunk below 400 DPI therefore
read as "tried at 400", and with the cap at 300 that is a higher target than
the one in force, which is exactly the case the column exists to re-open.
Every Reclaim pass would have re-run all 475 to learn nothing. The attempt
targeted the cap; record the cap.

The PDF/A shortfall counted 770. 726 were scans archived before the
`measured` format existed, when PDF/A was unconditional and Ghostscript's
conversion fell back to plain PDF; 0032 then marked every existing row as
wanting PDF/A. Under the rule in force since, a scan is not meant to be
PDF/A — there are no fonts to protect — so those are not shortfalls, and
listing them buried the 44 born-digital documents that genuinely are.

Revision ID: 0034
Revises: 0033
"""

import sqlalchemy as sa
from alembic import op

revision = "0034"
down_revision = "0033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The cap in force: the runtime override when one is set, else the
    # documented default. A row is only touched when its recorded target is
    # the tell-tale of the backfill — equal to the archive's own DPI.
    op.execute(
        """
        UPDATE documents
        SET downsample_tried_dpi = COALESCE(
            (SELECT value::int FROM app_settings
              WHERE key = 'archive_max_dpi' AND value ~ '^[0-9]+$'),
            300)
        WHERE downsample_note IS NOT NULL
          AND downsample_tried_blob = archive_blob_id
          AND downsample_tried_dpi = archive_dpi
        """
    )
    # A scan that never went through `measured` (its archive is not PDF/A and
    # was never chosen as such) was not meant to be PDF/A under the current
    # rule. Born-digital rows (original_dpi = 0) keep their marker.
    op.execute(
        """
        UPDATE documents
        SET archive_pdfa_wanted = false
        WHERE archive_pdfa IS false
          AND archive_pdfa_wanted
          AND original_dpi > 0
        """
    )


def downgrade() -> None:
    # The original values are not recoverable; the columns keep their shape.
    pass
