"""Regression tests for the security-audit fixes."""

import io
import uuid
import zipfile
from pathlib import Path


def _name(p):
    return f"{p}-{uuid.uuid4().hex[:8]}"


async def upload(client, auth, data, filename, ctype="application/pdf"):
    return await client.post(
        "/api/documents", headers=auth, files={"file": (filename, data, ctype)}
    )


# --- Path traversal in import manifest -------------------------------------

def test_import_manifest_rejects_traversal(tmp_path, monkeypatch):
    import json

    from app.config import settings
    from app.services.paperless_import import _safe_member

    src = tmp_path / "export"
    src.mkdir()
    (src / "real.pdf").write_bytes(b"x")
    # inside is fine
    assert _safe_member(src, "real.pdf") == (src / "real.pdf").resolve()
    # escapes are refused
    assert _safe_member(src, "/etc/passwd") is None
    assert _safe_member(src, "../../../../etc/passwd") is None
    assert _safe_member(src, "") is None
    assert _safe_member(src, None) is None


def test_zip_bomb_extract_guard(tmp_path, monkeypatch):
    import pytest

    from app.services import paperless_import as pi

    monkeypatch.setattr(pi, "MAX_EXTRACT_BYTES", 1000)
    z = tmp_path / "big.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("manifest.json", "x" * 5000)  # file_size > cap
    with pytest.raises(ValueError, match="zip bomb"):
        pi._extract_zip(z)


# --- Office decompression bomb ---------------------------------------------

def test_office_member_bomb_refused(tmp_path, monkeypatch):
    from app.services import textdocs

    monkeypatch.setattr(textdocs, "_MAX_MEMBER", 1000)
    docx = tmp_path / "b.docx"
    with zipfile.ZipFile(docx, "w", zipfile.ZIP_DEFLATED) as zf:
        # highly compressible member that decompresses past the cap
        zf.writestr(
            "word/document.xml",
            '<?xml version="1.0"?><w:document xmlns:w="x"><w:body><w:t>'
            + ("A" * 50000)
            + "</w:t></w:body></w:document>",
        )
    # returns None (text skipped) instead of decompressing the bomb
    assert textdocs.extract_text(docx) is None


def test_xxe_external_entity_blocked(tmp_path):
    from app.services.textdocs import extract_text

    docx = tmp_path / "x.docx"
    with zipfile.ZipFile(docx, "w") as zf:
        zf.writestr(
            "word/document.xml",
            '<?xml version="1.0"?><!DOCTYPE r [<!ENTITY x SYSTEM "file:///etc/hostname">]>'
            '<w:document xmlns:w="x"><w:body><w:t>&x;</w:t></w:body></w:document>',
        )
    # defusedxml raises on the DTD; extractor swallows it → no file contents
    assert not (extract_text(docx) or "").strip()


# --- Bulk FK cross-tenant guard --------------------------------------------

async def test_bulk_set_correspondent_rejects_foreign_id(client, auth, pdf_factory):
    doc = (await upload(client, auth, pdf_factory(text=_name("b")), "b.pdf")).json()
    r = await client.post(
        "/api/documents/bulk", headers=auth,
        json={
            "ids": [doc["id"]],
            "action": "set_correspondent",
            "correspondent_id": str(uuid.uuid4()),  # nonexistent / not ours
        },
    )
    assert r.status_code == 404


# --- Safe file serving ------------------------------------------------------

async def test_file_serving_has_nosniff(client, auth, pdf_factory):
    doc = (await upload(client, auth, pdf_factory(text=_name("s")), "s.pdf")).json()
    r = await client.get(f"/api/documents/{doc['id']}/file", headers=auth)
    assert r.headers.get("x-content-type-options") == "nosniff"


# --- Login timing equalization + rate limit --------------------------------

