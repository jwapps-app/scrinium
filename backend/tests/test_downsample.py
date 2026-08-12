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

    # The retry that pairs with that timeout now lives in the API process
    # rather than the entrypoint; test_startup.py covers it against the real
    # code instead of by grepping a shell script.


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


def test_the_worker_still_waits_for_the_schema_in_the_entrypoint():
    """The API owns migrations now, but the worker does not — it has to keep
    waiting for someone else to finish them, which is a shell-level concern
    because it happens before the worker process exists.
    """
    from pathlib import Path

    script = (Path(__file__).resolve().parents[1] / "entrypoint.sh").read_text()
    worker_branch = script.split("exec python -m app.worker")[0]
    assert "alembic current" in worker_branch and "alembic heads" in worker_branch
    assert "SCHEMA_WAIT_ATTEMPTS" in worker_branch, "and gives up rather than looping"


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


def _backup_script(code_only: bool = True) -> str:
    """The backup entrypoint script. Comments are stripped by default: they
    quote the very mistakes these tests forbid (`| gzip`, a bare pg_dump), and
    matching prose instead of code is how the first version of these tests
    managed to fail against a correct script."""
    from pathlib import Path

    import yaml

    compose = yaml.safe_load(
        (Path(__file__).resolve().parents[2] / "docker-compose.portainer.yml").read_text()
    )
    entrypoint = compose["services"]["backup"]["entrypoint"]
    assert isinstance(entrypoint, list), "a string entrypoint is what folded"
    script = entrypoint[-1]
    if not code_only:
        return script
    return "\n".join(
        line for line in script.splitlines() if not line.lstrip().startswith("#")
    )


def test_the_backup_survives_being_read_by_a_shell():
    """The entrypoint was a folded YAML scalar, and the fold turned its
    indented continuation lines into real newlines. The shell reads those as
    statement separators, so `pg_dump ... -Fc` ran alone — whole database, no
    exclusion, output to the container log — and the next line started
    `--exclude-table-data=...: not found`, feeding an empty pipe. gzip still
    exited 0, so `&&` still ran the delete: empty dumps written, good ones
    pruned. A list-form entrypoint with a literal block keeps a newline
    meaning what it looks like.
    """
    script = _backup_script()

    dump_line = next(line for line in script.splitlines() if "pg_dump" in line)
    assert dump_line.rstrip().endswith("\\"), (
        "the pg_dump call must continue onto the next line explicitly"
    )
    joined = " ".join(script.replace("\\\n", " ").split())
    assert "-Fc --exclude-table-data=document_pages" in joined


def test_the_backup_does_not_fire_on_every_deploy():
    """This container is recreated by every stack update, and dumping before
    sleeping turned eight repulls in one day into eight 5 GB dumps — 71 GB in
    the directory against a 14 GB database."""
    script = _backup_script()

    # Age of the newest dump decides, not the fact of having started.
    assert "stat -c %Y" in script
    assert "INTERVAL - age" in script, "sleep only what is left of the interval"
    assert "continue" in script


def test_backup_retention_counts_dumps_rather_than_days():
    """14 days of age-based retention is unbounded in size: at one dump a day
    it is 14, at eight it is 112. Keeping a fixed number bounds the directory
    whatever the cadence turns out to be."""
    script = _backup_script()

    assert "BACKUP_KEEP" in script
    assert "tail -n +" in script, "keep the newest N, delete the rest"
    assert "-mtime" not in script, "age-based retention cannot bound the size"
    # Both the new .dump and the .dump.gz left over from the double-compressed
    # era, or 71 GB of the old ones would never be reclaimed.
    assert "*.dump*" in script


def test_a_failed_dump_does_not_cost_us_a_good_one():
    """`pg_dump | gzip > file` reports gzip's exit status, so a dump that died
    halfway still looked like success: the truncated file was kept and the
    retention pass ran against it."""
    script = _backup_script()

    assert "gzip" not in script, "-Fc is already compressed; the pipe hid the status"
    assert "if pg_dump" in script, "branch on pg_dump's own exit status"
    # Written aside and renamed, so a partial file is never mistaken for a dump.
    assert ".in-progress" in script
    assert 'mv "$$TMP"' in script


def test_run_ocr_actually_constructs_its_result(monkeypatch, tmp_path):
    """197 tests passed while _run_ocr raised TypeError on every document.

    Nothing in the suite called it: it needs a provider, so it only ever ran
    against the real OCR stack in production. A new field added to the middle
    of IngestOutcome silently shifted every positional argument after it —
    archive_dpi landed on archive_pdfa_wanted, whose keyword then collided —
    and the first thing to notice was a live re-OCR failing 14 documents in a
    row. This calls it with the provider stubbed, so the construction is
    exercised whatever the field order happens to be.
    """
    from app.services import compress, ingest, storage, thumbnails

    class _Result:
        text = "recovered text"
        engine = "stub"

        def __init__(self, archive):
            self.archive_path = archive

    seen = {}

    class _Provider:
        def process(self, source, workdir, mode, pdfa):
            seen["pdfa"] = pdfa
            archive = workdir / "archive.pdf"
            archive.write_bytes(b"%PDF-1.7\n")
            return _Result(archive)

    monkeypatch.setattr(ingest, "get_provider", lambda engine: _Provider())
    # A scan: 400 DPI original, so `auto` must not ask for PDF/A.
    monkeypatch.setattr(
        compress, "max_image_dpi", lambda path: 400 if "input" in str(path) else 300
    )
    monkeypatch.setattr(compress, "over_cap", lambda dpi, cap: False)
    monkeypatch.setattr(compress, "is_pdfa", lambda path: False)
    monkeypatch.setattr(thumbnails, "make_thumbnail", lambda src, out: None)
    monkeypatch.setattr(ingest, "_page_count", lambda path: 7)

    blob = uuid.uuid4()
    monkeypatch.setattr(storage, "store_file", lambda path: (blob, "sha", 4242))

    original = tmp_path / "original-blob"
    original.write_bytes(b"%PDF-1.7\n")
    workdir = tmp_path / "work"
    workdir.mkdir()

    outcome = ingest._run_ocr(
        original, ".pdf", "redo", workdir, None, 300, "auto"
    )

    # Every field on the right attribute, which is what the shift broke.
    assert outcome.blob_id == blob
    assert outcome.size_bytes == 4242
    assert outcome.text == "recovered text"
    assert outcome.engine == "stub"
    assert outcome.page_count == 7
    assert outcome.original_dpi == 400
    assert outcome.archive_dpi == 300
    assert outcome.archive_pdfa is False
    # A 400 DPI scan under `auto`: PDF/A was never asked for, so a plain-PDF
    # archive is the intent and not a shortfall.
    assert outcome.archive_pdfa_wanted is False
    assert seen["pdfa"] is False


