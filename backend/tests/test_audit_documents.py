"""Audit fixes in the documents router: chunked-upload completion (F33),
filter-aware bundle downloads (F34), hard selection limits instead of silent
truncation (F35) and unique zip member names (F06).

The iOS app sends {"ids"} or {"filter_tag_id"} to /binder and /download-zip
and the web client sends only {"filename"} at completion, so every test that
adds a field also keeps the old shape passing.
"""

import hashlib
import io
import uuid
import zipfile

import pytest

from app.routers import documents as docs_router


def _name(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


async def upload(client, auth, pdf_bytes, filename):
    return await client.post(
        "/api/documents", headers=auth,
        files={"file": (filename, pdf_bytes, "application/pdf")},
    )


async def _session_with_parts(client, auth, parts):
    """Open a chunked session and PUT the given parts at the given indexes."""
    upload_id = (await client.post("/api/documents/uploads", headers=auth)).json()["upload_id"]
    for index, data in parts:
        r = await client.put(
            f"/api/documents/uploads/{upload_id}/{index}", headers=auth, content=data
        )
        assert r.status_code == 200 and r.json()["received"] == len(data)
    return upload_id


async def _complete(client, auth, upload_id, **body):
    return await client.post(
        f"/api/documents/uploads/{upload_id}/complete", headers=auth,
        json={"filename": f"{_name('chunked')}.pdf", **body},
    )


# --- F33: chunk completion ---------------------------------------------------


async def test_chunked_upload_happy_path_is_unchanged(client, auth, pdf_factory):
    """What the web client sends today: two parts, then a filename."""
    pdf = pdf_factory(text=_name("chunk"))
    half = len(pdf) // 2
    upload_id = await _session_with_parts(client, auth, [(0, pdf[:half]), (1, pdf[half:])])
    r = await _complete(client, auth, upload_id)
    assert r.status_code == 201, r.text
    assert not docs_router._upload_session_dir(uuid.UUID(upload_id)).exists()
    assert uuid.UUID(upload_id) not in docs_router._completion_locks


async def test_chunked_upload_with_declared_parts_size_and_hash(client, auth, pdf_factory):
    pdf = pdf_factory(text=_name("chunk"))
    half = len(pdf) // 2
    upload_id = await _session_with_parts(client, auth, [(0, pdf[:half]), (1, pdf[half:])])
    r = await _complete(
        client, auth, upload_id,
        parts=2, size=len(pdf), sha256=hashlib.sha256(pdf).hexdigest(),
    )
    assert r.status_code == 201, r.text


async def test_a_missing_middle_part_is_refused(client, auth, pdf_factory):
    """Parts 0 and 2 without 1 used to be concatenated into a truncated
    document that then counted as successfully stored."""
    pdf = pdf_factory(text=_name("chunk"))
    third = len(pdf) // 3
    upload_id = await _session_with_parts(
        client, auth, [(0, pdf[:third]), (2, pdf[2 * third:])]
    )
    r = await _complete(client, auth, upload_id)
    assert r.status_code == 400
    assert "Chunk 1" in r.json()["detail"]
    # The session survives a refusal so the client can send the missing part.
    assert docs_router._upload_session_dir(uuid.UUID(upload_id)).exists()


async def test_a_declared_part_count_that_disagrees_is_refused(client, auth, pdf_factory):
    pdf = pdf_factory(text=_name("chunk"))
    half = len(pdf) // 2
    upload_id = await _session_with_parts(client, auth, [(0, pdf[:half]), (1, pdf[half:])])
    r = await _complete(client, auth, upload_id, parts=3)
    assert r.status_code == 400
    assert "3" in r.json()["detail"] and "2" in r.json()["detail"]


async def test_a_declared_size_or_hash_that_disagrees_is_refused(client, auth, pdf_factory):
    pdf = pdf_factory(text=_name("chunk"))
    half = len(pdf) // 2
    upload_id = await _session_with_parts(client, auth, [(0, pdf[:half]), (1, pdf[half:])])
    assert (await _complete(client, auth, upload_id, size=len(pdf) + 1)).status_code == 400
    wrong = hashlib.sha256(b"not the file").hexdigest()
    assert (await _complete(client, auth, upload_id, sha256=wrong)).status_code == 400
    # Nothing was consumed by the refusals; the correct values still finish it.
    assert (await _complete(client, auth, upload_id, size=len(pdf))).status_code == 201


async def test_a_part_still_being_written_blocks_completion(client, auth, pdf_factory):
    """A .tmp left by an in-flight or crashed PUT is never concatenated;
    re-sending that index replaces it and completion goes through."""
    pdf = pdf_factory(text=_name("chunk"))
    half = len(pdf) // 2
    upload_id = await _session_with_parts(client, auth, [(0, pdf[:half])])
    session_dir = docs_router._upload_session_dir(uuid.UUID(upload_id))
    (session_dir / "part-00001.tmp").write_bytes(pdf[half:half + 10])
    r = await _complete(client, auth, upload_id)
    assert r.status_code == 400
    assert "still being written" in r.json()["detail"]
    r = await client.put(
        f"/api/documents/uploads/{upload_id}/1", headers=auth, content=pdf[half:]
    )
    assert r.status_code == 200
    assert not (session_dir / "part-00001.tmp").exists()
    assert (await _complete(client, auth, upload_id)).status_code == 201


# --- F34: whole-filter downloads ---------------------------------------------


async def _tagged_docs_for_two_correspondents(client, auth, pdf_factory):
    tag = (await client.post("/api/tags", headers=auth, json={"name": _name("ft")})).json()
    corrs = [
        (await client.post("/api/correspondents", headers=auth, json={"name": _name(f"c{i}")})).json()
        for i in range(2)
    ]
    ids = {0: [], 1: []}
    for which, count in ((0, 2), (1, 1)):
        for _ in range(count):
            doc = (await upload(client, auth, pdf_factory(text=_name("ft")), f"{_name('ft')}.pdf")).json()
            await client.patch(
                f"/api/documents/{doc['id']}", headers=auth,
                json={"tag_ids": [tag["id"]], "correspondent_id": corrs[which]["id"]},
            )
            ids[which].append(doc["id"])
    return tag, corrs, ids


async def test_zip_of_a_filter_honours_more_than_the_tag(client, auth, pdf_factory):
    """Two correspondents share one tag; the view filtered to tag + first
    correspondent must download exactly that correspondent's documents."""
    tag, corrs, ids = await _tagged_docs_for_two_correspondents(client, auth, pdf_factory)

    r = await client.post(
        "/api/documents/download-zip", headers=auth,
        json={"filter": {"tag_id": tag["id"], "correspondent_id": corrs[0]["id"]}},
    )
    assert r.status_code == 200, r.text
    assert len(zipfile.ZipFile(io.BytesIO(r.content)).namelist()) == len(ids[0]) == 2

    # The tag-only spelling the iOS app sends still means the whole tag.
    r = await client.post(
        "/api/documents/download-zip", headers=auth, json={"filter_tag_id": tag["id"]}
    )
    assert r.status_code == 200
    assert len(zipfile.ZipFile(io.BytesIO(r.content)).namelist()) == 3


# --- F35: selection limits ---------------------------------------------------


async def test_a_binder_over_the_id_limit_is_refused_with_the_limit(client, auth):
    ids = [str(uuid.uuid4()) for _ in range(docs_router.BINDER_MAX_DOCUMENTS + 1)]
    r = await client.post("/api/documents/binder", headers=auth, json={"ids": ids, "title": "Too many"})
    assert r.status_code == 400
    assert str(docs_router.BINDER_MAX_DOCUMENTS) in r.json()["detail"]


async def test_a_zip_over_the_id_limit_is_refused_with_the_limit(client, auth):
    ids = [str(uuid.uuid4()) for _ in range(docs_router.ZIP_MAX_IDS + 1)]
    r = await client.post("/api/documents/download-zip", headers=auth, json={"ids": ids})
    assert r.status_code == 400
    assert str(docs_router.ZIP_MAX_IDS) in r.json()["detail"]


async def test_a_filter_wider_than_the_limit_is_refused_not_truncated(
    client, auth, pdf_factory, monkeypatch
):
    """Three documents under a tag against a ceiling of two: previously the
    third was dropped from the file with no indication."""
    tag, corrs, ids = await _tagged_docs_for_two_correspondents(client, auth, pdf_factory)
    monkeypatch.setattr(docs_router, "ZIP_MAX_FILTERED", 2)
    monkeypatch.setattr(docs_router, "BINDER_MAX_DOCUMENTS", 2)

    for path in ("/api/documents/download-zip", "/api/documents/binder"):
        r = await client.post(path, headers=auth, json={"filter_tag_id": tag["id"]})
        assert r.status_code == 400, (path, r.text)
        assert "more than 2" in r.json()["detail"]

    # A narrower filter under the ceiling still works.
    r = await client.post(
        "/api/documents/download-zip", headers=auth,
        json={"filter": {"tag_id": tag["id"], "correspondent_id": corrs[0]["id"]}},
    )
    assert r.status_code == 200


# --- F06: zip member names ---------------------------------------------------


async def test_zip_members_are_unique_even_when_titles_collide_with_suffixes(
    client, auth, pdf_factory
):
    """x, x and x (2) used to yield two members named x (2).pdf."""
    base = _name("dup")
    ids = []
    for title in (base, base, f"{base} (2)"):
        doc = (await upload(client, auth, pdf_factory(text=_name("dup")), f"{_name('dup')}.pdf")).json()
        await client.patch(f"/api/documents/{doc['id']}", headers=auth, json={"title": title})
        ids.append(doc["id"])

    r = await client.post("/api/documents/download-zip", headers=auth, json={"ids": ids})
    assert r.status_code == 200
    names = zipfile.ZipFile(io.BytesIO(r.content)).namelist()
    assert len(names) == 3
    assert len({n.lower() for n in names}) == 3, names
    assert f"{base}.pdf" in names and f"{base} (2).pdf" in names