async def test_login_wrong_and_missing_both_401(client):
    a = await client.post(
        "/api/auth/login", json={"email": "nobody@nowhere.tld", "password": "x"}
    )
    b = await client.post(
        "/api/auth/login", json={"email": "tester@example.com", "password": "wrong"}
    )
    assert a.status_code == 401 and b.status_code == 401
    assert a.json()["detail"] == b.json()["detail"]  # identical message


async def test_login_rate_limited(client):
    codes = []
    for _ in range(14):
        r = await client.post(
            "/api/auth/login",
            json={"email": f"{uuid.uuid4()}@x.tld", "password": "x"},
        )
        codes.append(r.status_code)
    assert 429 in codes  # limiter kicks in within the window


async def test_runaway_rule_pattern_is_bounded_and_disabled(client, auth, pdf_factory):
    """A rule pattern that backtracks exponentially must not run forever: it is
    bounded, the rule disables itself, and classification still completes."""
    rule = (
        await client.post(
            "/api/rules",
            headers=auth,
            json={
                "name": "runaway",
                "match_type": "regex",
                # Compiles fine; catastrophic on a run of 'a's.
                "pattern": "(?:a|aa)+$",
                "priority": 1,
            },
        )
    ).json()

    doc = (
        await client.post(
            "/api/documents",
            headers=auth,
            files={"file": ("aaa.pdf", pdf_factory(text="seed"), "application/pdf")},
        )
    ).json()

    # The worker isn't running in tests, so put the pathological text on the row
    # directly — classification reads text_content.
    import sqlalchemy as sa

    from app.database import SessionLocal
    from app.models import Document

    async with SessionLocal() as session:
        await session.execute(
            sa.update(Document)
            .where(Document.id == uuid.UUID(doc["id"]))
            # Ends in a non-"a" so the anchored pattern cannot match: that is what
            # forces the exponential backtracking (a match returns fast).
            .values(text_content="a" * 300 + "b")
        )
        await session.commit()

    import time

    started = time.monotonic()
    resp = await client.post(f"/api/documents/{doc['id']}/classify", headers=auth)
    elapsed = time.monotonic() - started
    assert resp.status_code == 200
    # Bounded by the per-rule budget rather than running until the container dies.
    assert elapsed < 30

    rules = (await client.get("/api/rules", headers=auth)).json()
    offender = next(r for r in rules if r["id"] == rule["id"])
    assert offender["enabled"] is False
    assert offender["error"]

    # Editing the pattern gives it a fresh trial.
    fixed = (
        await client.patch(
            f"/api/rules/{rule['id']}", headers=auth,
            json={"pattern": "invoice", "match_type": "contains", "enabled": True},
        )
    ).json()
    assert fixed["error"] is None


async def test_rule_targets_must_belong_to_the_tenant(client, auth):
    """A rule cannot reference a tag/correspondent/type it doesn't own — that
    leaked the foreign name (and its ancestors) via classification, and an
    unknown id used to 500 as an existence oracle."""
    import uuid as _uuid

    for field in ("tag_id", "correspondent_id", "doc_type_id"):
        resp = await client.post(
            "/api/rules",
            headers=auth,
            json={
                "name": f"bad-{field}",
                "match_type": "contains",
                "pattern": "x",
                field: str(_uuid.uuid4()),
            },
        )
        assert resp.status_code == 422, (field, resp.status_code)


async def test_reprocess_refuses_a_second_concurrent_job(client, auth, pdf_factory):
    """Two jobs on one document each swap the archive pointer at the end, so the
    loser's work was silently discarded and its blob leaked."""
    import sqlalchemy as sa

    from app.database import SessionLocal
    from app.models import Job, JobStatus

    doc = (
        await client.post(
            "/api/documents", headers=auth,
            files={"file": ("dup.pdf", pdf_factory(text="reprocess guard"), "application/pdf")},
        )
    ).json()
    # The upload already queued an ingest job, so asking again must be refused.
    assert (
        await client.post(
            f"/api/documents/{doc['id']}/reprocess", headers=auth, json={"mode": "skip"}
        )
    ).status_code == 409

    # Once that job finishes, a fresh reprocess is allowed again...
    async with SessionLocal() as session:
        await session.execute(
            sa.update(Job)
            .where(Job.document_id == uuid.UUID(doc["id"]))
            .values(status=JobStatus.DONE)
        )
        await session.commit()
    assert (
        await client.post(
            f"/api/documents/{doc['id']}/reprocess", headers=auth, json={"mode": "skip"}
        )
    ).status_code == 200
    # ...and that newly queued job blocks another one.
    assert (
        await client.post(
            f"/api/documents/{doc['id']}/reprocess", headers=auth, json={"mode": "skip"}
        )
    ).status_code == 409


