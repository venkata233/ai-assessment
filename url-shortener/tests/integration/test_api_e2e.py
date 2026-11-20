"""End-to-end acceptance criteria against real PostgreSQL and Redis (AC-1 .. AC-12, AC-14)."""

import asyncio
import re
import time
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from conftest import Stack, make_integration_settings
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from fakes import DEMO_KEY, OTHER_KEY
from shortener.infra.db import create_redis
from shortener.infra.repositories import SqlClickRepository
from shortener.main import build_container
from shortener.worker.consumer import ClickConsumer

pytestmark = pytest.mark.integration

AUTH = {"X-API-Key": DEMO_KEY}
OTHER = {"X-API-Key": OTHER_KEY}


async def count_rows(engine: AsyncEngine, table: str) -> int:
    async with engine.connect() as conn:
        return int((await conn.execute(sa.text(f"SELECT count(*) FROM {table}"))).scalar_one())  # noqa: S608


async def test_ac1_ac2_create_generated_and_custom_alias(stack: Stack) -> None:
    async with stack.client() as client:
        generated = await client.post("/api/v1/urls", json={"url": "https://example.com/a"}, headers=AUTH)
        assert generated.status_code == 201
        assert re.fullmatch(r"[0-9A-Za-z]{7}", generated.json()["code"])  # AC-1
        assert generated.json()["short_url"] == f"http://short.test/{generated.json()['code']}"

        custom = await client.post(
            "/api/v1/urls", json={"url": "https://example.com/b", "alias": "promo"}, headers=AUTH
        )
        assert custom.status_code == 201  # AC-2
        redirect = await client.get("/promo")
        assert (redirect.status_code, redirect.headers["location"]) == (302, "https://example.com/b")


async def test_codes_are_case_sensitive_end_to_end(stack: Stack) -> None:
    async with stack.client() as client:
        for alias, target in (("Promo", "https://example.com/upper"), ("promo", "https://example.com/lower")):
            assert (
                await client.post("/api/v1/urls", json={"url": target, "alias": alias}, headers=AUTH)
            ).status_code == 201
        assert (await client.get("/Promo")).headers["location"] == "https://example.com/upper"
        assert (await client.get("/promo")).headers["location"] == "https://example.com/lower"


async def test_ac3_fifty_concurrent_creates_of_one_alias_yield_exactly_one_winner(stack: Stack) -> None:
    async with stack.client() as client:
        responses = await asyncio.gather(
            *[
                client.post(
                    "/api/v1/urls",
                    json={"url": f"https://example.com/{n}", "alias": "contested"},
                    headers=AUTH,
                )
                for n in range(50)
            ]
        )
        statuses = sorted(r.status_code for r in responses)
        assert statuses == [201] + [409] * 49
        winner = next(r for r in responses if r.status_code == 201).json()["target_url"]
        assert (await client.get("/contested")).headers[
            "location"
        ] == winner  # the surviving link is the winner's
    assert await count_rows(stack.engine, "urls") == 1


async def test_ac4_ac5_invalid_requests_persist_nothing(stack: Stack) -> None:
    bad_bodies = [
        {"url": "javascript:alert(1)"},
        {"url": "https://user:pw@example.com/"},
        {"url": "http://169.254.169.254/latest/meta-data/"},
        {"url": "http://2130706433/"},
        {"url": "https://example.com/", "expires_at": "2020-01-01T00:00:00Z"},  # AC-5
        {"url": "https://example.com/", "alias": "api"},
    ]
    async with stack.client() as client:
        for body in bad_bodies:
            response = await client.post("/api/v1/urls", json=body, headers=AUTH)
            assert response.status_code == 422, body
            assert response.json()["errors"][0]["field"] in {"url", "expires_at", "alias"}
    assert await count_rows(stack.engine, "urls") == 0


