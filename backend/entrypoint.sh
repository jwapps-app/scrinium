#!/bin/sh
set -e

if [ "$1" = "worker" ]; then
    # Migrations are the api container's job; the worker just waits for schema.
    exec python -m app.worker
fi

alembic upgrade head
exec uvicorn app.main:app --host 0.0.0.0 --port 8000