def test_forwarded_ip_is_only_trusted_from_a_trusted_peer():
    """The rate limiter must not key on a header any caller can set, or the
    limit is decorative for anything reaching the container directly."""
    from app.services.ratelimit import client_ip

    class _Req:
        def __init__(self, peer, headers):
            self.client = type("C", (), {"host": peer})()
            self.headers = headers

    spoofed = {"cf-connecting-ip": "203.0.113.9"}
    # Untrusted public peer: the header is ignored.
    assert client_ip(_Req("198.51.100.7", spoofed)) == "198.51.100.7"
    # Trusted proxy on the compose network: the header is honoured.
    assert client_ip(_Req("172.18.0.5", spoofed)) == "203.0.113.9"
    # No header: always the socket peer.
    assert client_ip(_Req("172.18.0.5", {})) == "172.18.0.5"


def test_watch_sweep_ignores_symlinks(tmp_path):
    """The watch share is writable by other NAS accounts; a symlink there would
    otherwise be read through and served as a document."""
    from app.services.watch import _candidates

    watch = tmp_path / "watch"
    watch.mkdir()
    (watch / "real.pdf").write_bytes(b"%PDF-1.4")
    (watch / "sneaky.pdf").symlink_to("/etc/hostname")
    names = {p.name for p in _candidates(watch)}
    assert names == {"real.pdf"}


async def test_account_rate_limit_survives_a_rotating_source_address(client):
    """Per-account limiting is what bounds guessing when the attacker can change
    source address (or spoof the forwarded header)."""
    codes = []
    for i in range(14):
        resp = await client.post(
            "/api/auth/login",
            headers={"CF-Connecting-IP": f"203.0.113.{i}"},
            json={"email": "ratelimit-target@example.com", "password": f"wrong-{i}"},
        )
        codes.append(resp.status_code)
    assert 429 in codes, codes


async def test_owner_only_actions_are_gated(client, auth, pdf_factory):
    """A second account must not be able to create co-owners, delete the owner,
    change global settings, or run import/export."""
    import sqlalchemy as sa

    from app.database import SessionLocal
    from app.models import User

    email = f"member-{uuid.uuid4().hex[:8]}@example.com"
    created = await client.post(
        "/api/auth/users", headers=auth, json={"email": email, "password": "password123"}
    )
    assert created.status_code == 201
    # Added accounts are not owners.
    async with SessionLocal() as session:
        row = (
            await session.execute(sa.select(User).where(User.email == email))
        ).scalar_one()
        assert row.is_admin is False
        owner_id = (
            await session.execute(
                sa.select(User.id).where(User.is_admin.is_(True)).limit(1)
            )
        ).scalar_one()

    tokens = (
        await client.post(
            "/api/auth/login", json={"email": email, "password": "password123"}
        )
    ).json()
    member = {"Authorization": f"Bearer {tokens['access_token']}"}

    assert (await client.get("/api/auth/me", headers=member)).json()["is_admin"] is False

    # Cannot mint another account, nor delete the owner.
    assert (
        await client.post(
            "/api/auth/users", headers=member,
            json={"email": "x@example.com", "password": "password123"},
        )
    ).status_code == 403
    assert (
        await client.delete(f"/api/auth/users/{owner_id}", headers=member)
    ).status_code == 403

    # Cannot change settings that affect the whole box, or move the library.
    assert (
        await client.post("/api/settings/archive-dpi", headers=member, json={"dpi": 150})
    ).status_code == 403
    assert (
        await client.post("/api/documents/processing", headers=member, json={"paused": True})
    ).status_code == 403
    assert (await client.post("/api/export", headers=member, json={})).status_code == 403
    assert (await client.get("/api/export", headers=member)).status_code == 403

    # But ordinary library work still works for them.
    doc = await client.post(
        "/api/documents", headers=member,
        files={"file": ("member.pdf", pdf_factory(text="member doc"), "application/pdf")},
    )
    assert doc.status_code == 201
    assert (await client.get("/api/documents", headers=member)).status_code == 200

    # The owner is still able to do all of it.
    assert (
        await client.post("/api/settings/archive-dpi", headers=auth, json={"dpi": 300})
    ).status_code == 200


