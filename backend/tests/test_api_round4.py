"""TOTP, expiry dates, zip downloads, share overview, suggestions, restore."""

import uuid
from datetime import date, timedelta


def _name(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


async def upload(client, auth, pdf_bytes, filename):
    resp = await client.post(
        "/api/documents", headers=auth,
        files={"file": (filename, pdf_bytes, "application/pdf")},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_totp_verify_roundtrip():
    import time

    from app.services.totp import _code_at, new_secret, verify

    secret = new_secret()
    code = _code_at(secret, time.time())
    assert verify(secret, code)
    assert verify(secret, f" {code[:3]} {code[3:]} ")  # whitespace tolerated
    assert not verify(secret, "000000") or code == "000000"
    assert not verify(secret, "junk")


async def test_totp_login_flow(client, auth):
    import time

    from app.services.totp import _code_at

    # separate account so the shared session user keeps plain login
    email = f"{_name('totp')}@example.com"
    await client.post(
        "/api/auth/users", headers=auth,
        json={"email": email, "password": "password123"},
    )
    login = await client.post(
        "/api/auth/login", json={"email": email, "password": "password123"}
    )
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    setup = (await client.post("/api/auth/totp/setup", headers=headers)).json()
    assert setup["otpauth_url"].startswith("otpauth://totp/")
    secret = setup["secret"]

    # wrong code refused, right code enables
    bad = await client.post("/api/auth/totp/enable", headers=headers, json={"code": "000001"})
    assert bad.status_code in (403, 200) and (bad.status_code != 200)
    ok = await client.post(
        "/api/auth/totp/enable", headers=headers,
        json={"code": _code_at(secret, time.time())},
    )
    assert ok.status_code == 200

    # password alone no longer logs in
    partial = await client.post(
        "/api/auth/login", json={"email": email, "password": "password123"}
    )
    assert partial.status_code == 401 and "totp_required" in partial.text

    # A code is single use, so enabling consumed the current step: log in with
    # the next one (still inside the accepted drift window).
    next_code = _code_at(secret, time.time() + 30)
    full = await client.post(
        "/api/auth/login",
        json={"email": email, "password": "password123", "totp": next_code},
    )
    assert full.status_code == 200

    # Replaying that same code is refused.
    replay = await client.post(
        "/api/auth/login",
        json={"email": email, "password": "password123", "totp": next_code},
    )
    assert replay.status_code == 401

    # disable needs password + code
    off = await client.post(
        "/api/auth/totp/disable", headers=headers,
        json={"password": "password123", "code": _code_at(secret, time.time())},
    )
    assert off.status_code == 200
    plain = await client.post(
        "/api/auth/login", json={"email": email, "password": "password123"}
    )
    assert plain.status_code == 200


async def test_expiry_bucket(client, auth, pdf_factory):
    doc = await upload(client, auth, pdf_factory(text=_name("exp")), "e.pdf")
    soon = (date.today() + timedelta(days=10)).isoformat()
    updated = (
        await client.patch(
            f"/api/documents/{doc['id']}", headers=auth, json={"expires_on": soon}
        )
    ).json()
    assert updated["expires_on"] == soon

    listed = (
        await client.get("/api/documents?expiring=true&limit=200", headers=auth)
    ).json()
    assert any(d["id"] == doc["id"] for d in listed["items"])
    stats = (await client.get("/api/documents/stats", headers=auth)).json()
    assert stats["expiring"] >= 1

    cleared = (
        await client.patch(
            f"/api/documents/{doc['id']}", headers=auth, json={"clear_expires": True}
        )
    ).json()
    assert cleared["expires_on"] is None


async def test_download_zip_selection_and_tag(client, auth, pdf_factory):
    import io
    import zipfile

    tag = (await client.post("/api/tags", headers=auth, json={"name": _name("dl")})).json()
    ids = []
    for i in range(2):
        doc = await upload(client, auth, pdf_factory(text=_name(f"dl{i}")), f"d{i}.pdf")
        ids.append(doc["id"])
        await client.patch(
            f"/api/documents/{doc['id']}", headers=auth, json={"tag_ids": [tag["id"]]}
        )

    by_ids = await client.post(
        "/api/documents/download-zip", headers=auth, json={"ids": ids}
    )
    assert by_ids.status_code == 200
    zf = zipfile.ZipFile(io.BytesIO(by_ids.content))
    assert len(zf.namelist()) == 2

    by_tag = await client.post(
        "/api/documents/download-zip", headers=auth, json={"filter_tag_id": tag["id"]}
    )
    assert by_tag.status_code == 200
    names = zipfile.ZipFile(io.BytesIO(by_tag.content)).namelist()
    assert len(names) == 2
    assert all(tag["name"] in n for n in names)  # tag-path folders

    assert (
        await client.post("/api/documents/download-zip", headers=auth, json={})
    ).status_code == 422


async def test_share_links_overview(client, auth, pdf_factory):
    doc = await upload(client, auth, pdf_factory(text=_name("shov")), "s.pdf")
    link = (
        await client.post(f"/api/documents/{doc['id']}/share", headers=auth, json={"days": 7})
    ).json()
    listed = (await client.get("/api/share-links", headers=auth)).json()
    mine = next((l for l in listed if l["id"] == link["id"]), None)
    assert mine and mine["document_title"] == doc["title"]
    assert (
        await client.delete(f"/api/share-links/{link['id']}", headers=auth)
    ).status_code == 204
    listed = (await client.get("/api/share-links", headers=auth)).json()
    assert not any(l["id"] == link["id"] for l in listed)


async def test_search_suggestions_on_typo(client, auth, pdf_factory):
    marker = f"generatorium{uuid.uuid4().hex[:4]}"
    doc = await upload(client, auth, pdf_factory(text=_name("sg")), "g.pdf")
    await client.patch(
        f"/api/documents/{doc['id']}", headers=auth, json={"title": f"{marker} manual"}
    )
    typo = marker[:-4] + "uim" + marker[-1]  # scrambled tail
    resp = (await client.get(f"/api/search?q={typo}", headers=auth)).json()
    assert resp["results"] == []
    assert any(marker.lower() in s.lower() for s in resp["suggestions"]), resp


async def test_scrinium_restore(client, auth, pdf_factory):
    import json
    import os
    import shutil
    import uuid as u

    import sqlalchemy as sa

    from app.database import SessionLocal
    from app.models import Document
    from app.services.paperless_import import run_import

    data_dir = os.environ["DATA_DIR"]
    import_dir = os.path.join(data_dir, "import", "restore-test")
    shutil.rmtree(os.path.join(data_dir, "import"), ignore_errors=True)
    os.makedirs(os.path.join(import_dir, "originals", "Water"), exist_ok=True)

    pdf = pdf_factory(text=u.uuid4().hex)
    original_rel = "originals/Water/Well Guide.pdf"
    with open(os.path.join(import_dir, original_rel), "wb") as f:
        f.write(pdf)
    manifest = {
        "app": "Scrinium",
        "documents": [
            {
                "title": "Well Guide",
                "original_filename": "well-guide.pdf",
                "original": original_rel,
                "archive": None,
                "page_count": 1,
                "doc_date": "2021-05-04",
                "correspondent": "County Office",
                "doc_type": "Guide",
                "tags": ["RestoreWater"],
                "notes": "restored note",
            }
        ],
    }
    with open(os.path.join(import_dir, "manifest.json"), "w") as f:
        json.dump(manifest, f)

    async with SessionLocal() as session:
        tenant_id = (
            await session.execute(sa.select(Document.tenant_id).limit(1))
        ).scalar_one()

    await run_import(tenant_id)

    listed = (
        await client.get("/api/documents?limit=500", headers=auth)
    ).json()
    doc = next((d for d in listed["items"] if d["title"] == "Well Guide"), None)
    assert doc is not None
    assert doc["doc_date"] == "2021-05-04"
    assert doc["correspondent_name"] == "County Office"
    assert doc["doc_type_name"] == "Guide"
    assert doc["notes"] == "restored note"
    assert any(t["name"] == "RestoreWater" for t in doc["tags"])

    # idempotent: re-running skips
    await run_import(tenant_id)
    listed = (
        await client.get("/api/documents?limit=500", headers=auth)
    ).json()
    assert sum(1 for d in listed["items"] if d["title"] == "Well Guide") == 1


async def test_search_pages_and_reports_the_true_total(client, auth, pdf_factory):
    """The first page is not the whole answer.

    A common word matches far more documents than one page holds, and with no
    total and no way to page, a document ranked past the cut looks as though
    it is not in the library at all — which is exactly how a large reference
    work goes missing while an in-document search still finds it.
    """
    marker = f"quincewort{uuid.uuid4().hex[:6]}"
    for i in range(3):
        doc = await upload(client, auth, pdf_factory(text=marker), f"q{i}.pdf")
        await client.patch(
            f"/api/documents/{doc['id']}",
            headers=auth,
            json={"title": f"{marker} volume {i}"},
        )

    first = (
        await client.get(f"/api/search?q={marker}&limit=2", headers=auth)
    ).json()
    assert len(first["results"]) == 2
    assert first["total"] == 3, "total counts every match, not the page"
    assert first["offset"] == 0

    second = (
        await client.get(f"/api/search?q={marker}&limit=2&offset=2", headers=auth)
    ).json()
    assert len(second["results"]) == 1
    assert second["total"] == 3
    assert second["offset"] == 2

    # Paging must not repeat or drop a row, even when ranks tie.
    ids = [r["id"] for r in first["results"]] + [r["id"] for r in second["results"]]
    assert len(set(ids)) == 3, "a document appeared on two pages or on neither"



async def test_long_document_outranks_a_passing_mention(client, auth, pdf_factory):
    """The bug this whole table exists for.

    ts_rank reads token positions and Postgres records them only for roughly
    the first 16,383 words, so a long book was scored on its opening pages. A
    volume that mentions the term throughout must beat one that mentions it
    once in passing, however large either is.
    """
    from sqlalchemy import select

    from app.database import SessionLocal
    from app.models import Document
    from app.services import page_index

    term = f"blueberrium{uuid.uuid4().hex[:6]}"
    filler = "orchard husbandry and the tending of trees. " * 400

    # Both documents are long. One mentions the term on many pages, the other
    # once, buried past the position window.
    throughout = "\f".join(f"{filler} {term} more text here." for _ in range(12))
    passing = "\f".join(
        (f"{filler} {term} only here." if i == 11 else filler) for i in range(12)
    )

    ids = {}
    for label, body in (("throughout", throughout), ("passing", passing)):
        doc = await upload(client, auth, pdf_factory(text="placeholder"), f"{label}.pdf")
        ids[label] = doc["id"]
        async with SessionLocal() as session:
            row = (
                await session.execute(
                    select(Document).where(Document.id == uuid.UUID(doc["id"]))
                )
            ).scalar_one()
            row.text_content = body
            row.title = f"{label} volume"
            await page_index.reindex_pages(session, row)
            await session.commit()

    resp = (await client.get(f"/api/search?q={term}", headers=auth)).json()
    order = [r["id"] for r in resp["results"]]
    assert ids["throughout"] in order and ids["passing"] in order, resp
    assert order.index(ids["throughout"]) < order.index(ids["passing"]), (
        "a document mentioning the term throughout must outrank a passing "
        f"mention: {[(r['title'], r['rank'], r['pages_hit']) for r in resp['results']]}"
    )

    hits = {r["id"]: r["pages_hit"] for r in resp["results"]}
    assert hits[ids["throughout"]] == 12
    assert hits[ids["passing"]] == 1


async def test_reindex_sees_text_assigned_but_not_yet_committed():
    """The page split runs in SQL against documents.text_content, and these
    sessions have autoflush off — so text the caller has only assigned in
    memory (exactly what the ingest path does) must still be flushed first.
    Without it a freshly OCR'd document indexes the previous value, which is
    NULL, and silently gets no pages at all."""
    from sqlalchemy import func, select

    from app.database import SessionLocal
    from app.models import Document, DocumentPage, Tenant
    from app.services import page_index

    async with SessionLocal() as session:
        tenant = (await session.execute(select(Tenant).limit(1))).scalar_one()
        doc = Document(
            tenant_id=tenant.id,
            title="flush check",
            original_filename="flush.pdf",
            original_blob_id=(
                await session.execute(select(Document.original_blob_id).limit(1))
            ).scalar_one(),
        )
        session.add(doc)
        await session.flush()

        # Assigned, deliberately not committed — the ingest path's exact shape.
        doc.text_content = "alpha page\fbeta page\fgamma page"
        written = await page_index.reindex_pages(session, doc)
        assert written == 3, "pending text must be visible to the split"

        rows = (
            await session.execute(
                select(func.count())
                .select_from(DocumentPage)
                .where(DocumentPage.document_id == doc.id)
            )
        ).scalar_one()
        assert rows == 3
        await session.rollback()


async def test_a_huge_vocabulary_document_still_ingests_and_is_searchable(
    client, auth, pdf_factory
):
    """The bug that took the whole ingest down.

    A tsvector caps at 1,048,575 bytes and its size tracks distinct lexemes,
    not characters — so an encyclopedia blew past the ceiling where a far
    larger catalogue did not. The whole-document generated column made that
    Postgres's problem with the UPDATE that wrote the OCR text, so the write
    failed, the exception escaped, and the job sat at RUNNING forever.
    """
    from sqlalchemy import func, select

    from app.database import SessionLocal
    from app.models import Document, DocumentPage
    from app.services import page_index

    term = f"zephyrine{uuid.uuid4().hex[:6]}"
    # Distinct words are what costs vector space; make a lot of them, spread
    # over pages, with the search term deep inside rather than at the front.
    pages = []
    for page in range(60):
        words = " ".join(f"w{page}x{i}word{i * 7}" for i in range(700))
        pages.append(f"{words} {term}" if page > 40 else words)
    body = "\f".join(pages)

    doc = await upload(client, auth, pdf_factory(text="placeholder"), "huge.pdf")
    async with SessionLocal() as session:
        row = (
            await session.execute(
                select(Document).where(Document.id == uuid.UUID(doc["id"]))
            )
        ).scalar_one()
        row.text_content = body
        row.title = "vocabulary heavy volume"
        await page_index.reindex_pages(session, row)
        await session.commit()  # must not raise on the whole-document vector

        rows = (
            await session.execute(
                select(func.count())
                .select_from(DocumentPage)
                .where(DocumentPage.document_id == row.id)
            )
        ).scalar_one()
        assert rows == 60

    # Found on a term that appears only past page 40 — nothing truncated.
    resp = (await client.get(f"/api/search?q={term}", headers=auth)).json()
    assert doc["id"] in [r["id"] for r in resp["results"]], resp
    hit = next(r for r in resp["results"] if r["id"] == doc["id"])
    assert hit["pages_hit"] == 19, hit


async def test_backfill_ignores_documents_whose_text_is_only_page_breaks():
    """OCR that produced nothing yields no rows however often it is indexed.
    Without excluding it, the sweep reselects it every pass and the backfill
    never reports itself finished."""
    from sqlalchemy import select

    from app.database import SessionLocal
    from app.models import Document, Tenant
    from app.services import page_index

    async with SessionLocal() as session:
        tenant = (await session.execute(select(Tenant).limit(1))).scalar_one()
        blob = (
            await session.execute(select(Document.original_blob_id).limit(1))
        ).scalar_one()
        doc = Document(
            tenant_id=tenant.id, title="empty scan",
            original_filename="e.pdf", original_blob_id=blob,
            text_content="\f\f\f",
        )
        session.add(doc)
        await session.flush()

        assert await page_index.reindex_pages(session, doc) == 0
        pending = await page_index.documents_missing_pages(session, limit=500)
        assert doc.id not in pending, "would be reselected forever"
        await session.rollback()
