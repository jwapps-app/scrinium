"""Races the audit found in auth and transfer, each pinned to its fix.

Every one of these is a two-callers-at-once story: setup, a one-time code,
a refresh token, an export start. The tests run the two callers together
with asyncio.gather where the wire allows it, and drive the helper directly
where the shared test database does not.
"""

import asyncio
import json
import time
import uuid

import pytest
import sqlalchemy as sa

from app.database import SessionLocal
from app.models import Document, RefreshToken, Rule, User
from app.security import decode_token
from app.services.app_state import get_value, set_value


def _name(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


async def _member(client, auth, password="password123"):
    """A fresh non-owner account, logged in."""
    email = f"{_name('member')}@example.com"
    r = await client.post(
        "/api/auth/users", headers=auth, json={"email": email, "password": password}
    )
    assert r.status_code == 201, r.text
    tokens = (
        await client.post("/api/auth/login", json={"email": email, "password": password})
    ).json()
    return email, tokens


async def _user_count():
    async with SessionLocal() as session:
        return (await session.execute(sa.select(sa.func.count(User.id)))).scalar_one()


# --- F13: first-run setup ----------------------------------------------------


async def test_two_simultaneous_setups_make_exactly_one_owner(client):
    """Only meaningful on an empty library, which this file gets when it is
    run on its own. In the full suite an earlier file has already set up, and
    the lock itself is covered by the test below."""
    if await _user_count() > 0:
        pytest.skip("library already set up by an earlier test file")
    # The same credentials the shared `token` fixture logs in with later, so
    # whichever request wins leaves the account every other test expects.
    creds = {"email": "tester@example.com", "password": "testpassword1"}
    a, b = await asyncio.gather(
        client.post("/api/auth/setup", json=creds),
        client.post("/api/auth/setup", json=creds),
    )
    assert sorted([a.status_code, b.status_code]) == [200, 403], (a.text, b.text)
    assert await _user_count() == 1


async def test_setup_count_waits_for_the_transaction_holding_the_lock():
    """The helper the route counts through: a second caller cannot get its
    count until the first has committed or rolled back."""
    from app.routers.auth import _lock_setup_and_count_users

    holder = SessionLocal()
    waiter = SessionLocal()
    try:
        await _lock_setup_and_count_users(holder)
        second = asyncio.create_task(_lock_setup_and_count_users(waiter))
        await asyncio.sleep(0.3)
        assert not second.done(), "the second count went through under the lock"
        await holder.rollback()
        assert await asyncio.wait_for(second, 5) == await _user_count()
    finally:
        await holder.close()
        await waiter.close()


# --- F14: one-time codes are consumed once ---------------------------------


async def test_a_totp_step_is_consumed_by_exactly_one_caller(client, auth):
    from app.routers.auth import _consume_totp_step

    _, tokens = await _member(client, auth)
    user_id = decode_token(tokens["access_token"], "access")[0]
    async with SessionLocal() as session:
        user = await session.get(User, user_id)
        assert await _consume_totp_step(session, user, 1000) is True
        assert user.totp_last_step == 1000
        assert await _consume_totp_step(session, user, 1000) is False
        assert await _consume_totp_step(session, user, 999) is False
        assert await _consume_totp_step(session, user, 1001) is True
        await session.commit()


async def test_simultaneous_logins_with_one_code_yield_one_session(client, auth):
    from app.services.totp import _code_at

    email, tokens = await _member(client, auth)
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    secret = (await client.post("/api/auth/totp/setup", headers=headers)).json()["secret"]
    # Enable with the previous step's code so the step the two logins present
    # is newer than the one enabling recorded.
    enabled = await client.post(
        "/api/auth/totp/enable", headers=headers,
        json={"code": _code_at(secret, time.time() - 30)},
    )
    assert enabled.status_code == 200, enabled.text

    body = {"email": email, "password": "password123", "totp": _code_at(secret, time.time())}
    a, b = await asyncio.gather(
        client.post("/api/auth/login", json=body),
        client.post("/api/auth/login", json=body),
    )
    assert sorted([a.status_code, b.status_code]) == [200, 401], (a.text, b.text)
    refused = a if a.status_code == 401 else b
    assert refused.json()["detail"] == "That code has already been used"


# --- F15: one refresh token, one successor ---------------------------------


async def _live_refresh_rows(user_id) -> int:
    async with SessionLocal() as session:
        return (
            await session.execute(
                sa.select(sa.func.count(RefreshToken.id)).where(
                    RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None)
                )
            )
        ).scalar_one()


async def test_simultaneous_refreshes_share_one_successor(client, auth):
    _, tokens = await _member(client, auth)
    user_id = decode_token(tokens["access_token"], "access")[0]
    assert await _live_refresh_rows(user_id) == 1

    a, b = await asyncio.gather(
        client.post("/api/auth/refresh", json={"refresh_token": tokens["refresh_token"]}),
        client.post("/api/auth/refresh", json={"refresh_token": tokens["refresh_token"]}),
    )
    codes = sorted([a.status_code, b.status_code])
    assert codes in ([200, 200], [200, 401]), (a.text, b.text)
    if codes == [200, 200]:
        assert (
            decode_token(a.json()["refresh_token"], "refresh")[2]
            == decode_token(b.json()["refresh_token"], "refresh")[2]
        )
    # The presented token is retired and exactly one successor is live.
    assert await _live_refresh_rows(user_id) == 1