async def test_remove_user_rejects_a_malformed_id(client, auth):
    """Was an uncaught ValueError -> 500."""
    assert (
        await client.delete("/api/auth/users/not-a-uuid", headers=auth)
    ).status_code == 422


async def test_duplicate_dismiss_requires_owned_documents(client, auth):
    """Accepted arbitrary UUIDs and grew junk rows: there is no FK on the pair."""
    resp = await client.post(
        "/api/insights/duplicates/dismiss",
        headers=auth,
        json={"a": str(uuid.uuid4()), "b": str(uuid.uuid4())},
    )
    assert resp.status_code == 404


async def test_logout_revokes_outstanding_tokens(client):
    """Signing out was purely client-side: the token stayed valid and renewable
    on the server, so a copy taken beforehand kept working for weeks."""
    email = f"logout-{uuid.uuid4().hex[:8]}@example.com"
    import sqlalchemy as sa

    from app.database import SessionLocal
    from app.models import User

    # Made by the owner fixture-independent path: create then log in.
    async with SessionLocal() as session:
        tenant_id = (
            await session.execute(sa.select(User.tenant_id).limit(1))
        ).scalar_one()
        from app.security import hash_password

        session.add(
            User(
                tenant_id=tenant_id,
                email=email,
                password_hash=hash_password("password123"),
            )
        )
        await session.commit()

    tokens = (
        await client.post(
            "/api/auth/login", json={"email": email, "password": "password123"}
        )
    ).json()
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    assert (await client.get("/api/documents", headers=headers)).status_code == 200

    assert (await client.post("/api/auth/logout", headers=headers)).status_code == 200

    # Both halves of the stolen pair are now dead.
    assert (await client.get("/api/documents", headers=headers)).status_code == 401
    assert (
        await client.post(
            "/api/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
        )
    ).status_code == 401


