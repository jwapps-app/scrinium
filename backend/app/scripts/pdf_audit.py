"""Read-only image-resolution audit for the document library.

For every non-deleted document it inspects the blob the app actually serves
(the OCR archive when present, else the original) and, using poppler's
`pdfimages -list`, records the highest embedded-image DPI and the current
blob size. It then reports the DPI distribution and, for a chosen target DPI,
a rough estimate of the space a downsample-to-target pass could reclaim.

Nothing is written or mutated — this only measures. Run it inside the api
container, which already has poppler-utils:

    docker compose exec api python -m app.scripts.pdf_audit
    docker compose exec api python -m app.scripts.pdf_audit --target 300 --top 40
    docker compose exec api python -m app.scripts.pdf_audit --limit 500   # quick sample
    docker compose exec api python -m app.scripts.pdf_audit --csv /data/pdf_audit.csv

The reclaim estimate scales the blob size by (target/dpi)^2 (image pixel-area
ratio) for docs above the target. It is deliberately rough: a blob is not
purely image data and codec compression is non-linear, so treat it as an
order-of-magnitude guide, not a promise. Lossless recompression
(ocrmypdf --optimize) reclaims further on top of this and is not modelled here.
"""

from __future__ import annotations

import argparse
import asyncio
import csv as csvmod
import subprocess
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import func, select

from app.database import SessionLocal
from app.models.blob import Blob
from app.models.document import Document
from app.services import storage

# DPI buckets for the histogram (upper bound inclusive; last is open-ended).
BUCKETS: list[tuple[str, float]] = [
    ("≤150", 150),
    ("151–300", 300),
    ("301–450", 450),
    ("451–600", 600),
    (">600", float("inf")),
]


@dataclass
class Row:
    doc_id: str
    title: str
    pages: int | None
    which: str          # "archive" or "original"
    size: int           # bytes
    dpi: int | None     # max embedded-image DPI; None = no raster images
    images: int
    error: str | None = None


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


PROBE_TIMEOUT = 60  # seconds per file; set from --timeout


