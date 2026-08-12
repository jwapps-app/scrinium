#!/bin/sh
set -e

if [ "$1" = "worker" ]; then
    # Migrations are the api container's job — but this genuinely has to wait
    # for them, which it previously only claimed to do. The worker starts
    # querying at once with code that expects the new schema, so a deploy whose
    # migration is delayed (behind the nightly dump, say) leaves it crash-
    # looping on UndefinedColumn until the schema catches up. Observed for
    # eleven minutes. Nothing was lost only because the failure landed on the
    # claim query itself, before any job's attempt counter moved; a few lines
    # later and every restart would have burned an attempt on a real document.
    attempt=1
    max_attempts="${SCHEMA_WAIT_ATTEMPTS:-120}"
    while :; do
        current=$(alembic current 2>/dev/null | grep -oE '^[0-9a-f]+' | head -1)
        head=$(alembic heads 2>/dev/null | grep -oE '^[0-9a-f]+' | head -1)
        if [ -n "$current" ] && [ "$current" = "$head" ]; then
            break
        fi
        if [ "$attempt" -ge "$max_attempts" ]; then
            echo "worker: schema still at ${current:-unknown}, wanted $head" >&2
            exit 1
        fi
        echo "worker: waiting for schema (${current:-none} -> ${head:-?})" >&2
        attempt=$((attempt + 1))
        sleep 5
    done
    exec python -m app.worker
fi

# Migrations are the API's own first act now (app/services/startup.py), not a
# step in front of it. The schema guarantee is unchanged — no route serves
# until it is at head — but running it in-process means the container is
# listening throughout, so /api/status can report progress and the UI can say
# "upgrading" instead of showing an empty library and inviting the user to
# recreate the stack over it. MANAGE_MIGRATIONS=false hands the job back.

# Share-link tokens are path segments; nginx already logs requests, and the
# uvicorn access log would duplicate those tokens into container stdout.
exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --no-access-log