async def test_over_long_filename_is_truncated_not_a_500(client, auth, pdf_factory):
    """title/original_filename are varchar(1024) and nothing trimmed them, so a
    long name raised on flush after the blob was written — a 500 plus a leaked
    blob, and via mail an endlessly retrying poison message."""
    long_name = "a" * 3000 + ".pdf"
    resp = await client.post(
        "/api/documents",
        headers=auth,
        files={"file": (long_name, pdf_factory(text="long name"), "application/pdf")},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert len(body["title"]) <= 1024
    assert len(body["original_filename"]) <= 1024


def test_unsupported_zip_compression_does_not_propagate(tmp_path):
    """NotImplementedError from an unknown compress_type escaped extract_text and
    became a permanent ingest failure."""
    import zipfile

    from app.services import textdocs

    path = tmp_path / "weird.docx"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("word/document.xml", "<w:t>hello</w:t>")
    raw = bytearray(path.read_bytes())
    # Patch every compression-method field to an unsupported value (99).
    for marker in (b"PK\x03\x04", b"PK\x01\x02"):
        start = 0
        while (idx := raw.find(marker, start)) != -1:
            offset = idx + (8 if marker == b"PK\x03\x04" else 10)
            raw[offset : offset + 2] = (99).to_bytes(2, "little")
            start = idx + 4
    path.write_bytes(bytes(raw))
    # Must return None rather than raising.
    assert textdocs.extract_text(path) is None


def test_blob_files_are_owner_only(tmp_path):
    """The store lives on a bind-mounted NAS share; 0644 let every local account
    read every document straight off disk."""
    import os

    from app.services import storage

    src = tmp_path / "x.bin"
    src.write_bytes(b"secret")
    blob_id, _, _ = storage.store_file(src)
    path = storage.blob_file(blob_id)
    assert oct(os.stat(path).st_mode & 0o777) == "0o600"


async def _member(client, auth):
    """Create a non-owner account and return its auth header."""
    email = f"member-{uuid.uuid4().hex[:8]}@example.com"
    created = await client.post(
        "/api/auth/users", headers=auth, json={"email": email, "password": "password123"}
    )
    assert created.status_code == 201, created.text
    tokens = (
        await client.post(
            "/api/auth/login", json={"email": email, "password": "password123"}
        )
    ).json()
    return {"Authorization": f"Bearer {tokens['access_token']}"}


async def test_members_cannot_destroy_or_reconfigure(client, auth, pdf_factory):
    """The member role draws the line at irreversible and library-wide actions."""
    member = await _member(client, auth)
    doc = (
        await client.post(
            "/api/documents", headers=member,
            files={"file": ("m.pdf", pdf_factory(text="member owned"), "application/pdf")},
        )
    ).json()
    tag = (
        await client.post("/api/tags", headers=member, json={"name": _name("mtag")})
    ).json()

    # Permanent deletion — of one document, or in bulk.
    await client.delete(f"/api/documents/{doc['id']}", headers=member)  # trash is fine
    assert (
        await client.delete(f"/api/documents/{doc['id']}/purge", headers=member)
    ).status_code == 403
    assert (
        await client.post(
            "/api/documents/bulk", headers=member,
            json={"action": "purge", "filter_trash": True},
        )
    ).status_code == 403

    # Removing shared vocabulary strips it from everyone's documents.
    assert (
        await client.delete(f"/api/tags/{tag['id']}", headers=member)
    ).status_code == 403
    assert (await client.delete("/api/tags/unused", headers=member)).status_code == 403

    # Rules rewrite titles and tags library-wide.
    assert (
        await client.post(
            "/api/rules", headers=member,
            json={"name": "x", "match_type": "contains", "pattern": "y"},
        )
    ).status_code == 403

    # Library-wide reprocessing replaces archives for good.
    assert (
        await client.post("/api/documents/downsample-archives", headers=member)
    ).status_code == 403
    assert (await client.post("/api/documents/upgrade-ocr", headers=member)).status_code == 403


async def test_members_can_still_do_the_everyday_work(client, auth, pdf_factory):
    """Read and organize must keep working, or the role is useless."""
    member = await _member(client, auth)

    doc = (
        await client.post(
            "/api/documents", headers=member,
            files={"file": ("w.pdf", pdf_factory(text="everyday"), "application/pdf")},
        )
    ).json()
    tag = (
        await client.post("/api/tags", headers=member, json={"name": _name("ok")})
    ).json()

    assert (await client.get("/api/documents", headers=member)).status_code == 200
    assert (await client.get("/api/insights", headers=member)).status_code == 200
    assert (
        await client.patch(
            f"/api/documents/{doc['id']}", headers=member,
            json={"title": "renamed by member", "tag_ids": [tag["id"]]},
        )
    ).status_code == 200
    # Trash and restore are reversible, so they stay available.
    assert (
        await client.delete(f"/api/documents/{doc['id']}", headers=member)
    ).status_code == 204
    assert (
        await client.post(f"/api/documents/{doc['id']}/restore", headers=member)
    ).status_code == 200
    # Sharing is no wider than the download they already have.
    assert (
        await client.post(
            f"/api/documents/{doc['id']}/share", headers=member, json={"days": 7}
        )
    ).status_code in (200, 201)
