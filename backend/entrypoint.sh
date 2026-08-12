#!/bin/sh
set -e

if [ "$1" = "worker" ]; then
    # Migrations are the api container's job; the worker just waits for schema.
    exec python -m app.worker
fi

# The API cannot serve before the schema matches the code — reading a column
# that does not exist yet is worse than a short wait. But most deploys carry no
# schema change at all, and those should not wait for anything.
#
# Checking costs one SELECT on alembic_version, which takes the same shared
# lock pg_dump holds and so is never blocked by it. Only DDL conflicts.
current=$(alembic current 2>/dev/null | grep -oE '^[0-9a-f]+' | head -1)
head=$(alembic heads 2>/dev/null | grep -oE '^[0-9a-f]+' | head -1)

if [ -n "$current" ] && [ "$current" = "$head" ]; then
    echo "migrations: already at $head, nothing to apply"
else
    # There is a real migration to run. It needs an exclusive lock, and
    # env.py sets a short lock_timeout so a failure to get one releases the
    # queue immediately instead of parking every other query behind it. Retry
    # until the coast is clear — during the nightly dump that is a few
    # attempts. The API is deliberately down for this; it is the only case
    # where that is the right answer.
    attempt=1
    max_attempts="${MIGRATION_MAX_ATTEMPTS:-40}"
    echo "migrations: applying ${current:-none} -> $head"
    until alembic upgrade head; do
        if [ "$attempt" -ge "$max_attempts" ]; then
            echo "migrations: could not acquire a lock after $attempt attempts" >&2
            exit 1
        fi
        echo "migrations: lock busy (attempt $attempt/$max_attempts), retrying in 15s" >&2
        attempt=$((attempt + 1))
        sleep 15
    done
fi

# Share-link tokens are path segments; nginx already logs requests, and the
# uvicorn access log would duplicate those tokens into container stdout.
exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --no-access-log
