import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.config import settings
from app.database import engine
from app.services import startup
from app.routers import (
    annotations,
    auth,
    classify,
    devices,
    documents,
    insights,
    organize,
    rules,
    search,
    share,
    transfer,
    settings as settings_router,
    tags,
)


logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.require_strong_secret()
    migration_task = None
    if settings.manage_migrations:
        # Marked before uvicorn accepts anything, so no request can slip
        # through against a schema that has not been brought to head yet.
        startup.STATE.state = "starting"
        migration_task = asyncio.create_task(startup.run_migrations())
    yield
    if migration_task is not None and not migration_task.done():
        migration_task.cancel()
    await engine.dispose()


app = FastAPI(title=settings.app_name, lifespan=lifespan)

# Reachable while the schema is still being brought up to date; everything
# else is not. /api/health so container healthchecks keep working, /api/status
# so the UI can say what is going on rather than looking empty.
ALWAYS_OPEN = {"/api/status", "/api/health"}


@app.middleware("http")
async def refuse_until_schema_is_ready(request, call_next):
    """Serve the reason instead of the connection error.

    Routes must not run against a half-migrated schema, so this still refuses
    — but a 503 carrying the progress is something the UI can explain, where a
    dead upstream just looks like an empty library.
    """
    path = request.url.path
    if (
        startup.STATE.state != "ready"
        and path.startswith("/api")
        and path not in ALWAYS_OPEN
    ):
        body = startup.STATE.as_dict()
        body["detail"] = "The database is being upgraded. Nothing has been lost."
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content=body,
            headers={"Retry-After": "5"},
        )
    return await call_next(request)


@app.get("/api/status")
async def startup_status(response: Response) -> dict:
    """Unauthenticated on purpose: it is what the login screen polls, and it
    reveals only whether the schema is current."""
    body = startup.STATE.as_dict()
    if body["state"] != "ready":
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return body

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

API_PREFIX = "/api"
app.include_router(auth.router, prefix=API_PREFIX)
app.include_router(classify.router, prefix=API_PREFIX)
app.include_router(devices.router, prefix=API_PREFIX)
app.include_router(documents.router, prefix=API_PREFIX)
app.include_router(organize.router, prefix=API_PREFIX)
app.include_router(rules.router, prefix=API_PREFIX)
app.include_router(search.router, prefix=API_PREFIX)
app.include_router(annotations.router, prefix=API_PREFIX)
app.include_router(insights.router, prefix=API_PREFIX)
app.include_router(share.router, prefix=API_PREFIX)
app.include_router(transfer.router, prefix=API_PREFIX)
app.include_router(settings_router.router, prefix=API_PREFIX)
app.include_router(tags.router, prefix=API_PREFIX)


@app.get("/api/health")
async def health(response: Response) -> dict:
    # A container healthcheck must still see "not ready" during a migration,
    # or the stack reports healthy while every route is refusing.
    if startup.STATE.state != "ready":
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": startup.STATE.state, "database": False}
    # Readiness: Postgres is essential — report 503 without it.
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        db_ok = True
    except Exception:
        db_ok = False
    if not db_ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"status": "ok" if db_ok else "degraded", "database": db_ok}
