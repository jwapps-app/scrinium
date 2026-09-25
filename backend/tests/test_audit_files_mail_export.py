"""Regressions for the 2026-09-24 audit findings on the file, mail and export
paths: F04/F05/F42 (watch folder), F11/F31 (IMAP), F06/F20 (export and
restore), F07 (purge ordering), F21 (importer caching across a rollback)."""

import email.message
import json
import os
import shutil
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
import sqlalchemy as sa

from app.config import settings
from app.database import SessionLocal
from app.models import (
    Annotation,
    AppSetting,
    CustomField,
    Document,
    ReadingPosition,
    Tag,
    Tenant,
    User,
    document_custom_values,
)
from app.services import intake, storage
from app.services.watch import _candidates, sweep_retention


async def _tenant_id() -> uuid.UUID:
    async with SessionLocal() as session:
        return (
            await session.execute(sa.select(Tenant.id).order_by(Tenant.created_at))
        ).scalars().first()


async def _upload(client, auth, pdf_bytes: bytes, name: str) -> dict:
    resp = await client.post(
        "/api/documents",
        headers=auth,
        files={"file": (name, pdf_bytes, "application/pdf")},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


# --- F04: symlink swap and changed-while-reading -----------------------------


def test_source_open_refuses_a_symlink(tmp_path):
    target = tmp_path / "target.txt"
    target.write_bytes(b"secret")
    link = tmp_path / "link.pdf"
    link.symlink_to(target)
    with pytest.raises(OSError):
        storage.sha256_of(link)
    with pytest.raises(OSError):
        storage.store_file(link)
    # A regular file still works, and both readers agree.
    blob_id, sha, _ = storage.store_file(target)
    assert sha == storage.sha256_of(target)
    storage.delete_blob(blob_id)


async def test_ingest_rejects_a_file_that_changed_between_reads(
    auth, pdf_factory, tmp_path, monkeypatch
):
    """The dedup hash and the stored copy are two reads of the same path.
    When they disagree the blob must go and the ingest must fail the way any
    failed ingest does, so the watch sweep files it under .failed/."""
    source = tmp_path / "moving.pdf"
    source.write_bytes(pdf_factory(text=uuid.uuid4().hex))
    stored: list[uuid.UUID] = []
    real_store = storage.store_file

    def recording_store(path, blob_id=None):
        result = real_store(path, blob_id)
        stored.append(result[0])
        return result

    monkeypatch.setattr(storage, "sha256_of", lambda path: "0" * 64)
    monkeypatch.setattr(storage, "store_file", recording_store)

    tenant_id = await _tenant_id()
    async with SessionLocal() as session:
        with pytest.raises(intake.SourceChanged) as info:
            await intake.ingest_file(session, tenant_id, source, "moving.pdf")
        await session.rollback()
    assert "changed while it was being read" in str(info.value)
    assert stored, "the copy was attempted"
    assert not storage.blob_file(stored[0]).exists(), "the mismatched blob was removed"


# --- F05: retention sweep and symlinks ---------------------------------------


def test_sweep_retention_never_follows_symlinks(tmp_path, monkeypatch):
    watch = tmp_path / "watch"
    outside = tmp_path / "outside"
    (watch / ".duplicates").mkdir(parents=True)
    outside.mkdir()
    old = time.time() - 40 * 86400

    def old_file(path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
        os.utime(path, (old, old))
        return path

    # .consumed itself points outside the watch tree.
    victim_root = old_file(outside / "root" / "victim.pdf")
    (watch / ".consumed").symlink_to(outside / "root")
    # A symlinked subdirectory inside a genuine filing dir.
    victim_sub = old_file(outside / "sub" / "victim.pdf")
    (watch / ".duplicates" / "linked").symlink_to(outside / "sub")
    # A symlinked file inside a genuine filing dir.
    victim_file = old_file(outside / "loose.pdf")
    link = watch / ".duplicates" / "loose.pdf"
    link.symlink_to(victim_file)
    os.utime(link, (old, old), follow_symlinks=False)
    # The one thing that should go.
    real_old = old_file(watch / ".duplicates" / "real-old.pdf")

    monkeypatch.setattr(settings, "watch_dir", str(watch))
    monkeypatch.setattr(settings, "consumed_retention_days", 30)
    assert sweep_retention() == 1
    assert not real_old.exists()
    assert victim_root.exists()
    assert victim_sub.exists()
    assert victim_file.exists()
    assert link.is_symlink(), "the link itself is left alone"
    assert (watch / ".duplicates" / "linked").is_symlink()


# --- F42: the scan prunes excluded trees -------------------------------------


def test_candidates_prunes_excluded_dirs_and_keeps_order(tmp_path, monkeypatch):
    watch = tmp_path / "watch"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "elsewhere.pdf").write_bytes(b"%PDF-1.4")
    layout = [
        "z.pdf",
        "A/b.pdf",
        "A/.hidden/c.pdf",
        "A/notes.txt",
        "A/skip.zip",
        ".consumed/old.pdf",
        ".consumed/deep/older.pdf",
        ".duplicates/dup.pdf",
        ".failed/bad.pdf",
        "@eaDir/thumb.pdf",
        "#recycle/gone.pdf",
        "._apple.pdf",
    ]
    for rel in layout:
        path = watch / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"%PDF-1.4")
    (watch / "linkdir").symlink_to(outside)
    (watch / "linkfile.pdf").symlink_to(outside / "elsewhere.pdf")

    visited: list[str] = []
    real_scandir = os.scandir

    def recording_scandir(path=".", *args, **kwargs):
        visited.append(os.fspath(path))
        return real_scandir(path, *args, **kwargs)

    monkeypatch.setattr(os, "scandir", recording_scandir)
    found = _candidates(watch)
    assert found == [watch / "A" / "b.pdf", watch / "A" / "notes.txt", watch / "z.pdf"]
    assert found == sorted(found)
    entered = {Path(p).relative_to(watch).as_posix() for p in visited if p != str(watch)}
    assert entered == {"A"}, entered


