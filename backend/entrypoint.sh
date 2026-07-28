#!/bin/sh
set -e

if [ "$1" = "worker" ]; then
    # Migrations are the api container's job; the worker just waits for schema.
    exec python -m app.worker
fi

alembic upgrade head
# Share-link tokens are path segments; nginx already logs requests, and the
# uvicorn access log would duplicate those tokens into container stdout.
exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --no-access-log
