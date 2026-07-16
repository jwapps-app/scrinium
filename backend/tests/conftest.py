"""Test harness: isolated Postgres database, real migrations, ASGI client.

Environment is pinned BEFORE any app import (settings are an import-time
singleton). The test database is dropped and recreated per run, then brought
to head with the real alembic migrations — so tests exercise the same schema
(including the FTS generated column) that production containers get.
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]

TEST_DB_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://app:app@localhost:5434/scrinium_test",
)

os.environ["DATABASE_URL"] = TEST_DB_URL
# Counts must be fresh per request in tests — no stats micro-cache.
os.environ["STATS_CACHE_SECONDS"] = "0"
os.environ["ENVIRONMENT"] = "development"
os.environ["SECRET_KEY"] = "test-secret-key-that-is-long-enough-000"
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="scrinium-test-data-")
os.environ["WATCH_DIR"] = ""
os.environ["OCR_ENGINE"] = "tesseract"
os.environ["APPLE_OCR_URL"] = ""
os.environ["ALLOWED_ORIGINS"] = "http://testserver"

import psycopg2  # noqa: E402
import pytest  # noqa: E402


def _admin_dsn() -> str:
    # Same server, maintenance database, sync driver.
    url = TEST_DB_URL.replace("postgresql+asyncpg://", "postgresql://")
    base, _, _dbname = url.rpartition("/")
    return f"{base}/postgres"


def _test_dbname() -> str:
    return TEST_DB_URL.rpartition("/")[2]


def _recreate_database() -> None:
    conn = psycopg2.connect(_admin_dsn())
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(
            f'DROP DATABASE IF EXISTS "{_test_dbname()}" WITH (FORCE)'
        )
        cur.execute(f'CREATE DATABASE "{_test_dbname()}"')
    conn.close()


def _migrate() -> None:
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=BACKEND_DIR,
        env={**os.environ},
        check=True,
        capture_output=True,
    )


_recreate_database()
_migrate()


@pytest.fixture(scope="session")
async def client():
    import httpx

    from app.main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c

    from app.database import engine

    await engine.dispose()


@pytest.fixture(scope="session")
async def token(client):
    """First-run setup creates the user; later calls just log in."""
    creds = {"email": "tester@example.com", "password": "testpassword1"}
    resp = await client.post("/api/auth/setup", json=creds)
    if resp.status_code not in (200, 201):
        resp = await client.post("/api/auth/login", json=creds)
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


@pytest.fixture(scope="session")
async def auth(token):
    return {"Authorization": f"Bearer {token}"}


def make_pdf(pages: int = 1, text: str = "hello") -> bytes:
    """A real, valid PDF built with pikepdf (blank pages + metadata title
    carrying the text so content hashes differ per call)."""
    import io

    import pikepdf

    pdf = pikepdf.new()
    for _ in range(pages):
        pdf.add_blank_page(page_size=(612, 792))
    with pdf.open_metadata() as meta:
        meta["dc:title"] = text
    buf = io.BytesIO()
    pdf.save(buf)
    return buf.getvalue()


@pytest.fixture()
def pdf_factory():
    return make_pdf
