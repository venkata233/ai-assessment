"""Environment-only configuration. Secrets have no defaults: startup fails fast when they are missing."""

import re
from functools import cached_property
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_LABEL_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

# The demo keys published in docker-compose.yml and the tests, and the marker used in throwaway passwords.
DEMO_KEY_HASHES = frozenset(
    {
        "c1d5df720e5bccea4c80f959195421baa0c28d3d50ed8f74e6073f57bc04da17",
        "e37e3942d7d26684a71c7ef7a7f5384e0d0431a74cdb819e4002f9a8125e7508",
    }
)
DEV_ONLY_MARKER = "dev_only"
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


class Settings(BaseSettings):
    # Never echo submitted values in validation errors: they would print (parts of) secrets into startup logs.
    model_config = SettingsConfigDict(
        env_file=None, extra="ignore", case_sensitive=False, hide_input_in_errors=True
    )

    database_url: SecretStr
    redis_url: SecretStr
    # "label:sha256hex" pairs separated by commas. Only hashes are configured, never plaintext keys.
    api_keys: SecretStr
    base_url: str
    analytics_secret: SecretStr

    geo_country_header: str = "CF-IPCountry"
    # Off by default: the header is client-controlled unless the edge strips and rewrites it.
    trust_geo_header: bool = False
    trusted_proxy_hops: int = Field(default=0, ge=0, le=10)
    code_length: int = Field(default=7, ge=6, le=16)
    cache_ttl_seconds: int = Field(default=3600, ge=1)
    negative_cache_ttl_seconds: int = Field(default=30, ge=1)
    rate_limit_create_per_min: int = Field(default=30, ge=1)
    rate_limit_api_per_min: int = Field(default=300, ge=1)
    rate_limit_redirect_per_min: int = Field(default=600, ge=1)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    dns_check_enabled: bool = True
    dns_timeout_seconds: float = Field(default=2.0, gt=0)
    docs_enabled: bool = False
    # Off by default: /metrics is unauthenticated. Enable it only where the ingress restricts access.
    metrics_enabled: bool = False
    # Break-glass for tests: allows the published demo credentials on a non-local BASE_URL.
    allow_dev_credentials: bool = False

    click_emit_timeout_ms: int = Field(default=50, ge=1)
    click_stream_maxlen: int = Field(default=1_000_000, ge=1000)
    redis_socket_timeout_seconds: float = Field(default=0.25, gt=0)
    db_pool_size: int = Field(default=10, ge=1)
    db_max_overflow: int = Field(default=10, ge=0)

    @field_validator("base_url")
    @classmethod
    def _validate_base_url(cls, value: str) -> str:
        parts = urlsplit(value)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            raise ValueError("BASE_URL must be an absolute http(s) URL")
        if parts.path not in {"", "/"} or parts.query or parts.fragment or parts.username:
            raise ValueError("BASE_URL must be an origin without path, query, or credentials")
        return value.rstrip("/")

    @field_validator("database_url")
    @classmethod
    def _validate_database_url(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().startswith("postgresql+asyncpg://"):
            raise ValueError("DATABASE_URL must use the postgresql+asyncpg:// scheme")
        return value

    @field_validator("analytics_secret")
    @classmethod
    def _validate_analytics_secret(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value()) < 16:
            raise ValueError("ANALYTICS_SECRET must be at least 16 characters")
        return value

    @field_validator("api_keys")
    @classmethod
    def _validate_api_keys(cls, value: SecretStr) -> SecretStr:
        parse_api_keys(value.get_secret_value())
        return value

    @model_validator(mode="after")
    def _refuse_dev_credentials_outside_localhost(self) -> "Settings":
        """Fail closed: demo keys and *_dev_only passwords are public, so only localhost may use them."""
        if self.allow_dev_credentials or (urlsplit(self.base_url).hostname or "") in _LOCAL_HOSTS:
            return self
        found: list[str] = []
        if DEMO_KEY_HASHES & set(self.api_key_hashes):
            found.append("the published demo API keys")
        if DEV_ONLY_MARKER in self.database_url.get_secret_value():
            found.append("a *_dev_only database password")
        if DEV_ONLY_MARKER in self.analytics_secret.get_secret_value():
            found.append("a *_dev_only analytics secret")
        if found:
            raise ValueError(
                f"refusing to start: BASE_URL is not local but the configuration uses {', '.join(found)}"
            )
        return self

    @cached_property
    def api_key_hashes(self) -> dict[str, str]:
        """Map of sha256 hex digest -> owner label."""
        return parse_api_keys(self.api_keys.get_secret_value())


def parse_api_keys(raw: str) -> dict[str, str]:
    result: dict[str, str] = {}
    labels: set[str] = set()
    for item in (part.strip() for part in raw.split(",")):
        if not item:
            continue
        label, sep, digest = item.partition(":")
        digest = digest.lower()
        if not sep or not _LABEL_RE.fullmatch(label) or not _SHA256_RE.fullmatch(digest):
            raise ValueError("API_KEYS entries must look like label:<64 hex chars of sha256>")
        if label in labels or digest in result:
            raise ValueError("API_KEYS contains a duplicate label or hash")
        labels.add(label)
        result[digest] = label
    if not result:
        raise ValueError("API_KEYS must contain at least one key")
    return result
