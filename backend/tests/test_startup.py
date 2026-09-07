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


def test_abandoned_temp_directories_are_cleared_at_startup(tmp_path, monkeypatch):
    """42 GB of them filled a 63 GB root disk over two days of restarts.

    TemporaryDirectory cleans up on a normal exit and not otherwise, so every
    OOM, redeploy and reclaim left a full document's worth of page images
    behind. Once the disk filled, every OCR job died on FileNotFoundError
    writing its own stdout, requeued, and died again — while Postgres, on the
    same volume, dropped into recovery. Container metrics showed nothing:
    CPU healthy, memory healthy, zero restarts.
    """
    import os

    from app import worker

    monkeypatch.setattr(worker.tempfile, "gettempdir", lambda: str(tmp_path))
    for prefix in worker.TEMP_PREFIXES:
        (tmp_path / f"{prefix}live").mkdir()
    (tmp_path / "not-ours").mkdir()
    (tmp_path / "a-file").write_text("x")

    # Startup: this worker owns nothing yet, so everything of ours is orphaned.
    freed = worker._sweep_stale_tempdirs()

    assert freed == len(worker.TEMP_PREFIXES)
    assert (tmp_path / "not-ours").exists(), "only our own prefixes"
    assert (tmp_path / "a-file").exists(), "files are not directories"


def test_the_periodic_sweep_leaves_a_running_job_alone(tmp_path, monkeypatch):
    """Called with an age from the maintenance loop, so a working directory
    belonging to a job still legitimately running is never pulled out from
    under it — a 2,000-page book can grind for hours."""
    import os

    from app import worker

    monkeypatch.setattr(worker.tempfile, "gettempdir", lambda: str(tmp_path))
    fresh = tmp_path / "ingest-running"
    fresh.mkdir()
    stale = tmp_path / "ingest-abandoned"
    stale.mkdir()
    os.utime(stale, (0, 0))

    freed = worker._sweep_stale_tempdirs(max_age_seconds=3600)

    assert freed == 1
    assert fresh.exists(), "a live job's workdir must survive"
    assert not stale.exists()


def test_the_sweep_runs_before_any_job_is_claimed():
    """Ordering is the point: claiming a job onto a full disk fails it, and
    the failure looks like a bug in OCR rather than a full filesystem."""
    import inspect

    from app import worker

    source = inspect.getsource(worker.main)
    sweep = source.index("_sweep_stale_tempdirs")
    reclaim = source.index("reclaim_interrupted_jobs")
    assert sweep < reclaim, "clear the disk before taking work back"


def test_the_sweep_knows_the_child_processes_own_directories():
    """ocrmypdf and Ghostscript name their own scratch, and those are the
    largest directories in /tmp by a wide margin. Three of them, idle since
    the morning, filled the root disk on 2026-09-07 while the sweep walked
    past them looking only for the app's own prefixes."""
    from app import worker

    assert "ocrmypdf.io." in worker.TEMP_PREFIXES
    assert "gs_" in worker.TEMP_PREFIXES


def test_an_idle_directory_nobody_holds_is_swept_before_the_age_limit(
    tmp_path, monkeypatch
):
    """A day's age was the only criterion, and a killed job's scratch does not
    take a day to matter. Idle past the stall window with no process attached
    is dead — the watchdog would have killed anything that quiet."""
    import os

    from app import worker

    monkeypatch.setattr(worker.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(worker, "_dirs_held_open", lambda: set())
    abandoned = tmp_path / "ocrmypdf.io.dead"
    abandoned.mkdir()
    os.utime(abandoned, (0, 0))
    recent = tmp_path / "ocrmypdf.io.working"
    recent.mkdir()

    freed = worker._sweep_stale_tempdirs(max_age_seconds=86400)

    assert freed == 1
    assert not abandoned.exists()
    assert recent.exists(), "fresh scratch is a running job's"


def test_a_directory_a_process_holds_open_survives_however_idle(tmp_path, monkeypatch):
    import os
    import time as _time

    from app import worker

    monkeypatch.setattr(worker.tempfile, "gettempdir", lambda: str(tmp_path))
    held = tmp_path / "gs_busy"
    held.mkdir()
    # Idle past the stall window but well inside the day: only the
    # held-open check stands between it and deletion.
    os.utime(held, (_time.time() - 3600, _time.time() - 3600))
    monkeypatch.setattr(worker, "_dirs_held_open", lambda: {held})

    assert worker._sweep_stale_tempdirs(max_age_seconds=86400) == 0
    assert held.exists()


def test_a_cancelled_run_is_killed_by_its_own_watchdog(tmp_path, monkeypatch):
    """A lane that loses its job must not leave the OCR subprocess running:
    it rasterises into /tmp for nobody, and the recovered lane starts
    another beside it. During the 2026-09-07 outage that doubled the
    scratch on a disk already at its limit."""
    import time as _time

    from app.services.ocr import tesseract

    monkeypatch.setattr(tesseract.time, "sleep", lambda s: None)
    tesseract.request_cancel(tmp_path)
    code, reason = tesseract._run_watched(["sleep", "30"], tmp_path)
    assert code == 137 and "cancelled" in reason


def test_cancellation_reaches_the_subprocess_when_the_lane_fails(tmp_path):
    """The async side's only handle on the thread is the workdir; the marker
    it leaves there is what the watchdog reads."""
    from app.services.ocr import tesseract

    assert not tesseract.cancel_requested(tmp_path)
    tesseract.request_cancel(tmp_path)
    assert tesseract.cancel_requested(tmp_path)


def test_a_cancelled_apple_run_does_not_fall_back_to_tesseract(tmp_path, monkeypatch):
    """The fallback exists for a sidecar that is down, not for a job that
    is gone. When the four orphans of 2026-09-07 were killed, each started
    a Tesseract run of its own in a directory that no longer existed."""
    from app.services.ocr import apple, tesseract

    class _Fallback:
        called = False

        def process(self, *a, **k):
            _Fallback.called = True

    provider = apple.AppleVisionProvider(fallback=_Fallback())
    monkeypatch.setattr(provider, "sidecar_healthy", lambda: True)

    def _boom(*a, **k):
        raise tesseract.OCRError("killed: job cancelled")

    monkeypatch.setattr(provider, "_ocr_via_sidecar", _boom)
    tesseract.request_cancel(tmp_path)

    import pytest as _pytest

    with _pytest.raises(tesseract.OCRError):
        provider.process(tmp_path / "input.pdf", tmp_path, "redo", True)
    assert not _Fallback.called
