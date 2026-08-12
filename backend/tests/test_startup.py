"""The upgrade window: reachable, honest, and impossible to serve through.

The failure this guards against is not downtime. It is that the downtime was
invisible: the API was not listening, so the library rendered its first-run
empty state and told the user to upload their first document. Everything here
is about the window being legible instead.
"""

from dataclasses import replace

import pytest
from sqlalchemy.exc import OperationalError, ProgrammingError

from app.services import startup


@pytest.fixture
def state():
    """Snapshot the process-wide startup state; leaving it dirty would 503
    every test that ran afterwards."""
    before = replace(startup.STATE)
    yield startup.STATE
    for field, value in vars(before).items():
        setattr(startup.STATE, field, value)


class _Orig(Exception):
    def __init__(self, sqlstate):
        self.sqlstate = sqlstate


def _db_error(kind, sqlstate):
    return kind("SELECT 1", {}, _Orig(sqlstate))


def test_a_busy_lock_is_retried_and_a_broken_migration_is_not():
    """env.py deliberately refuses to queue for its lock (a DDL waiting behind
    the nightly pg_dump parks every later read behind it too), so a timed-out
    migration is expected and must be retried. Wrong SQL is a different thing:
    the shell loop this replaced retried it forty times over ten minutes and
    only then reported it.
    """
    busy = _db_error(OperationalError, startup.LOCK_NOT_AVAILABLE)
    assert startup._retry_delay(busy) == startup.LOCK_RETRY_SECONDS

    still_starting = _db_error(OperationalError, "57P03")
    assert startup._retry_delay(still_starting) == startup.CONNECT_RETRY_SECONDS

    # Nothing to wait for — the column genuinely is not there.
    undefined_column = _db_error(ProgrammingError, "42703")
    assert startup._retry_delay(undefined_column) is None


def test_a_socket_that_never_opened_is_worth_waiting_for():
    """The whole stack starts at once, so Postgres routinely is not listening
    yet. That arrives with no SQLSTATE at all."""
    refused = OperationalError("connect", {}, ConnectionRefusedError(61, "refused"))
    assert startup._retry_delay(refused) == startup.CONNECT_RETRY_SECONDS


def test_progress_counts_each_migration_as_it_lands(state):
    """Alembic's own per-step hook, so '3 of 5' is real rather than guessed."""
    state.total = 2
    state.step = 0

    class _Step:
        up_revision_id = "0031"
        doc = "record the DPI a downsample was tried at"

    startup._version_applied(None, _Step(), None, None)
    assert state.step == 1
    assert "0031" in state.message and "downsample" in state.message


async def test_a_deploy_with_no_migration_does_not_run_one(state, monkeypatch):
    """Most deploys carry no schema change. The test database is already at
    head, so this is the real 'nothing to apply' path."""
    called = []
    monkeypatch.setattr(
        startup.command, "upgrade", lambda *a, **k: called.append(a)
    )

    await startup.run_migrations()

    assert called == [], "ran an upgrade with nothing to upgrade"
    assert state.state == "ready"
    assert state.total == 0


async def test_the_api_refuses_to_serve_a_half_migrated_schema(client, state):
    """The guarantee that predates all of this: no route runs against a schema
    that is not at head."""
    state.state = "migrating"
    state.total, state.step = 5, 2

    resp = await client.get("/api/documents")

    assert resp.status_code == 503
    assert resp.headers["Retry-After"] == "5"


async def test_it_refuses_by_explaining_rather_than_by_vanishing(client, state):
    """The whole point. A dead upstream is indistinguishable from an empty
    library; a 503 carrying progress is something the UI can render."""
    state.state = "migrating"
    state.total, state.step, state.message = 5, 2, "0031 add downsample_tried_dpi"

    body = (await client.get("/api/documents")).json()

    assert body["state"] == "migrating"
    assert (body["step"], body["total"]) == (2, 5)
    assert "nothing has been lost" in body["detail"].lower()


async def test_status_stays_reachable_while_the_rest_is_gated(client, state):
    """It is what the login screen polls, so it cannot be behind the gate it
    reports on — nor behind authentication."""
    state.state = "migrating"
    state.total, state.step = 5, 2

    resp = await client.get("/api/status")  # no auth header on purpose

    assert resp.status_code == 503
    assert resp.json()["state"] == "migrating"
    assert resp.json()["step"] == 2


async def test_health_reports_unready_so_a_healthcheck_still_fails(client, state):
    """Otherwise the stack shows healthy while every route refuses."""
    state.state = "migrating"

    resp = await client.get("/api/health")

    assert resp.status_code == 503
    assert resp.json()["status"] == "migrating"


async def test_a_failed_migration_says_why_instead_of_crash_looping(state, monkeypatch):
    """A container that exits takes the explanation with it. Holding the
    failure visible is what lets the user read it."""
    def _boom(*a, **k):
        raise _db_error(ProgrammingError, "42703")

    monkeypatch.setattr(startup.command, "upgrade", _boom)
    monkeypatch.setattr(
        startup, "_pending", _fake_pending("0030", "0031", 1)
    )

    await startup.run_migrations()

    assert state.state == "failed"
    assert state.error


async def test_a_busy_database_is_retried_before_giving_up(state, monkeypatch):
    """The nightly dump is a few attempts, not a deploy failure."""
    attempts = []

    def _busy_once(*a, **k):
        attempts.append(1)
        if len(attempts) == 1:
            raise _db_error(OperationalError, startup.LOCK_NOT_AVAILABLE)

    monkeypatch.setattr(startup.command, "upgrade", _busy_once)
    monkeypatch.setattr(startup, "_pending", _fake_pending("0030", "0031", 1))
    monkeypatch.setattr(startup.asyncio, "sleep", _no_wait)

    await startup.run_migrations()

    assert len(attempts) == 2, "gave up on a lock it should have waited for"
    assert state.state == "ready"


def _fake_pending(current, head, total):
    async def _pending(cfg):
        return current, head, total

    return _pending


async def _no_wait(seconds):
    """Patched over asyncio.sleep, so it must not call asyncio.sleep."""
    return None