# --- F11 / F31: IMAP fetch semantics -----------------------------------------


class FakeIMAP:
    """Just enough of imaplib for the poll: UID SEARCH/FETCH/STORE, recorded."""

    def __init__(self, messages: dict[int, bytes]):
        self.messages = messages
        self.calls: list[tuple] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def select(self, folder):
        self.calls.append(("SELECT", folder))
        return "OK", [b"1"]

    def uid(self, command, *args):
        self.calls.append((command, *args))
        if command == "SEARCH":
            return "OK", [b" ".join(str(k).encode() for k in sorted(self.messages))]
        if command == "FETCH":
            uid = int(args[0])
            raw = self.messages[uid]
            if args[1] == "(RFC822.SIZE)":
                return "OK", [f"{uid} (UID {uid} RFC822.SIZE {len(raw)})".encode()]
            if args[1] == "(BODY.PEEK[])":
                header = f"{uid} (UID {uid} BODY[] {{{len(raw)}}}".encode()
                return "OK", [(header, raw), b")"]
            raise AssertionError(f"unexpected fetch item {args[1]}")
        if command == "STORE":
            return "OK", [b""]
        raise AssertionError(f"unexpected command {command}")


def _message(filename: str, payload: bytes, sender="Sender <s@example.com>") -> bytes:
    msg = email.message.EmailMessage()
    msg["From"] = sender
    msg["Subject"] = filename
    msg.set_content("attached")
    msg.add_attachment(
        payload, maintype="application", subtype="pdf", filename=filename
    )
    return msg.as_bytes()


def test_imap_connection_carries_a_timeout(monkeypatch):
    import imaplib

    from app.services import mail

    seen: dict = {}

    class FakeSSL:
        def __init__(self, host, port, **kwargs):
            seen.update(kwargs, host=host, port=port)

        def login(self, user, password):
            seen["login"] = user

    monkeypatch.setattr(imaplib, "IMAP4_SSL", FakeSSL)
    monkeypatch.setattr(settings, "mail_host", "imap.example.com")
    monkeypatch.setattr(settings, "mail_username", "u")
    monkeypatch.setattr(settings, "mail_password", "p")
    mail._connect()
    assert seen["timeout"] == mail.MAIL_TIMEOUT_SECONDS
    assert seen["ssl_context"] is not None