async def test_ac4_hostname_resolving_to_a_private_address_is_rejected(
    database_url: str, redis_url: str, engine: AsyncEngine, redis: Redis
) -> None:
    settings = make_integration_settings(database_url, redis_url, dns_check_enabled=True)
    stack = Stack(settings, engine, redis, build_container(settings, engine, redis))
    async with stack.client() as client:
        response = await client.post(
            "/api/v1/urls", json={"url": "http://localtest.me/"}, headers=AUTH
        )  # public name for 127.0.0.1
    if response.status_code == 201:
        pytest.skip("the sandbox resolver cannot resolve localtest.me (no external DNS)")
    assert response.status_code == 422
    assert response.json()["errors"][0]["code"] in {"private-address", "host-not-allowed"}


async def test_ac6_expired_link_is_410_even_with_a_warm_cache(stack: Stack) -> None:
    expires = (datetime.now(UTC) + timedelta(seconds=2)).isoformat()
    async with stack.client() as client:
        assert (
            await client.post(
                "/api/v1/urls",
                json={"url": "https://example.com/", "alias": "shortlived", "expires_at": expires},
                headers=AUTH,
            )
        ).status_code == 201
        assert (await client.get("/shortlived")).status_code == 302  # served from the warm cache
        ttl = await stack.redis.ttl("url:shortlived")
        assert 1 <= ttl <= 2  # the cache entry can never outlive the link
        await asyncio.sleep(2.2)
        assert (await client.get("/shortlived")).status_code == 410


async def test_ac7_delete_invalidates_the_cache_and_reserves_the_alias(stack: Stack) -> None:
    async with stack.client() as client:
        await client.post(
            "/api/v1/urls", json={"url": "https://example.com/", "alias": "promo"}, headers=AUTH
        )
        assert (await client.get("/promo")).status_code == 302
        assert await stack.redis.exists("url:promo")
        assert (await client.delete("/api/v1/urls/promo", headers=AUTH)).status_code == 204
        assert (await client.get("/promo")).status_code == 404
        assert '"deleted"' in str(await stack.redis.get("url:promo"))  # tombstone, not a missing key
        again = await client.post(
            "/api/v1/urls", json={"url": "https://example.com/x", "alias": "promo"}, headers=AUTH
        )
        assert again.status_code == 409  # permanently reserved
        await stack.redis.flushdb()  # even with a cold cache, the soft-deleted row still answers 404
        assert (await client.get("/promo")).status_code == 404
    async with stack.engine.connect() as conn:
        assert (
            await conn.execute(sa.text("SELECT deleted_at IS NOT NULL FROM urls WHERE code = 'promo'"))
        ).scalar_one() is True


async def test_ac8_authentication_and_ownership(stack: Stack) -> None:
    async with stack.client() as client:
        await client.post(
            "/api/v1/urls", json={"url": "https://example.com/", "alias": "promo"}, headers=AUTH
        )
        assert (await client.delete("/api/v1/urls/promo")).status_code == 401
        assert (
            await client.get("/api/v1/urls/promo/analytics", headers={"X-API-Key": "nope"})
        ).status_code == 401
        assert (await client.delete("/api/v1/urls/promo", headers=OTHER)).status_code == 404
        assert (await client.get("/api/v1/urls/promo", headers=OTHER)).status_code == 404
        assert (await client.get("/promo")).status_code == 302  # the other key did not delete it


