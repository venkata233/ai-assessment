"""Environment-only configuration. Secrets have no defaults: startup fails fast when they are missing."""

import re
from functools import cached_property
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_LABEL_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore", case_sensitive=False)

    database_url: SecretStr
    redis_url: SecretStr
    # "label:sha256hex" pairs separated by commas. Only hashes are configured, never plaintext keys.
    api_keys: SecretStr
    base_url: str
    analytics_secret: SecretStr

    geo_country_header: str = "CF-IPCountry"
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
    metrics_enabled: bool = True

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
        if not sep or not _LABEL_RE.match(label) or not _SHA256_RE.match(digest):
            raise ValueError("API_KEYS entries must look like label:<64 hex chars of sha256>")
        if label in labels or digest in result:
            raise ValueError("API_KEYS contains a duplicate label or hash")
        labels.add(label)
        result[digest] = label
    if not result:
        raise ValueError("API_KEYS must contain at least one key")
    return result
