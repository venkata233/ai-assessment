import asyncio
import time
from collections.abc import AsyncIterator
from datetime import timedelta

import httpx
import pytest

from fakes import DEMO_KEY, NOW, OTHER_KEY, Env
from shortener.api.app import create_app
from shortener.domain.errors import StorageUnavailable

AUTH = {"X-API-Key": DEMO_KEY}
OTHER_AUTH = {"X-API-Key": OTHER_KEY}


@pytest.fixture
def env() -> Env:
    return Env()


@pytest.fixture
async def client(env: Env) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(env.container)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://short.test") as c:
        yield c


async def make(
    client: httpx.AsyncClient, url: str = "https://example.com/page", **extra: object
) -> dict[str, object]:
    response = await client.post("/api/v1/urls", json={"url": url, **extra}, headers=AUTH)
    assert response.status_code == 201, response.text
    body: dict[str, object] = response.json()
    return body


def assert_problem(response: httpx.Response, status: int, slug: str) -> dict[str, object]:
    assert response.status_code == status, response.text
    assert response.headers["content-type"] == "application/problem+json"
    body: dict[str, object] = response.json()
    assert body["status"] == status
    assert body["type"] == f"https://shortener.example/problems/{slug}"
    assert body["request_id"] == response.headers["x-request-id"]
    return body


# ----- create ---------------------------------------------------------------------------------------


async def test_create_returns_201_with_location_and_resource(client: httpx.AsyncClient, env: Env) -> None:
    env.use_codes(["aB3x9Zk"])
    response = await client.post(
        "/api/v1/urls", json={"url": "https://example.com/products/42?ref=n"}, headers=AUTH
    )
    assert response.status_code == 201
    assert response.headers["location"] == "/api/v1/urls/aB3x9Zk"
    assert response.json() == {
        "code": "aB3x9Zk",
        "short_url": "http://short.test/aB3x9Zk",
        "target_url": "https://example.com/products/42?ref=n",
        "is_custom_alias": False,
        "status": "active",
        "click_count": 0,
        "created_at": "2026-11-20T12:00:00Z",
        "expires_at": None,
    }


async def test_responses_carry_request_id_and_security_headers(client: httpx.AsyncClient) -> None:
    response = await client.get("/health", headers={"X-Request-ID": "trace-12345678"})
    assert response.headers["x-request-id"] == "trace-12345678"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["cache-control"] == "no-store"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    hostile = await client.get("/health", headers={"X-Request-ID": "bad id\twith spaces"})
    assert hostile.headers["x-request-id"] != "bad id\twith spaces"
    assert len(hostile.headers["x-request-id"]) == 32


@pytest.mark.parametrize("headers", [{}, {"X-API-Key": "wrong"}, {"X-API-Key": ""}])
async def test_missing_or_wrong_key_is_401(client: httpx.AsyncClient, headers: dict[str, str]) -> None:
    response = await client.post("/api/v1/urls", json={"url": "https://example.com/"}, headers=headers)
    assert_problem(response, 401, "unauthorized")
    assert response.headers["www-authenticate"] == "ApiKey"


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/v1/urls/promo"),
        ("DELETE", "/api/v1/urls/promo"),
        ("GET", "/api/v1/urls/promo/analytics"),
    ],
)
async def test_management_endpoints_require_a_key(client: httpx.AsyncClient, method: str, path: str) -> None:
    await make(client, alias="promo")
    assert_problem(await client.request(method, path), 401, "unauthorized")


async def test_alias_conflict_is_409_and_original_survives(client: httpx.AsyncClient) -> None:
    await make(client, "https://example.com/one", alias="promo")
    response = await client.post(
        "/api/v1/urls", json={"url": "https://example.com/two", "alias": "promo"}, headers=OTHER_AUTH
    )
    assert_problem(response, 409, "alias-conflict")
    assert (await client.get("/promo")).headers["location"] == "https://example.com/one"


@pytest.mark.parametrize(
    ("body", "field", "code"),
    [
        ({"url": "javascript:alert(1)"}, "url", "scheme-not-allowed"),
        ({"url": "http://169.254.169.254/latest"}, "url", "private-address"),
        ({"url": "https://user:pw@example.com/"}, "url", "credentials-not-allowed"),
        ({"url": "https://example.com/", "alias": "api"}, "alias", "alias-reserved"),
        ({"url": "https://example.com/", "alias": "no spaces"}, "alias", "alias-invalid"),
        (
            {"url": "https://example.com/", "expires_at": "2020-01-01T00:00:00Z"},
            "expires_at",
            "expiry-in-past",
        ),
        (
            {"url": "https://example.com/", "expires_at": "2030-01-01T00:00:00"},
            "expires_at",
            "expiry-timezone-required",
        ),
        ({"url": "https://example.com/", "expires_at": "tomorrow"}, "expires_at", "expiry-invalid"),
        ({}, "url", "missing"),
        ({"url": 123}, "url", "string_type"),
        ({"url": "https://example.com/", "owner": "evil"}, "owner", "extra_forbidden"),
    ],
)
async def test_validation_errors_are_422_problems_naming_the_field(
    client: httpx.AsyncClient, env: Env, body: dict[str, object], field: str, code: str
) -> None:
    response = await client.post("/api/v1/urls", json=body, headers=AUTH)
    problem = assert_problem(response, 422, "validation-error")
    assert {"field": field, "code": code}.items() <= next(
        e for e in problem["errors"] if e["field"] == field
    ).items()  # type: ignore[attr-defined,union-attr]
    assert env.links_repo.insert_calls == 0  # nothing persisted


