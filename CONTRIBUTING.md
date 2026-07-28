# Contributing

Thanks for looking. This is a personal project I use for my own document
library, so it is opinionated by design — `SCRINIUM.md` records what was decided
and why, including things deliberately *not* built. Reading the relevant entry
first will save you writing something I have already turned down.

## Before a large change

Open an issue and describe what you want to do. I would rather talk about a big
change before you build it than reject a finished pull request. Small fixes —
bugs, docs, tests — need no ceremony; just send them.

## Running it

```bash
docker compose up --build            # postgres + api + worker
cd frontend && npm install && npm run dev
```

The dev host ports are offset (Postgres `5434`, API `8010`) because other
projects on my machine hold the usual ones. Vite serves on `5173` and proxies
`/api` to the API.

On first run the UI routes to a setup page that creates the initial account.
That endpoint only works while the users table is empty.

## Tests

```bash
docker compose exec api python -m pytest tests/ -q
```

Tests run against a real Postgres database, created and migrated with the actual
Alembic migrations rather than `create_all`, so schema mistakes surface. CI runs
the same suite and must pass.

A bug fix should come with a test that fails before it and passes after. There
are examples in `backend/tests/test_security.py`, where each test names the
behaviour it protects.

## Style

- **Async SQLAlchemy throughout.** No sync sessions.
- **Migrations, not `create_all`.** Every schema change gets a numbered Alembic
  revision; `upgrade head` runs at container start.
- **Comments explain *why*.** The code already says what it does. If a line
  exists because of a specific failure, say which failure — that is the comment
  worth having.
- **Branding stays configuration.** The display name appears only via
  `settings.app_name` and `frontend/src/constants/branding.js`; never hard-code
  it in routes, schema, or env keys.
- **Originals are never modified.** OCR output is a separate archive blob. Any
  change that rewrites a stored original will be rejected.
- **Ingestion fails soft.** A document that cannot be processed gets flagged and
  the queue moves on. Nothing may drop a document or wedge the queue.
- Match the surrounding code rather than introducing a new idiom.

## Commit messages

Plain prose, present tense, explaining the reason for the change rather than
restating the diff. No tooling trailers, badges, or generated attributions in
committed content.

## Licensing

Contributions are accepted under the project's AGPL-3.0 license (see `LICENSE`).
By opening a pull request you agree your work may be distributed under it.
