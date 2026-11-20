"""Redis adapters: link cache (ADR-005), click stream publisher (ADR-004), rate-limit counters."""

import asyncio
import json
from datetime import datetime
from typing import Any

import structlog
from redis.asyncio import Redis
from redis.exceptions import RedisError

from shortener.domain.models import ClickRecord
from shortener.infra.circuit_breaker import CircuitBreaker
from shortener.metrics import Metrics
from shortener.ports import CacheEntry

CLICK_STREAM = "clicks"
_REDIS_FAILURES = (RedisError, OSError, TimeoutError)
_log = structlog.get_logger()


def _key(code: str) -> str:
    return f"url:{code}"


def _encode(entry: CacheEntry) -> str:
    if entry.state != "active":
        return json.dumps({"state": entry.state})
    return json.dumps(
        {
            "url_id": entry.url_id,
            "target": entry.target,
            "expires_at": entry.expires_at.isoformat() if entry.expires_at else None,
        }
    )


def _decode(raw: str) -> CacheEntry:
    data = json.loads(raw)
    state = data.get("state")
    if state in {"absent", "deleted"}:
        return CacheEntry(state)
    expires_at = datetime.fromisoformat(data["expires_at"]) if data.get("expires_at") else None
    return CacheEntry("active", int(data["url_id"]), str(data["target"]), expires_at)


class RedisLinkCache:
    """Never raises: any Redis problem is logged, counted, and treated as a miss / skipped write."""

    def __init__(self, redis: Redis, metrics: Metrics, breaker: CircuitBreaker | None = None) -> None:
        self._redis = redis
        self._metrics = metrics
        self._breaker = breaker or CircuitBreaker()

    def _error(self, operation: str, exc: Exception) -> None:
        self._metrics.inc("cache_errors_total")
        _log.warning("cache_error", operation=operation, error=type(exc).__name__)

    async def get(self, code: str) -> CacheEntry | None:
        if not self._breaker.allow():
            self._metrics.inc("cache_errors_total")
            return None
        try:
            raw = await self._redis.get(_key(code))
        except _REDIS_FAILURES as exc:  # Redis down or slow: fall back to PostgreSQL
            self._breaker.record_failure()
            self._error("get", exc)
            return None
        self._breaker.record_success()
        try:
            return _decode(str(raw)) if raw is not None else None
        except (ValueError, KeyError, TypeError, AttributeError) as exc:  # corrupt value: treat as a miss
            self._error("get", exc)
            return None

    async def put(self, code: str, entry: CacheEntry, ttl_seconds: int) -> None:
        await self._write("put", code, entry, ttl_seconds, nx=False)

    async def put_if_absent(self, code: str, entry: CacheEntry, ttl_seconds: int) -> None:
        await self._write("put_if_absent", code, entry, ttl_seconds, nx=True)

    async def _write(
        self, operation: str, code: str, entry: CacheEntry, ttl_seconds: int, *, nx: bool
    ) -> None:
        if not self._breaker.allow():
            self._metrics.inc("cache_errors_total")
            return
        try:
            await self._redis.set(_key(code), _encode(entry), ex=ttl_seconds, nx=nx)
        except _REDIS_FAILURES as exc:
            self._breaker.record_failure()
            self._error(operation, exc)
        else:
            self._breaker.record_success()


class RedisClickPublisher:
    def __init__(
        self,
        redis: Redis,
        metrics: Metrics,
        *,
        timeout_ms: int = 50,
        maxlen: int = 1_000_000,
        stream: str = CLICK_STREAM,
        breaker: CircuitBreaker | None = None,
    ) -> None:
        self._redis = redis
        self._metrics = metrics
        self._timeout = timeout_ms / 1000
        self._maxlen = maxlen
        self._stream = stream
        self._breaker = breaker or CircuitBreaker()

    async def publish(self, record: ClickRecord) -> None:
        if not self._breaker.allow():
            self._metrics.inc("clicks_dropped_total", reason="circuit_open")
            return
        fields: dict[Any, Any] = {
            "url_id": str(record.url_id),
            "ts_ms": str(record.ts_ms),
            "referrer_origin": record.referrer or "",
            "ua": record.user_agent or "",
            "ua_family": record.ua_family,
            "country": record.country or "",
            "visitor_hash": record.visitor_hash or "",
        }
        try:
            await asyncio.wait_for(
                self._redis.xadd(self._stream, fields, maxlen=self._maxlen, approximate=True),
                self._timeout,
            )
        except TimeoutError:
            self._breaker.record_failure()
            self._metrics.inc("clicks_dropped_total", reason="timeout")
        except Exception as exc:  # never surface to the visitor: drop and count
            self._breaker.record_failure()
            self._metrics.inc("clicks_dropped_total", reason="error")
            _log.warning("click_publish_failed", error=type(exc).__name__)
        else:
            self._breaker.record_success()
            self._metrics.inc("clicks_published_total")


class RedisRateLimitStore:
    def __init__(self, redis: Redis, breaker: CircuitBreaker | None = None) -> None:
        self._redis = redis
        self._breaker = breaker or CircuitBreaker()

    async def hit(self, key: str, window_seconds: int) -> int:
        if not self._breaker.allow():
            raise ConnectionError("redis circuit open")  # the limiter treats any error as fail-open
        try:
            async with self._redis.pipeline(transaction=True) as pipe:
                pipe.incr(key)
                pipe.expire(
                    key, window_seconds, nx=True
                )  # every counter carries a TTL (volatile-lru, ADR-004)
                results = await pipe.execute()
        except _REDIS_FAILURES:
            self._breaker.record_failure()
            raise
        self._breaker.record_success()
        return int(results[0])
