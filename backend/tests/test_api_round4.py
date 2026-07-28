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
