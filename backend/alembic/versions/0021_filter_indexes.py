"""Indexes for common library filters/sorts that were seq-scanning:
correspondent_id and doc_type_id (list filters, organize counts, sort keys)
and ocr_engine (engine filter, upgrade advisor).

Revision ID: 0021
Revises: 0020
"""

from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_documents_correspondent_id", "documents", ["correspondent_id"]
    )
    op.create_index("ix_documents_doc_type_id", "documents", ["doc_type_id"])
    op.create_index("ix_documents_ocr_engine", "documents", ["ocr_engine"])


def downgrade() -> None:
    op.drop_index("ix_documents_ocr_engine", table_name="documents")
    op.drop_index("ix_documents_doc_type_id", table_name="documents")
    op.drop_index("ix_documents_correspondent_id", table_name="documents")
