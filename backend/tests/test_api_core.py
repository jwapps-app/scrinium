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


async def test_tag_rename_and_clash(client, auth):
    a = (await client.post("/api/tags", headers=auth, json={"name": _name("ren-a")})).json()
    b = (await client.post("/api/tags", headers=auth, json={"name": _name("ren-b")})).json()
    renamed = await client.patch(
        f"/api/tags/{a['id']}", headers=auth, json={"name": a["name"] + "-new"}
    )
    assert renamed.status_code == 200 and renamed.json()["name"].endswith("-new")
    clash = await client.patch(
        f"/api/tags/{a['id']}", headers=auth, json={"name": b["name"]}
    )
    assert clash.status_code == 409


async def test_auto_color_tags(client, auth):
    root = (await client.post("/api/tags", headers=auth, json={"name": _name("ac-root")})).json()
    child = (
        await client.post(
            "/api/tags", headers=auth,
            json={"name": _name("ac-child"), "parent_id": root["id"]},
        )
    ).json()
    r = await client.post("/api/tags/auto-color", headers=auth)
    assert r.status_code == 200 and r.json()["colored"] >= 2
    tags = {t["id"]: t for t in (await client.get("/api/tags", headers=auth)).json()}
    root_c, child_c = tags[root["id"]]["color"], tags[child["id"]]["color"]
    assert root_c and child_c and root_c != child_c
    # child is a lighter shade: same hue family → higher lightness means
    # a strictly larger max RGB component sum for our fixed saturation
    def lum(hex_):
        return sum(int(hex_[i:i+2], 16) for i in (1, 3, 5))
    assert lum(child_c) > lum(root_c)


def test_auto_color_single_root_fanout():
    """One root with many children (the shape of a folder dump) must yield
    hues spread across the whole wheel, not shades of one color."""
    import uuid as u

    from app.services.palette import assign_palette

    root = u.uuid4()
    kids = [u.uuid4() for _ in range(20)]
    grandkids = [u.uuid4() for _ in range(3)]
    nodes = [(root, None, "root")]
    nodes += [(k, root, f"kid{i:02d}") for i, k in enumerate(kids)]
    nodes += [(g, kids[0], f"g{i}") for i, g in enumerate(grandkids)]
    palette = assign_palette(nodes)

    hues = sorted(palette[k][0] for k in kids)
    gaps = [hues[i + 1] - hues[i] for i in range(19)]
    # 20 children of a single root: evenly spread ~18° apart
    assert min(gaps) > 12, gaps
    assert max(hues) - min(hues) > 300

    # grandchildren stay inside their parent's slice and are lighter.
    # kid00 sorts first: weight 4 (itself + 3 children) of 23 total under
    # the root, so its slice is exactly [0°, 360 * 4/23].
    slice_end = 360 * 4 / 23
    for g in grandkids:
        assert 0 <= palette[g][0] <= slice_end + 0.01
        assert palette[g][2] > palette[kids[0]][2]


def test_auto_color_small_roots_dont_steal_the_wheel():
    """A big tree plus a couple of stray leaf roots (like the auto-created
    Email tag): the big tree's branches must still get real hue variety."""
    import uuid as u

    from app.services.palette import assign_palette

    main = u.uuid4()
    stray1, stray2 = u.uuid4(), u.uuid4()
    kids = [u.uuid4() for _ in range(20)]
    nodes = [(main, None, "a-main"), (stray1, None, "email"), (stray2, None, "zmisc")]
    nodes += [(k, main, f"kid{i:02d}") for i, k in enumerate(kids)]
    # give each kid a few children so the main subtree carries the weight
    for i, k in enumerate(kids):
        for j in range(3):
            nodes.append((u.uuid4(), k, f"kid{i:02d}-sub{j}"))
    palette = assign_palette(nodes)

    hues = sorted(palette[k][0] for k in kids)
    gaps = [hues[i + 1] - hues[i] for i in range(len(hues) - 1)]
    assert min(gaps) > 12, gaps  # still ~17° apart despite the stray roots


async def test_all_sort_options_work(client, auth):
    for sort in ("newest", "oldest", "docdate", "title", "updated",
                 "expires", "tag", "correspondent", "doctype", "pages", "size"):
        resp = await client.get(f"/api/documents?sort={sort}&limit=5", headers=auth)
        assert resp.status_code == 200, (sort, resp.text)
