from functools import lru_cache
from urllib.parse import urlsplit

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
    # Network-layer egress enforcement: when set, every source request is tunneled through
    # this CONNECT proxy (`http://host:port`) and no other connection can be opened.
    # Required outside development/test whenever network access is enabled.
    source_egress_proxy_url: str = Field(default="", validation_alias="SOURCE_EGRESS_PROXY_URL")
    # Directory holding secret files for `file:<name>` references (secret-manager projected).
    secrets_dir: str = Field(default="", validation_alias="SECRETS_DIR")
    # Health alert webhook: a secret reference to the URL, and the only host it may use.
    alert_webhook_ref: str = Field(default="", validation_alias="ALERT_WEBHOOK_REF")
    alert_webhook_host: str = Field(default="", validation_alias="ALERT_WEBHOOK_HOST")

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    @property
    def source_egress_allowlist_entries(self) -> tuple[str, ...]:
        return tuple(
            item.strip().lower().rstrip(".")
            for item in self.source_egress_allowlist.split(",")
            if item.strip()
        )

    @property
    def is_development_like(self) -> bool:
        return self.environment.lower() in {"development", "test"}

    @property
    def source_egress_proxy(self) -> tuple[str, int] | None:
        """(host, port) of the egress proxy, or None when requests connect directly."""
        if not self.source_egress_proxy_url.strip():
            return None
        parts = urlsplit(self.source_egress_proxy_url.strip())
        return (parts.hostname or "", parts.port or 0)

    @property
    def alerting_enabled(self) -> bool:
        return bool(self.alert_webhook_ref.strip() and self.alert_webhook_host.strip())

    @property
    def cors_origin_list(self) -> list[str]:
        return [item.strip() for item in self.cors_origins.split(",") if item.strip()]

    def validate_runtime_secrets(self) -> None:
        if len(self.session_secret) < 32:
            raise RuntimeError("SESSION_SECRET must be at least 32 characters")
        if not self.is_development_like and not self.cookie_secure:
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
            # Exact hosts or wildcards under a registrable domain only: no "*", "*.com".
            if not _is_valid_hostname(host):
                raise RuntimeError(f"SOURCE_EGRESS_ALLOWLIST entry is not a valid host: {entry}")
        self._validate_egress_proxy()
        self._validate_alerting()

    def _validate_egress_proxy(self) -> None:
        raw = self.source_egress_proxy_url.strip()
        if not raw:
            if self.source_network_access_enabled and not self.is_development_like:
                raise RuntimeError(
                    "SOURCE_EGRESS_PROXY_URL is required when SOURCE_NETWORK_ACCESS_ENABLED "
                    "is true outside development"
                )
            return
        try:
            parts = urlsplit(raw)
            port = parts.port
        except ValueError as exc:
            raise RuntimeError("SOURCE_EGRESS_PROXY_URL is not a valid URL") from exc
        if (
            parts.scheme != "http"
            or not parts.hostname
            or port is None
            or parts.username is not None
            or parts.password is not None
            or parts.path not in {"", "/"}
            or parts.query
            or parts.fragment
        ):
            raise RuntimeError(
                "SOURCE_EGRESS_PROXY_URL must be http://host:port with no credentials or path"
            )

    def _validate_alerting(self) -> None:
        ref = self.alert_webhook_ref.strip()
        host = self.alert_webhook_host.strip().lower()
        if bool(ref) != bool(host):
            raise RuntimeError("ALERT_WEBHOOK_REF and ALERT_WEBHOOK_HOST must be set together")
        if host and not _is_valid_hostname(host):
            raise RuntimeError("ALERT_WEBHOOK_HOST must be a single exact hostname")


def _is_valid_hostname(host: str) -> bool:
    labels = host.split(".")
    return (
        len(labels) >= 2
        and all(label and label.replace("-", "").isalnum() and label.isascii() for label in labels)
        and not labels[-1].isdigit()
    )


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.validate_runtime_secrets()
    return settings