def test_fetch_peeks_and_skips_oversized_messages_before_fetching(
    monkeypatch, pdf_factory
):
    from app.services import mail

    monkeypatch.setattr(settings, "mail_max_attachment_mb", 1)
    small = _message("small.pdf", pdf_factory(text="small"))
    huge = _message("huge.pdf", b"%PDF" + b"0" * (2 * 1024 * 1024))
    fake = FakeIMAP({1: small, 2: huge})
    monkeypatch.setattr(mail, "_connect", lambda: fake)

    found, scanned = mail._fetch_unseen()

    assert [f["filename"] for f in found] == ["small.pdf"]
    assert scanned == [b"1", b"2"], "too large to ever fit, so it counts as handled"
    fetches = [c for c in fake.calls if c[0] == "FETCH"]
    assert ("FETCH", "1", "(RFC822.SIZE)") in fetches
    assert ("FETCH", "2", "(RFC822.SIZE)") in fetches
    assert ("FETCH", "1", "(BODY.PEEK[])") in fetches
    assert not any(c[1] == "2" and c[2] != "(RFC822.SIZE)" for c in fetches), (
        "the body of the oversized message was never transferred"
    )
    assert not any("RFC822)" in str(c) for c in fetches), "a plain RFC822 fetch sets Seen"
    assert not any(c[0] == "STORE" for c in fake.calls), "reading never flags"


async def test_poll_marks_seen_only_after_a_successful_ingest(
    auth, pdf_factory, monkeypatch
):
    from app.services import mail

    monkeypatch.setattr(settings, "mail_host", "imap.example.com")
    monkeypatch.setattr(settings, "mail_username", "u")
    monkeypatch.setattr(settings, "mail_password", "p")
    monkeypatch.setattr(settings, "mail_max_attachment_mb", 1)
    mail._FAILURES.clear()

    good = _message(f"good-{uuid.uuid4().hex[:6]}.pdf", pdf_factory(text=uuid.uuid4().hex))
    huge = _message("huge.pdf", b"%PDF" + b"0" * (2 * 1024 * 1024))
    bad = _message("bad.pdf", pdf_factory(text=uuid.uuid4().hex))
    fake = FakeIMAP({1: good, 2: huge, 3: bad})
    monkeypatch.setattr(mail, "_connect", lambda: fake)

    real_ingest = intake.ingest_file

    async def flaky_ingest(session, tenant_id, source, filename, **kwargs):
        if filename == "bad.pdf":
            raise RuntimeError("simulated ingest failure")
        return await real_ingest(session, tenant_id, source, filename, **kwargs)

    monkeypatch.setattr(intake, "ingest_file", flaky_ingest)

    assert await mail.poll_once() == 1

    stores = [c for c in fake.calls if c[0] == "STORE"]
    # The good one and the oversized one are flagged; the failed ingest is
    # left unseen for the retry. Oversized left unseen would hold a poll
    # slot for ever.
    assert sorted(stores) == [
        ("STORE", "1", "+FLAGS", "\\Seen"),
        ("STORE", "2", "+FLAGS", "\\Seen"),
    ], stores
    assert not any(c == ("FETCH", "2", "(BODY.PEEK[])") for c in fake.calls)
    assert all(c[2] == "(BODY.PEEK[])" or c[2] == "(RFC822.SIZE)"
               for c in fake.calls if c[0] == "FETCH")


# --- F06: export names never collide -----------------------------------------


def test_reserve_name_never_hands_out_the_same_path_twice():
    from app.services.export import reserve_name

    taken: set[str] = set()
    names = [
        reserve_name(taken, "F", title, ".pdf", with_archive=True)
        for title in ("x", "x", "x (2)", "X")
    ]
    assert names == ["x", "x (2)", "x (2) (2)", "X (3)"]
    assert len({n.lower() for n in names}) == 4
    # A .png original still collides with a .pdf one in the searchable tree.
    assert reserve_name(taken, "F", "x", ".png", with_archive=True) == "x (4)"
    # Without a searchable copy the .png original itself is still free.
    assert reserve_name(taken, "F", "x", ".png", with_archive=False) == "x"


