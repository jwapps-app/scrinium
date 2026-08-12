"""Archive downsampling: the compress helpers, the DPI setting, and the
low-priority backfill fleet endpoint."""

import tempfile
import uuid
from pathlib import Path


async def upload(client, auth, pdf_bytes, filename):
    resp = await client.post(
        "/api/documents", headers=auth,
        files={"file": (filename, pdf_bytes, "application/pdf")},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()



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
        # The reason comes back with it now, so a document that cannot be
        # improved is recorded rather than queued to fail again.
        fmt, why = compress.downsample_archive(src, Path(tmp) / "out.pdf", 300)
        assert fmt is None
        assert why in {"not_smaller", "page_mismatch", "lost_text",
                       "gs_failed", "unreadable"}, why
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


def test_migrations_fail_fast_rather_than_queue_for_a_lock():
    """A DDL statement waiting for a lock parks every query behind it.

    Postgres grants locks in order, so a migration queued behind the nightly
    pg_dump also blocks the ordinary reads that arrive after it — the API stays
    up and answers nothing, and the app reads as permanently "loading". It
    happened on two consecutive deploys, and got worse once document_pages
    (2.4M rows) joined the dump.
    """
    from pathlib import Path

    env = Path(__file__).resolve().parents[1] / "alembic" / "env.py"
    source = env.read_text()
    assert "lock_timeout" in source, "a migration must not queue for its lock"
    assert "MIGRATION_LOCK_TIMEOUT" in source, "and the wait must be tunable"

    entry = Path(__file__).resolve().parents[1] / "entrypoint.sh"
    script = entry.read_text()
    assert "until alembic upgrade head" in script, "a timed-out migration retries"
    assert "MIGRATION_MAX_ATTEMPTS" in script, "and gives up rather than looping"


async def test_file_details_distinguishes_cap_limited_from_source_limited(
    client, auth, pdf_factory
):
    """The question the panel exists to answer.

    An archive sitting at the DPI cap is ambiguous in the list view: it might
    have been downsampled from a much higher-resolution scan, where a rebuild
    recovers detail, or it might be untouched because the source was never
    better, where no setting helps. Same row, opposite answers.
    """
    import uuid as _uuid

    from sqlalchemy import update

    from app.database import SessionLocal
    from app.models import Document

    doc = await upload(client, auth, pdf_factory(text="detail"), "d.pdf")
    doc_id = _uuid.UUID(doc["id"])

    async def set_dpi(original, archive):
        """Give the document an archive as well as the DPI values.

        Tests do not run the worker, so an uploaded document has no archive at
        all — and 'archive_dpi with no archive' is a state production cannot
        produce. Pointing the archive at the same blob keeps the row coherent;
        what is under test is the comparison, not blob distinctness.
        """
        async with SessionLocal() as session:
            doc_row = await session.get(Document, doc_id)
            await session.execute(
                update(Document).where(Document.id == doc_id)
                .values(
                    original_dpi=original,
                    archive_dpi=archive,
                    archive_blob_id=doc_row.original_blob_id,
                )
            )
            await session.commit()

    # Source has more than the archive → a rebuild would gain something.
    await set_dpi(600, 300)
    got = (await client.get(f"/api/documents/{doc['id']}/files", headers=auth)).json()
    assert got["can_improve"] is True
    assert got["max_useful_dpi"] == 600
    assert got["original"]["dpi"] == 600 and got["archive"]["dpi"] == 300

    # Source is the limit → no setting can help, so do not offer.
    await set_dpi(300, 300)
    got = (await client.get(f"/api/documents/{doc['id']}/files", headers=auth)).json()
    assert got["can_improve"] is False

    # Unmeasured is not a promise in either direction.
    await set_dpi(None, 300)
    got = (await client.get(f"/api/documents/{doc['id']}/files", headers=auth)).json()
    assert got["can_improve"] is False
    assert got["original"]["dpi"] is None

    # Original and archive are reported separately, not as one total.
    assert got["original"]["size_bytes"] > 0
    assert "size_bytes" in got["archive"]

    # No archive at all — the original is the document, and there is nothing
    # a rebuild could improve.
    async with SessionLocal() as session:
        await session.execute(
            update(Document).where(Document.id == doc_id)
            .values(archive_blob_id=None, archive_dpi=None, original_dpi=600)
        )
        await session.commit()
    got = (await client.get(f"/api/documents/{doc['id']}/files", headers=auth)).json()
    assert got["archive"]["exists"] is False
    assert got["can_improve"] is False


def test_startup_only_waits_when_there_is_a_migration_to_run():
    """The API cannot serve before the schema matches the code, so a real
    migration is worth waiting for. Most deploys carry none, and those were
    waiting anyway — the app stayed down for the whole nightly dump for no
    reason at all.
    """
    from pathlib import Path

    script = (Path(__file__).resolve().parents[1] / "entrypoint.sh").read_text()
    assert "alembic current" in script and "alembic heads" in script
    assert "nothing to apply" in script, "skip the wait when already current"
    # And when there is one, still retry rather than block the queue.
    assert "until alembic upgrade head" in script


def test_backup_skips_the_derivable_page_index():
    """document_pages is 9 GB of a 13 GB database and is rebuilt from
    text_content by a trigger, so dumping it triples the backup — and every
    minute of dump is a minute a deploy's migration waits for its lock. The
    table definition still ships; only the rows are skipped, and the worker
    sweep repopulates them after a restore."""
    from pathlib import Path

    compose = (
        Path(__file__).resolve().parents[2] / "docker-compose.portainer.yml"
    ).read_text()
    assert "--exclude-table-data=document_pages" in compose
    # Data only — the schema and its trigger must survive a restore.
    assert "--exclude-table=document_pages" not in compose


def test_worker_waits_for_the_schema_it_expects():
    """The worker's own comment said it waits for the schema; it did not.

    It starts querying immediately with code expecting the new columns, so a
    migration delayed behind the nightly dump left it crash-looping on
    UndefinedColumn for eleven minutes. No jobs were harmed only because the
    failure hit the claim query before any attempt counter moved — a few lines
    later and every restart would have burned an attempt on a real document.
    """
    from pathlib import Path

    script = (Path(__file__).resolve().parents[1] / "entrypoint.sh").read_text()
    # Everything before the API path is the worker branch. Splitting on "fi"
    # would stop at the nested one inside the loop, mid-branch.
    worker_branch = script.split("exec python -m app.worker")[0]
    assert "alembic current" in worker_branch
    assert "waiting for schema" in worker_branch
    assert "SCHEMA_WAIT_ATTEMPTS" in worker_branch, "bounded, not an infinite wait"
    # And the wait must come before the worker starts, not after.
    assert worker_branch.index("waiting for schema") < len(worker_branch)


def test_auto_picks_pdfa_only_where_it_earns_its_cost():
    """PDF/A guarantees text renders decades hence by embedding every font.

    A scan has no text to protect — just page images and the invisible
    glyphless OCR font, embedded regardless — and Ghostscript's conversion
    costs ~4x (measured: 104% of source as plain PDF, 406% as PDF/A, with no
    colour strategy avoiding it). A born-digital document inverts both halves.
    """
    from app.services.app_state import wants_pdfa

    # auto: original_dpi is the discriminator. 0 = no raster images.
    assert wants_pdfa("auto", 0) is True, "born-digital: fonts are worth protecting"
    assert wants_pdfa("auto", 300) is False, "a scan: nothing to protect, 4x cost"
    assert wants_pdfa("auto", 600) is False
    # Unmeasured must not be quietly downgraded.
    assert wants_pdfa("auto", None) is True

    # Explicit settings override the judgement entirely.
    assert wants_pdfa("pdfa", 300) is True
    assert wants_pdfa("pdf", 0) is False


def test_remedy_chain_skips_pdfa_rungs_when_not_asking_for_pdfa():
    """The first two rungs rescue a failing PDF/A conversion. With plain PDF
    requested there is none to rescue, and running them wastes two full
    Ghostscript passes per document."""
    from app.services.ocr import tesseract as T

    plain_cmd = [
        "python3", "-m", "ocrmypdf", "--skip-text",
        "--output-type", "pdf", "--quiet", "in.pdf", "out.pdf",
    ]
    labels = [label for label, _ in T.pdfa_fallback_commands(plain_cmd)]
    assert labels == ["plain-pdf", "force-raster"]


def test_downsample_marker_remembers_the_target_it_tried():
    """Lowering the cap must re-qualify documents.

    The marker recorded only the archive blob, so everything marked
    `not_smaller` at 300 would stay excluded after a drop to 200 — the new
    setting silently doing nothing for 1,135 documents.
    """
    import inspect

    from app.routers import documents

    source = inspect.getsource(documents._downsample_eligible)
    assert "downsample_tried_dpi" in source
    assert "> target_dpi" in source, "a higher previous target must re-qualify"
