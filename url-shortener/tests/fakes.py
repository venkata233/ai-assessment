"""In-memory test doubles for the ports. Used by unit and API tests; integration tests use real adapters."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from shortener.api.container import Container
from shortener.config import Settings
from shortener.domain.errors import CodeTaken, StorageUnavailable
from shortener.domain.models import ClickEvent, ClickRecord, Link
from shortener.metrics import Metrics
from shortener.ports import CacheEntry, ClickTotals, CountItem, TimeBucket, TopColumn
from shortener.services.analytics_service import AnalyticsService
from shortener.services.auth import ApiKeyAuthenticator
from shortener.services.link_service import LinkService
from shortener.services.rate_limiter import RateLimiter

DEMO_KEY = "demo-key-local-dev-only"
DEMO_KEY_SHA256 = "c1d5df720e5bccea4c80f959195421baa0c28d3d50ed8f74e6073f57bc04da17"
OTHER_KEY = "second-key-local-dev-only"
OTHER_KEY_SHA256 = "e37e3942d7d26684a71c7ef7a7f5384e0d0431a74cdb819e4002f9a8125e7508"
NOW = datetime(2026, 11, 20, 12, 0, 0, tzinfo=UTC)


class FakeClock:
    def __init__(self, now: datetime = NOW) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now

    def advance(self, **kwargs: float) -> None:
        self._now += timedelta(**kwargs)


class InMemoryLinkRepository:
    def __init__(self) -> None:
        self.rows: dict[str, Link] = {}
        self._next_id = 1
        self.down = False
        self.insert_calls = 0
        self.get_calls = 0

    def _check(self) -> None:
        if self.down:
            raise StorageUnavailable

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
        self._check()
        self.insert_calls += 1
        if code in self.rows:  # includes soft-deleted rows: the unique constraint is the tombstone
            raise CodeTaken
        link = Link(
            self._next_id, code, target_url, owner_key_id, is_custom_alias, 0, created_at, expires_at, None
        )
        self._next_id += 1
        self.rows[code] = link
        return link

    async def get_by_code(self, code: str) -> Link | None:
        self._check()
        self.get_calls += 1
        return self.rows.get(code)

    async def soft_delete(self, *, code: str, owner_key_id: str, deleted_at: datetime) -> bool:
        self._check()
        link = self.rows.get(code)
        if link is None or link.deleted_at is not None or link.owner_key_id != owner_key_id:
            return False
        self.rows[code] = replace(link, deleted_at=deleted_at)
        return True


class InMemoryCache:
    def __init__(self) -> None:
        self.data: dict[str, tuple[CacheEntry, int]] = {}
        self.down = False

    async def get(self, code: str) -> CacheEntry | None:
        if self.down:
            return None
        item = self.data.get(code)
        return item[0] if item else None

    async def put(self, code: str, entry: CacheEntry, ttl_seconds: int) -> None:
        if not self.down:
            self.data[code] = (entry, ttl_seconds)

    async def put_if_absent(self, code: str, entry: CacheEntry, ttl_seconds: int) -> None:
        if not self.down and code not in self.data:
            self.data[code] = (entry, ttl_seconds)


class RecordingPublisher:
    def __init__(self) -> None:
        self.records: list[ClickRecord] = []

    async def publish(self, record: ClickRecord) -> None:
        self.records.append(record)


class FakeDns:
    def __init__(self, mapping: dict[str, list[str]] | None = None) -> None:
        self.mapping = mapping or {}
        self.calls: list[str] = []

    async def resolve(self, host: str) -> list[str]:
        self.calls.append(host)
        return self.mapping.get(host, [])


class SequenceGenerator:
    def __init__(self, codes: list[str]) -> None:
        self._codes = list(codes)
        self.calls = 0

    def generate(self) -> str:
        self.calls += 1
        return self._codes.pop(0) if self._codes else f"gen{self.calls:04d}"


class InMemoryRateLimitStore:
    def __init__(self) -> None:
        self.counts: dict[str, int] = {}
        self.down = False

    async def hit(self, key: str, window_seconds: int) -> int:
        if self.down:
            raise ConnectionError("redis down")
        self.counts[key] = self.counts.get(key, 0) + 1
        return self.counts[key]


class InMemoryClickRepository:
    """Computes analytics from stored events so service logic can be tested without PostgreSQL."""

    def __init__(self) -> None:
        self.events: list[ClickEvent] = []

    async def insert_batch(self, events: list[ClickEvent]) -> int:
        known = {e.event_id for e in self.events}
        fresh = [e for e in events if e.event_id not in known]
        self.events.extend(fresh)
        return len(fresh)

    def _window(self, url_id: int, start: datetime, end: datetime) -> list[ClickEvent]:
        return [e for e in self.events if e.url_id == url_id and start <= e.clicked_at < end]

    async def totals(self, url_id: int, start: datetime, end: datetime) -> ClickTotals:
        rows = self._window(url_id, start, end)
        return ClickTotals(len(rows), len({e.visitor_hash for e in rows if e.visitor_hash}))

    async def time_series(
        self, url_id: int, start: datetime, end: datetime, granularity: str
    ) -> list[TimeBucket]:
        buckets: dict[datetime, int] = {}
        for e in self._window(url_id, start, end):
            t = e.clicked_at.astimezone(UTC)
            key = t.replace(minute=0, second=0, microsecond=0)
            if granularity == "day":
                key = key.replace(hour=0)
            buckets[key] = buckets.get(key, 0) + 1
        return [TimeBucket(k, v) for k, v in sorted(buckets.items())]

    async def top_values(
        self, url_id: int, column: TopColumn, start: datetime, end: datetime, limit: int
    ) -> list[CountItem]:
        counts: dict[str | None, int] = {}
        for e in self._window(url_id, start, end):
            value = {"referrer": e.referrer, "ua_family": e.ua_family, "country": e.country}[column]
            counts[value] = counts.get(value, 0) + 1
        ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0] or ""))
        return [CountItem(v, n) for v, n in ordered[:limit]]

    async def recent(
        self, url_id: int, start: datetime, end: datetime, cursor: tuple[datetime, str] | None, limit: int
    ) -> list[ClickEvent]:
        rows = sorted(
            self._window(url_id, start, end), key=lambda e: (e.clicked_at, e.event_id), reverse=True
        )
        if cursor is not None:
            rows = [e for e in rows if (e.clicked_at, e.event_id) < cursor]
        return rows[:limit]


def make_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "database_url": "postgresql+asyncpg://u:p@localhost/db",
        "redis_url": "redis://localhost:6379/0",
        "api_keys": f"demo:{DEMO_KEY_SHA256},other:{OTHER_KEY_SHA256}",
        "base_url": "http://short.test",
        "analytics_secret": "unit-test-analytics-secret",
        "dns_check_enabled": False,
        "rate_limit_create_per_min": 1000,
        "rate_limit_api_per_min": 1000,
        "rate_limit_redirect_per_min": 1000,
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


class Env:
    """A fully wired container backed by fakes, with handles to poke at them."""

    def __init__(self, **settings_overrides: object) -> None:
        self.settings = make_settings(**settings_overrides)
        self.clock = FakeClock()
        self.metrics = Metrics()
        self.links_repo = InMemoryLinkRepository()
        self.cache = InMemoryCache()
        self.dns = FakeDns()
        self.generator = SequenceGenerator([])
        self.publisher = RecordingPublisher()
        self.rate_store = InMemoryRateLimitStore()
        self.clicks_repo = InMemoryClickRepository()
        self.link_service = LinkService(
            links=self.links_repo,
            cache=self.cache,
            clock=self.clock,
            generator=self.generator,
            dns=self.dns,
            metrics=self.metrics,
            own_origins=frozenset({("short.test", 80)}),
            cache_ttl_seconds=self.settings.cache_ttl_seconds,
            negative_cache_ttl_seconds=self.settings.negative_cache_ttl_seconds,
            dns_check_enabled=self.settings.dns_check_enabled,
        )
        self.analytics = AnalyticsService(links=self.link_service, clicks=self.clicks_repo, clock=self.clock)
        self.container = Container(
            settings=self.settings,
            links=self.link_service,
            analytics=self.analytics,
            authenticator=ApiKeyAuthenticator(self.settings.api_key_hashes),
            rate_limiter=RateLimiter(self.rate_store, self.clock, self.metrics),
            publisher=self.publisher,
            clock=self.clock,
            metrics=self.metrics,
            readiness=self._readiness,
        )
        self.db_up = True
        self.redis_up = True

    async def _readiness(self) -> tuple[bool, bool]:
        return self.db_up, self.redis_up

    def use_codes(self, codes: list[str]) -> None:
        self.generator = SequenceGenerator(codes)
        self.link_service._generator = self.generator
