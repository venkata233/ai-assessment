"""Redis adapters (cache, publisher, rate-limit store) and the migration, against real services."""

import json
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from conftest import ADMIN_URL, PROJECT_ROOT, Stack, database_url_for, run_alembic
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import create_async_engine

from shortener.domain.models import ClickRecord
from shortener.infra.redis_adapters import RedisClickPublisher, RedisLinkCache, RedisRateLimitStore
from shortener.metrics import Metrics
from shortener.ports import CacheEntry

pytestmark = pytest.mark.integration

ACTIVE = CacheEntry("active", 7, "https://example.com/x", datetime(2030, 1, 1, tzinfo=UTC))


async def test_cache_round_trip_preserves_values_and_ttl(redis: Redis) -> None:
    cache = RedisLinkCache(redis, Metrics())
    await cache.put("promo", ACTIVE, 120)
    assert await cache.get("promo") == ACTIVE
    assert 100 < await redis.ttl("url:promo") <= 120
    assert json.loads(str(await redis.get("url:promo"))) == {
        "url_id": 7,
        "target": "https://example.com/x",
        "expires_at": "2030-01-01T00:00:00+00:00",
    }
    assert await cache.get("missing") is None


@pytest.mark.parametrize("state", ["absent", "deleted"])
async def test_sentinels_round_trip(redis: Redis, state: str) -> None:
    cache = RedisLinkCache(redis, Metrics())
    await cache.put("x1", CacheEntry(state), 30)  # type: ignore[arg-type]
    assert await cache.get("x1") == CacheEntry(state)  # type: ignore[arg-type]


async def test_set_nx_never_overwrites_a_tombstone_or_a_newer_value(redis: Redis) -> None:
    cache = RedisLinkCache(redis, Metrics())
    await cache.put("promo", CacheEntry("deleted"), 3600)
    await cache.put_if_absent("promo", ACTIVE, 3600)  # a slow reader writing back stale data
    assert (await cache.get("promo")) == CacheEntry("deleted")
    await cache.put("promo", ACTIVE, 3600)  # an explicit overwrite (create) does replace
    assert (await cache.get("promo")) == ACTIVE


async def test_corrupt_values_count_as_misses_not_errors_for_the_visitor(redis: Redis) -> None:
    metrics = Metrics()
    await redis.set("url:broken", "{not json", ex=60)
    assert await RedisLinkCache(redis, metrics).get("broken") is None
    assert metrics.get("cache_errors_total") == 1


async def test_publisher_writes_the_documented_fields_and_bounds_the_stream(redis: Redis) -> None:
    metrics = Metrics()
    publisher = RedisClickPublisher(redis, metrics, timeout_ms=500, maxlen=1000)
    record = ClickRecord(7, 1_700_000_000_000, "https://a.example", "UA", "chrome", "DE", "ab" * 8)
    await publisher.publish(record)
    await publisher.publish(ClickRecord(7, 1_700_000_000_001, None, None, "other", None, None))
    entries = await redis.xrange("clicks")
    assert entries[0][1] == {
        "url_id": "7",
        "ts_ms": "1700000000000",
        "referrer_origin": "https://a.example",
        "ua": "UA",
        "ua_family": "chrome",
        "country": "DE",
        "visitor_hash": "abababababababab",
    }
    assert entries[1][1]["referrer_origin"] == "" and entries[1][1]["country"] == ""
    assert metrics.get("clicks_published_total") == 2
    assert await redis.ttl("clicks") == -1  # the stream has no TTL: the cache can never expire it


async def test_publisher_never_raises_and_counts_drops() -> None:
    from redis.asyncio import Redis as R

    dead = R.from_url("redis://127.0.0.1:6390/0", socket_connect_timeout=0.2)
    metrics = Metrics()
    try:
        await RedisClickPublisher(dead, metrics, timeout_ms=100).publish(
            ClickRecord(1, 1, None, None, "other", None, None)
        )
    finally:
        await dead.aclose()
    assert (
        metrics.get("clicks_dropped_total", reason="error")
        + metrics.get("clicks_dropped_total", reason="timeout")
        == 1
    )


async def test_rate_limit_store_counts_and_sets_a_ttl_exactly_once(redis: Redis) -> None:
    store = RedisRateLimitStore(redis)
    assert [await store.hit("rl:t:1", 60) for _ in range(3)] == [1, 2, 3]
    first_ttl = await redis.ttl("rl:t:1")
    assert 0 < first_ttl <= 60
    await store.hit("rl:t:1", 5000)  # NX: a later hit must not extend the window
    assert await redis.ttl("rl:t:1") <= first_ttl


async def test_the_stream_survives_cache_flush_patterns(stack: Stack) -> None:
    async with stack.client() as client:
        await client.post(
            "/api/v1/urls",
            json={"url": "https://example.com/", "alias": "promo"},
            headers={"X-API-Key": "demo-key-local-dev-only"},
        )
        await client.get("/promo")
    cache_keys = [k async for k in stack.redis.scan_iter(match="url:*")]
    await stack.redis.delete(*cache_keys)  # simulating eviction of every cache key
    assert await stack.redis.xlen("clicks") == 1


# ----- migrations --------------------------------------------------------------------------------------------


async def test_migrations_round_trip_and_match_the_models() -> None:
    name = "shortener_migration_check"
    admin = create_async_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as conn:
            await conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
            await conn.execute(sa.text(f'CREATE DATABASE "{name}"'))
        url = database_url_for(name)
        for args in (["upgrade", "head"], ["check"], ["downgrade", "base"], ["upgrade", "head"], ["check"]):
            result = run_alembic(url, *args)
            assert result.returncode == 0, (args, result.stderr[-800:])
        heads = run_alembic(url, "heads")
        assert heads.stdout.count("(head)") == 1  # one linear history
    finally:
        async with admin.connect() as conn:
            await conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        await admin.dispose()
    assert (PROJECT_ROOT / "migrations" / "versions" / "0001_initial_schema.py").exists()
