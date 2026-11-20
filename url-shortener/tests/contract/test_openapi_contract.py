"""Contract tests: the running app must match the committed openapi.yaml (the Phase 2 contract)."""

from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from jsonschema import Draft202012Validator, FormatChecker
from openapi_spec_validator import validate

from fakes import DEMO_KEY, NOW, OTHER_KEY, Env
from shortener.api.app import create_app
from shortener.domain.models import ClickEvent

SPEC_PATH = Path(__file__).resolve().parents[2] / "openapi.yaml"
SPEC: dict[str, Any] = yaml.safe_load(SPEC_PATH.read_text(encoding="utf-8"))
AUTH = {"X-API-Key": DEMO_KEY}
HTTP_METHODS = {"get", "post", "delete", "put", "patch"}


def schema_validator(name: str) -> Draft202012Validator:
    return Draft202012Validator(
        {"$ref": f"#/components/schemas/{name}", "components": SPEC["components"]},
        format_checker=FormatChecker(),
    )


def assert_matches(name: str, payload: object) -> None:
    errors = sorted(schema_validator(name).iter_errors(payload), key=lambda e: list(e.path))
    assert not errors, [f"{list(e.path)}: {e.message}" for e in errors]


def declared_statuses(path: str, method: str) -> set[str]:
    return set(SPEC["paths"][path][method.lower()]["responses"])


def resolve_response(path: str, method: str, status: int) -> dict[str, Any]:
    response = SPEC["paths"][path][method.lower()]["responses"][str(status)]
    if "$ref" in response:
        response = SPEC["components"]["responses"][response["$ref"].rsplit("/", 1)[1]]
    return response  # type: ignore[no-any-return]


def declared_headers(path: str, method: str, status: int) -> set[str]:
    headers = set(resolve_response(path, method, status).get("headers", {}))
    return {h.lower() for h in headers}


@pytest.fixture
async def env() -> Env:
    return Env()


@pytest.fixture
async def client(env: Env) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(env.container)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://short.test") as c:
        yield c


def test_the_contract_itself_is_a_valid_openapi_document() -> None:
    validate(SPEC)


def test_every_documented_operation_exists_and_nothing_undocumented_is_exposed(env: Env) -> None:
    generated = create_app(env.container).openapi()["paths"]
    exposed = {(p, m) for p, ops in generated.items() for m in ops if m in HTTP_METHODS}
    documented = {(p, m) for p, ops in SPEC["paths"].items() for m in ops if m in HTTP_METHODS}
    assert exposed == documented
    operation_ids = {
        op["operationId"] for ops in generated.values() for m, op in ops.items() if m in HTTP_METHODS
    }
    spec_ids = {
        op["operationId"] for ops in SPEC["paths"].values() for m, op in ops.items() if m in HTTP_METHODS
    }
    assert operation_ids == spec_ids


def test_the_examples_in_the_contract_validate_against_its_own_schemas() -> None:
    for response in SPEC["components"]["responses"].values():
        for media in response.get("content", {}).values():
            if "example" in media:
                assert_matches("Problem", media["example"])
    created = SPEC["paths"]["/api/v1/urls"]["post"]["responses"]["201"]["content"]["application/json"][
        "example"
    ]
    assert_matches("UrlResource", created)


async def test_create_get_and_delete_responses(client: httpx.AsyncClient) -> None:
    path = "/api/v1/urls"
    created = await client.post(
        path,
        json={"url": "https://example.com/p", "alias": "promo", "expires_at": "2027-03-31T23:59:59Z"},
        headers=AUTH,
    )
    assert created.status_code == 201 and str(created.status_code) in declared_statuses(path, "post")
    assert_matches("UrlResource", created.json())
    assert {"location", "x-request-id"} <= declared_headers(path, "post", 201)
    assert {"location", "x-request-id"} <= set(created.headers)

    fetched = await client.get(f"{path}/promo", headers=AUTH)
    assert fetched.status_code == 200
    assert_matches("UrlResource", fetched.json())

    deleted = await client.delete(f"{path}/promo", headers=AUTH)
    assert deleted.status_code == 204 and "204" in declared_statuses("/api/v1/urls/{code}", "delete")
    assert deleted.content == b""


async def test_analytics_response_with_data(client: httpx.AsyncClient, env: Env) -> None:
    await env.link_service.create(owner="demo", url="https://example.com/", alias="promo", expires_at=None)
    base = NOW - timedelta(hours=3)
    await env.clicks_repo.insert_batch(
        [
            ClickEvent(
                f"{int(base.timestamp() * 1000) + i}-0",
                1,
                base + timedelta(minutes=i),
                ref,
                "Mozilla/5.0 Firefox/121.0",
                fam,
                cty,
                "ab12cd34ef56ab12",
            )
            for i, (ref, fam, cty) in enumerate(
                [
                    ("https://news.example.com", "firefox", "DE"),
                    (None, "curl", None),
                    ("http://a.example:8080", "chrome", "US"),
                ]
            )
        ]
    )
    response = await client.get("/api/v1/urls/promo/analytics?limit=2&granularity=hour", headers=AUTH)
    assert response.status_code == 200
    body = response.json()
    assert_matches("AnalyticsResponse", body)
    assert body["total_clicks"] == 3 and len(body["recent_clicks"]["items"]) == 2
    assert body["recent_clicks"]["next_cursor"]
    second = await client.get(
        f"/api/v1/urls/promo/analytics?limit=2&cursor={body['recent_clicks']['next_cursor']}", headers=AUTH
    )
    assert_matches("AnalyticsResponse", second.json())
    assert len(second.json()["recent_clicks"]["items"]) == 1


