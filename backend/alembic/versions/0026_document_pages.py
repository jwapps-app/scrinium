"""Index each page separately so large books rank on their real relevance.

Postgres records token positions only for roughly the first 16,383 words of a
text, and ts_rank scores from positions. A 4.75 MB encyclopedia therefore
stored two positions for a word occurring twenty-eight times and ranked 82nd
out of 376 matches — effectively invisible — while an in-document search found
every one of them.

One vector per page keeps every page far under that ceiling, so positions are
complete and summing the pages gives a true score. It also demotes the very
large volumes correctly: a 1,109-page encyclopedia with two matching pages
should rank below a 37-page monograph on the subject, and with per-page
vectors it does.

Only the vector is stored, not the page text — the text already exists on
documents.text_content, which is what snippets are drawn from.

Revision ID: 0026
Revises: 0025
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import TSVECTOR, UUID

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "document_pages",
        sa.Column("document_id", UUID(as_uuid=True), nullable=False),
        sa.Column("page", sa.Integer(), nullable=False),
        sa.Column("search_vector", TSVECTOR(), nullable=False),
        sa.ForeignKeyConstraint(
            ["document_id"], ["documents.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("document_id", "page"),
    )
    op.create_index(
        "ix_document_pages_search_vector",
        "document_pages",
        ["search_vector"],
        postgresql_using="gin",
    )


def downgrade() -> None:
    op.drop_index("ix_document_pages_search_vector", table_name="document_pages")
    op.drop_table("document_pages")
