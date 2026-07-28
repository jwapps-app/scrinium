from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

PLACEHOLDER_SECRETS = {"dev-secret-change-me", "changeme", "secret"}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    environment: str = "production"
    app_name: str = "Scrinium"
    app_tagline: str = "Your documents, searchable."
    database_url: str = "postgresql+asyncpg://app:app@postgres:5432/app"
    secret_key: str = "dev-secret-change-me"
    allowed_origins: str = "http://localhost:5173"

    data_dir: str = "/data"

    access_token_minutes: int = 30
    refresh_token_days: int = 30
    # Peers whose CF-Connecting-IP header is believed for rate limiting. In the
    # normal deployment the only thing talking to the api container is nginx on
    # the compose network, so the private ranges cover it; anything reaching the
    # container from elsewhere is rate-limited on its real socket address.
    trusted_proxies: str = "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,127.0.0.0/8,::1/128"

    @property
    def trusted_proxy_list(self) -> list[str]:
        return [p.strip() for p in self.trusted_proxies.split(",") if p.strip()]

    # OCR
    ocr_engine: str = "tesseract"  # "tesseract" | "apple"
    ocr_languages: str = "eng"
    apple_ocr_url: str = ""  # e.g. http://host.docker.internal:9876
    # ocrmypdf parallel jobs per document. Kept modest so that, combined
    # with WORKER_CONCURRENCY, total concurrent OCR fan-out (and load on the
    # Vision sidecar) stays bounded rather than pages_per_doc × docs.
    ocr_jobs: int = 3
    # Cap archive-image resolution at this DPI: OCR output (and the backfill
    # job) downsamples images above it, since a document library never needs
    # more than ~300 DPI. 0 disables downsampling entirely. Runtime-adjustable
    # via Settings (app_state ARCHIVE_MAX_DPI).
    archive_max_dpi: int = 300
    # Auto-straighten sideways scans: ocrmypdf runs Tesseract OSD per page and
    # rotates only the pages that need it. An upright page reports "rotate 0"
    # regardless of confidence, so the threshold only gates non-zero rotations
    # and can't flip a good text page; the ocrmypdf default (14) misses genuine
    # rotations that score ~12, and even 8 dropped one at pipeline DPI, so 5
    # reliably catches all four orientations (verified) while leaving upright
    # pages alone. Only sparse/ambiguous pages risk a spurious flip.
    rotate_pages: bool = True
    rotate_pages_threshold: float = 5.0
    # Wall-clock budget for one classification rule against one document. A
    # regex that compiles can still backtrack exponentially, so matching is
    # bounded and an offending rule is disabled rather than stalling the queue.
    rule_match_timeout: float = 2.0
    # OCR watchdog: kill a run only when page progress stalls this long
    # (wedged Ghostscript), not on a fixed clock — a 2,000-page book on
    # Tesseract may healthily grind for hours. Hard ceiling as a backstop.
    ocr_stall_minutes: int = 30
    ocr_max_hours: int = 24

    worker_poll_seconds: float = 2.0
    # Documents processed at once per worker container. Raise to fill the
    # idle time each doc spends waiting on the OCR round-trip; keep modest
    # so ocrmypdf/Ghostscript don't oversubscribe CPU.
    worker_concurrency: int = 1
    # True for the normal single-worker-container deployment: on startup the
    # worker owns no jobs yet, so ANY job still marked RUNNING is an orphan
    # from the previous life and is reclaimed at once. Set False only when
    # running multiple worker replicas (then rely on heartbeat staleness).
    worker_single: bool = True
    max_upload_mb: int = 500
    # /documents/stats micro-cache TTL; 0 disables (tests need fresh counts).
    stats_cache_seconds: float = 3.0

    # Document-date extraction: MDY (US) or DMY for ambiguous 03/04/2024
    date_order: str = "MDY"

    # Trash: soft-deleted documents purge for real after this many days
    trash_retention_days: int = 30

    # Email ingestion (all three of host/username/password set = enabled)
    mail_host: str = ""
    mail_port: int = 993
    mail_username: str = ""
    mail_password: str = ""
    mail_folder: str = "INBOX"
    mail_poll_seconds: float = 300.0
    # Attachments are buffered in memory until the batch ingests, so bound both
    # a single attachment and one poll's total.
    mail_max_attachment_mb: int = 50
    mail_max_poll_mb: int = 200

    def mail_enabled(self) -> bool:
        return bool(self.mail_host and self.mail_username and self.mail_password)

    # Watched-folder ingest (empty = disabled)
    watch_dir: str = ""
    watch_poll_seconds: float = 5.0
    # Max files ingested per sweep, so a huge folder dump can't starve OCR
    # jobs — intake and processing interleave.
    watch_batch_size: int = 25
    # Age in days after which filed copies in .consumed/ and .duplicates/
    # are removed. 0 (default) keeps them forever — deletion is opt-in.
    consumed_retention_days: int = 0
    # Split multi-page PDFs on separator barcode pages (PATCHT convention,
    # same as Paperless — existing separator sheets keep working).
    split_on_separators: bool = False
    separator_barcode: str = "PATCHT"

    # Automatic full-library exports: every N days (0 = manual only),
    # keeping the newest `export_keep` exports.
    export_every_days: int = 0
    export_keep: int = 3
    # "folder" (hardlinked tree on the same volume — instant, no extra disk)
    # or "zip" (portable archive parts of ~export_part_gb each).
    export_format: str = "folder"
    export_part_gb: int = 10

    # Push via the shared push-relay (all three set = enabled)
    push_relay_url: str = ""      # e.g. http://YOUR_NAS_IP:8088
    push_relay_api_key: str = ""  # value after '=' in the relay's apps.keys
    # The iOS app's real bundle id: the push relay keys its credentials on this,
    # and it is not sensitive (bundle ids are readable in any shipped app).
    # Override via APNS_BUNDLE_ID when running your own build.
    apns_bundle_id: str = "com.jworthington.scrinium"

    def push_enabled(self) -> bool:
        return bool(self.push_relay_url and self.push_relay_api_key and self.apns_bundle_id)

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.allowed_origins.split(",") if o.strip()]

    def require_strong_secret(self) -> None:
        if self.environment == "production" and (
            len(self.secret_key) < 32 or self.secret_key.lower() in PLACEHOLDER_SECRETS
        ):
            raise RuntimeError(
                "SECRET_KEY is weak or a placeholder; refusing to start in production. "
                "Set a 32+ char random value."
            )


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