async def test_folder_export_keeps_every_document_under_a_distinct_name(
    client, auth, pdf_factory
):
    from app.services.export import _run_export

    marker = f"dup-{uuid.uuid4().hex[:6]}"
    ids = []
    for title in (marker, marker, f"{marker} (2)"):
        doc = await _upload(client, auth, pdf_factory(text=uuid.uuid4().hex), "d.pdf")
        await client.patch(f"/api/documents/{doc['id']}", headers=auth, json={"title": title})
        ids.append(doc["id"])

    tenant_id = await _tenant_id()
    await _run_export(tenant_id, fmt="folder")
    export_root = max(
        (
            Path(p)
            for p in Path(os.environ["DATA_DIR"]).joinpath("export").glob("library-export-*")
            if p.is_dir()
        ),
        key=lambda p: p.stat().st_mtime,
    )
    manifest = json.loads((export_root / "manifest.json").read_text())
    assert manifest["version"] == 2
    mine = [d for d in manifest["documents"] if d["id"] in ids]
    assert len(mine) == 3
    originals = [d["original"] for d in mine]
    assert len(set(originals)) == 3, originals
    for rel in originals:
        assert (export_root / rel).is_file(), rel
    # Manifest and tree agree: one file per manifest entry, no silent skips.
    on_disk = sum(len(names) for _b, _d, names in os.walk(export_root / "originals"))
    assert on_disk == len(set(d["original"].lower() for d in manifest["documents"]))


# --- F07: files outlive a failed commit --------------------------------------


async def test_purge_leaves_files_in_place_when_the_commit_fails(
    client, auth, pdf_factory
):
    from app.services import deletion

    doc = await _upload(client, auth, pdf_factory(text=uuid.uuid4().hex), "keep.pdf")
    doc_id = uuid.UUID(doc["id"])
    async with SessionLocal() as session:
        row = await session.get(Document, doc_id)
        blob_path = storage.blob_file(row.original_blob_id)
        row.deleted_at = datetime.now(timezone.utc) - timedelta(days=400)
        await session.commit()
    assert blob_path.exists()

    async def failing_commit():
        raise RuntimeError("simulated commit failure")

    # The retention sweep: rows are flushed, the commit dies, the file stays.
    async with SessionLocal() as session:
        session.commit = failing_commit
        with pytest.raises(RuntimeError, match="commit failure"):
            await deletion.purge_expired(session)
        await session.rollback()
    assert blob_path.exists(), "unlinked before the commit was durable"
    async with SessionLocal() as session:
        assert await session.get(Document, doc_id) is not None

    # The single-document purge, same story.
    async with SessionLocal() as session:
        session.commit = failing_commit
        row = await session.get(Document, doc_id)
        released = await deletion.purge_document(session, row)
        with pytest.raises(RuntimeError):
            await session.commit()
        await session.rollback()
    assert blob_path.exists()
    assert released.blob_ids, "the purge reported what it would remove"

    # And a purge whose commit succeeds removes the file after the fact.
    async with SessionLocal() as session:
        assert await deletion.purge_expired(session) >= 1
    assert not blob_path.exists()
    async with SessionLocal() as session:
        assert await session.get(Document, doc_id) is None


# --- F20: export / restore round trip ----------------------------------------