def test_run_ocr_records_the_intent_for_a_born_digital_original(
    monkeypatch, tmp_path
):
    """The other branch of `auto`, through the same constructor."""
    from app.services import compress, ingest, storage, thumbnails

    class _Result:
        text = "digital text"
        engine = "stub"

        def __init__(self, archive):
            self.archive_path = archive

    class _Provider:
        def process(self, source, workdir, mode, pdfa):
            assert pdfa is True, "a born-digital original must ask for PDF/A"
            archive = workdir / "archive.pdf"
            archive.write_bytes(b"%PDF-1.7\n")
            return _Result(archive)

    monkeypatch.setattr(ingest, "get_provider", lambda engine: _Provider())
    monkeypatch.setattr(compress, "max_image_dpi", lambda path: 0)
    monkeypatch.setattr(compress, "over_cap", lambda dpi, cap: False)
    monkeypatch.setattr(compress, "is_pdfa", lambda path: True)
    monkeypatch.setattr(thumbnails, "make_thumbnail", lambda src, out: None)
    monkeypatch.setattr(ingest, "_page_count", lambda path: 1)
    monkeypatch.setattr(
        storage, "store_file", lambda path: (uuid.uuid4(), "sha", 100)
    )

    original = tmp_path / "original-blob"
    original.write_bytes(b"%PDF-1.7\n")
    workdir = tmp_path / "work"
    workdir.mkdir()

    outcome = ingest._run_ocr(original, ".pdf", "redo", workdir, None, 300, "auto")

    assert outcome.original_dpi == 0
    assert outcome.archive_pdfa_wanted is True
    assert outcome.archive_pdfa is True


def test_downsampling_does_not_resurrect_pdfa_over_a_plain_pdf(monkeypatch, tmp_path):
    """downsample_archive keeps PDF/A by default, which silently undid the
    format choice for any document above the DPI cap.

    Caught live on the first re-OCR trial: a 600 DPI scan asked for plain PDF,
    the OCR pass produced it, and the downsample handed back PDF/A —
    wanted=false, got=true. Since the cap is what large scans exceed, this
    would have quietly cancelled most of the saving the setting exists for.
    """
    from app.services import compress, ingest, storage, thumbnails

    captured = {}

    class _Result:
        text = "t"
        engine = "stub"

        def __init__(self, archive):
            self.archive_path = archive

    class _Provider:
        def process(self, source, workdir, mode, pdfa):
            archive = workdir / "archive.pdf"
            archive.write_bytes(b"%PDF-1.7\n")
            return _Result(archive)

    def _fake_downsample(src, dst, target, keep_pdfa=True):
        captured["keep_pdfa"] = keep_pdfa
        dst.write_bytes(b"%PDF-1.7\n")
        return "pdf", None

    monkeypatch.setattr(ingest, "get_provider", lambda engine: _Provider())
    monkeypatch.setattr(compress, "max_image_dpi", lambda path: 600)
    monkeypatch.setattr(compress, "over_cap", lambda dpi, cap: True)  # above cap
    monkeypatch.setattr(compress, "downsample_archive", _fake_downsample)
    monkeypatch.setattr(compress, "is_pdfa", lambda path: False)
    monkeypatch.setattr(thumbnails, "make_thumbnail", lambda src, out: None)
    monkeypatch.setattr(ingest, "_page_count", lambda path: 1)
    monkeypatch.setattr(
        storage, "store_file", lambda path: (uuid.uuid4(), "sha", 10)
    )

    original = tmp_path / "orig"
    original.write_bytes(b"%PDF-1.7\n")
    workdir = tmp_path / "w"
    workdir.mkdir()

    outcome = ingest._run_ocr(original, ".pdf", "redo", workdir, None, 300, "auto")

    assert captured["keep_pdfa"] is False, (
        "a scan archived as plain PDF must not be handed back as PDF/A"
    )
    assert outcome.archive_pdfa_wanted is False


def test_the_downsample_job_preserves_the_documents_format(monkeypatch):
    """The standalone backfill has the same hazard: rebuilding for DPI must
    not change the format underneath the document."""
    import inspect

    from app.services import compress

    source = inspect.getsource(compress.process_downsample_job)
    assert "document.archive_pdfa_wanted" in source, (
        "the DPI rebuild must carry the document's own format intent"
    )
