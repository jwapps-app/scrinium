# Scrinium

> A self-hosted PDF document library, organizer, and viewer with local OCR.
> Effectively a Paperless-ngx replacement, deliberately built to fix its known weak spots.

*Scrinium* (Latin): the cylindrical case for holding scrolls and documents — the ancestor of "shrine." Sibling to **Bibliocapsa** (books); where Bibliocapsa holds the books, Scrinium holds the documents.

---

## Purpose

A single-user (with a path to multi-tenant) document management system that ingests PDFs and images, runs local OCR to make them searchable, and provides a clean library + viewer. Built because Paperless-ngx is mature but opinionated in ways that don't match how I want things — this is "Paperless, my way," targeting predictable personal/business document types rather than arbitrary-input robustness.

---

## Design Principles

1. **Storage identity is decoupled from display name.** Blobs are stored under stable, opaque keys (UUID or content hash). The human-readable name lives as metadata in Postgres and is applied only at download/export time. Originals are never renamed or mutated on disk. *(Fixes Paperless's file-naming mangling.)*
2. **OCR engine is pluggable.** `ocrmypdf` always does the PDF plumbing; the recognition engine behind it is swappable. Tesseract ships as the portable default; Apple Vision is an opt-in upgrade for Mac hosts. *(Fixes Paperless's Tesseract-only OCR ceiling.)*
3. **Classification is transparent and on-demand.** No silent, rot-prone pickle model. Reclassification is a visible, idempotent, user-triggered action. *(Fixes Paperless's fragile scikit-learn classification.)*
4. **Postgres-first.** No SQLite concurrency footguns. *(Fixes Paperless's default-SQLite locking issues.)*
5. **Fail-soft ingestion.** OCR failures keep the document, flag it, and move on. Consumer status and errors are surfaced in the UI, not buried in logs. *(Fixes Paperless's consumer fragility.)*
6. **PWA-first, real mobile experience.** Share-sheet-to-ingest flow on iOS. *(Fixes Paperless's lagging mobile experience.)*

---

## Stack

- **Backend:** FastAPI (Python), async SQLAlchemy per the fleet skeleton
- **Frontend:** React + Vite PWA
- **Database:** PostgreSQL (multi-tenant-ready)
- **Auth:** bcrypt + JWT access/refresh (kryptovox/genealogy pattern); first-run setup creates the initial user
- **Job queue:** Postgres-backed `jobs` table + dedicated worker container (`FOR UPDATE SKIP LOCKED`); no Redis unless a later need appears
- **Deployment:** Docker Compose + Portainer on the NAS, behind the shared Cloudflare Tunnel
- **OCR pipeline wrapper:** `ocrmypdf` (Ghostscript + unpaper + engine)
- **Default OCR engine:** Tesseract (in-container)
- **Optional OCR engine (server-side):** Apple Vision (native macOS sidecar)
- **Capture-time OCR:** Apple Vision on-device in the iOS companion app
- **iOS companion app:** Swift, system document scanner + on-device Vision OCR
- **Full-text search:** Postgres `tsvector` / `pg_trgm` (option to add Meilisearch/Typesense later)

---

## The Six Complaints Being Fixed

| # | Paperless pain point | Scrinium's answer |
|---|----------------------|-------------------|
| 1 | Mangled on-disk file naming (char stripping, path-length limits, `_01` collisions) | Opaque blob keys; pretty names are metadata only |
| 2 | Tesseract-only OCR, frequent "garbage" output | Pluggable engine; Apple Vision opt-in for better accuracy |
| 3 | Fragile, opaque scikit-learn classification (pickle rot, manual retrains) | Transparent, on-demand, idempotent classification (rules or LLM) |
| 4 | SQLite concurrency locking for new users | Postgres-first by default |
| 5 | Consumer fragility (inotify/polling confusion, hard OCR failures) | Fail-soft defaults; per-document status surfaced in UI |
| 6 | Lagging mobile app / clunky phone upload | PWA-first with iOS share-sheet ingest |

---

## Ingestion Pipeline

```
PDF / image arrives (watched folder, upload, or share-sheet)
  → dedup check (content hash)
  → rasterize pages to images (ocrmypdf / pdftoppm)
  → OCR via selected provider:
        - "tesseract" → ocrmypdf in-container (subprocess)
        - "apple"     → POST each page image to sidecar /ocr, receive text + boxes
  → overlay invisible searchable text layer (ocrmypdf) → PDF/A
  → store ORIGINAL untouched under opaque key
  → store searchable archive copy under opaque key
  → index text + metadata in Postgres
  → (optional) classification pass → suggested title / type / tags
  → surface status in UI (success / flagged / failed)
```

**Original-vs-archive split:** the untouched source is always preserved; the OCR'd searchable version is a separate blob. OCR never mutates the source.

**Skip/redo logic:** detect existing text layer; skip re-OCR of digital-native PDFs unless forced (skip / redo / force modes).

---

## OCR Provider Abstraction

A single interface both engines sit behind. The app calls `provider.ocr(file)` and doesn't care whether the work happens in-process or over HTTP.

```python
class OCRProvider(Protocol):
    def ocr(self, image_or_pdf) -> list[OCRBlock]:
        ...

# OCRBlock: { text: str, confidence: float, bbox: [x0, y0, x1, y1] }

class TesseractProvider:      # in-container, runs ocrmypdf/Tesseract as subprocess
    ...

class AppleVisionProvider:    # POSTs to macOS sidecar; falls back to Tesseract on failure
    ...
```

**Fallback rule:** `AppleVisionProvider` catches connection failure and falls back to Tesseract (or queues for retry). A stopped sidecar never wedges ingestion.

---

## The Apple Vision Add-On (Sidecar)

### What it is
A standalone **native macOS executable** (Swift) running a small HTTP server on the Mac host — **outside** the Docker container. It exists because Apple's Vision framework (`VNRecognizeTextRequest`) only runs on macOS and cannot run inside a Linux container. The HTTP boundary bridges that OS wall.

### Endpoints
- `GET /health` → `200 OK` so the Scrinium settings page can show "helper connected."
- `POST /ocr` → takes a page image, runs Vision `.accurate`, returns JSON.

### Response shape
```json
{
  "page": 3,
  "width": 1700,
  "height": 2200,
  "blocks": [
    { "text": "Invoice #4471", "confidence": 0.98, "bbox": [0.12, 0.90, 0.38, 0.94] }
  ]
}
```

### Vision config
- `recognitionLevel = .accurate`
- `usesLanguageCorrection = true`
- optional `recognitionLanguages`
- returns `VNRecognizedTextObservation` array: text + normalized (0–1) bounding box + confidence

### Division of labor
- **Container rasterizes** (it already does PDF work via `ocrmypdf`); the sidecar just takes images. Keeps the Swift piece simple.
- **`ocrmypdf` still does the PDF plumbing** (deskew, overlay, PDF/A). Vision only replaces the recognition engine in the middle.

### Two wiring options
- **Option A — `ocrmypdf` engine plugin (tightest):** write a plugin that calls the sidecar instead of Tesseract, mapping Vision boxes into ocrmypdf coordinates. Keeps 100% of ocrmypdf's output quality. Mirrors how `ocrmypdf-easyocr` works.
- **Option B — provider bypass (simpler to start):** `AppleVisionProvider` rasterizes → POSTs → collects text+boxes → builds the searchable layer itself (or just stores text for search). Faster to stand up; start here to prove the round-trip, graduate to A later.

### Networking
- Container reaches the Mac host at `host.docker.internal:PORT`.
- Sidecar binds a fixed local port; passed to the app via env var, e.g. `APPLE_OCR_URL=http://host.docker.internal:9876`.

### Lifecycle
- Run under `launchd` so it starts on login and restarts on crash.

### Known fiddly bit
- **Coordinate systems differ.** Vision origin is bottom-left, normalized 0–1; PDF/ocrmypdf may expect top-left / absolute points. Mismatches only affect copy-paste/highlight alignment, not search. Get the mapping right.

### Settings-page setup flow (UX)
The Scrinium UI can automate everything up to the macOS security boundary:
- Serve the sidecar download (binary / `.pkg` / notarized `.app`).
- Live health indicator (polls `/health`): connected / not detected.
- Generate a preconfigured `launchd` plist (correct port baked in) + one-line install script + copy-paste commands.
- Stepwise checklist that reacts to the health check.

What the UI **cannot** do (OS sandbox wall): silently install, register launchd, or grant permissions — the user runs one thing themselves. Speed bumps: Gatekeeper/notarization (needs Apple Developer ID, $99/yr, to avoid the "unidentified developer" dialog) and macOS permission prompts.

**Long-term ideal:** distribute as a small **notarized menu-bar `.app`** — drag to Applications, open once, self-registers to launch on login. Most Mac-native flow. Ship the download-plus-script version first, graduate to this.

---

## Three-Tier OCR Model (Apple quality across Mac, iPhone, and Mac-less setups)

The key insight: **iOS Vision is the same engine as macOS Vision.** `VNRecognizeTextRequest`, `.accurate`, same observation output (text + normalized boxes + confidence). An iPhone running current iOS produces OCR of the same character and quality as the Mac. iOS additionally has `VNDocumentCameraViewController` (system edge-detection scanner), making it arguably the *better* Vision host for the capture path.

Because a phone is intermittent and not addressable, it can't be a pull-model server the container calls (that's the Mac sidecar's role). Instead the phone uses a **push** model: it OCRs at capture time and sends results *up* to Scrinium. This yields three tiers:

| OCR source | Model | Applies to | Engine |
|------------|-------|-----------|--------|
| **Server-side (default)** | pull / in-process | Watched folder, web upload, email import, bulk PDF import | Tesseract |
| **Server-side (optional upgrade)** | pull / HTTP | Same server-side docs, when a **Mac host** runs the sidecar | Apple Vision (Mac sidecar) |
| **Capture-time** | push / on-device | Anything scanned with the iOS companion app | Apple Vision (on iPhone) |

### What this means

- **The iOS app should OCR on-device at capture and upload the text alongside the image/PDF.** This is strictly better than uploading raw pixels for later server OCR: recognition runs on the freshest, uncompressed image; capture + OCR are one step; and it offloads OCR work from the server. Same JSON contract as the Mac sidecar, so it drops into the existing `AppleVisionProvider` shape.

- **The iPhone makes the Mac sidecar *optional* for a large fraction of users.** Receipts, letters, mail, and forms are naturally phone-captured — so most Apple-quality OCR happens at capture on the phone, where the best image and the best engine already coincide. The Mac sidecar is then only needed to bring Apple quality to documents that arrive *server-side* on a machine that happens to be a Mac.

- **iPhone as backup:** partial, not a drop-in. The phone can own OCR for *phone-originated* docs when the Mac sidecar is down, but it can't OCR arbitrary server-side jobs (it's not an always-on addressable server). More natural framing: "the phone owns OCR for phone-captured docs" rather than "the phone backstops the Mac."

- **iPhone as Mac replacement (no-Mac user):** works for the *capture funnel*. A Mac-less user gets Apple-quality OCR for everything they scan with the app, and Tesseract for server-side docs. For users whose documents are mostly phone-captured, this removes the need for a Mac entirely.

### Honest caveat
This only upgrades documents that flow *through the iPhone*. A Mac-less user bulk-importing 500 existing PDFs from a folder still gets Tesseract on those, because that path never touches Apple hardware. Positioning: **"Apple-quality OCR for anything you scan with the app (Mac or iPhone); Tesseract for everything else unless you run the Mac sidecar."**

### Reframed architecture
Rather than "Tesseract default, optional Mac sidecar for Apple quality," the cleaner model is:
- **Server-side OCR engine:** Tesseract (default), optionally upgraded by a Mac sidecar if one exists.
- **Capture-time OCR:** iOS app runs Vision on-device and uploads text.
- **Mac sidecar is the specialized tool** for server-side Apple OCR — not the main Apple path, and unnecessary for many users.

---

## Conventions (from established fleet patterns)

Canonical patterns live in the Obsidian vault at `/Users/jworthington/knowledge`; these are the ones applied here.

### Git & GitHub
- Repo: `git@github.com:jwapps-app/scrinium.git` (org, never personal account).
- Commit messages in a plain, consistent voice; no tooling trailers or badges in committed content.

### Branding-as-config
- "Scrinium" appears only as `APP_NAME` (backend env → `config.py`) and `src/constants/branding.js` (frontend). UI strings and templates reference those, never the literal.
- Deliberate exception (decided 2026-07-09): the repo, Docker images (`scrinium-api`, `scrinium-web`), and future iOS bundle id (`com.jworthington.scrinium`) use the scrinium name rather than a generic category name.
- Routes, schema, env var keys stay name-free (`/api/documents`, not `/api/scrinium/...`).

### Backend skeleton
- Layout per the fleet FastAPI skeleton: `config.py` (pydantic-settings + `get_settings()`), `database.py` (async engine, `pool_pre_ping`, commit-on-success `get_db`), `deps.py`, `security.py`, `models/`, `routers/`, `schemas/`, `services/`.
- Async SQLAlchemy throughout (genealogy's sync pattern is the legacy exception).
- All routers under a single `/api` prefix; readiness `/api/health` returns 503 if Postgres is down.
- Alembic `upgrade head` at container start, not in app code.
- Strong-secret gate: production refuses to start on a weak/placeholder `SECRET_KEY`.
- Behind the tunnel, read `CF-Connecting-IP` for the true client IP.

### Deployment
- Two compose files: `docker-compose.yml` (dev, `build:`) and `docker-compose.portainer.yml` (NAS, image-only, `mem_limit`, env via Portainer UI).
- CI (GitHub Actions) builds and publishes private images to `ghcr.io/jwapps-app/scrinium-api` / `scrinium-web` (`:latest`, `:sha-<short>`, `:vX.Y.Z` on tags). The NAS never builds — Portainer pulls.
- Host: Synology DS1621+ (`192.168.1.10`). Web container publishes `${APP_PORT:-8220}` (8088/3300/8095/8210 and DSM/Portainer ports are taken).
- Public access via the shared Cloudflare Tunnel: `scrinium.example.com` → `192.168.1.10:8220`. No exposed ports.
- Nightly `pg_dump` sidecar (Fc/gzip, N-day retention); bind mounts under `/volume1/docker/scrinium/`.

### Push (deferred to iOS-app phase)
- When the iOS companion app lands, wire pushes through the shared **push-relay** per the standardized recipe: `{token, platform, environment}` registration body on `POST /api/devices`, single `notify_user()` dispatch seam, per-token environment self-heal, no-presence-skip, full dead-token reason set. Bundle id `com.jworthington.scrinium` added to the relay's `apps.keys`.

---

## Build Order (suggested)

1. Core backend: FastAPI + Postgres schema (documents, blobs, tags, tenants), opaque-key storage, upload endpoint.
2. Tesseract ingestion path via `ocrmypdf` (default, portable) — full round-trip to searchable + indexed.
3. React/Vite PWA: library list, viewer (PDF.js), search, per-document status.
4. Apple Vision sidecar (Option B provider bypass) — prove the round-trip.
5. Settings-page health check + guided sidecar setup.
6. Graduate Apple path to Option A (`ocrmypdf` plugin) for exact PDF/A parity.
7. Transparent classification (rules first; optional local-LLM pass later).
8. iOS companion app: system document scanner + **on-device Vision OCR at capture**, uploading image/PDF *plus* recognized text (same JSON contract as the Mac sidecar). Also covers the share-sheet ingest flow.

---

## Decisions Log

- **2026-07-09:** Repo/image/package name = `scrinium` (deliberate exception to the generic-naming rule). Auth = bcrypt + JWT access/refresh. Ingestion jobs = Postgres-backed queue + worker container (no Redis). Host port 8210, subdomain `scrinium.example.com`. Build order steps 1–3 scaffolded (backend core, Tesseract round-trip, PWA).
- **2026-07-09 (later):** Step 4 built — Swift sidecar (`sidecar/`, SwiftPM, Network.framework + Vision, env `OCR_HELPER_PORT`/`OCR_HELPER_LANGUAGES`) and `AppleVisionProvider` Option B (pdftoppm rasterize → POST pages → text-for-search; **no archive PDF on this path until Option A**, viewer falls back to the original and any prior archive is kept). Fallback verified: sidecar down → Tesseract, never wedges. Settings page (`/settings`) shows live helper health — the first slice of step 5. Providers receive a suffixed symlink to the blob (opaque keys carry no extension — don't dispatch on blob paths).
- **2026-07-09 (evening):** Steps 5–7 built. **Step 6 / Option A:** `apple_engine_plugin.py` is an ocrmypdf engine plugin (`python3 -m ocrmypdf --plugin app.services.ocr.apple_engine_plugin --pdf-renderer hocr`); Vision blocks → hOCR with the bottom-left→top-left coordinate flip; verified archive creator tag `OCRmyPDF … / Apple Vision sidecar 1.0` with extractable text layer. Option B's text-only path is superseded (OCRResult.archive_path stays optional). **Step 5:** guided setup checklist on `/settings` — `GET /api/settings/sidecar-setup` generates build commands, a port-baked launchd plist (label `com.example.scrinium-ocr-helper`), and server env; steps auto-check off the live health poll. **Step 7:** rules-based classification — `rules` table (contains/regex → add tag and/or set title, priority-ordered), `POST /api/documents/{id}/classify` and `POST /api/classify/run`, both idempotent (verified: second bulk run changes 0); managed in Settings, per-doc Classify button. Local-LLM classification pass remains deferred.

- **2026-07-09 (night):** Library UI pass (dense/utilitarian per decision). Sidebar shell (status buckets + tag counts + recent + nav; drawer on mobile), thumbnail card grid with list toggle, URL-param filters (status, tag, engine, date range, sort, `?q=` search). Thumbnails: first-page PNG (~480px) generated at ingest (poppler for PDFs, Pillow for images), stored as blobs (`documents.thumbnail_blob_id`, migration 0003), lazily backfilled by `GET /documents/{id}/thumbnail`; frontend fetches with auth and caches object URLs (`<img src>` can't carry the bearer token). `status_filter` accepts a comma list so the Processing bucket (pending+processing) is one query.

- **2026-07-09 (late night):** Step 8 built — iOS companion app (`ios/`, XcodeGen project, SwiftUI, iOS 16+, bundle `com.jworthington.scrinium`, team preset). Capture tier per the three-tier model: `VNDocumentCameraViewController` → on-device Vision `.accurate` OCR → PDF assembly → multipart upload with `ocr_text`/`ocr_engine`/`page_count`. Server side: `POST /api/documents` accepts those fields — captured docs go **straight to `ready` with no ingest job** (verified: instant ready, engine `apple`, searchable). Share extension (`com.jworthington.scrinium.share`) uploads shared PDFs/images for normal server-side OCR; credentials shared via App Group (Keychain migration deferred). Simulator-verified login + live library; camera/share-sheet need a real device. Push via relay still deferred — captures are ready instantly, nothing push-worthy yet.

- **2026-07-10:** Watched-folder ingest + iOS PDF viewer. **Watched folder:** worker sweeps `WATCH_DIR` (default `/data/watch`, so no extra mount) every `WATCH_POLL_SECONDS`; files go through the same `services/intake.py` path as uploads (extracted shared helper — upload endpoint refactored onto it); consumed files move to `.consumed/`, content-hash duplicates to `.duplicates/`, crashes to `.failed/` — nothing is ever deleted; 3s settle time skips mid-copy files; idles until the first tenant exists. Verified: drop-in consumed and OCR'd via Apple sidecar, duplicate correctly filed. **iOS:** document rows now open a PDFKit viewer streaming the archive with auth (verified in simulator via UI tap). Push remains deferred — now with a real trigger available (watched-folder completions), it's the natural next candidate.

- **2026-07-10 (later):** Push notifications per the fleet recipe. Server: `device_tokens` table (migration 0004, pk=token, idempotent upsert via `POST /api/devices` `{token, platform, environment}`, DELETE on sign-out), single `notify_tenant()` seam in `services/push.py` — relay contract with per-token `sandbox`, BadDeviceToken flip-retry **self-heal**, full dead-token prune set, loud 403 log, no presence-skip, never raises. Triggers: ingest job success ("ready to search") and flagged ("needs attention"). iOS: `PushService` (completion-handler delegates — not async, per the known UIKit snapshot crash), hex token, compile-time sandbox/production, re-register on foreground, `document_id` custom-data deep link into the viewer. Verified: registration upsert, real-relay 403 contract on a live watched-folder ingest (fail-soft confirmed), and simulated push → tap → app opened the exact document from background. **Remaining manual:** register the App ID (same team, no new .p8), add `com.jworthington.scrinium=<key>` to the relay's `apps.keys` + restart relay, set `PUSH_RELAY_URL`/`PUSH_RELAY_API_KEY` in server env, run the app on a real iPhone.

- **2026-07-10 (icon):** App icon chosen — **open scroll** (concept C): cream unrolled scroll with rolled ends + spiral end-caps, burnt-orange text lines, on the app's dark-stone `#1c1917` tile. Source of truth is `frontend/public/icon.svg`; rasters generated from it (qlmanage): PWA `icon-192/512.png` (+ maskable purpose in manifest), `favicon-32.png`, `apple-touch-icon.png`, and iOS `AppIcon` (single 1024, alpha-stripped — Apple rejects alpha). Deliberately reads as a sibling to Bibliocapsa's dark book tile. Regenerate rasters from the SVG if the mark changes.

- **2026-07-10 (progress + match jump):** Real OCR progress: `progress_plugin.py` implements ocrmypdf's ProgressBar hook (page-unit bars only) writing "done total" to `SCRINIUM_PROGRESS_FILE`; the worker polls it every 1.5s onto `jobs.pages_done/pages_total` (migration 0005); API exposes `progress` (0..1) on documents; UI shows % chips + bars on cards/rows/viewer (verified live: 5→25→46→67→87→100 on a 150-page force re-OCR; Vision did 30 pp in 4.9s). **Match jump:** `GET /documents/{id}/search?q=` splits `text_content` on `\f` in Postgres (same stemming as global search) and returns per-page snippets plus the literal matched words from ts_headline markers; viewer rewritten with scroll-position lazy rendering (**not IntersectionObserver — its callbacks and pdf.js's rAF-driven rendering freeze in hidden documents**), term-highlight overlays via text-item transforms, focus-page scroll with post-render realignment (estimated slot heights drift; snap once when the target page renders), match prev/next nav, find-in-document bar; search results deep-link `?q=`. Verified on the real 1,151-page scan: opened to p. 14 with "Blueberry" highlighted.

- **2026-07-10 (toolbar + auto-classify):** Viewer toolbar reworked per discussion. Single **Download** menu: Searchable (PDF/A) + Original, Original-only when no archive (captures/flagged) — note digital-native PDFs still get an archive (PDF/A conversion, text layer carried through; skip-mode only skips *recognition*). **Re-OCR is engine-aware**: shown only as "Re-OCR with Apple Vision" when server engine=apple AND sidecar healthy AND doc.ocr_engine != apple (a Tesseract-only user never sees it); flagged docs get "Retry OCR" regardless. **Classification now auto-runs after every successful OCR** (and on captured uploads) — deterministic + idempotent so safe; runs before the push so notifications carry rule-set titles; manual "Run classification" moved to the ⋯ overflow with Delete. Deferred: runtime engine toggle in Settings (needs DB-backed pref instead of env), iOS on-demand Re-OCR (phone as pull-based Vision engine for server docs; updates search text only, not the archive layer). PWA cannot use Apple Vision (no browser API) — that's the architectural reason for the native app + sidecar.

- **2026-07-10 (folder tags):** Watched folder now recurses into subfolders and each folder level becomes a tag (Paperless's subdirs-as-tags): `watch/Taxes/2023/x.pdf` → tags "Taxes", "2023" (created or reused per tenant). Consumed/duplicate/failed files keep their relative folder structure inside the filing dirs; emptied drop folders are pruned after each sweep. Verified: nested drop → tagged, OCR'd, filed, pruned.

- **2026-07-10 (tag hierarchy):** Tags can nest (`tags.parent_id`, migration 0007), Paperless semantics per decision: **applying a tag materializes its whole ancestor chain on the document** (all assignment routes — manual PATCH, rules, folder drops — via `services/tag_tree.with_ancestors`), so filters/counts need no recursive queries. Folder drops build the tree (`get_or_create_tag_path`; existing tags never re-parented by drops). Tags API: parent_id on create, PATCH rename/re-parent with cycle rejection, delete promotes children to root. Sidebar renders the indented tree; Settings gains a Tags manager (rename inline, re-parent dropdown, delete, add-with-parent). Verified: folder drop built Construction└Architecture with doc carrying both; tagging with only "Auto" also applied "Insurance"; cycle re-parent rejected 422. Known behavior: re-parenting later doesn't retroactively re-tag existing docs.

- **2026-07-10 (bulk-ingest scale):** Two changes for very large folder dumps (tested at 250GB scale in design, 6-file/2-batch/2-worker in practice). Sweeps are **batch-capped** (`WATCH_BATCH_SIZE`, default 25) so intake interleaves with OCR instead of starving it for hours. Workers are **horizontally scalable** (`WORKER_REPLICAS` in the Portainer stack, or `--scale worker=N` dev): job claims were already replica-safe (SKIP LOCKED); the watch sweep now takes a Postgres advisory lock so exactly one replica sweeps. Verified: 6 files consumed as 3 sweeps of 2 with both workers pulling jobs and zero duplicate ingestion. Bulk-run guidance: ~3× corpus disk headroom (originals + archives; clear `.consumed/` as you go), stage-then-`mv` into the watch dir for multi-hour copies.

- **2026-07-10 (PDF/A remedy chain + status polish):** Some born-digital PDFs use print-industry color (DeviceN/spot, overprint) that Ghostscript can't carry into strict PDF/A ("inappropriate alternate" → exit 1). ocrmypdf runs now use a remedy chain: strict PDF/A → retry `--color-conversion-strategy RGB` (still PDF/A) → retry `--output-type pdf` (plain searchable PDF; archival format sacrificed for that document, search kept). Fallbacks trigger only on Ghostscript/PDF-A-shaped stderr, so corrupt inputs still fail once, fast; fallback use is logged. Status wording: "ready" renamed to **Completed** in UI, and completed documents show **no status chip at all** (web + iOS) — only pending/processing/flagged badge; sidebar bucket renamed.

- **2026-07-10 (bulk actions):** Library gains selection mode (Select button → click cards/rows to toggle, Select-all-filtered) with a bulk bar: **Re-OCR** (skip mode — retry/upgrade), **Tag** / **Untag** (tag menu; add applies ancestors per hierarchy semantics, remove removes exactly the chosen tag), **Delete** (confirm; shares the single-delete blob-cleanup path). One endpoint: `POST /api/documents/bulk` `{ids, action, mode?, tag_ids?}`, ≤500 ids, foreign/unknown ids skipped and reported. Verified: UI select→tag→untag round-trip, bulk reprocess upgraded two tesseract-era docs to apple.

- **2026-07-10 (pause/resume):** Processing queue can be paused: the in-flight job always finishes; new job claims AND watch-folder sweeps hold until resume. Flag lives in Postgres (`app_settings` key-value, migration 0008) so **pausing survives restarts** — the intended workflow for rebooting the NAS or the Mac (Apple OCR host) mid-batch without losing work or silently degrading scans to Tesseract. API: `POST /api/documents/processing {paused}`; state rides `/documents/stats`. Sidebar shows ⏸ Pause / ▶ Resume whenever the queue is active or paused (hidden when idle). Verified: paused mid-batch → current 150pp finished, queued doc held; worker restart while paused → came back paused; resume → processed.

- **2026-07-10 (consumed-copy cleanup):** Documents ingested via the watch folder record where their filed copy went (`documents.source_path`, migration 0009, relative to WATCH_DIR). Deleting the document (single or bulk) now also removes that `.consumed/` copy and prunes emptied folders — path-checked to never reach outside the watch dir. The api service now gets WATCH_DIR in both compose files (deletion runs there). Pre-existing consumed copies (no source_path) still need manual clearing. `.duplicates/`/`.failed/` untouched by design.

- **2026-07-10 (Synology junk + filter-wide cleanup):** The watcher now skips path parts starting with `.`, `@`, or `#` — covering Synology's `@eaDir` thumbnail-metadata dirs (whose SYNOFILE_THUMB_* images were being ingested as documents during a big NAS dump), `@Recycle`, `#recycle`/`#snapshot`, and AppleDouble files. Cleanup tooling: bulk endpoint accepts `filter_tag_id` to act on *every* document carrying a tag (deletes chunked at 500/request with `remaining`; row-only actions single-pass) — UI gains "Entire filter (N)" in the bulk bar when a tag filter is active; `DELETE /api/tags/unused` + Settings button removes all zero-document tags, collapsing emptied parent chains bottom-up. Pause button now shows an alert on request failure and toggles optimistically.

- **2026-07-11 (Tier 1 Paperless parity):** Six features in one migration (0010). **Document dates:** extracted from text at ingest (regex heuristics, first plausible match in reading order, `DATE_ORDER` env for ambiguous numerics), editable in the details strip; date filters/sort use `COALESCE(doc_date, created)`; Settings has a backfill button. **Correspondents & document types:** first-class entities with counts, CRUD, sidebar section (correspondents) and toolbar filter (types); rules can assign both (never stomping set values); document cards show correspondent + doc date. **Trash:** deletes are soft (`deleted_at`); hidden from lists/search/stats; sidebar Trash bucket, per-doc banner with Restore/Delete-forever, trash-mode bulk bar (Restore / Delete forever / Entire trash); worker purges past `TRASH_RETENTION_DAYS` (default 30) hourly under an advisory lock — purge is when blobs/files/consumed-copies actually go. **Saved views:** current filter combo saved by name to the sidebar (× to remove). **Custom fields:** typed field definitions (text/number/date/money/url/bool) managed in Settings, values edited on the document details strip. **Email ingestion:** worker polls IMAP (`MAIL_HOST/USERNAME/PASSWORD`, app password for Gmail), consumes PDF/image attachments from unseen messages with an "Email" tag and sender-as-correspondent, marks seen, fail-soft, advisory-locked, status surfaced in Settings. Beautification: document details strip replaces bare tag chips, card hover lift, sidebar Views/Correspondents sections.

- **2026-07-11 (OCR resilience):** Hardened the pipeline against the failure classes a big real-world corpus surfaced. The ocrmypdf remedy chain now escalates: strict PDF/A → `--color-conversion-strategy RGB` → **`--force-ocr --continue-on-soft-render-error --output-type pdf`** (rebuilds every page from fresh raster, discarding bad halftone dicts that cause `rangecheck in setscreen` exit 7, and corrupt embedded JPEGs that cause exit 4). Retry gate broadened: escalate on any failure except known-unfixable (encrypted/bad-args). Final safety net `text_only_fallback`: if the entire ocrmypdf/Ghostscript pipeline fails, extract text with **poppler + Tesseract only (never Ghostscript)** — pdftotext for existing layers, else pdftoppm raster → tesseract per page; stores text-only (engine `text-only`, no archive, viewer falls back to original) so no document is ever a searchable dead end. Both engines route through `process_with_fallbacks`. Verified: raster recovery of an image-only page, and the OCRError→text-only handoff.

- **2026-07-11 (progress in sidebar + ETAs):** `/documents/stats` now returns live processing telemetry: `current` (running job's title, progress, phase, per-file `eta_seconds` from its own page rate), `running_count`, `rate_per_min` (docs completed in the last 5 min — parallelism-aware), and `queue_eta_seconds` (remaining ÷ rate). Sidebar gained a processing panel under the pause button: current-file bar + ETA, and an overall **queue burndown bar** (client-side high-water-mark of the queue; fills 0→100% as the backlog drains, dips when a fresh wave arrives) with "N in queue · ETA". Shell polls every 2.5s while active, 15s idle. ETAs are deliberately coarse (`formatEta`: <1m / ~Nm / ~Nh Nm). Verified: per-file ETA counting down, queue draining 2→1→0 with rate climbing, panel text + bars in the DOM.

- **2026-07-11 (worker concurrency):** Worker refactored into N independent processor lanes + one maintenance lane (`asyncio.gather`); `WORKER_CONCURRENCY` (default 1) sets documents processed at once per container — fills the idle time each doc spends on the OCR round-trip to the sidecar. SKIP LOCKED already makes lanes claim distinct jobs; pause is honored per lane; watch/mail/purge sweeps stay singular in the maintenance lane (advisory locks). Lighter than `WORKER_REPLICAS` (no duplicated runtime); the two compose. Verified: concurrency=2 → running_count 2. Guidance: 2–3 is the sweet spot on the DS1621+ (4c/8t); watch NAS + Mac CPU and back off if either saturates.

- **2026-07-11 (interrupted-job recovery):** Worker now requeues jobs left RUNNING by a killed container (redeploy / crash / NAS reboot) at startup — resets them to QUEUED and their docs to PENDING so nothing strands in "processing" forever; reprocessing is idempotent. Makes a mid-import redeploy safe even without waiting for the in-flight file. Safe for single-container (concurrency) setups; multiple worker *replicas* would need a heartbeat instead (noted in code). Verified: killed worker mid-OCR → orphaned RUNNING job → restart requeued and completed it.

- **2026-07-11 (concurrency-safe: stacked bars, sidecar hardening):** Stats `running` is now a list (all in-flight jobs, ordered by start time so slots stay stable) with per-file progress/phase/ETA; sidebar stacks a bar per concurrent file plus the overall queue burndown + queue ETA. **Found and fixed a real sidecar wedge:** under concurrent load (multiple worker lanes × ocrmypdf `-j` fan-out) the Swift sidecar exhausted its dispatch thread pool and hung — even /health stopped answering, stalling all OCR at 0%. Fix: recognition runs on a dedicated concurrent queue gated by a semaphore (`OCR_HELPER_MAX_CONCURRENCY`, default 4); each connection gets its own I/O queue so the listener never starves. Client fan-out also bounded via `OCR_JOBS` (default 3). Verified: 3 lanes advancing in parallel (0→80%) with the sidecar responsive on every probe. **Requires reinstalling the sidecar** (swift build + sudo cp + launchctl kickstart) — the launchd binary is the old one until then.

- **2026-07-11 (mobile polish round 2):** Hamburger moved clearly inside the screen edge (`left: max(1.1rem + safe-area-inset-left, 1.1rem)`) with border + shadow so it reads as a button, not a stray glyph. Library gained fixed-density grid options — ▦ (auto-fill), **3× and 4× tiles across**, ☰ list — with compact typography at 3×/4× (4× shows title only). Density is a remembered preference (localStorage) so it survives navigation, but a saved view's explicit `?view=` still wins. **Login/Setup autofill fixed:** inputs had `autocomplete` but no `name`/`id`, so iOS password managers didn't recognize the form; added `name`/`id` plus `autocapitalize=none`/`autocorrect=off`/`inputmode=email` on email. Setup keeps `new-password` so Keychain offers to save on first run.

## Open Questions / Deferred

- Notarized menu-bar app vs. plain binary + script for v1 of the sidecar.
- Classification: deterministic rules vs. local LLM (Ollama) — start rules, evaluate LLM.
- Search backend: stay on Postgres FTS vs. add Meilisearch/Typesense at scale.
- Multi-tenancy: single-user pilot first; tenant isolation hardening before any shared deployment.
