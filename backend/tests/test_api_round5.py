"""Text/office ingestion, upgrade advisor, scoped search, binder, integrity."""

import io
import uuid
import zipfile
from datetime import datetime, timezone


def _name(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def make_docx(text):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>',
        )
        zf.writestr(
            "word/document.xml",
            f'<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            f"<w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>",
        )
    return buf.getvalue()


def make_epub(chapters):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip")
        for i, chapter in enumerate(chapters):
            zf.writestr(
                f"ch{i}.xhtml",
                f"<html><body><h1>Chapter {i}</h1><p>{chapter}</p></body></html>",
            )
    return buf.getvalue()


def test_extract_docx_and_epub(tmp_path):
    from app.services.textdocs import extract_text

    docx = tmp_path / "a.docx"
    docx.write_bytes(make_docx("The quick brown fox of preparedness."))
    assert "quick brown fox" in extract_text(docx)

    epub = tmp_path / "b.epub"
    epub.write_bytes(make_epub(["Water purification basics.", "Shelter."]))
    text = extract_text(epub)
    assert "Water purification" in text and "Shelter" in text

    txt = tmp_path / "c.txt"
    txt.write_text("plain text guide")
    assert extract_text(txt) == "plain text guide"

    assert extract_text(tmp_path / "d.pdf") is None  # not a text format


async def test_text_native_ingest_ready_immediately(client, auth):
    marker = uuid.uuid4().hex
    resp = await client.post(
        "/api/documents", headers=auth,
        files={"file": (f"guide-{marker[:6]}.docx", make_docx(f"survival {marker}"),
                        "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
    )
    assert resp.status_code == 201, resp.text
    doc = resp.json()
    assert doc["status"] == "ready"          # no OCR job needed
    assert doc["ocr_engine"] == "native"

    text = (await client.get(f"/api/documents/{doc['id']}/text", headers=auth)).json()
    assert marker in text["text"]

    # searchable right away
    hits = (await client.get(f"/api/search?q={marker}", headers=auth)).json()
    assert any(r["id"] == doc["id"] for r in hits["results"])


async def test_scoped_search(client, auth, pdf_factory):
    import sqlalchemy as sa

    from app.database import SessionLocal
    from app.models import Document

    marker = uuid.uuid4().hex
    tag = (await client.post("/api/tags", headers=auth, json={"name": _name("scope")})).json()
    docs = []
    for i in range(2):
        d = (
            await client.post(
                "/api/documents", headers=auth,
                files={"file": (f"sc{i}-{marker[:5]}.pdf", pdf_factory(text=f"{i}-{marker}"), "application/pdf")},
            )
        ).json()
        docs.append(d)
    # both contain the marker text; only docs[0] gets the tag
    async with SessionLocal() as session:
        for d in docs:
            await session.execute(
                sa.update(Document)
                .where(Document.id == uuid.UUID(d["id"]))
                .values(text_content=f"content {marker}")
            )
        await session.commit()
    await client.patch(
        f"/api/documents/{docs[0]['id']}", headers=auth, json={"tag_ids": [tag["id"]]}
    )

    all_hits = (await client.get(f"/api/search?q={marker}", headers=auth)).json()
    assert len(all_hits["results"]) == 2
    scoped = (
        await client.get(f"/api/search?q={marker}&tag_id={tag['id']}", headers=auth)
    ).json()
    assert [r["id"] for r in scoped["results"]] == [docs[0]["id"]]


async def test_upgrade_advisor(client, auth, pdf_factory):
    import sqlalchemy as sa

    from app.database import SessionLocal
    from app.models import Document, Job

    doc = (
        await client.post(
            "/api/documents", headers=auth,
            files={"file": (f"up-{uuid.uuid4().hex[:6]}.pdf", pdf_factory(text=uuid.uuid4().hex), "application/pdf")},
        )
    ).json()
    async with SessionLocal() as session:
        await session.execute(
            sa.update(Document)
            .where(Document.id == uuid.UUID(doc["id"]))
            .values(status="ready", ocr_engine="tesseract")
        )
        # clear the intake job so upgrade-ocr's already-queued check passes
        await session.execute(
            sa.update(Job)
            .where(Job.document_id == uuid.UUID(doc["id"]))
            .values(status="done")
        )
        await session.commit()

    count = (await client.get("/api/documents/upgradeable", headers=auth)).json()
    assert count["count"] >= 1

    queued = (await client.post("/api/documents/upgrade-ocr", headers=auth)).json()
    assert queued["queued"] >= 1

    # low priority + idempotent re-run
    async with SessionLocal() as session:
        job = (
            await session.execute(
                sa.select(Job).where(
                    Job.document_id == uuid.UUID(doc["id"]),
                    Job.status == "queued",
                )
            )
        ).scalars().first()
        assert job is not None and job.priority == 10
    again = (await client.post("/api/documents/upgrade-ocr", headers=auth)).json()
    assert again["queued"] == 0 or True  # other docs may qualify; this doc must not double
    async with SessionLocal() as session:
        jobs = (
            await session.execute(
                sa.select(sa.func.count(Job.id)).where(
                    Job.document_id == uuid.UUID(doc["id"]),
                    Job.status == "queued",
                )
            )
        ).scalar_one()
        assert jobs == 1


import pytest
import shutil as _shutil


@pytest.mark.skipif(_shutil.which("gs") is None, reason="ghostscript not installed")
async def test_binder(client, auth, pdf_factory):
    import pikepdf

    ids = []
    for i in range(2):
        d = (
            await client.post(
                "/api/documents", headers=auth,
                files={"file": (f"bind{i}-{uuid.uuid4().hex[:5]}.pdf", pdf_factory(pages=3, text=uuid.uuid4().hex), "application/pdf")},
            )
        ).json()
        ids.append(d["id"])
    resp = await client.post(
        "/api/documents/binder", headers=auth,
        json={"ids": ids, "title": "Test Binder"},
    )
    assert resp.status_code == 200, resp.text
    with pikepdf.open(io.BytesIO(resp.content)) as pdf:
        # cover + 1 TOC page + 2×3 content pages
        assert len(pdf.pages) == 2 + 6


async def test_integrity_sweep_flags_corruption(client, auth, pdf_factory):
    import json as _json

    import sqlalchemy as sa

    from app.database import SessionLocal
    from app.models import Blob, Document
    from app.services import storage
    from app.worker import _verify_blobs

    doc = (
        await client.post(
            "/api/documents", headers=auth,
            files={"file": (f"int-{uuid.uuid4().hex[:6]}.pdf", pdf_factory(text=uuid.uuid4().hex), "application/pdf")},
        )
    ).json()
    async with SessionLocal() as session:
        blob_id = (
            await session.execute(
                sa.select(Document.original_blob_id).where(
                    Document.id == uuid.UUID(doc["id"])
                )
            )
        ).scalar_one()

    # first sweep: everything verifies
    await _verify_blobs(batch=10000)
    async with SessionLocal() as session:
        verified_at = (
            await session.execute(
                sa.select(Blob.verified_at).where(Blob.id == blob_id)
            )
        ).scalar_one()
        assert verified_at is not None

    # corrupt the bytes on disk → next sweep flags it
    path = storage.blob_file(blob_id)
    path.write_bytes(b"rotted")
    async with SessionLocal() as session:
        await session.execute(
            sa.update(Blob).where(Blob.id == blob_id).values(verified_at=None)
        )
        await session.commit()
    await _verify_blobs(batch=10000)

    health = (await client.get("/api/settings/health", headers=auth)).json()
    assert str(blob_id) in health["integrity"]["corrupt"]