async def test_malformed_json_is_422_and_does_not_echo_input(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/api/v1/urls",
        content=b'{"url": "https://example.com/", SECRET-TOKEN',
        headers={**AUTH, "content-type": "application/json"},
    )
    assert_problem(response, 422, "validation-error")
    assert "SECRET-TOKEN" not in response.text


async def test_oversized_url_is_rejected(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/api/v1/urls", json={"url": "https://example.com/" + "a" * 9000}, headers=AUTH
    )
    assert_problem(response, 422, "validation-error")


# ----- redirect -------------------------------------------------------------------------------------


async def test_redirect_is_302_with_cache_and_referrer_headers(client: httpx.AsyncClient) -> None:
    await make(client, "https://example.com/target?x=1", alias="promo")
    response = await client.get("/promo")
    assert response.status_code == 302
    assert response.headers["location"] == "https://example.com/target?x=1"
    assert response.headers["cache-control"] == "private, max-age=0"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.content == b""


async def test_click_is_published_with_privacy_preserving_fields(client: httpx.AsyncClient, env: Env) -> None:
    link = await make(client, alias="promo")
    await client.get(
        "/promo",
        headers={
            "Referer": "https://news.example.com/story?token=SECRET#x",
            "User-Agent": "Mozilla/5.0 Firefox/121.0",
            "CF-IPCountry": "de",
        },
    )
    assert len(env.publisher.records) == 1
    record = env.publisher.records[0]
    assert record.referrer == "https://news.example.com"
    assert (record.ua_family, record.country) == ("firefox", "DE")
    assert record.ts_ms == int(NOW.timestamp() * 1000)
    assert record.url_id == env.links_repo.rows[str(link["code"])].id
    assert record.visitor_hash is not None and len(record.visitor_hash) == 16
    assert "127.0.0.1" not in repr(record)  # raw IPs never enter the event
    assert "SECRET" not in repr(record)


async def test_head_redirects_but_is_not_counted(client: httpx.AsyncClient, env: Env) -> None:
    await make(client, alias="promo")
    response = await client.head("/promo")
    assert response.status_code == 302 and response.headers["location"] == "https://example.com/page"
    assert env.publisher.records == []


async def test_unknown_deleted_malformed_and_reserved_codes_are_404(
    client: httpx.AsyncClient, env: Env
) -> None:
    await make(client, alias="gone01")
    await client.delete("/api/v1/urls/gone01", headers=AUTH)
    for path in (
        "/missing1",
        "/gone01",
        "/bad!code",
        "/ab",
        "/" + "x" * 40,
        "/docs",
        "/Admin",
        "/favicon.ico",
    ):
        assert_problem(await client.get(path), 404, "link-not-found")
    assert env.publisher.records == []


async def test_malformed_and_reserved_codes_never_reach_storage(client: httpx.AsyncClient, env: Env) -> None:
    before = env.links_repo.get_calls
    for path in ("/bad!code", "/ab", "/docs", "/Admin"):
        await client.get(path)
    assert env.links_repo.get_calls == before


async def test_expired_link_is_410_even_when_cached(client: httpx.AsyncClient, env: Env) -> None:  # AC-6
    await make(client, alias="promo", expires_at="2026-11-20T12:05:00Z")
    assert (await client.get("/promo")).status_code == 302
    env.clock.advance(minutes=6)
    assert_problem(await client.get("/promo"), 410, "link-expired")
    assert len(env.publisher.records) == 1  # only the successful click counted


async def test_delete_takes_effect_immediately(client: httpx.AsyncClient) -> None:  # AC-7
    await make(client, alias="promo")
    assert (await client.get("/promo")).status_code == 302
    assert (await client.delete("/api/v1/urls/promo", headers=AUTH)).status_code == 204
    assert_problem(await client.get("/promo"), 404, "link-not-found")


async def test_redirect_works_with_redis_down(client: httpx.AsyncClient, env: Env) -> None:  # AC-11
    await make(client, alias="promo")
    env.cache.down = True
    env.rate_store.down = True
    assert (await client.get("/promo")).status_code == 302


