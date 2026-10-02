from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "JobFinderAgent"
    environment: str = Field(default="development", validation_alias="APP_ENV")
    api_prefix: str = "/api/v1"
    database_url: str = Field(
        default="sqlite+pysqlite:///./data/jobfinderagent.db",
        validation_alias="DATABASE_URL",
    )
    session_secret: str = Field(default="", validation_alias="SESSION_SECRET")
    session_ttl_minutes: int = Field(default=60, gt=0, validation_alias="SESSION_TTL_MINUTES")
    cookie_secure: bool = Field(default=False, validation_alias="COOKIE_SECURE")
    cors_origins: str = Field(default="http://localhost:5173", validation_alias="CORS_ORIGINS")
    artifact_storage_path: str = Field(
        default="./data/artifacts", validation_alias="ARTIFACT_STORAGE_PATH"
    )
    artifact_encryption_key: str = Field(default="", validation_alias="ARTIFACT_ENCRYPTION_KEY")
    max_upload_bytes: int = Field(default=5_000_000, gt=0, validation_alias="MAX_UPLOAD_BYTES")
    log_level: str = Field(default="INFO", validation_alias="LOG_LEVEL")
    # Global egress gate for source providers that need network access. It stays false
    # until an egress allowlist and SSRF controls exist; local providers are unaffected.
    source_network_access_enabled: bool = Field(
        default=False, validation_alias="SOURCE_NETWORK_ACCESS_ENABLED"
    )
    # Deployment-level egress allowlist (comma-separated exact hosts or `*.domain`). A
    # connector may only reach hosts present both here and in its own reviewed config.
    source_egress_allowlist: str = Field(default="", validation_alias="SOURCE_EGRESS_ALLOWLIST")
    source_http_connect_timeout_seconds: float = Field(
        default=5.0, gt=0, le=30, validation_alias="SOURCE_HTTP_CONNECT_TIMEOUT_SECONDS"
    )
    source_http_read_timeout_seconds: float = Field(
        default=15.0, gt=0, le=120, validation_alias="SOURCE_HTTP_READ_TIMEOUT_SECONDS"
    )
    source_http_total_timeout_seconds: float = Field(
        default=30.0, gt=0, le=300, validation_alias="SOURCE_HTTP_TOTAL_TIMEOUT_SECONDS"
    )
    source_http_max_response_bytes: int = Field(
        default=5_000_000, gt=0, le=50_000_000, validation_alias="SOURCE_HTTP_MAX_RESPONSE_BYTES"
    )
    source_http_max_redirects: int = Field(
        default=3, ge=0, le=5, validation_alias="SOURCE_HTTP_MAX_REDIRECTS"
    )
    source_http_max_requests_per_run: int = Field(
        default=60, gt=0, le=1000, validation_alias="SOURCE_HTTP_MAX_REQUESTS_PER_RUN"
    )

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    @property
    def source_egress_allowlist_entries(self) -> tuple[str, ...]:
        return tuple(
            item.strip().lower().rstrip(".")
            for item in self.source_egress_allowlist.split(",")
            if item.strip()
        )

    @property
    def cors_origin_list(self) -> list[str]:
        return [item.strip() for item in self.cors_origins.split(",") if item.strip()]

    def validate_runtime_secrets(self) -> None:
        if len(self.session_secret) < 32:
            raise RuntimeError("SESSION_SECRET must be at least 32 characters")
        if self.environment.lower() not in {"development", "test"} and not self.cookie_secure:
            raise RuntimeError("COOKIE_SECURE must be true outside development")
        if "*" in self.cors_origin_list:
            raise RuntimeError(
                "CORS_ORIGINS cannot contain '*' when credentialed cookies are enabled"
            )
        if self.artifact_encryption_key == "":
            raise RuntimeError("ARTIFACT_ENCRYPTION_KEY is required")
        if self.source_network_access_enabled and not self.source_egress_allowlist_entries:
            raise RuntimeError(
                "SOURCE_EGRESS_ALLOWLIST must list hosts when SOURCE_NETWORK_ACCESS_ENABLED is true"
            )
        for entry in self.source_egress_allowlist_entries:
            host = entry[2:] if entry.startswith("*.") else entry
            labels = host.split(".")
            # Exact hosts or wildcards under a registrable domain only: no "*", "*.com".
            if (
                len(labels) < 2
                or not all(
                    label and label.replace("-", "").isalnum() and label.isascii()
                    for label in labels
                )
                or labels[-1].isdigit()
            ):
                raise RuntimeError(f"SOURCE_EGRESS_ALLOWLIST entry is not a valid host: {entry}")


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.validate_runtime_secrets()
    return settings
