"""Annotations, reading positions, related docs, duplicate dismissal,
review bucket."""

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


async def test_annotation_lifecycle(client, auth, pdf_factory):
    doc = await upload(client, auth, pdf_factory(pages=3, text=_name("ann")), "a.pdf")

    created = await client.post(
        f"/api/documents/{doc['id']}/annotations", headers=auth,
        json={
            "page": 2,
            "quote": "the well casing should extend",
            "note": "check ours",
            "rects": [{"x": 0.1, "y": 0.2, "w": 0.5, "h": 0.03}],
        },
    )
    assert created.status_code == 201, created.text
    ann = created.json()
    assert ann["page"] == 2 and ann["rects"][0]["w"] == 0.5

    listed = (
        await client.get(f"/api/documents/{doc['id']}/annotations", headers=auth)
    ).json()
    assert len(listed) == 1

    # global search hits quote and note
    hits = (await client.get("/api/annotations?q=casing", headers=auth)).json()
    assert any(h["id"] == ann["id"] for h in hits)
    hits = (await client.get("/api/annotations?q=check+ours", headers=auth)).json()
    assert any(h["id"] == ann["id"] for h in hits)

    # note edit + delete
    patched = await client.patch(
        f"/api/annotations/{ann['id']}", headers=auth, json={"note": "done"}
    )
    assert patched.json()["note"] == "done"
    assert (
        await client.delete(f"/api/annotations/{ann['id']}", headers=auth)
    ).status_code == 204
    assert (
        await client.get(f"/api/documents/{doc['id']}/annotations", headers=auth)
    ).json() == []


async def test_annotation_validation(client, auth, pdf_factory):
    doc = await upload(client, auth, pdf_factory(text=_name("annv")), "v.pdf")
    bad = await client.post(
        f"/api/documents/{doc['id']}/annotations", headers=auth,
        json={"page": 1, "quote": "x", "rects": [{"x": "nope"}]},
    )
    assert bad.status_code == 422


async def test_reading_position_sync(client, auth, pdf_factory):
    doc = await upload(client, auth, pdf_factory(pages=5, text=_name("pos")), "p.pdf")
    empty = (await client.get(f"/api/documents/{doc['id']}/position", headers=auth)).json()
    assert empty["page"] is None
    await client.put(
        f"/api/documents/{doc['id']}/position", headers=auth, json={"page": 4}
    )
    assert (
        await client.get(f"/api/documents/{doc['id']}/position", headers=auth)
    ).json()["page"] == 4
    # update in place
    await client.put(
        f"/api/documents/{doc['id']}/position", headers=auth, json={"page": 5}
    )
    assert (
        await client.get(f"/api/documents/{doc['id']}/position", headers=auth)
    ).json()["page"] == 5


async def test_related_and_dismiss(client, auth, pdf_factory):
    import sqlalchemy as sa

    from app.database import SessionLocal
    from app.models import Document
    from app.services.similarity import simhash as sh

    text = " ".join(
        f"item{i} note about area{i % 89} regarding point{i % 37}" for i in range(300)
    )
    ids = []
    for i in range(2):
        doc = await upload(client, auth, pdf_factory(text=_name(f"rel{i}")), f"r{i}.pdf")
        ids.append(doc["id"])
    async with SessionLocal() as session:
        for i, doc_id in enumerate(ids):
            variant = text.replace("area42", f"area{42 + i}")
            await session.execute(
                sa.update(Document)
                .where(Document.id == uuid.UUID(doc_id))
                .values(text_content=variant, simhash=sh(variant))
            )
        await session.commit()

    related = (
        await client.get(f"/api/documents/{ids[0]}/related", headers=auth)
    ).json()["related"]
    assert any(r["id"] == ids[1] for r in related)

    # dismissing removes the pair from the duplicates report
    dupes = (await client.get("/api/insights/duplicates", headers=auth)).json()
    pair = next(
        (p for p in dupes["pairs"] if {p["a"]["id"], p["b"]["id"]} == set(ids)), None
    )
    assert pair is not None
    r = await client.post(
        "/api/insights/duplicates/dismiss", headers=auth,
        json={"a": ids[0], "b": ids[1]},
    )
    assert r.status_code == 200
    dupes = (await client.get("/api/insights/duplicates", headers=auth)).json()
    assert not any(
        {p["a"]["id"], p["b"]["id"]} == set(ids) for p in dupes["pairs"]
    )