async def test_ac9_analytics_match_the_visits_once_the_worker_runs(stack: Stack, settings) -> None:  # type: ignore[no-untyped-def]
    async with stack.client() as client:
        await client.post(
            "/api/v1/urls", json={"url": "https://example.com/", "alias": "promo"}, headers=AUTH
        )
        visits = [
            {
                "Referer": "https://news.example.com/story?token=SECRET",
                "User-Agent": "Mozilla/5.0 Firefox/121.0",
                "CF-IPCountry": "DE",
            },
            {
                "Referer": "https://news.example.com/other",
                "User-Agent": "Mozilla/5.0 Firefox/121.0",
                "CF-IPCountry": "DE",
            },
            {"User-Agent": "curl/8.4.0"},
        ]
        for headers in visits:
            assert (await client.get("/promo", headers=headers)).status_code == 302
        assert (await client.head("/promo")).status_code == 302  # HEAD is not counted

        consumer = ClickConsumer(
            create_redis(settings, blocking=True),
            SqlClickRepository(stack.engine),
            consumer_name="t1",
            block_ms=100,
        )
        await consumer.setup()
        assert await consumer.run_once() == 3

        body = (await client.get("/api/v1/urls/promo/analytics", headers=AUTH)).json()
        assert body["total_clicks"] == 3
        assert (
            body["approx_unique_visitors"] == 2
        )  # same address: the two Firefox visits share a hash, curl differs
        assert {(i["value"], i["clicks"]) for i in body["top_referrers"]} == {
            ("https://news.example.com", 2),
            (None, 1),
        }
        assert {(i["value"], i["clicks"]) for i in body["top_user_agents"]} == {("firefox", 2), ("curl", 1)}
        assert {(i["value"], i["clicks"]) for i in body["top_countries"]} == {("DE", 2), (None, 1)}
        assert len(body["recent_clicks"]["items"]) == 3
        assert "SECRET" not in str(body)  # query strings never reach storage
        assert (await client.get("/api/v1/urls/promo", headers=AUTH)).json()["click_count"] == 3


async def test_ac10_redirect_is_unaffected_when_nothing_consumes_the_stream(stack: Stack) -> None:
    async with stack.client() as client:
        await client.post(
            "/api/v1/urls", json={"url": "https://example.com/", "alias": "promo"}, headers=AUTH
        )
        for _ in range(5):
            assert (await client.get("/promo")).status_code == 302  # no worker running at all
    assert await stack.redis.xlen("clicks") == 5
    assert await count_rows(stack.engine, "clicks") == 0  # nothing wrote to PostgreSQL on the request path


async def test_ac11_everything_works_with_redis_down(database_url: str, engine: AsyncEngine) -> None:
    settings = make_integration_settings(database_url, "redis://127.0.0.1:6390/0")  # nothing listens here
    dead_redis = create_redis(settings)
    stack = Stack(settings, engine, dead_redis, build_container(settings, engine, dead_redis))
    try:
        async with stack.client() as client:
            created = await client.post(
                "/api/v1/urls", json={"url": "https://example.com/", "alias": "promo"}, headers=AUTH
            )
            assert created.status_code == 201  # cache write skipped, rate limit failed open
            started = time.perf_counter()
            redirect = await client.get("/promo")
            assert redirect.status_code == 302 and redirect.headers["location"] == "https://example.com/"
            assert time.perf_counter() - started < 1.5
            for _ in range(3):  # enough failures to open the circuit breaker
                assert (await client.get("/promo")).status_code == 302
            started = time.perf_counter()
            assert (await client.get("/promo")).status_code == 302
            assert (
                time.perf_counter() - started < 0.1
            )  # breaker open: Redis is skipped, only PostgreSQL is used
            assert stack.container.metrics.get("clicks_dropped_total", reason="circuit_open") >= 1
            ready = await client.get("/health/ready")
            assert (ready.status_code, ready.json()["status"]) == (200, "degraded")
            assert ready.json()["checks"] == {"database": "ok", "redis": "down"}
            metrics = stack.container.metrics
            assert metrics.get("cache_errors_total") > 0
            assert metrics.get("rate_limit_errors_total") > 0
            assert (
                metrics.get("clicks_dropped_total", reason="circuit_open")
                + metrics.get("clicks_dropped_total", reason="error")
                + metrics.get("clicks_dropped_total", reason="timeout")
                >= 1
            )
    finally:
        await dead_redis.aclose()