async def test_the_grace_retry_still_returns_the_same_successor(client, auth):
    """Sequential form of the same contract, now that the row is locked."""
    _, tokens = await _member(client, auth)
    first = await client.post("/api/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    retry = await client.post("/api/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert first.status_code == retry.status_code == 200
    assert (
        decode_token(first.json()["refresh_token"], "refresh")[2]
        == decode_token(retry.json()["refresh_token"], "refresh")[2]
    )
    # And the successor is still usable afterwards.
    nxt = await client.post(
        "/api/auth/refresh", json={"refresh_token": first.json()["refresh_token"]}
    )
    assert nxt.status_code == 200


# --- F26: import/export status survives a restart honestly ------------------


async def _write_status(key, payload):
    async with SessionLocal() as session:
        await set_value(session, key, json.dumps(payload))
        await session.commit()


async def _read_status(key) -> dict:
    async with SessionLocal() as session:
        raw = await get_value(session, key)
    return json.loads(raw) if raw else {}


@pytest.fixture()
async def quiet_transfers():
    """Leave no transfer marked running behind, whatever the test did."""
    from app.services.export import EXPORT_STATUS
    from app.services.paperless_import import IMPORT_STATUS

    before = {k: await _read_status(k) for k in (IMPORT_STATUS, EXPORT_STATUS)}
    yield
    for key, state in before.items():
        await _write_status(key, state)


async def test_startup_turns_a_stale_running_status_into_interrupted(
    client, auth, quiet_transfers
):
    from app.routers.transfer import INTERRUPTED_MESSAGE, reset_interrupted_transfers
    from app.services.export import EXPORT_STATUS
    from app.services.paperless_import import IMPORT_STATUS

    await _write_status(EXPORT_STATUS, {"state": "running", "done": 3, "total": 9})
    await _write_status(IMPORT_STATUS, {"state": "done", "imported": 2, "skipped": 0})

    changed = await reset_interrupted_transfers()
    assert changed == [EXPORT_STATUS]

    exp = (await client.get("/api/export", headers=auth)).json()["status"]
    assert exp["state"] == "interrupted"
    assert exp["error"] == INTERRUPTED_MESSAGE
    assert (exp["done"], exp["total"]) == (3, 9), "progress is kept for the reader"
    imp = (await client.get("/api/import/paperless", headers=auth)).json()["status"]
    assert imp["state"] == "done", "a finished run is left alone"

    # And a start is no longer refused.
    assert await reset_interrupted_transfers() == []


async def test_simultaneous_export_starts_launch_one_export(
    client, auth, monkeypatch, quiet_transfers
):
    from app.routers import transfer
    from app.services.export import EXPORT_STATUS

    await _write_status(EXPORT_STATUS, {"state": "done"})
    launched = []

    async def fake_export(tenant_id, fmt, part_gb):
        launched.append((fmt, part_gb))

    monkeypatch.setattr(transfer, "run_export", fake_export)
    a, b = await asyncio.gather(
        client.post("/api/export", headers=auth, json={"format": "folder"}),
        client.post("/api/export", headers=auth, json={"format": "folder"}),
    )
    assert sorted([a.status_code, b.status_code]) == [200, 409], (a.text, b.text)
    assert len(launched) == 1
    # The claim was committed by the request, not left to the task.
    assert (await _read_status(EXPORT_STATUS))["state"] == "running"
    third = await client.post("/api/export", headers=auth, json={"format": "folder"})
    assert third.status_code == 409


async def test_simultaneous_import_starts_launch_one_import(
    client, auth, monkeypatch, quiet_transfers, tmp_path
):
    from app.routers import transfer
    from app.services.paperless_import import IMPORT_STATUS

    await _write_status(IMPORT_STATUS, {})
    launched = []

    async def fake_import(tenant_id):
        launched.append(tenant_id)

    monkeypatch.setattr(transfer, "run_import", fake_import)
    monkeypatch.setattr(transfer, "find_export", lambda root: tmp_path)
    a, b = await asyncio.gather(
        client.post("/api/import/paperless", headers=auth),
        client.post("/api/import/paperless", headers=auth),
    )
    assert sorted([a.status_code, b.status_code]) == [200, 409], (a.text, b.text)
    assert len(launched) == 1
    assert (await _read_status(IMPORT_STATUS))["state"] == "running"


# --- F30: a rule that times out is tried once per batch ----------------------


async def test_a_rule_that_times_out_is_not_retried_on_the_next_document(
    client, auth, pdf_factory, monkeypatch
):
    from app.services import classify

    ids = []
    for _ in range(2):
        r = await client.post(
            "/api/documents", headers=auth,
            files={"file": (f"{_name('slow')}.pdf", pdf_factory(), "application/pdf")},
        )
        assert r.status_code in (200, 201), r.text
        ids.append(uuid.UUID(r.json()["id"]))

    evaluations = []

    def too_slow(rule, text):
        evaluations.append(rule.name)
        raise classify.RuleTooSlow("pattern took longer than 2s to match")

    monkeypatch.setattr(classify, "rule_matches", too_slow)

    async with SessionLocal() as session:
        docs = (
            await session.execute(sa.select(Document).where(Document.id.in_(ids)))
        ).scalars().all()
        assert len(docs) == 2
        # A transient rule: what the library-wide run hands every document.
        rule = Rule(
            tenant_id=docs[0].tenant_id, name="catastrophic", match_type="regex",
            pattern=r"(\s*\w+)+$", enabled=True, priority=1,
        )
        shared = [rule]
        for doc in docs:
            await classify.classify_document(session, doc, shared)
        await session.rollback()

    assert evaluations == ["catastrophic"], "the second document paid the timeout again"
    assert rule.enabled is False
    assert "longer than" in (rule.error or "")
