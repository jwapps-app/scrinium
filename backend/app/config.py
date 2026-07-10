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

    # OCR
    ocr_engine: str = "tesseract"  # "tesseract" | "apple"
    ocr_languages: str = "eng"
    apple_ocr_url: str = ""  # e.g. http://host.docker.internal:9876

    worker_poll_seconds: float = 2.0
    max_upload_mb: int = 500

    # Watched-folder ingest (empty = disabled)
    watch_dir: str = ""
    watch_poll_seconds: float = 5.0

    # Push via the shared push-relay (all three set = enabled)
    push_relay_url: str = ""      # e.g. http://192.168.1.10:8088
    push_relay_api_key: str = ""  # value after '=' in the relay's apps.keys
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
