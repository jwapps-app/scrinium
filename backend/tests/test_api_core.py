"""API workflows against the real (migrated) schema: auth, ingest + dedup,
trash lifecycle, tag hierarchy, bulk actions."""

import uuid


def _name(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


async def upload(client, auth, pdf_bytes, filename):
    return await client.post(
        "/api/documents",
        headers=auth,
        files={"file": (filename, pdf_bytes, "application/pdf")},
    )


async def test_setup_only_once(client, token):
    resp = await client.post(
        "/api/auth/setup", json={"email": "x@y.z", "password": "password123"}
    )
    assert resp.status_code == 403


async def test_auth_required(client):
    resp = await client.get("/api/documents")
    assert resp.status_code == 401


async def test_upload_dedup_and_reject(client, auth, pdf_factory):
    pdf = pdf_factory(text=_name("dedup"))
    first = await upload(client, auth, pdf, "a.pdf")
    assert first.status_code == 201
    assert first.json()["status"] == "pending"

    dup = await upload(client, auth, pdf, "same-bytes-other-name.pdf")
    assert dup.status_code == 409

    bad = await client.post(
        "/api/documents", headers=auth,
        files={"file": ("evil.exe", b"MZ", "application/octet-stream")},
    )
    assert bad.status_code == 415


async def test_trash_lifecycle(client, auth, pdf_factory):
    doc = (await upload(client, auth, pdf_factory(text=_name("trash")), "t.pdf")).json()

    # soft delete → hidden from default list, counted in trash
    assert (await client.delete(f"/api/documents/{doc['id']}", headers=auth)).status_code == 204
    listing = (await client.get("/api/documents?limit=200", headers=auth)).json()
    assert doc["id"] not in [d["id"] for d in listing["items"]]
    stats = (await client.get("/api/documents/stats", headers=auth)).json()
    assert stats["trash"] >= 1

    # restore brings it back
    restored = await client.post(f"/api/documents/{doc['id']}/restore", headers=auth)
    assert restored.status_code == 200 and restored.json()["deleted_at"] is None

    # purge removes it for good
    assert (await client.delete(f"/api/documents/{doc['id']}", headers=auth)).status_code == 204
    assert (await client.delete(f"/api/documents/{doc['id']}/purge", headers=auth)).status_code in (200, 204)
    gone = await client.get(f"/api/documents/{doc['id']}", headers=auth)
    assert gone.status_code == 404


async def test_tag_hierarchy_and_cycle_guard(client, auth, pdf_factory):
    parent = (await client.post("/api/tags", headers=auth, json={"name": _name("parent")})).json()
    child = (
        await client.post(
            "/api/tags", headers=auth,
            json={"name": _name("child"), "parent_id": parent["id"]},
        )
    ).json()

    # making the parent a child of its own child must be refused
    cycle = await client.patch(
        f"/api/tags/{parent['id']}", headers=auth, json={"parent_id": child["id"]}
    )
    assert cycle.status_code == 422

    # applying the child tag materializes the ancestor too
    doc = (await upload(client, auth, pdf_factory(text=_name("tags")), "tg.pdf")).json()
    updated = (
        await client.patch(
            f"/api/documents/{doc['id']}", headers=auth,
            json={"tag_ids": [child["id"]]},
        )
    ).json()
    names = {t["id"] for t in updated["tags"]}
    assert child["id"] in names and parent["id"] in names


async def test_tag_color(client, auth):
    tag = (await client.post("/api/tags", headers=auth, json={"name": _name("color")})).json()
    updated = (
        await client.patch(f"/api/tags/{tag['id']}", headers=auth, json={"color": "#1f78b4"})
    ).json()
    assert updated["color"] == "#1f78b4"
    cleared = (
        await client.patch(f"/api/tags/{tag['id']}", headers=auth, json={"clear_color": True})
    ).json()
    assert cleared["color"] is None


async def test_bulk_actions(client, auth, pdf_factory):
    ids = []
    for i in range(2):
        ids.append(
            (await upload(client, auth, pdf_factory(text=_name(f"bulk{i}")), f"b{i}.pdf")).json()["id"]
        )
    tag = (await client.post("/api/tags", headers=auth, json={"name": _name("bulktag")})).json()
    corr = (await client.post("/api/correspondents", headers=auth, json={"name": _name("corr")})).json()

    r = await client.post(
        "/api/documents/bulk", headers=auth,
        json={"ids": ids, "action": "add_tags", "tag_ids": [tag["id"]]},
    )
    assert r.json()["processed"] == 2

    r = await client.post(
        "/api/documents/bulk", headers=auth,
        json={"ids": ids, "action": "set_correspondent", "correspondent_id": corr["id"]},
    )
    assert r.json()["processed"] == 2
    doc = (await client.get(f"/api/documents/{ids[0]}", headers=auth)).json()
    assert doc["correspondent_name"] == corr["name"]
    assert tag["id"] in [t["id"] for t in doc["tags"]]

    # clearing via the same action with no id
    await client.post(
        "/api/documents/bulk", headers=auth, json={"ids": ids, "action": "set_correspondent"}
    )
    doc = (await client.get(f"/api/documents/{ids[0]}", headers=auth)).json()
    assert doc["correspondent_name"] is None

    r = await client.post(
        "/api/documents/bulk", headers=auth, json={"ids": ids, "action": "delete"}
    )
    assert r.json()["processed"] == 2


async def test_notes_roundtrip(client, auth, pdf_factory):
    doc = (await upload(client, auth, pdf_factory(text=_name("notes")), "n.pdf")).json()
    updated = (
        await client.patch(
            f"/api/documents/{doc['id']}", headers=auth, json={"notes": "remember this"}
        )
    ).json()
    assert updated["notes"] == "remember this"
    cleared = (
        await client.patch(f"/api/documents/{doc['id']}", headers=auth, json={"notes": ""})
    ).json()
    assert cleared["notes"] is None


async def test_insights_shape(client, auth):
    data = (await client.get("/api/insights", headers=auth)).json()
    for key in ("documents", "pages", "storage_bytes", "monthly", "tags", "engines"):
        assert key in data
    assert data["documents"] >= 1
