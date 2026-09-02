"""The audit's fixes, each pinned to the failure it closes.

Several of these are contracts the iOS app depends on — the single-shot
upload, the /text reader, the refresh exchange — so the tests are written
against the wire the app sees, not the internals that changed.
"""

import asyncio
import shutil
import uuid
from datetime import timedelta

import pytest
import sqlalchemy as sa

from app.database import SessionLocal
from app.models import Document, Tag, User


def _name(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


async def upload(client, auth, pdf_bytes, filename):
    return await client.post(
        "/api/documents", headers=auth,
        files={"file": (filename, pdf_bytes, "application/pdf")},
    )


async def _set_text(doc_id, text):
    """Write OCR text the way the worker would; the trigger builds the page
    vectors from it."""
    async with SessionLocal() as session:
        await session.execute(
            sa.update(Document).where(Document.id == uuid.UUID(doc_id))
            .values(text_content=text, text_length=len(text), status="ready")
        )
        await session.commit()


# --- search --------------------------------------------------------------


async def test_search_aggregates_the_page_index_once_and_snippets_the_right_page(
    client, auth, pdf_factory
):
    """The rewrite: one GIN scan, joined, with the count from the same set
    and the snippet from the page that matched — not the whole book."""
    marker = _name("orrery").replace("-", "")
    doc = (await upload(client, auth, pdf_factory(pages=3), f"{marker}.pdf")).json()
    # Page 3 carries the term; pages 1-2 are filler the snippet must not show.
    pages = ["opening filler text only", "middle filler text only",
             f"the {marker} mechanism turns on page three"]
    await _set_text(doc["id"], "\f".join(pages))

    resp = (await client.get(f"/api/search?q={marker}", headers=auth)).json()
    hit = next(r for r in resp["results"] if r["id"] == doc["id"])
    assert resp["total"] == len(resp["results"]) >= 1
    assert hit["pages_hit"] == 1
    assert marker in hit["snippet"].replace("[[", "").replace("]]", "")
    assert "filler" not in hit["snippet"], "snippet came from the wrong page"


async def test_search_total_and_paging_agree_with_the_rows(client, auth, pdf_factory):
    marker = _name("gimbal").replace("-", "")
    ids = []
    for i in range(3):
        d = (await upload(client, auth, pdf_factory(), f"{marker}-{i}.pdf")).json()
        await _set_text(d["id"], f"{marker} appears here {i}")
        ids.append(d["id"])
    first = (await client.get(f"/api/search?q={marker}&limit=2", headers=auth)).json()
    second = (
        await client.get(f"/api/search?q={marker}&limit=2&offset=2", headers=auth)
    ).json()
    assert first["total"] == second["total"] == 3
    assert len(first["results"]) == 2 and len(second["results"]) == 1
    seen = {r["id"] for r in first["results"]} | {r["id"] for r in second["results"]}
    assert seen == set(ids)


async def test_search_scoped_to_a_tag_still_scopes(client, auth, pdf_factory):
    marker = _name("sextant").replace("-", "")
    tagged = (await upload(client, auth, pdf_factory(), f"{marker}-a.pdf")).json()
    other = (await upload(client, auth, pdf_factory(), f"{marker}-b.pdf")).json()
    for d in (tagged, other):
        await _set_text(d["id"], f"{marker} text")
    tag = (await client.post("/api/tags", headers=auth, json={"name": _name("t")})).json()
    await client.patch(
        f"/api/documents/{tagged['id']}", headers=auth, json={"tag_ids": [tag["id"]]}
    )
    scoped = (
        await client.get(f"/api/search?q={marker}&tag_id={tag['id']}", headers=auth)
    ).json()
    assert [r["id"] for r in scoped["results"]] == [tagged["id"]]
    assert scoped["total"] == 1


# --- text_content deferred -------------------------------------------------


async def test_the_reader_endpoint_still_returns_the_text(client, auth, pdf_factory):
    """text_content is deferred on the ownership helper; /text is the route
    the iOS reader uses and must load it explicitly rather than trip a lazy
    load outside the session."""
    doc = (await upload(client, auth, pdf_factory(), f"{_name('reader')}.pdf")).json()
    await _set_text(doc["id"], "the whole text of the document")
    resp = await client.get(f"/api/documents/{doc['id']}/text", headers=auth)
    assert resp.status_code == 200, resp.text
    assert resp.json()["text"] == "the whole text of the document"


async def test_a_page_edit_snapshot_keeps_the_pre_edit_text(client, auth, pdf_factory):
    """The undo snapshot copies text_content; with the column deferred that
    read has to be explicit or the snapshot is silently empty."""
    doc = (await upload(client, auth, pdf_factory(pages=2), f"{_name('rot')}.pdf")).json()
    await _set_text(doc["id"], "first page\fsecond page")
    resp = await client.post(
        f"/api/documents/{doc['id']}/pages", headers=auth,
        json={"action": "rotate", "pages": [1], "degrees": 90},
    )
    assert resp.status_code == 200, resp.text
    async with SessionLocal() as session:
        snapshot_text = (
            await session.execute(
                sa.select(Document.text_content).where(
                    Document.title == f"{doc['title']} (before page edit)"
                )
            )
        ).scalar_one()
    assert snapshot_text == "first page\fsecond page"


async def test_zip_download_still_takes_ids(client, auth, pdf_factory):
    ids = [
        (await upload(client, auth, pdf_factory(), f"{_name('zip')}.pdf")).json()["id"]
        for _ in range(2)
    ]
    z = await client.post("/api/documents/download-zip", headers=auth, json={"ids": ids})
    assert z.status_code == 200 and z.headers["content-type"] == "application/zip"


@pytest.mark.skipif(shutil.which("gs") is None, reason="binder cover needs Ghostscript")
async def test_binder_still_takes_ids(client, auth, pdf_factory):
    ids = [
        (await upload(client, auth, pdf_factory(), f"{_name('bind')}.pdf")).json()["id"]
        for _ in range(2)
    ]
    b = await client.post(
        "/api/documents/binder", headers=auth, json={"ids": ids, "title": "Go bag"}
    )
    assert b.status_code == 200 and b.headers["x-total-pages"]


# --- download names --------------------------------------------------------


async def test_a_title_cannot_break_the_download_header(client, auth, pdf_factory):
    doc = (await upload(client, auth, pdf_factory(), f"{_name('hdr')}.pdf")).json()
    await client.patch(
        f"/api/documents/{doc['id']}", headers=auth,
        json={"title": 'say "hello"\r\nX-Injected: yes'},
    )
    resp = await client.get(
        f"/api/documents/{doc['id']}/file?version=original", headers=auth
    )
    assert resp.status_code == 200
    disposition = resp.headers["content-disposition"]
    assert "\n" not in disposition and "\r" not in disposition
    assert "x-injected" not in resp.headers


# --- auth ------------------------------------------------------------------


async def _member(client, auth):
    """A non-owner account, logged in."""
    email = f"member-{uuid.uuid4().hex[:8]}@example.com"
    r = await client.post(
        "/api/auth/users", headers=auth, json={"email": email, "password": "password123"}
    )
    assert r.status_code == 201, r.text
    tokens = (
        await client.post("/api/auth/login", json={"email": email, "password": "password123"})
    ).json()
    return tokens


async def test_the_user_list_is_owner_only(client, auth):
    member = await _member(client, auth)
    headers = {"Authorization": f"Bearer {member['access_token']}"}
    assert (await client.get("/api/auth/users", headers=headers)).status_code == 403
    assert (await client.get("/api/auth/users", headers=auth)).status_code == 200


async def test_passwords_past_bcrypts_limit_are_refused_when_set_not_when_used(
    client, auth
):
    long = "p" * 80
    r = await client.post(
        "/api/auth/users", headers=auth,
        json={"email": f"long-{uuid.uuid4().hex[:6]}@example.com", "password": long},
    )
    assert r.status_code == 422
    # Checking a long password against an existing account must not 500 —
    # it is simply wrong.
    r = await client.post(
        "/api/auth/login", json={"email": "tester@example.com", "password": long}
    )
    assert r.status_code == 401


async def test_refresh_tokens_are_single_use(client, auth):
    tokens = await _member(client, auth)
    first = await client.post("/api/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert first.status_code == 200
    fresh = first.json()
    assert fresh["refresh_token"] != tokens["refresh_token"]

    # The successor works.
    second = await client.post("/api/auth/refresh", json={"refresh_token": fresh["refresh_token"]})
    assert second.status_code == 200


async def test_a_retry_inside_the_grace_window_gets_the_same_successor(client, auth):
    """A lost response is not a stolen token: the client that never saw the
    new pair retries with what it has and gets the same successor back."""
    tokens = await _member(client, auth)
    first = (
        await client.post("/api/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    ).json()
    retry = await client.post("/api/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert retry.status_code == 200
    from app.security import decode_token

    assert decode_token(retry.json()["refresh_token"], "refresh")[2] == decode_token(
        first["refresh_token"], "refresh"
    )[2]


async def test_a_reused_token_outside_the_window_is_refused(client, auth, monkeypatch):
    from app.routers import auth as auth_router

    monkeypatch.setattr(auth_router, "REFRESH_REUSE_GRACE", timedelta(seconds=0))
    tokens = await _member(client, auth)
    assert (
        await client.post("/api/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    ).status_code == 200
    reuse = await client.post("/api/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert reuse.status_code == 401


async def test_a_refresh_token_from_before_rotation_is_still_honoured(client, auth):
    """The deploy must not sign every device out: an id-less token is valid
    until it expires and is rotated into the scheme on first use."""
    from datetime import timedelta as _td

    from app.security import _mint, decode_token

    tokens = await _member(client, auth)
    user_id, version, _ = decode_token(tokens["access_token"], "access")
    legacy = _mint(user_id, "refresh", _td(days=1), version)  # no jti
    resp = await client.post("/api/auth/refresh", json={"refresh_token": legacy})
    assert resp.status_code == 200
    assert decode_token(resp.json()["refresh_token"], "refresh")[2] is not None


# --- rate limiting attribution --------------------------------------------


def test_client_ip_prefers_nginxs_view_of_its_peer(monkeypatch):
    from app.config import settings
    from app.services.ratelimit import client_ip

    class _Client:
        def __init__(self, host):
            self.host = host

    class _Req:
        def __init__(self, peer, headers):
            self.client = _Client(peer)
            self.headers = headers

    nginx = "172.18.0.5"
    # A LAN caller with no Cloudflare header is limited on its real address,
    # not on nginx's — before this every LAN user shared one bucket.
    assert client_ip(_Req(nginx, {"x-real-ip": "192.168.1.50"})) == "192.168.1.50"
    # Tunnel peers unset: CF-Connecting-IP is believed from any trusted proxy.
    monkeypatch.setattr(settings, "tunnel_peers", "")
    assert client_ip(_Req(nginx, {"x-real-ip": "192.168.1.50", "cf-connecting-ip": "203.0.113.9"})) == "203.0.113.9"
    # Tunnel peers set: only when nginx's peer was the tunnel.
    monkeypatch.setattr(settings, "tunnel_peers", "192.168.1.42")
    assert client_ip(_Req(nginx, {"x-real-ip": "192.168.1.42", "cf-connecting-ip": "203.0.113.9"})) == "203.0.113.9"
    assert client_ip(_Req(nginx, {"x-real-ip": "192.168.1.50", "cf-connecting-ip": "203.0.113.9"})) == "192.168.1.50"


# --- background classification --------------------------------------------


async def test_library_classification_runs_in_the_background(client, auth):
    started = await client.post("/api/classify/run", headers=auth)
    assert started.status_code == 200 and started.json()["started"] is True
    for _ in range(100):
        state = (await client.get("/api/classify/run", headers=auth)).json()
        if state.get("state") in ("done", "failed"):
            break
        await asyncio.sleep(0.1)
    assert state["state"] == "done", state
    assert state["examined"] == state["total"]


# --- typed bodies ------------------------------------------------------------


async def test_bad_types_are_422_not_500(client, auth, pdf_factory):
    assert (await client.post("/api/export", headers=auth, json={"part_gb": "abc"})).status_code == 422
    doc = (await upload(client, auth, pdf_factory(), f"{_name('ann')}.pdf")).json()
    bad = await client.post(
        f"/api/documents/{doc['id']}/annotations", headers=auth,
        json={"page": "x", "quote": "q", "rects": [{"x": 0, "y": 0, "w": 1, "h": 1}]},
    )
    assert bad.status_code == 422
    # The shape the iOS app sends still works.
    good = await client.post(
        f"/api/documents/{doc['id']}/annotations", headers=auth,
        json={"page": 1, "quote": "q", "rects": [{"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.05}],
              "color": "#ffcc00"},
    )
    assert good.status_code == 201, good.text
    assert (
        await client.put(f"/api/documents/{doc['id']}/position", headers=auth, json={"page": 2})
    ).status_code == 200
    assert (
        await client.post("/api/documents/processing", headers=auth, json={"paused": False})
    ).status_code == 200


# --- tags, uploads ------------------------------------------------------------


async def test_tag_counts_leave_the_trash_out(client, auth, pdf_factory):
    tag = (await client.post("/api/tags", headers=auth, json={"name": _name("count")})).json()
    doc = (await upload(client, auth, pdf_factory(), f"{_name('cnt')}.pdf")).json()
    await client.patch(f"/api/documents/{doc['id']}", headers=auth, json={"tag_ids": [tag["id"]]})
    counts = {t["id"]: t["count"] for t in (await client.get("/api/tags", headers=auth)).json()}
    assert counts[tag["id"]] == 1
    await client.delete(f"/api/documents/{doc['id']}", headers=auth)
    counts = {t["id"]: t["count"] for t in (await client.get("/api/tags", headers=auth)).json()}
    assert counts[tag["id"]] == 0


async def test_open_upload_sessions_are_bounded(client, auth):
    from app.routers import documents as docs_router

    created = []
    try:
        for _ in range(docs_router.MAX_OPEN_UPLOAD_SESSIONS):
            r = await client.post("/api/documents/uploads", headers=auth)
            assert r.status_code == 200
            created.append(r.json()["upload_id"])
        assert (await client.post("/api/documents/uploads", headers=auth)).status_code == 429
    finally:
        for upload_id in created:
            shutil.rmtree(docs_router._upload_session_dir(uuid.UUID(upload_id)), ignore_errors=True)