@pytest.mark.parametrize(
    ("method", "path", "kwargs", "status", "extra_headers"),
    [
        ("POST", "/api/v1/urls", {"json": {"url": "https://example.com/"}}, 401, {"www-authenticate"}),
        ("GET", "/api/v1/urls/missing1", {"headers": AUTH}, 404, set()),
        ("DELETE", "/api/v1/urls/missing1", {"headers": AUTH}, 404, set()),
        ("GET", "/api/v1/urls/promo/analytics", {"headers": {"X-API-Key": OTHER_KEY}}, 404, set()),
        ("POST", "/api/v1/urls", {"json": {"url": "javascript:x"}, "headers": AUTH}, 422, set()),
        ("GET", "/api/v1/urls/ab", {"headers": AUTH}, 422, set()),
        ("GET", "/api/v1/urls/promo/analytics?limit=0", {"headers": AUTH}, 422, set()),
    ],
)
async def test_error_responses_are_declared_problem_documents(
    client: httpx.AsyncClient,
    env: Env,
    method: str,
    path: str,
    kwargs: dict[str, Any],
    status: int,
    extra_headers: set[str],
) -> None:
    await env.link_service.create(owner="demo", url="https://example.com/", alias="promo", expires_at=None)
    response = await client.request(method, path, **kwargs)
    assert response.status_code == status
    template = path.split("?")[0]
    template = (
        "/api/v1/urls/{code}/analytics"
        if template.endswith("/analytics")
        else ("/api/v1/urls/{code}" if template != "/api/v1/urls" else template)
    )
    assert str(status) in declared_statuses(template, method)
    assert response.headers["content-type"] == "application/problem+json"
    assert_matches("Problem", response.json())
    assert extra_headers <= set(response.headers)
    assert extra_headers <= declared_headers(template, method, status)


async def test_conflict_gone_rate_limit_and_unavailable_problems(env: Env) -> None:
    env = Env(rate_limit_create_per_min=1)
    app = create_app(env.container)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://short.test") as c:
        first = await c.post(
            "/api/v1/urls",
            json={
                "url": "https://example.com/",
                "alias": "promo",
                "expires_at": (NOW + timedelta(minutes=1)).isoformat(),
            },
            headers=AUTH,
        )
        assert first.status_code == 201
        limited = await c.post("/api/v1/urls", json={"url": "https://example.com/"}, headers=AUTH)
        assert limited.status_code == 429 and "429" in declared_statuses("/api/v1/urls", "post")
        assert_matches("Problem", limited.json())
        assert (
            "retry-after" in declared_headers("/api/v1/urls", "post", 429)
            and "retry-after" in limited.headers
        )

        env.clock.advance(minutes=2)
        gone = await c.get("/promo")
        assert gone.status_code == 410 and "410" in declared_statuses("/{code}", "get")
        assert_matches("Problem", gone.json())

        env.rate_store.counts.clear()
        conflict = await c.post(
            "/api/v1/urls", json={"url": "https://example.com/", "alias": "promo"}, headers=AUTH
        )
        assert conflict.status_code == 409 and "409" in declared_statuses("/api/v1/urls", "post")
        assert_matches("Problem", conflict.json())

        env.cache.data.clear()
        env.links_repo.down = True
        unavailable = await c.get("/promo")
        assert unavailable.status_code == 503
        assert_matches("Problem", unavailable.json())
        env.rate_store.counts.clear()
        post_503 = await c.post("/api/v1/urls", json={"url": "https://example.com/"}, headers=AUTH)
        assert post_503.status_code == 503 and "503" in declared_statuses("/api/v1/urls", "post")
        assert "retry-after" in declared_headers("/api/v1/urls", "post", 503)


async def test_redirect_response_matches_the_contract(client: httpx.AsyncClient, env: Env) -> None:
    await env.link_service.create(owner="demo", url="https://example.com/x", alias="promo", expires_at=None)
    response = await client.get("/promo")
    assert response.status_code == 302 and "302" in declared_statuses("/{code}", "get")
    for header in declared_headers("/{code}", "get", 302):
        assert header in response.headers, header
    missing = await client.get("/missing1")
    assert missing.status_code == 404
    assert_matches("Problem", missing.json())


@pytest.mark.parametrize(
    ("db_up", "redis_up", "status"), [(True, True, 200), (True, False, 200), (False, True, 503)]
)
async def test_readiness_matches_the_contract(
    client: httpx.AsyncClient, env: Env, db_up: bool, redis_up: bool, status: int
) -> None:
    env.db_up, env.redis_up = db_up, redis_up
    response = await client.get("/health/ready")
    assert response.status_code == status and str(status) in declared_statuses("/health/ready", "get")
    assert_matches("Readiness", response.json())
    assert (await client.get("/health")).json() == {"status": "ok"}
