"""API tokens: the contract a service like Quaero is written against."""

import uuid

import sqlalchemy as sa

from app.database import SessionLocal
from app.models import User


def _name(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


async def upload(client, auth, pdf_bytes, filename):
    return await client.post(
        "/api/documents", headers=auth,
        files={"file": (filename, pdf_bytes, "application/pdf")},
    )


async def _make(client, auth, name="svc", read_only=False):
    r = await client.post(
        "/api/auth/tokens", headers=auth, json={"name": name, "read_only": read_only}
    )
    assert r.status_code == 201, r.text
    return r.json()


async def test_a_token_is_shown_once_and_listed_only_by_name(client, auth):
    made = await _make(client, auth, "quaero")
    assert made["token"].startswith("scr_") and len(made["token"]) > 40
    listed = (await client.get("/api/auth/tokens", headers=auth)).json()
    mine = next(t for t in listed if t["id"] == made["id"])
    assert mine["name"] == "quaero"
    assert "token" not in mine, "the secret is never listed"
    assert mine["suffix"] == made["token"][-4:]
    assert mine["read_only"] is False and mine["last_used_at"] is None


async def test_a_token_is_accepted_everywhere_a_session_is_and_resolves_to_its_maker(
    client, auth, pdf_factory
):
    made = await _make(client, auth)
    bearer = {"Authorization": f"Bearer {made['token']}"}
    doc = (await upload(client, auth, pdf_factory(), f"{_name('tok')}.pdf")).json()

    # The five calls Quaero makes.
    assert (await client.get("/api/tags", headers=bearer)).status_code == 200
    assert (await client.get("/api/documents", headers=bearer)).status_code == 200
    assert (await client.get(f"/api/documents/{doc['id']}", headers=bearer)).status_code == 200
    assert (await client.get(f"/api/documents/{doc['id']}/text", headers=bearer)).status_code == 200
    assert (await client.get(f"/api/documents/{doc['id']}/file", headers=bearer)).status_code == 200

    me_session = (await client.get("/api/auth/me", headers=auth)).json()
    me_token = (await client.get("/api/auth/me", headers=bearer)).json()
    assert me_token["id"] == me_session["id"]


async def test_a_read_only_token_reads_but_cannot_write(client, auth, pdf_factory):
    made = await _make(client, auth, "ro", read_only=True)
    bearer = {"Authorization": f"Bearer {made['token']}"}
    doc = (await upload(client, auth, pdf_factory(), f"{_name('ro')}.pdf")).json()
    assert (await client.get(f"/api/documents/{doc['id']}", headers=bearer)).status_code == 200
    denied = await client.patch(
        f"/api/documents/{doc['id']}", headers=bearer, json={"title": "changed"}
    )
    assert denied.status_code == 403
    assert "read-only" in denied.json()["detail"]
    # And the full token can.
    full = await _make(client, auth, "rw")
    ok = await client.patch(
        f"/api/documents/{doc['id']}",
        headers={"Authorization": f"Bearer {full['token']}"},
        json={"title": "changed"},
    )
    assert ok.status_code == 200


async def test_a_revoked_or_unknown_token_is_a_plain_401_with_a_detail(client, auth):
    made = await _make(client, auth)
    bearer = {"Authorization": f"Bearer {made['token']}"}
    assert (await client.get("/api/documents", headers=bearer)).status_code == 200

    gone = await client.delete(f"/api/auth/tokens/{made['id']}", headers=auth)
    assert gone.status_code == 204
    after = await client.get("/api/documents", headers=bearer)
    assert after.status_code == 401
    assert after.json() == {"detail": "Invalid or revoked API token"}
    listed = (await client.get("/api/auth/tokens", headers=auth)).json()
    assert made["id"] not in {t["id"] for t in listed}

    bogus = await client.get(
        "/api/documents", headers={"Authorization": "Bearer scr_" + "x" * 43}
    )
    assert bogus.status_code == 401
    assert bogus.json()["detail"] == "Invalid or revoked API token"


async def test_a_token_cannot_manage_tokens(client, auth):
    made = await _make(client, auth)
    bearer = {"Authorization": f"Bearer {made['token']}"}
    assert (await client.get("/api/auth/tokens", headers=bearer)).status_code == 403
    assert (
        await client.post("/api/auth/tokens", headers=bearer, json={"name": "more"})
    ).status_code == 403
    assert (
        await client.delete(f"/api/auth/tokens/{made['id']}", headers=bearer)
    ).status_code == 403


async def test_a_token_is_not_asked_for_the_one_time_code(client, auth):
    """The code was answered when the person made the token, signed in.
    Turning two-factor on afterwards must not lock the service out."""
    made = await _make(client, auth, "after-2fa")
    me = (await client.get("/api/auth/me", headers=auth)).json()
    async with SessionLocal() as session:
        await session.execute(
            sa.update(User).where(User.id == uuid.UUID(me["id"]))
            .values(totp_enabled=True, totp_secret="JBSWY3DPEHPK3PXP")
        )
        await session.commit()
    try:
        r = await client.get(
            "/api/documents", headers={"Authorization": f"Bearer {made['token']}"}
        )
        assert r.status_code == 200
    finally:
        async with SessionLocal() as session:
            await session.execute(
                sa.update(User).where(User.id == uuid.UUID(me["id"]))
                .values(totp_enabled=False, totp_secret=None, totp_last_step=None)
            )
            await session.commit()


async def test_a_token_survives_sign_out_and_records_use(client, auth):
    """It is a service's credential, not a session: signing the browser out
    everywhere leaves it working, and using it stamps last_used_at."""
    email = f"svc-{uuid.uuid4().hex[:8]}@example.com"
    r = await client.post(
        "/api/auth/users", headers=auth, json={"email": email, "password": "password123"}
    )
    assert r.status_code == 201
    session_tokens = (
        await client.post("/api/auth/login", json={"email": email, "password": "password123"})
    ).json()
    their_auth = {"Authorization": f"Bearer {session_tokens['access_token']}"}
    made = await _make(client, their_auth, "keeps-working")
    bearer = {"Authorization": f"Bearer {made['token']}"}

    assert (await client.post("/api/auth/logout", headers=their_auth)).status_code == 200
    assert (await client.get("/api/documents", headers=their_auth)).status_code == 401
    assert (await client.get("/api/documents", headers=bearer)).status_code == 200

    fresh = (
        await client.post("/api/auth/login", json={"email": email, "password": "password123"})
    ).json()
    listed = (
        await client.get(
            "/api/auth/tokens", headers={"Authorization": f"Bearer {fresh['access_token']}"}
        )
    ).json()
    assert listed[0]["last_used_at"] is not None


def test_the_jwt_path_is_untouched():
    """The contract's own requirement: the existing check stays as it was."""
    import inspect

    from app import deps

    source = inspect.getsource(deps.get_current_user)
    assert 'decode_token(bearer, "access")' in source
    assert "token_version != user.token_version" in source