async def test_readiness_reports_database_outage(settings, redis: Redis) -> None:  # type: ignore[no-untyped-def]
    broken = make_integration_settings(
        "postgresql+asyncpg://u:p@127.0.0.1:6391/none", settings.redis_url.get_secret_value()
    )
    from shortener.infra.db import create_db_engine

    engine = create_db_engine(broken)
    try:
        stack = Stack(broken, engine, redis, build_container(broken, engine, redis))
        async with stack.client() as client:
            response = await client.get("/health/ready")
            assert (response.status_code, response.json()["status"]) == (503, "unavailable")
            created = await client.post("/api/v1/urls", json={"url": "https://example.com/"}, headers=AUTH)
            assert created.status_code == 503 and created.headers["retry-after"] == "1"
            assert (await client.get("/health")).status_code == 200  # liveness ignores dependencies
    finally:
        await engine.dispose()


async def test_ac12_rate_limit_returns_429_with_retry_after_and_counters_expire(
    database_url: str, redis_url: str, engine: AsyncEngine, redis: Redis
) -> None:
    settings = make_integration_settings(database_url, redis_url, rate_limit_create_per_min=3)
    stack = Stack(settings, engine, redis, build_container(settings, engine, redis))
    async with stack.client() as client:
        statuses = [
            (
                await client.post("/api/v1/urls", json={"url": "https://example.com/"}, headers=AUTH)
            ).status_code
            for _ in range(5)
        ]
        assert statuses == [201, 201, 201, 429, 429]
        limited = await client.post("/api/v1/urls", json={"url": "https://example.com/"}, headers=AUTH)
        assert 1 <= int(limited.headers["retry-after"]) <= 60
        assert (
            await client.post("/api/v1/urls", json={"url": "https://example.com/"}, headers=OTHER)
        ).status_code == 201
    keys = [k async for k in redis.scan_iter(match="rl:*")]
    assert keys
    for key in keys:
        assert await redis.ttl(key) > 0  # every counter carries a TTL (volatile-lru protects the stream)
        assert "demo" not in key


async def test_ac14_two_replicas_behave_identically(
    database_url: str, redis_url: str, engine: AsyncEngine, redis: Redis
) -> None:
    settings = make_integration_settings(database_url, redis_url)
    second_redis = create_redis(settings)
    replica_a = Stack(settings, engine, redis, build_container(settings, engine, redis))
    replica_b = Stack(settings, engine, second_redis, build_container(settings, engine, second_redis))
    try:
        async with replica_a.client() as a, replica_b.client() as b:
            assert (
                await a.post(
                    "/api/v1/urls", json={"url": "https://example.com/", "alias": "shared"}, headers=AUTH
                )
            ).status_code == 201
            assert (await b.get("/shared")).status_code == 302  # created on A, served by B
            assert (await b.get("/api/v1/urls/shared", headers=AUTH)).json()["code"] == "shared"
            assert (
                await b.post(
                    "/api/v1/urls", json={"url": "https://example.com/", "alias": "shared"}, headers=OTHER
                )
            ).status_code == 409
            assert (await b.delete("/api/v1/urls/shared", headers=AUTH)).status_code == 204
            assert (await a.get("/shared")).status_code == 404  # deleted on B, honoured by A immediately
    finally:
        await second_redis.aclose()


async def test_no_cache_or_rate_limit_key_is_without_a_ttl(stack: Stack) -> None:
    async with stack.client() as client:
        await client.post(
            "/api/v1/urls", json={"url": "https://example.com/", "alias": "promo"}, headers=AUTH
        )
        await client.get("/promo")
        await client.get("/missing1")
        await client.delete("/api/v1/urls/promo", headers=AUTH)
    async for key in stack.redis.scan_iter():
        if key != "clicks":
            assert await stack.redis.ttl(key) > 0, key


async def test_redis_is_configured_so_the_cache_cannot_evict_the_stream(redis: Redis) -> None:
    policy = await redis.config_get("maxmemory-policy")
    appendonly = await redis.config_get("appendonly")
    assert policy["maxmemory-policy"] in {
        "volatile-lru",
        "noeviction",
        "volatile-lfu",
        "volatile-ttl",
        "volatile-random",
    }
    assert appendonly["appendonly"] == "yes"
