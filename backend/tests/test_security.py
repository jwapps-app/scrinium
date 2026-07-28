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