def probe(path: Path) -> tuple[int | None, int, str | None]:
    """Return (max_dpi, image_count, error) for a PDF via `pdfimages -list`."""
    try:
        out = subprocess.run(
            ["pdfimages", "-list", str(path)],
            capture_output=True, encoding="utf-8", errors="replace",
            timeout=PROBE_TIMEOUT,
            # DEVNULL, not the console: an encrypted/malformed PDF makes
            # pdfimages prompt for a password on stdin and block forever.
            # With no stdin it gets EOF and fails fast instead.
            stdin=subprocess.DEVNULL,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return None, 0, f"{type(exc).__name__}: {str(exc)[:80]}"
    if out.returncode != 0:
        detail = (out.stderr or out.stdout).strip().splitlines()
        return None, 0, (detail[-1][:80] if detail else f"exit {out.returncode}")

    max_dpi = 0
    count = 0
    # Header is two lines (labels + dashed rule); data rows follow. Columns:
    # page num type w h color comp bpc enc interp object ID x-ppi y-ppi size ratio
    for line in out.stdout.splitlines()[2:]:
        parts = line.split()
        if len(parts) < 14 or parts[2] not in ("image", "stencil"):
            continue
        count += 1
        for idx in (12, 13):  # x-ppi, y-ppi
            try:
                dpi = int(round(float(parts[idx])))
            except ValueError:
                continue
            max_dpi = max(max_dpi, dpi)
    return (max_dpi or None), count, None


def analyze(job: tuple[str, str, int | None, str, int, str]) -> Row:
    doc_id, title, pages, which, size, blob_hex = job
    path = storage.blob_file(uuid.UUID(blob_hex))
    if not path.exists():
        return Row(doc_id, title, pages, which, size, None, 0, "blob missing on disk")
    dpi, images, err = probe(path)
    return Row(doc_id, title, pages, which, size, dpi, images, err)


async def collect(limit: int | None) -> list[tuple[str, str, int | None, str, int, str]]:
    """Load (doc_id, title, pages, which, size, blob_hex) for PDF blobs."""
    jobs: list[tuple[str, str, int | None, str, int, str]] = []
    async with SessionLocal() as db:
        # Select only the scalar columns we need — NOT full ORM rows, which
        # would drag every document's text_content into memory and OOM the
        # process on a large library.
        q = (
            select(
                Document.id, Document.title, Document.page_count,
                Document.archive_blob_id, Blob.id, Blob.size_bytes, Blob.mime_type,
            )
            .join(
                Blob,
                Blob.id == func.coalesce(Document.archive_blob_id, Document.original_blob_id),
            )
            .where(Document.deleted_at.is_(None))
            .order_by(Document.created_at.desc())
        )
        if limit:
            q = q.limit(limit)
        for doc_id, title, pages, archive_id, blob_id, size, mime in (
            await db.execute(q)
        ).all():
            if (mime or "").lower() != "application/pdf":
                continue
            which = "archive" if archive_id else "original"
            jobs.append(
                (str(doc_id), title, pages, which, size or 0, blob_id.hex)
            )
    return jobs


def report(rows: list[Row], target: int, top: int, csv_path: str | None) -> None:
    ok = [r for r in rows if r.error is None]
    errored = [r for r in rows if r.error is not None]
    raster = [r for r in ok if r.dpi is not None]
    vector = [r for r in ok if r.dpi is None]

    total_size = sum(r.size for r in ok)
    print()
    print("=" * 72)
    print(f"PDF resolution audit — {len(rows)} documents inspected")
    print("=" * 72)
    print(f"  raster (image) PDFs : {len(raster):>7}   {human(sum(r.size for r in raster)):>10}")
    print(f"  vector/text PDFs    : {len(vector):>7}   {human(sum(r.size for r in vector)):>10}"
          f"   (no embedded images — not downsample candidates)")
    if errored:
        print(f"  unreadable          : {len(errored):>7}   (see --csv for details)")
    print(f"  total blob bytes    : {human(total_size):>10}")

    # DPI histogram over raster PDFs.
    print("\n  DPI distribution (raster PDFs, by highest embedded-image DPI):")
    prev = 0.0
    for label, hi in BUCKETS:
        bucket = [r for r in raster if prev < (r.dpi or 0) <= hi]
        prev = hi
        if bucket:
            print(f"    {label:>9} : {len(bucket):>7} docs   {human(sum(b.size for b in bucket)):>10}")

    # Reclaim estimate at the target DPI.
    over = [r for r in raster if (r.dpi or 0) > target]
    def est_after(r: Row) -> float:
        return r.size * (target / r.dpi) ** 2
    reclaim = sum(r.size - est_after(r) for r in over)
    print(f"\n  Downsample-to-{target}-DPI candidates: {len(over)} docs "
          f"({human(sum(r.size for r in over))} today)")
    print(f"  Rough reclaimable by downsampling alone : ~{human(reclaim)} "
          f"({reclaim / total_size * 100:.0f}% of library)" if total_size else "")
    print("  (ocrmypdf --optimize would reclaim further, on top of this.)")

    # Worst offenders by reclaimable bytes.
    over.sort(key=lambda r: r.size - est_after(r), reverse=True)
    print(f"\n  Top {min(top, len(over))} by reclaimable space:")
    print(f"    {'DPI':>5}  {'now':>9}  {'→ ~after':>9}  {'pp':>4}  where     title")
    for r in over[:top]:
        print(f"    {r.dpi:>5}  {human(r.size):>9}  {human(est_after(r)):>9}  "
              f"{(r.pages or 0):>4}  {r.which:<8}  {r.title[:48]}")

    if csv_path:
        with open(csv_path, "w", newline="") as fh:
            w = csvmod.writer(fh)
            w.writerow(["doc_id", "title", "pages", "which", "size_bytes",
                        "dpi", "images", "est_after_bytes", "error"])
            for r in rows:
                after = int(est_after(r)) if (r.dpi and r.dpi > target) else r.size
                w.writerow([r.doc_id, r.title, r.pages or "", r.which, r.size,
                            r.dpi or "", r.images, after, r.error or ""])
        print(f"\n  Per-document detail written to {csv_path}")
    print()


async def main() -> None:
    ap = argparse.ArgumentParser(description="Audit embedded-image DPI across the library.")
    ap.add_argument("--target", type=int, default=300, help="target DPI for the reclaim estimate")
    ap.add_argument("--top", type=int, default=25, help="how many worst offenders to list")
    ap.add_argument("--limit", type=int, default=None, help="only scan the N newest docs (sampling)")
    ap.add_argument("--workers", type=int, default=6, help="parallel pdfimages probes")
    ap.add_argument("--timeout", type=int, default=60, help="per-file pdfimages timeout (s)")
    ap.add_argument("--csv", type=str, default=None, help="write per-document detail to this path")
    args = ap.parse_args()

    global PROBE_TIMEOUT
    PROBE_TIMEOUT = args.timeout

    print("Loading document list…", file=sys.stderr, flush=True)
    jobs = await collect(args.limit)
    total = len(jobs)
    print(f"Probing {total} PDF blobs with {args.workers} workers…",
          file=sys.stderr, flush=True)

    # Report as each probe COMPLETES (unordered), so one slow/bad file can't
    # stall the whole run's output. Tick often enough to show life on any size.
    tick = 100 if total > 1000 else 25
    rows: list[Row] = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(analyze, j) for j in jobs]
        for i, fut in enumerate(as_completed(futures), 1):
            rows.append(fut.result())
            if i % tick == 0 or i == total:
                print(f"  …{i}/{total}", file=sys.stderr, flush=True)

    report(rows, args.target, args.top, args.csv)


if __name__ == "__main__":
    asyncio.run(main())
