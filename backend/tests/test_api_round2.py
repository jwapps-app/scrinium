"""User management, merge, page-edit undo snapshot, health."""

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


async def test_change_password_requires_current(client, auth):
    r = await client.post(
        "/api/auth/change-password", headers=auth,
        json={"current_password": "wrong", "new_password": "whatever123"},
    )
    assert r.status_code == 403


async def test_change_password_roundtrip(client, auth):
    # change it — the response carries a fresh token pair, and every token
    # minted before the change (including the shared fixture token) dies
    r = await client.post(
        "/api/auth/change-password", headers=auth,
        json={"current_password": "testpassword1", "new_password": "temporarypass2"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body.get("access_token") and body.get("refresh_token")
    fresh = {"Authorization": f"Bearer {body['access_token']}"}

    # the pre-change token is now invalid (that's the point of the version bump)
    stale = await client.get("/api/auth/users", headers=auth)
    assert stale.status_code == 401

    login = await client.post(
        "/api/auth/login",
        json={"email": "tester@example.com", "password": "temporarypass2"},
    )
    assert login.status_code == 200

    # change it back using the fresh token
    r = await client.post(
        "/api/auth/change-password", headers=fresh,
        json={"current_password": "temporarypass2", "new_password": "testpassword1"},
    )
    assert r.status_code == 200

    # Restore token_version so the session-scoped fixture token (minted at
    # version 0) keeps working for the rest of the suite.
    import sqlalchemy as sa

    from app.database import SessionLocal
    from app.models import User

    async with SessionLocal() as session:
        await session.execute(
            sa.update(User)
            .where(User.email == "tester@example.com")
            .values(token_version=0)
        )
        await session.commit()

    ok = await client.get("/api/auth/users", headers=auth)
    assert ok.status_code == 200


async def test_user_management(client, auth):
    email = f"{_name('user')}@example.com"
    created = await client.post(
        "/api/auth/users", headers=auth,
        json={"email": email, "password": "password123"},
    )
    assert created.status_code == 201

    # the new account can log in
    login = await client.post(
        "/api/auth/login", json={"email": email, "password": "password123"}
    )
    assert login.status_code == 200

    users = (await client.get("/api/auth/users", headers=auth)).json()
    me = next(u for u in users if u["is_me"])
    other = next(u for u in users if u["email"] == email)

    # can't remove yourself
    assert (
        await client.delete(f"/api/auth/users/{me['id']}", headers=auth)
    ).status_code == 400
    # can remove the other account
    assert (
        await client.delete(f"/api/auth/users/{other['id']}", headers=auth)
    ).status_code == 204


async def test_merge_documents(client, auth, pdf_factory):
    a = await upload(client, auth, pdf_factory(pages=2, text=_name("ma")), "a.pdf")
    b = await upload(client, auth, pdf_factory(pages=3, text=_name("mb")), "b.pdf")

    r = await client.post(
        "/api/documents/merge", headers=auth,
        json={"ids": [a["id"], b["id"]], "title": _name("merged")},
    )
    assert r.status_code == 200, r.text
    merged_id = r.json()["new_document_id"]

    merged = (await client.get(f"/api/documents/{merged_id}", headers=auth)).json()
    assert merged["status"] == "pending"  # queued for OCR
    # sources went to the trash
    for doc in (a, b):
        gone = (await client.get(f"/api/documents/{doc['id']}", headers=auth)).json()
        assert gone["deleted_at"] is not None

    # fewer than two → 422
    assert (
        await client.post(
            "/api/documents/merge", headers=auth, json={"ids": [merged_id]}
        )
    ).status_code == 422


async def test_page_edit_leaves_undo_snapshot(client, auth, pdf_factory):
    doc = await upload(client, auth, pdf_factory(pages=4, text=_name("undo")), "u.pdf")

    r = await client.post(
        f"/api/documents/{doc['id']}/pages", headers=auth,
        json={"action": "delete", "pages": [4]},
    )
    assert r.status_code == 200, r.text

    # the pre-edit version sits in the trash, restorable
    trash = (
        await client.get("/api/documents?status_filter=trash&limit=200", headers=auth)
    ).json()
    snapshot = next(
        (d for d in trash["items"] if d["title"] == f"{doc['title']} (before page edit)"),
        None,
    )
    assert snapshot is not None
    restored = await client.post(
        f"/api/documents/{snapshot['id']}/restore", headers=auth
    )
    assert restored.status_code == 200
    # snapshot's file is servable (blob ownership transferred intact)
    f = await client.get(
        f"/api/documents/{snapshot['id']}/file?version=original", headers=auth
    )
    assert f.status_code == 200


async def test_health_endpoint(client, auth):
    h = (await client.get("/api/settings/health", headers=auth)).json()
    for key in ("queue", "running", "worker_alive", "disk"):
        assert key in h
    assert h["disk"] is None or h["disk"]["total_gb"] > 0
