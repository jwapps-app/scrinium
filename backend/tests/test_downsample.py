"""Archive downsampling: the compress helpers, the DPI setting, and the
low-priority backfill fleet endpoint."""

import tempfile
import uuid
from pathlib import Path



def test_compress_fail_soft_on_imageless_pdf():
    """A blank/vector PDF has no raster images to shrink, so the DPI probe is
    None and downsampling reports no win (keep the original archive)."""
    import pikepdf

    from app.services import compress

    pdf = pikepdf.new()
    pdf.add_blank_page(page_size=(612, 792))
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "src.pdf"
        pdf.save(str(src))
        assert compress.max_image_dpi(src) is None
        # No raster images to shrink → no usable result → keep the original.
        assert compress.downsample_archive(src, Path(tmp) / "out.pdf", 300) is None
        assert compress.is_pdfa(src) is False  # a bare pikepdf is not PDF/A


async def test_archive_dpi_setting_roundtrip(client, auth):
    # Env default is 300 with no override set.
    got = (await client.get("/api/settings/archive-dpi", headers=auth)).json()
    assert got["dpi"] == 300

    # Set a runtime override, read it back.
    await client.post("/api/settings/archive-dpi", headers=auth, json={"dpi": 200})
    got = (await client.get("/api/settings/archive-dpi", headers=auth)).json()
    assert got["dpi"] == 200 and got["dpi_override"] == "200"

    # Too low is rejected; 0 (disable) is allowed.
    assert (
        await client.post("/api/settings/archive-dpi", headers=auth, json={"dpi": 50})
    ).status_code == 400
    assert (
        await client.post("/api/settings/archive-dpi", headers=auth, json={"dpi": 5000})
    ).status_code == 400
    assert (
        await client.post("/api/settings/archive-dpi", headers=auth, json={"dpi": 0})
    ).status_code == 200

    # Empty string returns to the env default.
    await client.post("/api/settings/archive-dpi", headers=auth, json={"dpi": ""})
    got = (await client.get("/api/settings/archive-dpi", headers=auth)).json()
    assert got["dpi"] == 300


async def test_downsample_disabled_returns_400(client, auth):
    await client.post("/api/settings/archive-dpi", headers=auth, json={"dpi": 0})
    resp = await client.post("/api/documents/downsample-archives", headers=auth)
    assert resp.status_code == 400
    # Restore for other tests sharing the module DB.
    await client.post("/api/settings/archive-dpi", headers=auth, json={"dpi": ""})


async def test_downsample_queues_low_priority_and_is_idempotent(
    client, auth, pdf_factory
):
    import sqlalchemy as sa

    from app.database import SessionLocal
    from app.models import Document, Job

    doc = (
        await client.post(
            "/api/documents", headers=auth,
            files={"file": (f"ds-{uuid.uuid4().hex[:6]}.pdf",
                            pdf_factory(text=uuid.uuid4().hex), "application/pdf")},
        )
    ).json()
    doc_id = uuid.UUID(doc["id"])
    async with SessionLocal() as session:
        # Make it a completed doc that owns an archive (point the archive at the
        # original blob — enough to satisfy the eligibility filter).
        original = (
            await session.execute(
                sa.select(Document.original_blob_id).where(Document.id == doc_id)
            )
        ).scalar_one()
        await session.execute(
            sa.update(Document).where(Document.id == doc_id)
            .values(status="ready", archive_blob_id=original)
        )
        await session.execute(
            sa.update(Job).where(Job.document_id == doc_id).values(status="done")
        )
        await session.commit()

    candidates = (
        await client.get("/api/documents/downsample-candidates", headers=auth)
    ).json()
    assert candidates["count"] >= 1 and candidates["enabled"] is True

    first = (
        await client.post("/api/documents/downsample-archives", headers=auth)
    ).json()
    assert first["queued"] >= 1

    async with SessionLocal() as session:
        job = (
            await session.execute(
                sa.select(Job).where(Job.document_id == doc_id, Job.kind == "downsample")
            )
        ).scalar_one()
        assert job.priority == 5 and job.status == "queued"

    # Re-running skips docs that already have an active downsample job.
    second = (
        await client.post("/api/documents/downsample-archives", headers=auth)
    ).json()
    async with SessionLocal() as session:
        count = (
            await session.execute(
                sa.select(sa.func.count(Job.id)).where(
                    Job.document_id == doc_id, Job.kind == "downsample"
                )
            )
        ).scalar_one()
    assert count == 1  # not double-queued


def test_acceptable_says_why_it_rejected():
    """"Already as small as it gets" and "the rebuild lost the text layer" are
    different problems. The sweep spent a day guessing between them."""
    from app.services import compress

    assert compress._acceptable.__doc__  # documented contract
    import inspect

    source = inspect.getsource(compress._acceptable)
    for reason in ("page_mismatch", "lost_text", "not_smaller"):
        assert f'"{reason}"' in source, reason
    assert "return None" in source, "None must mean acceptable"


def test_downsample_returns_reason_rather_than_stashing_it():
    """Several of these run at once in worker threads; a shared slot would
    have them overwriting each other's reason."""
    import inspect

    from app.services import compress

    assert not hasattr(compress, "last_reason"), "no shared mutable slot"
    sig = inspect.signature(compress.downsample_archive)
    assert "tuple" in str(sig.return_annotation)


def test_eligibility_excludes_archives_already_proven_unshrinkable():
    """The loop that burned CPU on 999 documents.

    A rebuild Ghostscript cannot make smaller leaves the archive — and its
    measured DPI — untouched, so on DPI alone the document stayed eligible and
    was queued to fail the same way for ever. Keyed on the blob, not a flag, so
    a re-OCR that writes a new archive re-qualifies it.
    """
    import inspect

    from app.routers import documents

    source = inspect.getsource(documents._downsample_eligible)
    assert "downsample_tried_blob" in source
    assert "is_distinct_from" in source, (
        "must compare against the current archive, so a re-OCR re-qualifies"
    )