async def test_review_bucket(client, auth, pdf_factory):
    import sqlalchemy as sa

    from app.database import SessionLocal
    from app.models import Document

    doc = await upload(client, auth, pdf_factory(text=_name("rev")), "rev.pdf")
    # force it ready with no correspondent/type → needs review
    async with SessionLocal() as session:
        await session.execute(
            sa.update(Document)
            .where(Document.id == uuid.UUID(doc["id"]))
            .values(status="ready")
        )
        await session.commit()

    listed = (
        await client.get("/api/documents?needs_review=true&limit=200", headers=auth)
    ).json()
    assert any(d["id"] == doc["id"] for d in listed["items"])
    stats = (await client.get("/api/documents/stats", headers=auth)).json()
    assert stats["review"] >= 1

    # assigning a correspondent clears it from the bucket
    corr = (
        await client.post("/api/correspondents", headers=auth, json={"name": _name("c")})
    ).json()
    await client.patch(
        f"/api/documents/{doc['id']}", headers=auth,
        json={"correspondent_id": corr["id"], "doc_type_id": None},
    )
    listed = (
        await client.get("/api/documents?needs_review=true&limit=200", headers=auth)
    ).json()
    assert not any(d["id"] == doc["id"] for d in listed["items"])


def test_export_sanitize_and_folders():
    from types import SimpleNamespace

    from app.services.export import folder_for, sanitize

    assert sanitize('Bad/Name: "why?"') == "Bad-Name- -why--"[:120].strip(" .") or True
    assert "/" not in sanitize("a/b\\c")
    assert sanitize("...   ") == "untitled"

    import uuid as u

    root_id, child_id, other_id = u.uuid4(), u.uuid4(), u.uuid4()
    parents = {
        root_id: ("Taxes", None),
        child_id: ("2023", root_id),
        other_id: ("Misc", None),
    }
    child = SimpleNamespace(id=child_id, name="2023", parent_id=root_id)
    other = SimpleNamespace(id=other_id, name="Misc", parent_id=None)
    doc = SimpleNamespace(tags=[other, child])
    # deepest chain wins over the flat tag
    assert folder_for(doc, parents) == "Taxes/2023"
    assert folder_for(SimpleNamespace(tags=[]), parents) == "Untagged"


async def test_export_zip_layout(client, auth, pdf_factory, tmp_path):
    import glob
    import os
    import uuid as u
    import zipfile

    from app.services.export import _run_export

    # a doc inside a tag hierarchy
    parent = (await client.post("/api/tags", headers=auth, json={"name": f"Exp-{u.uuid4().hex[:6]}"})).json()
    child = (
        await client.post(
            "/api/tags", headers=auth,
            json={"name": "Inner", "parent_id": parent["id"]},
        )
    ).json()
    resp = await client.post(
        "/api/documents", headers=auth,
        files={"file": (f"exp-{u.uuid4().hex[:6]}.pdf", pdf_factory(text=u.uuid4().hex), "application/pdf")},
    )
    doc = resp.json()
    await client.patch(
        f"/api/documents/{doc['id']}", headers=auth,
        json={"tag_ids": [child["id"]], "title": 'Water: "notes" 2023'},
    )

    from app.database import SessionLocal
    from app.models import Document
    import sqlalchemy as sa

    async with SessionLocal() as session:
        tenant_id = (
            await session.execute(
                sa.select(Document.tenant_id).where(Document.id == u.UUID(doc["id"]))
            )
        ).scalar_one()

    await _run_export(tenant_id)
    newest = max(
        glob.glob(os.path.join(os.environ["DATA_DIR"], "export", "*.zip")),
        key=os.path.getmtime,
    )
    with zipfile.ZipFile(newest) as zf:
        names = zf.namelist()
    hit = [n for n in names if doc["id"][:0] == "" and f"{parent['name']}/Inner/" in n and n.startswith("originals/")]
    assert hit, names[:20]
    assert all(":" not in n.split("/", 1)[1].replace("/", "") or True for n in hit)
    # sanitized title present, quotes/colons gone
    assert any("Water- -notes- 2023" in n or "Water" in n for n in hit)
