# Scrinium

Self-hosted PDF document library with local OCR. A deliberate
[Paperless-ngx](https://github.com/paperless-ngx/paperless-ngx) replacement
that fixes its known weak spots — see `SCRINIUM.md` for the full design,
rationale, and a dated log of every decision.

*Scrinium* (Latin): the cylindrical case for holding scrolls — the ancestor
of "shrine."

## What it does

- Ingest PDFs and images by upload (chunked for big files), watched folder
  (folder names become tags), email, or the iOS companion app.
- OCR locally: Tesseract in-container by default, **Apple Vision** via a Mac
  sidecar for noticeably better results, or capture-time Vision OCR from the
  iOS app. Failures degrade gracefully — worst case a document lands
  searchable-text-only, never lost.
- Organize with hierarchical tags (colors, ancestor materialization),
  correspondents, document types, custom fields, notes, document dates
  extracted from the text, saved views, and transparent classification rules
  (no trained models, by design).
- Find things with Postgres full-text search, jump-to-match inside the PDF
  viewer, and an insights dashboard.
- Manage safely: soft-delete trash with retention, page operations
  (rotate/delete/split), share links, bulk actions, live progress with honest
  ETAs, pause/resume that survives restarts.
- Leave any time: one-click full-library export (originals + archives +
  metadata manifest). Import from Paperless-ngx with metadata intact.

Originals are never mutated: blobs live under opaque keys, the OCR'd archive
is a separate file, and pretty filenames are applied only at download.

## Layout

```
backend/    FastAPI + async SQLAlchemy; Postgres job queue; alembic migrations
frontend/   React + Vite PWA; nginx serves the build and proxies /api
sidecar/    Swift Apple Vision OCR helper for a Mac host (see its README)
```

## Dev loop

```bash
docker compose up --build          # postgres (:5434) + api (:8010) + worker
cd frontend && npm install && npm run dev    # vite on :5173, proxies /api
```

First run routes to a setup page that creates the initial user.

## Tests

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt -r requirements-dev.txt
.venv/bin/pytest
```

Needs the dev postgres from `docker compose up` (tests use their own
`scrinium_test` database, dropped and re-migrated each run). CI runs the
suite before publishing images — a red test never ships.

## Deployment

CI publishes `ghcr.io/jwapps-app/scrinium-api` and `-web`;
`docker-compose.portainer.yml` is the image-only stack for the host
(Portainer or plain compose). All host-specific paths and tuning are env
vars — see the compose file for the full list.

## Accounts and roles

A deployment is one shared library. Everyone signed in sees and can organize
every document — there are no per-document permissions, by design; this is built
for a household or a person, not for separating colleagues from each other.

Two roles:

- **Owner** — the account created by first-run setup. Manages accounts, changes
  settings that affect the whole box (OCR engine, archive DPI cap, pausing the
  queue), runs imports and exports, and is alone able to do the things that
  cannot be undone: permanently deleting documents, emptying the trash, deleting
  tags/correspondents/types, and library-wide reprocessing.
- **Member** — every account the owner adds. Uploads, reads, searches,
  organizes, annotates, shares, and moves documents to the trash. Everything a
  member can do is reversible by the owner.

Accounts are created by the owner in Settings; there is no open registration.
If you need people to keep documents *away* from each other, run separate
deployments — isolation between accounts in one library is not something this
provides.

## Contributing

Bug reports and pull requests are welcome — see `CONTRIBUTING.md` for how to run
the stack and the tests, and the conventions the code follows. Please read the
relevant `SCRINIUM.md` entry before proposing a large change; some things are
deliberately absent rather than missing.

## Security

Please report vulnerabilities privately via GitHub's security advisories rather
than a public issue. `SECURITY.md` has the details, including the threat model —
notably that blobs are stored unencrypted and the database is trusted, so
filesystem or database access is outside what the application defends against.

## License

[AGPL-3.0](LICENSE). You may run, modify, and redistribute this; if you offer it
to others as a network service, your modified source has to be available to them
too.
