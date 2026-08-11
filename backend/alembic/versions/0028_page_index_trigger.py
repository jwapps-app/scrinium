"""Maintain the page index in the database, not in application code.

Replacing the generated search_vector with a table meant indexing became
something callers had to remember. The generated column was maintained by
Postgres, so every writer got it for free — ingest, capture, the Paperless
importer, restore-from-export, a hand-written UPDATE. Moving it into Python
quietly made the ones that were not updated invisible to search, which the
scoped-search test found by writing text_content directly.

A trigger restores the property the generated column had — nobody can write
document text and forget to index it — without the 1 MB ceiling that made the
combined vector unusable for a document with a large vocabulary.

Revision ID: 0028
Revises: 0027
"""

from alembic import op

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None

# 90k chars is far below the 1 MB tsvector ceiling even for text that is all
# distinct words; it only guards against a pathological "page" produced by a
# broken split or a text-only fallback that emitted one enormous run.
FUNCTION = """
CREATE OR REPLACE FUNCTION documents_reindex_pages() RETURNS trigger AS $$
BEGIN
    DELETE FROM document_pages WHERE document_id = NEW.id;
    IF NEW.text_content IS NOT NULL THEN
        INSERT INTO document_pages (document_id, page, search_vector)
        SELECT NEW.id,
               part.ordinality,
               to_tsvector('english', left(part.body, 90000))
        FROM unnest(string_to_array(NEW.text_content, chr(12)))
                 WITH ORDINALITY AS part(body, ordinality)
        WHERE btrim(part.body) <> '';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

TRIGGER = """
CREATE TRIGGER trg_documents_reindex_pages
AFTER INSERT OR UPDATE OF text_content ON documents
FOR EACH ROW EXECUTE FUNCTION documents_reindex_pages();
"""


def upgrade() -> None:
    op.execute(FUNCTION)
    op.execute(TRIGGER)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_documents_reindex_pages ON documents")
    op.execute("DROP FUNCTION IF EXISTS documents_reindex_pages()")