# ----- metadata / delete / analytics -----------------------------------------------------------------


async def test_metadata_reports_status_and_hides_other_owners(client: httpx.AsyncClient, env: Env) -> None:
    await make(client, alias="promo", expires_at="2026-11-20T12:05:00Z")
    assert (await client.get("/api/v1/urls/promo", headers=AUTH)).json()["status"] == "active"
    env.clock.advance(minutes=10)
    assert (await client.get("/api/v1/urls/promo", headers=AUTH)).json()[
        "status"
    ] == "expired"  # owner still sees it
    for headers, path in ((OTHER_AUTH, "/api/v1/urls/promo"), (AUTH, "/api/v1/urls/missing1")):  # AC-8
        assert_problem(await client.get(path, headers=headers), 404, "link-not-found")


async def test_path_code_must_match_the_pattern(client: httpx.AsyncClient) -> None:
    problem = assert_problem(await client.get("/api/v1/urls/a", headers=AUTH), 422, "validation-error")
    assert problem["errors"][0]["field"] == "code"  # type: ignore[index]


async def test_delete_is_owner_only_and_not_idempotent(client: httpx.AsyncClient) -> None:
    await make(client, alias="promo")
    assert_problem(await client.delete("/api/v1/urls/promo", headers=OTHER_AUTH), 404, "link-not-found")
    assert (await client.delete("/api/v1/urls/promo", headers=AUTH)).status_code == 204
    assert_problem(await client.delete("/api/v1/urls/promo", headers=AUTH), 404, "link-not-found")


async def test_analytics_endpoint_shape_and_validation(client: httpx.AsyncClient, env: Env) -> None:
    await make(client, alias="promo")
    response = await client.get("/api/v1/urls/promo/analytics", headers=AUTH)
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {
        "code",
        "from",
        "to",
        "granularity",
        "total_clicks",
        "approx_unique_visitors",
        "clicks_over_time",
        "top_referrers",
        "top_user_agents",
        "top_countries",
        "recent_clicks",
    }
    assert body["total_clicks"] == 0 and body["recent_clicks"] == {"items": [], "next_cursor": None}
    for query, field in (
        ("limit=0", "limit"),
        ("limit=101", "limit"),
        ("granularity=week", "granularity"),
        ("from=notadate", "from"),
    ):
        problem = assert_problem(
            await client.get(f"/api/v1/urls/promo/analytics?{query}", headers=AUTH), 422, "validation-error"
        )
        assert problem["errors"][0]["field"] == field  # type: ignore[index]
    window = f"from={(NOW - timedelta(days=100)).isoformat().replace('+00:00', 'Z')}"
    assert_problem(
        await client.get(f"/api/v1/urls/promo/analytics?{window}", headers=AUTH), 422, "validation-error"
    )
    assert_problem(
        await client.get("/api/v1/urls/promo/analytics", headers=OTHER_AUTH), 404, "link-not-found"
    )


# ----- rate limiting --------------------------------------------------------------------------------


async def test_create_is_rate_limited_per_key(env: Env) -> None:  # AC-12
    env = Env(rate_limit_create_per_min=2)
    app = create_app(env.container)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://short.test") as c:
        for _ in range(2):
            assert (
                await c.post("/api/v1/urls", json={"url": "https://example.com/"}, headers=AUTH)
            ).status_code == 201
        limited = await c.post("/api/v1/urls", json={"url": "https://example.com/"}, headers=AUTH)
        assert_problem(limited, 429, "rate-limited")
        assert int(limited.headers["retry-after"]) >= 1
        assert (
            await c.post("/api/v1/urls", json={"url": "https://example.com/"}, headers=OTHER_AUTH)
        ).status_code == 201


async def test_redirects_are_rate_limited_per_client_and_xff_cannot_evade_it() -> None:
    env = Env(rate_limit_redirect_per_min=2)  # TRUSTED_PROXY_HOPS defaults to 0: the header is not trusted
    await env.link_service.create(owner="demo", url="https://example.com/", alias="promo", expires_at=None)
    app = create_app(env.container)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://short.test") as c:
        for n in range(2):
            assert (await c.get("/promo", headers={"X-Forwarded-For": f"9.9.9.{n}"})).status_code == 302
        limited = await c.get("/promo", headers={"X-Forwarded-For": "9.9.9.99"})
        assert_problem(limited, 429, "rate-limited")
        assert "retry-after" in limited.headers


async def test_failed_authentication_attempts_are_throttled() -> None:
    env = Env(rate_limit_api_per_min=2)
    app = create_app(env.container)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://short.test") as c:
        statuses = [
            (await c.get("/api/v1/urls/promo", headers={"X-API-Key": "guess"})).status_code for _ in range(4)
        ]
    assert statuses == [401, 401, 429, 429]


