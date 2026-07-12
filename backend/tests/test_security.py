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
