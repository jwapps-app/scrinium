"""Stay reachable through the upgrade instead of refusing connections.

Migrations used to run in the entrypoint, before uvicorn was started. That is
correct about the schema — serving a request against a table that has not been
altered yet is worse than a wait — but it makes the wait invisible: nginx has
nothing to proxy to, every /api call fails, and the library renders its
first-run empty state. "Drop a PDF here to get started" is a alarming thing to
read after a deploy, and the obvious reactions to it (re-run setup, restore an
export, recreate the stack) are the ones that actually destroy data.

So the API now starts first and applies migrations behind a gate. Every route
still refuses to serve until the schema matches — the guarantee is unchanged —
but it refuses with a 503 that says what is happening, how far along it is and
that nothing has been lost, which the UI turns into a progress panel.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy.exc import InterfaceError, OperationalError

from app.config import settings
from app.database import engine

logger = logging.getLogger(__name__)

BACKEND_DIR = Path(__file__).resolve().parents[2]

# Postgres SQLSTATEs worth another attempt. 55P03 is the lock timeout env.py
# deliberately provokes rather than queueing behind the nightly dump; the rest
# are the database still coming up, which is routine when the whole stack
# starts at once.
LOCK_NOT_AVAILABLE = "55P03"
RETRYABLE_SQLSTATES = {
    LOCK_NOT_AVAILABLE,
    "57P03",  # cannot_connect_now — starting up
    "57P01",  # admin_shutdown
    "53300",  # too_many_connections
}
LOCK_RETRY_SECONDS = 15.0
CONNECT_RETRY_SECONDS = 3.0


@dataclass
class StartupState:
    """What the API is doing before it can serve. Read by /api/status.

    Defaults to ready: the tests drive the ASGI app without a lifespan, and a
    gate that fails closed there would 503 the entire suite rather than the
    one thing it is meant to guard.
    """

    state: str = "ready"  # ready | starting | migrating | failed
    message: str = ""
    step: int = 0
    total: int = 0
    current: str | None = None
    target: str | None = None
    error: str | None = None
    started_at: float = field(default_factory=time.monotonic)

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "state": self.state,
            "message": self.message,
            "step": self.step,
            "total": self.total,
            "elapsed_seconds": round(time.monotonic() - self.started_at, 1),
        }
        if self.current or self.target:
            payload["revision"] = {"current": self.current, "target": self.target}
        if self.error:
            payload["error"] = self.error
        return payload


STATE = StartupState()


def _config() -> Config:
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    # env.py reconfigures logging from alembic.ini when it owns the process.
    # In-process that would tear down the app's own handlers mid-request.
    cfg.attributes["in_app"] = True
    cfg.attributes["on_version_apply"] = _version_applied
    return cfg


def _version_applied(ctx, step, heads, run_args) -> None:  # noqa: ANN001
    """Alembic's per-step hook — real progress rather than a parsed log line."""
    STATE.step += 1
    doc = (getattr(step, "doc", "") or "").strip()
    revision = getattr(step, "up_revision_id", None) or "?"
    STATE.message = f"{revision} {doc}".strip()
    logger.info("migration %s applied (%s of %s)", revision, STATE.step, STATE.total)


async def _pending(cfg: Config) -> tuple[str | None, str | None, int]:
    """(current, head, how many migrations separate them)."""
    script = ScriptDirectory.from_config(cfg)
    head = script.get_current_head()
    async with engine.connect() as conn:
        current = await conn.run_sync(
            lambda sync_conn: MigrationContext.configure(sync_conn).get_current_revision()
        )
    if current == head:
        return current, head, 0
    return current, head, len(list(script.iterate_revisions(head, current or "base")))


def _retry_delay(exc: BaseException) -> float | None:
    """Seconds to wait before another attempt, or None to give up.

    A migration that is merely queued behind something is worth retrying; one
    whose SQL is wrong is not, and looping on it for ten minutes only delays
    the report. The shell loop this replaces could not tell the difference.
    """
    orig = getattr(exc, "orig", exc)
    sqlstate = getattr(orig, "sqlstate", None)
    if sqlstate in RETRYABLE_SQLSTATES:
        return LOCK_RETRY_SECONDS if sqlstate == LOCK_NOT_AVAILABLE else CONNECT_RETRY_SECONDS
    # No SQLSTATE means the conversation never got far enough to have one:
    # Postgres not accepting connections yet, DNS not up, socket refused.
    if isinstance(exc, (OperationalError, InterfaceError)) or isinstance(orig, OSError):
        return CONNECT_RETRY_SECONDS
    return None


async def run_migrations(max_attempts: int | None = None) -> None:
    """Bring the schema to head, publishing progress as it goes."""
    if max_attempts is None:
        max_attempts = settings.migration_max_attempts
    cfg = _config()
    STATE.state = "starting"
    STATE.message = "Checking the database schema"

    for attempt in range(1, max_attempts + 1):
        try:
            current, head, total = await _pending(cfg)
        except Exception as exc:
            delay = _retry_delay(exc)
            if delay is None or attempt >= max_attempts:
                _fail(exc, "could not read the schema version")
                return
            STATE.message = "Waiting for the database"
            await asyncio.sleep(delay)
            continue

        STATE.current, STATE.target, STATE.total = current, head, total
        if total == 0:
            logger.info("migrations: already at %s, nothing to apply", head)
            _ready()
            return
        break
    else:
        _fail(RuntimeError("database never became reachable"), "database unreachable")
        return

    STATE.state = "migrating"
    STATE.step = 0
    STATE.message = f"Applying {total} database {'change' if total == 1 else 'changes'}"
    logger.info("migrations: applying %s -> %s (%s steps)", current or "none", head, total)

    for attempt in range(1, max_attempts + 1):
        try:
            # env.py calls asyncio.run(), which cannot run inside this loop —
            # a worker thread has no running loop of its own, so it can.
            await asyncio.to_thread(command.upgrade, cfg, "head")
        except Exception as exc:
            delay = _retry_delay(exc)
            if delay is None or attempt >= max_attempts:
                _fail(exc, "migration failed")
                return
            STATE.step = 0  # the whole upgrade is one transaction; it rolled back
            STATE.message = (
                f"Database busy — retrying (attempt {attempt + 1} of {max_attempts})"
            )
            logger.warning("migrations: attempt %s failed (%s); retrying", attempt, exc)
            await asyncio.sleep(delay)
            continue
        logger.info("migrations: now at %s", head)
        _ready()
        return


def _ready() -> None:
    STATE.state = "ready"
    STATE.message = ""
    STATE.error = None


def _fail(exc: BaseException, what: str) -> None:
    STATE.state = "failed"
    STATE.message = what
    STATE.error = str(exc)[:2000]
    logger.error("startup: %s: %s", what, exc)
