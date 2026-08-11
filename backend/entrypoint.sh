#!/bin/sh
set -e

if [ "$1" = "worker" ]; then
    # Migrations are the api container's job; the worker just waits for schema.
    exec python -m app.worker
fi

# Migrations take an exclusive lock, and Postgres grants locks in order — so a
# DDL statement waiting behind the nightly pg_dump also parks every ordinary
# read that arrives after it. The API stays up and answers nothing, which reads
# as the whole app hanging on "loading" until the dump finishes.
#
# env.py sets a short lock_timeout so the migration gives up instead of
# queueing, which frees everyone else immediately. Here we just try again until
# it gets a clear run. Backup windows are minutes, so this converges quickly;
# the ceiling only exists so a genuinely stuck lock surfaces as a failed start
# rather than an infinite loop.
attempt=1
max_attempts="${MIGRATION_MAX_ATTEMPTS:-40}"
until alembic upgrade head; do
    if [ "$attempt" -ge "$max_attempts" ]; then
        echo "migrations: still could not acquire a lock after $attempt attempts" >&2
        exit 1
    fi
    echo "migrations: lock busy (attempt $attempt/$max_attempts), retrying in 15s" >&2
    attempt=$((attempt + 1))
    sleep 15
done

# Share-link tokens are path segments; nginx already logs requests, and the
# uvicorn access log would duplicate those tokens into container stdout.
exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --no-access-log