async def test_rate_limiting_fails_open_when_redis_is_down(client: httpx.AsyncClient, env: Env) -> None:
    env.rate_store.down = True
    assert (
        await client.post("/api/v1/urls", json={"url": "https://example.com/"}, headers=AUTH)
    ).status_code == 201
    assert env.metrics.get("rate_limit_errors_total") >= 1


# ----- operations and error handling -----------------------------------------------------------------


async def test_liveness_does_not_touch_dependencies(client: httpx.AsyncClient, env: Env) -> None:
    env.db_up = env.redis_up = False
    response = await client.get("/health")
    assert (response.status_code, response.json()) == (200, {"status": "ok"})


@pytest.mark.parametrize(
    ("db_up", "redis_up", "status", "body_status"),
    [
        (True, True, 200, "ok"),
        (True, False, 200, "degraded"),
        (False, True, 503, "unavailable"),
        (False, False, 503, "unavailable"),
    ],
)
async def test_readiness(
    client: httpx.AsyncClient, env: Env, db_up: bool, redis_up: bool, status: int, body_status: str
) -> None:
    env.db_up, env.redis_up = db_up, redis_up
    response = await client.get("/health/ready")
    assert response.status_code == status
    assert response.json() == {
        "status": body_status,
        "checks": {"database": "ok" if db_up else "down", "redis": "ok" if redis_up else "down"},
    }


async def test_metrics_endpoint_exposes_counters(client: httpx.AsyncClient) -> None:
    await make(client, alias="promo")
    await client.get("/promo")
    text = (await client.get("/metrics")).text
    assert "cache_hits_total 1" in text
    assert "# TYPE clicks_dropped_total counter" in text


async def test_metrics_can_be_disabled() -> None:
    env = Env(metrics_enabled=False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(env.container)), base_url="http://short.test"
    ) as c:
        assert (await c.get("/metrics")).status_code == 404


async def test_redirect_response_is_complete_before_the_click_is_published(env: Env) -> None:
    """AC-10: the visitor's response never waits for (or depends on) the analytics emit."""
    await env.link_service.create(owner="demo", url="https://example.com/", alias="promo", expires_at=None)
    events: list[tuple[str, float]] = []
    started = time.perf_counter()

    class SlowPublisher:
        async def publish(self, record: object) -> None:
            await asyncio.sleep(0.3)  # a slow or hung Redis
            events.append(("published", time.perf_counter() - started))

    env.container.publisher = SlowPublisher()  # type: ignore[assignment]
    app = create_app(env.container)
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "path": "/promo",
        "raw_path": b"/promo",
        "query_string": b"",
        "headers": [(b"host", b"short.test")],
        "client": ("127.0.0.1", 5000),
        "server": ("short.test", 80),
        "scheme": "http",
        "root_path": "",
    }

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, object]) -> None:
        if message["type"] == "http.response.start":
            events.append((f"status-{message['status']}", time.perf_counter() - started))
        elif message["type"] == "http.response.body" and not message.get("more_body"):
            events.append(("response-complete", time.perf_counter() - started))

    await app(scope, receive, send)  # type: ignore[arg-type]
    assert [name for name, _ in events] == ["status-302", "response-complete", "published"]
    assert events[1][1] < 0.15  # the response did not wait for the 300 ms publish


async def test_unknown_routes_and_methods_use_problem_json(client: httpx.AsyncClient) -> None:
    assert_problem(await client.get("/api/v1/nothing/here"), 404, "not-found")
    assert_problem(await client.get("/api/v1/urls"), 405, "method-not-allowed")


async def test_database_outage_is_a_503_problem_with_retry_after(client: httpx.AsyncClient, env: Env) -> None:
    await make(client, alias="promo")
    env.cache.data.clear()
    env.links_repo.down = True
    response = await client.get("/promo")
    assert_problem(response, 503, "service-unavailable")
    assert response.headers["retry-after"] == "1"


async def test_unexpected_errors_do_not_leak_internals(client: httpx.AsyncClient, env: Env) -> None:
    async def boom(code: str) -> None:
        raise RuntimeError("db password is hunter2 at 10.1.2.3")

    env.links_repo.get_by_code = boom  # type: ignore[method-assign,assignment]
    response = await client.get("/whatever1")
    body = assert_problem(response, 500, "internal-error")
    assert "hunter2" not in response.text and "10.1.2.3" not in response.text
    assert body["detail"] == "An unexpected error occurred."


async def test_storage_unavailable_on_create_is_503(client: httpx.AsyncClient, env: Env) -> None:
    env.links_repo.down = True
    response = await client.post("/api/v1/urls", json={"url": "https://example.com/"}, headers=AUTH)
    assert_problem(response, 503, "service-unavailable")
    assert isinstance(StorageUnavailable(), Exception)
