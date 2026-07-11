"""API workflows: share links, classification rules, saved views, custom
fields, page operations."""

import uuid


def _name(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


async def upload(client, auth, pdf_bytes, filename):
    resp = await client.post(
        "/api/documents", headers=auth,
        files={"file": (filename, pdf_bytes, "application/pdf")},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def test_share_link_lifecycle(client, auth, pdf_factory):
    doc = await upload(client, auth, pdf_factory(text=_name("share")), "s.pdf")

    link = (
        await client.post(f"/api/documents/{doc['id']}/share", headers=auth, json={"days": 7})
    ).json()
    assert link["url_path"] == f"/share/{link['token']}"

    # public, no auth at all
    meta = await client.get(f"/api/share/{link['token']}")
    assert meta.status_code == 200 and meta.json()["title"] == doc["title"]
    body = await client.get(f"/api/share/{link['token']}/file")
    assert body.status_code == 200
    assert body.headers["content-type"].startswith("application/pdf")

    listed = (await client.get(f"/api/documents/{doc['id']}/share", headers=auth)).json()
    assert len(listed) == 1

    # revoke → indistinguishable 404
    assert (
        await client.delete(f"/api/documents/{doc['id']}/share", headers=auth)
    ).status_code == 204
    assert (await client.get(f"/api/share/{link['token']}")).status_code == 404
    assert (await client.get(f"/api/share/{link['token']}/file")).status_code == 404


async def test_share_link_of_trashed_doc_is_dead(client, auth, pdf_factory):
    doc = await upload(client, auth, pdf_factory(text=_name("shtrash")), "st.pdf")
    link = (
        await client.post(f"/api/documents/{doc['id']}/share", headers=auth, json={"days": 7})
    ).json()
    await client.delete(f"/api/documents/{doc['id']}", headers=auth)
    assert (await client.get(f"/api/share/{link['token']}")).status_code == 404


async def test_rules_classify(client, auth, pdf_factory):
    marker = _name("acme").replace("-", "")
    tagname = _name("ruletag")
    rule = await client.post(
        "/api/rules", headers=auth,
        json={
            "name": _name("rule"),
            "match_type": "contains",
            "pattern": marker,
            "tag_id": (
                await client.post("/api/tags", headers=auth, json={"name": tagname})
            ).json()["id"],
        },
    )
    assert rule.status_code in (200, 201), rule.text

    # pattern matches the filename → classification applies the tag
    doc = await upload(
        client, auth, pdf_factory(text=_name("classify")), f"{marker}-invoice.pdf"
    )
    result = (
        await client.post(f"/api/documents/{doc['id']}/classify", headers=auth)
    ).json()
    assert tagname in result["added_tags"]
    assert any(t["name"] == tagname for t in result["document"]["tags"])


async def test_saved_views_crud(client, auth):
    view = (
        await client.post(
            "/api/views", headers=auth,
            json={"name": _name("view"), "params": "status=ready&sort=docdate"},
        )
    ).json()
    listed = (await client.get("/api/views", headers=auth)).json()
    assert view["id"] in [v["id"] for v in listed]
    assert (
        await client.delete(f"/api/views/{view['id']}", headers=auth)
    ).status_code in (200, 204)


async def test_custom_fields(client, auth, pdf_factory):
    field = (
        await client.post(
            "/api/custom-fields", headers=auth,
            json={"name": _name("amount"), "kind": "money"},
        )
    ).json()
    doc = await upload(client, auth, pdf_factory(text=_name("cf")), "cf.pdf")
    updated = (
        await client.patch(
            f"/api/documents/{doc['id']}", headers=auth,
            json={"custom_values": {field["id"]: "84.50"}},
        )
    ).json()
    assert updated["custom_values"][field["id"]] == "84.50"
    # empty string clears
    cleared = (
        await client.patch(
            f"/api/documents/{doc['id']}", headers=auth,
            json={"custom_values": {field["id"]: ""}},
        )
    ).json()
    assert field["id"] not in cleared["custom_values"]


async def test_page_operations(client, auth, pdf_factory):
    doc = await upload(client, auth, pdf_factory(pages=5, text=_name("pages")), "p5.pdf")

    # extract two pages → new document
    r = await client.post(
        f"/api/documents/{doc['id']}/pages", headers=auth,
        json={"action": "extract", "pages": [2, 3], "title": _name("slice")},
    )
    assert r.status_code == 200, r.text
    new_id = r.json()["new_document_id"]
    assert (await client.get(f"/api/documents/{new_id}", headers=auth)).status_code == 200

    # rotate re-queues OCR with a fresh original
    r = await client.post(
        f"/api/documents/{doc['id']}/pages", headers=auth,
        json={"action": "rotate", "pages": [1], "degrees": 90},
    )
    assert r.status_code == 200
    assert r.json()["document"]["status"] == "pending"

    # guardrails
    assert (
        await client.post(
            f"/api/documents/{doc['id']}/pages", headers=auth,
            json={"action": "delete", "pages": [1, 2, 3, 4, 5]},
        )
    ).status_code == 400
    assert (
        await client.post(
            f"/api/documents/{doc['id']}/pages", headers=auth,
            json={"action": "rotate", "pages": [99]},
        )
    ).status_code == 400


async def test_ocr_engine_toggle_guard(client, auth):
    # apple without APPLE_OCR_URL configured must be refused
    r = await client.post("/api/settings/ocr", headers=auth, json={"engine": "apple"})
    assert r.status_code == 400
    r = await client.post("/api/settings/ocr", headers=auth, json={"engine": "nonsense"})
    assert r.status_code == 400
    r = await client.post("/api/settings/ocr", headers=auth, json={"engine": "tesseract"})
    assert r.status_code == 200
    r = await client.post("/api/settings/ocr", headers=auth, json={"engine": ""})
    assert r.status_code == 200
