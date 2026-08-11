"""Drop the whole-document search vector; the per-page index replaces it.

A tsvector cannot exceed 1,048,575 bytes. `documents.search_vector` was a
stored generated column over title || text_content, so writing the OCR text
for a document with a large vocabulary made Postgres try to build a vector
past that ceiling and fail the whole UPDATE:

    ProgramLimitExceededError: string is too long for tsvector
    (3572932 bytes, max 1048575 bytes)

Size tracks distinct lexemes, not characters, which is why a 50 MB catalogue
indexed fine while a 4 MB encyclopedia did not — catalogues repeat themselves
and encyclopedias do not. Thirty-seven documents already sit above 900 kB.

The failure took out the whole ingest: the exception escaped process_job, the
job row was never marked done or failed, and it sat as an orphan until the
reclaimer requeued it to fail the same way, five times, then gave up.

document_pages (0026) has no such ceiling — a page is small — and already
provides better ranking. Matching moves there too, so nothing is lost and
long books stay fully searchable rather than being silently truncated.

Dropping is deliberate over clamping the expression: re-adding a generated
column rewrites the table and recomputes every vector across ~9 GB of text
under an exclusive lock. A drop is metadata only.

Revision ID: 0027
Revises: 0026
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import TSVECTOR

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index("ix_documents_search_vector", table_name="documents")
    op.drop_column("documents", "search_vector")
    # Titles still need to be searchable — "find the document called X" is not
    # served by the page index, which only holds page text. A title is a few
    # words, so this vector can never come near the 1 MB ceiling that made the
    # combined one unsafe.
    op.add_column(
        "documents",
        sa.Column(
            "title_vector",
            TSVECTOR(),
            sa.Computed("to_tsvector('english', coalesce(title, ''))", persisted=True),
        ),
    )
    op.create_index(
        "ix_documents_title_vector", "documents", ["title_vector"],
        postgresql_using="gin",
    )


def downgrade() -> None:
    op.drop_index("ix_documents_title_vector", table_name="documents")
    op.drop_column("documents", "title_vector")
    op.add_column(
        "documents",
        sa.Column(
            "search_vector",
            TSVECTOR(),
            sa.Computed(
                "to_tsvector('english', coalesce(title, '') || ' ' "
                "|| coalesce(text_content, ''))",
                persisted=True,
            ),
        ),
    )
    op.create_index(
        "ix_documents_search_vector",
        "documents",
        ["search_vector"],
        postgresql_using="gin",
    )