async def test_manifest_v2_round_trip(client, auth, pdf_factory):
    from app.services.export import _run_export
    from app.services.paperless_import import run_import

    stamp = uuid.uuid4().hex[:6]
    pdf = pdf_factory(text=uuid.uuid4().hex)
    doc = await _upload(client, auth, pdf, f"rt-{stamp}.pdf")
    doc_id = uuid.UUID(doc["id"])
    tenant_id = await _tenant_id()
    parent_name, child_name, field_name = f"RT-P-{stamp}", f"RT-C-{stamp}", f"RT-F-{stamp}"

    async with SessionLocal() as session:
        user = (
            await session.execute(sa.select(User).where(User.tenant_id == tenant_id))
        ).scalars().first()
        parent = Tag(tenant_id=tenant_id, name=parent_name, color="#112233")
        session.add(parent)
        await session.flush()
        child = Tag(tenant_id=tenant_id, name=child_name, color="#445566", parent_id=parent.id)
        field = CustomField(tenant_id=tenant_id, name=field_name, kind="date")
        session.add_all([child, field])
        await session.flush()
        row = await session.get(Document, doc_id)
        row.tags = [parent, child]
        row.title = f"Round trip {stamp}"
        row.expires_on = date(2031, 3, 4)
        original_created = row.created_at
        await session.execute(
            document_custom_values.insert().values(
                document_id=doc_id, field_id=field.id, value="2030-01-02"
            )
        )
        session.add(
            Annotation(
                tenant_id=tenant_id, document_id=doc_id, page=1, quote="a quote",
                note="a note", rects='[{"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4}]',
                color="#ff0000",
            )
        )
        session.add(ReadingPosition(user_id=user.id, document_id=doc_id, page=7))
        await session.commit()
        user_email, user_id = user.email, user.id

    await _run_export(tenant_id, fmt="folder")
    export_root = max(
        (
            Path(p)
            for p in Path(os.environ["DATA_DIR"]).joinpath("export").glob("library-export-*")
            if p.is_dir()
        ),
        key=lambda p: p.stat().st_mtime,
    )
    manifest = json.loads((export_root / "manifest.json").read_text())
    entry = next(d for d in manifest["documents"] if d["id"] == doc["id"])
    assert entry["expires_on"] == "2031-03-04"
    assert entry["created_at"] == original_created.isoformat()
    assert entry["custom_values"] == {field_name: "2030-01-02"}
    assert entry["annotations"][0]["quote"] == "a quote"
    assert entry["annotations"][0]["rects"] == [{"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4}]
    assert entry["reading_positions"] == [{"user": user_email, "page": 7}]
    assert {"name": field_name, "kind": "date"} in manifest["custom_fields"]
    tags = {t["name"]: t for t in manifest["tags"]}
    assert tags[child_name] == {"name": child_name, "color": "#445566", "parent": parent_name}
    assert tags[parent_name]["parent"] is None

    # Wipe it all and restore from a manifest trimmed to this document.
    resp = await client.delete(f"/api/documents/{doc['id']}/purge", headers=auth)
    assert resp.status_code == 204, resp.text
    async with SessionLocal() as session:
        for name in (child_name, parent_name):
            tag = (
                await session.execute(sa.select(Tag).where(Tag.name == name))
            ).scalar_one()
            await session.delete(tag)
        field = (
            await session.execute(sa.select(CustomField).where(CustomField.name == field_name))
        ).scalar_one()
        await session.delete(field)
        await session.commit()

    import_root = Path(os.environ["DATA_DIR"]) / "import"
    shutil.rmtree(import_root, ignore_errors=True)
    target = import_root / f"roundtrip-{stamp}"
    (target / Path(entry["original"])).parent.mkdir(parents=True)
    shutil.copyfile(export_root / entry["original"], target / entry["original"])
    trimmed = {
        **manifest,
        "documents": [entry],
        "tags": [t for t in manifest["tags"] if t["name"] in (parent_name, child_name)],
        "custom_fields": [f for f in manifest["custom_fields"] if f["name"] == field_name],
    }
    (target / "manifest.json").write_text(json.dumps(trimmed))

    await run_import(tenant_id)

    async with SessionLocal() as session:
        status = json.loads((await session.get(AppSetting, "paperless_import_status")).value)
        assert status["imported"] == 1, status
        restored = (
            await session.execute(
                sa.select(Document).where(Document.title == f"Round trip {stamp}")
            )
        ).scalar_one()
        assert restored.expires_on == date(2031, 3, 4)
        assert restored.created_at == original_created
        assert {t.name for t in restored.tags} == {parent_name, child_name}
        by_name = {t.name: t for t in restored.tags}
        assert by_name[child_name].color == "#445566"
        assert by_name[child_name].parent_id == by_name[parent_name].id
        assert by_name[parent_name].color == "#112233"
        field = (
            await session.execute(sa.select(CustomField).where(CustomField.name == field_name))
        ).scalar_one()
        assert field.kind == "date"
        value = (
            await session.execute(
                sa.select(document_custom_values.c.value).where(
                    document_custom_values.c.document_id == restored.id,
                    document_custom_values.c.field_id == field.id,
                )
            )
        ).scalar_one()
        assert value == "2030-01-02"
        note = (
            await session.execute(
                sa.select(Annotation).where(Annotation.document_id == restored.id)
            )
        ).scalar_one()
        assert (note.page, note.quote, note.note, note.color) == (1, "a quote", "a note", "#ff0000")
        assert json.loads(note.rects) == [{"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4}]
        position = await session.get(ReadingPosition, (user_id, restored.id))
        assert position is not None and position.page == 7


async def test_version_1_manifest_still_restores(client, auth, pdf_factory):
    """No version key, custom values keyed by UUID, flat tag names: the
    shape every export before today produced."""
    from app.services.paperless_import import run_import

    stamp = uuid.uuid4().hex[:6]
    import_root = Path(os.environ["DATA_DIR"]) / "import"
    shutil.rmtree(import_root, ignore_errors=True)
    target = import_root / f"v1-{stamp}"
    (target / "originals" / "Old").mkdir(parents=True)
    (target / "originals" / "Old" / "legacy.pdf").write_bytes(pdf_factory(text=uuid.uuid4().hex))
    (target / "manifest.json").write_text(json.dumps({
        "app": "Scrinium",
        "documents": [{
            "title": f"Legacy {stamp}",
            "original_filename": "legacy.pdf",
            "original": "originals/Old/legacy.pdf",
            "archive": None,
            "doc_date": "2020-02-03",
            "tags": [f"V1-Tag-{stamp}"],
            "notes": "old note",
            "custom_values": {str(uuid.uuid4()): "ignored"},
        }],
    }))
    await run_import(await _tenant_id())
    async with SessionLocal() as session:
        restored = (
            await session.execute(sa.select(Document).where(Document.title == f"Legacy {stamp}"))
        ).scalar_one()
        assert restored.doc_date == date(2020, 2, 3)
        assert restored.notes == "old note"
        assert [t.name for t in restored.tags] == [f"V1-Tag-{stamp}"]
        assert restored.expires_on is None
        stray = (
            await session.execute(
                sa.select(sa.func.count()).select_from(CustomField).where(
                    CustomField.name.like("________-____-____-____-____________")
                )
            )
        ).scalar_one()
        assert stray == 0, "UUID-keyed values must not become field definitions"


# --- F21: a duplicate must not poison later metadata -------------------------


async def test_paperless_import_survives_a_duplicate_between_documents(
    client, auth, pdf_factory
):
    from app.services.paperless_import import run_import

    stamp = uuid.uuid4().hex[:6]
    tenant_id = await _tenant_id()
    tag_name, corr_name = f"PL-Tag-{stamp}", f"PL-Corr-{stamp}"
    # The tag already exists with a parent, so ancestor expansion has to
    # walk the tree for every document — the access the audit flagged.
    async with SessionLocal() as session:
        parent = Tag(tenant_id=tenant_id, name=f"PL-Parent-{stamp}")
        session.add(parent)
        await session.flush()
        session.add(Tag(tenant_id=tenant_id, name=tag_name, parent_id=parent.id))
        await session.commit()

    import_root = Path(os.environ["DATA_DIR"]) / "import"
    shutil.rmtree(import_root, ignore_errors=True)
    target = import_root / f"paperless-{stamp}"
    target.mkdir(parents=True)
    first = pdf_factory(text=uuid.uuid4().hex)
    (target / "a.pdf").write_bytes(first)
    (target / "b.pdf").write_bytes(first)  # byte-identical: a duplicate
    (target / "c.pdf").write_bytes(pdf_factory(text=uuid.uuid4().hex))

    def doc(pk, name):
        return {
            "model": "documents.document",
            "pk": pk,
            "fields": {
                "title": f"PL {name} {stamp}",
                "created": "2019-01-01T00:00:00Z",
                "correspondent": 1,
                "document_type": 1,
                "tags": [1],
            },
            "__exported_file_name__": f"{name}.pdf",
        }

    fixture = [
        {"model": "documents.tag", "pk": 1, "fields": {"name": tag_name, "color": "#abcdef"}},
        {"model": "documents.correspondent", "pk": 1, "fields": {"name": corr_name}},
        {"model": "documents.documenttype", "pk": 1, "fields": {"name": f"PL-Type-{stamp}"}},
        doc(1, "a"),
        doc(2, "b"),
        doc(3, "c"),
    ]
    (target / "manifest.json").write_text(json.dumps(fixture))

    await run_import(tenant_id)

    async with SessionLocal() as session:
        status = json.loads((await session.get(AppSetting, "paperless_import_status")).value)
        assert (status["imported"], status["skipped"], status["failed"]) == (2, 1, 0), status
        third = (
            await session.execute(sa.select(Document).where(Document.title == f"PL c {stamp}"))
        ).scalar_one()
        assert third.correspondent.name == corr_name
        assert third.doc_type.name == f"PL-Type-{stamp}"
        assert {t.name for t in third.tags} == {tag_name, f"PL-Parent-{stamp}"}
