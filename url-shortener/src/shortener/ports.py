"""Ports (protocols). Services depend on these; `infra/` implements them; `main.py` wires them."""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

from shortener.domain.models import ClickEvent, ClickRecord, Link


class Clock(Protocol):
    def now(self) -> datetime:
        """Timezone-aware UTC now."""
        ...


class DnsResolver(Protocol):
    async def resolve(self, host: str) -> list[str]:
        """Resolved IP strings; an empty list when the name does not resolve or the lookup fails."""
        ...


class LinkRepository(Protocol):
    async def insert(
        self,
        *,
        code: str,
        target_url: str,
        owner_key_id: str,
        is_custom_alias: bool,
        created_at: datetime,
        expires_at: datetime | None,
    ) -> Link:
        """Raises CodeTaken on a unique violation and StorageUnavailable when the database is down."""
        ...

    async def get_by_code(self, code: str) -> Link | None:
        """Returns soft-deleted rows too, so the caller can distinguish tombstones."""
        ...

    async def soft_delete(self, *, code: str, owner_key_id: str, deleted_at: datetime) -> bool:
        """True when a live row owned by `owner_key_id` was deleted by this call."""
        ...


@dataclass(frozen=True)
class CacheEntry:
    state: Literal["active", "absent", "deleted"]
    url_id: int | None = None
    target: str | None = None
    expires_at: datetime | None = None


class LinkCache(Protocol):
    """Best-effort cache: implementations swallow Redis errors (and count them) instead of raising."""

    async def get(self, code: str) -> CacheEntry | None: ...

    async def put(self, code: str, entry: CacheEntry, ttl_seconds: int) -> bool:
        """Unconditional overwrite (create, delete tombstone). False when the write did not happen."""
        ...

    async def put_if_absent(self, code: str, entry: CacheEntry, ttl_seconds: int) -> None:
        """SET NX: a slow reader can never overwrite a newer write or a tombstone."""
        ...


class ClickPublisher(Protocol):
    async def publish(self, record: ClickRecord) -> None:
        """Best effort: never raises, never blocks longer than the configured timeout."""
        ...


class RateLimitStore(Protocol):
    async def hit(self, key: str, window_seconds: int) -> int:
        """Increment the counter for the current window and return the new count. May raise."""
        ...


@dataclass(frozen=True)
class ClickTotals:
    total: int
    unique_visitors: int


@dataclass(frozen=True)
class TimeBucket:
    bucket_start: datetime
    clicks: int


@dataclass(frozen=True)
class CountItem:
    value: str | None
    clicks: int


TopColumn = Literal["referrer", "ua_family", "country"]


class ClickRepository(Protocol):
    async def insert_batch(self, events: list[ClickEvent]) -> int:
        """Idempotent insert; bumps `urls.click_count` only for rows actually inserted. Returns that count."""
        ...

    async def totals(self, url_id: int, start: datetime, end: datetime) -> ClickTotals: ...

    async def time_series(
        self, url_id: int, start: datetime, end: datetime, granularity: Literal["hour", "day"]
    ) -> list[TimeBucket]: ...

    async def top_values(
        self, url_id: int, column: TopColumn, start: datetime, end: datetime, limit: int
    ) -> list[CountItem]: ...

    async def recent(
        self,
        url_id: int,
        start: datetime,
        end: datetime,
        cursor: tuple[datetime, str] | None,
        limit: int,
    ) -> list[ClickEvent]: ...
