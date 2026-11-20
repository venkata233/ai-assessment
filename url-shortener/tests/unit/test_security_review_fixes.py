"""Regression tests for the Phase 4 security review (see docs/VALIDATION.md for the triage of each finding)."""

import ipaddress
from datetime import UTC, datetime, timedelta, timezone

import httpx
import pytest
from pydantic import ValidationError
from starlette.requests import Request

from fakes import DEMO_KEY, DEMO_KEY_SHA256, NOW, OTHER_KEY_SHA256, Env, make_settings
from shortener.api.app import create_app
from shortener.api.routes import _client_ip
from shortener.config import Settings
from shortener.domain.codes import is_reserved, is_valid_code_format, validate_alias
from shortener.domain.errors import ValidationFailed
from shortener.domain.url_validator import is_public_ip, own_origins_from_base_url, validate_target_url

AUTH = {"X-API-Key": DEMO_KEY}


# ----- finding 1: trailing newline slipped through the code/alias regex --------------------------------------


@pytest.mark.parametrize("code", ["abc\n", "admin\n", "abc\r\n", "abc\n\n", "\nabc", "ab\nc"])
def test_codes_with_newlines_are_not_valid(code: str) -> None:
    assert not is_valid_code_format(code)
    with pytest.raises(ValidationFailed) as info:
        validate_alias(code)
    assert info.value.issues[0].code == "alias-invalid"


def test_reserved_words_are_still_reserved_when_well_formed() -> None:
    assert is_reserved("admin") and is_reserved("ADMIN")


async def test_api_rejects_alias_with_trailing_newline_as_422_not_500() -> None:
    env = Env()
    transport = httpx.ASGITransport(app=create_app(env.container), raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://short.test") as client:
        response = await client.post(
            "/api/v1/urls", json={"url": "https://example.com/", "alias": "admin\n"}, headers=AUTH
        )
        assert response.status_code == 422
        assert response.json()["errors"][0]["code"] == "alias-invalid"
        before = env.links_repo.get_calls
        redirect = await client.get("/abc%0A")
        assert redirect.status_code == 404
        assert env.links_repo.get_calls == before  # rejected before any storage lookup
        assert env.cache.data == {}  # and no cache key with a newline was written
        assert (await client.get("/api/v1/urls/abc%0A", headers=AUTH)).status_code == 422


# ----- finding 2: self-reference check ignored the scheme and IDN normalisation ------------------------------


def test_own_origins_cover_both_schemes_and_the_configured_port() -> None:
    assert own_origins_from_base_url("https://sho.rt") == {("sho.rt", 443), ("sho.rt", 80)}
    assert own_origins_from_base_url("http://sho.rt:8080") == {
        ("sho.rt", 8080),
        ("sho.rt", 80),
        ("sho.rt", 443),
    }


@pytest.mark.parametrize(
    "target",
    ["http://sho.rt/x", "https://sho.rt/x", "http://SHO.rt./x", "https://sho.rt:443/x", "http://sho.rt:80/"],
)
def test_links_to_the_service_are_refused_whatever_the_scheme(target: str) -> None:
    origins = own_origins_from_base_url("https://sho.rt")
    with pytest.raises(ValidationFailed) as info:
        validate_target_url(target, own_origins=origins)
    assert info.value.issues[0].code == "self-reference"


def test_idn_base_url_is_compared_in_punycode() -> None:
    origins = own_origins_from_base_url("https://münchen.de")
    assert ("xn--mnchen-3ya.de", 443) in origins
    with pytest.raises(ValidationFailed):
        validate_target_url("https://xn--mnchen-3ya.de/x", own_origins=origins)
    with pytest.raises(ValidationFailed):
        validate_target_url("https://münchen.de/x", own_origins=origins)


def test_other_hosts_are_still_allowed() -> None:
    origins = own_origins_from_base_url("https://sho.rt")
    assert validate_target_url("https://notsho.rt/x", own_origins=origins).host == "notsho.rt"
    assert validate_target_url("https://sub.sho.rt/x", own_origins=origins).host == "sub.sho.rt"


# ----- theoretical concern: ranges that some Python versions report as global ---------------------------------


@pytest.mark.parametrize("address", ["192.0.0.8", "192.88.99.1", "198.18.0.1", "198.19.255.255", "3fff::1"])
def test_ranges_blocked_regardless_of_the_interpreters_tables(address: str) -> None:
    assert not is_public_ip(ipaddress.ip_address(address))
    with pytest.raises(ValidationFailed):
        validate_target_url(f"http://[{address}]/" if ":" in address else f"http://{address}/")


# ----- finding 3: deleting while Redis is down must be visible ------------------------------------------------


async def test_failed_tombstone_write_is_counted_and_the_delete_still_succeeds() -> None:
    env = Env()
    await env.link_service.create(owner="demo", url="https://example.com/", alias="promo", expires_at=None)
    env.cache.down = True
    await env.link_service.delete(owner="demo", code="promo")  # the row is deleted: no exception
    assert env.metrics.get("cache_invalidation_failures_total") == 1
    assert env.links_repo.rows["promo"].deleted_at is not None


async def test_successful_tombstone_write_is_not_counted_as_a_failure() -> None:
    env = Env()
    await env.link_service.create(owner="demo", url="https://example.com/", alias="promo", expires_at=None)
    await env.link_service.delete(owner="demo", code="promo")
    assert env.metrics.get("cache_invalidation_failures_total") == 0


# ----- finding 4: /metrics is opt-in -------------------------------------------------------------------------


def test_metrics_endpoint_and_geo_trust_are_off_by_default() -> None:
    settings = Settings(  # type: ignore[call-arg]
        database_url="postgresql+asyncpg://u:p@h/db",
        redis_url="redis://h:6379/0",
        api_keys="demo:" + "a" * 64,
        base_url="http://localhost:8000",
        analytics_secret="x" * 16,
    )
    assert settings.metrics_enabled is False
    assert settings.trust_geo_header is False
    assert settings.docs_enabled is False


async def test_metrics_route_does_not_exist_unless_enabled() -> None:
    env = Env(metrics_enabled=False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(env.container)), base_url="http://short.test"
    ) as client:
        assert (await client.get("/metrics")).status_code == 404


# ----- finding 5: demo credentials fail closed outside localhost ---------------------------------------------

BASE = {
    "database_url": "postgresql+asyncpg://u:p@h/db",
    "redis_url": "redis://h:6379/0",
    "api_keys": "owner:" + "b" * 64,
    "analytics_secret": "a-real-secret-value",
}


def settings_for(base_url: str, **overrides: object) -> Settings:
    return Settings(**{**BASE, "base_url": base_url, **overrides})  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "overrides",
    [
        {"api_keys": f"demo:{DEMO_KEY_SHA256}"},
        {"api_keys": f"x:{'b' * 64},other:{OTHER_KEY_SHA256}"},
        {"database_url": "postgresql+asyncpg://shortener:shortener_dev_only@db/shortener"},
        {"analytics_secret": "analytics_secret_dev_only_change_me"},
    ],
)
def test_public_dev_credentials_are_refused_on_a_non_local_base_url(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError, match="refusing to start"):
        settings_for("https://short.example.com", **overrides)


@pytest.mark.parametrize("base", ["http://localhost:8000", "http://127.0.0.1:8000", "http://[::1]:8000"])
def test_public_dev_credentials_are_fine_locally(base: str) -> None:
    assert settings_for(base, api_keys=f"demo:{DEMO_KEY_SHA256}").api_key_hashes


def test_real_credentials_are_fine_anywhere() -> None:
    assert settings_for("https://short.example.com").base_url == "https://short.example.com"


def test_startup_errors_never_echo_secret_values() -> None:
    secret = "tail-of-a-real-secret-0123456789"
    for overrides in (
        {"analytics_secret": secret[:10]},  # too short: rejected
        {"database_url": f"postgresql://user:{secret}@h/db"},  # wrong scheme: rejected
        {"api_keys": f"owner:{secret}"},  # malformed: rejected
        {"analytics_secret": secret + "_dev_only"},  # dev marker on a non-local base URL
    ):
        with pytest.raises(ValidationError) as info:
            settings_for("https://short.example.com", **overrides)
        assert secret[:10] not in str(info.value) and "input_value" not in str(info.value), overrides


def test_break_glass_flag_allows_dev_credentials() -> None:
    assert make_settings().allow_dev_credentials  # the test fixtures rely on it
    assert settings_for(
        "https://x.example.com", api_keys=f"demo:{DEMO_KEY_SHA256}", allow_dev_credentials=True
    )


# ----- finding 6: the country header is only honoured when the operator says the edge sets it ----------------


async def click_country(env: Env, headers: dict[str, str]) -> str | None:
    await env.link_service.create(owner="demo", url="https://example.com/", alias="promo", expires_at=None)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(env.container)), base_url="http://short.test"
    ) as client:
        assert (await client.get("/promo", headers=headers)).status_code == 302
    return env.publisher.records[-1].country


async def test_country_header_is_ignored_by_default() -> None:
    assert await click_country(Env(trust_geo_header=False), {"CF-IPCountry": "DE"}) is None


async def test_country_header_is_used_when_trusted() -> None:
    assert await click_country(Env(trust_geo_header=True), {"CF-IPCountry": "DE"}) == "DE"


# ----- theoretical: multiple X-Forwarded-For header lines ----------------------------------------------------


def request_with(headers: list[tuple[bytes, bytes]]) -> Request:
    scope = {"type": "http", "method": "GET", "path": "/", "headers": headers, "client": ("10.0.0.9", 1234)}
    return Request(scope)


def test_forwarded_for_split_across_header_lines_is_treated_as_one_list() -> None:
    env = Env(trusted_proxy_hops=1)
    split = request_with([(b"x-forwarded-for", b"6.6.6.6"), (b"x-forwarded-for", b"1.2.3.4")])
    assert _client_ip(split, env.container) == "1.2.3.4"  # the last entry is the one our proxy appended
    single = request_with([(b"x-forwarded-for", b"6.6.6.6, 1.2.3.4")])
    assert _client_ip(single, env.container) == "1.2.3.4"
    assert _client_ip(request_with([]), env.container) == "10.0.0.9"
    assert _client_ip(split, Env(trusted_proxy_hops=0).container) == "10.0.0.9"


# ----- theoretical: timestamps that overflow datetime arithmetic ---------------------------------------------


async def test_out_of_range_analytics_timestamps_are_validation_errors_not_500s() -> None:
    env = Env()
    await env.link_service.create(owner="demo", url="https://example.com/", alias="promo", expires_at=None)
    for kwargs in (
        {"start": datetime(1, 1, 1, tzinfo=timezone(timedelta(hours=5)))},
        {"end": datetime(1, 1, 1, tzinfo=UTC)},  # `to` minus the default 7-day window underflows
        {"end": datetime(1, 1, 1, tzinfo=timezone(timedelta(hours=-5)))},
    ):
        with pytest.raises(ValidationFailed):
            await env.analytics.report(
                owner="demo",
                code="promo",
                start=kwargs.get("start"),
                end=kwargs.get("end"),
                granularity="day",
                limit=10,
                cursor=None,
            )
    assert NOW  # fixture import used above


async def test_out_of_range_timestamps_over_http_are_422() -> None:
    env = Env()
    await env.link_service.create(owner="demo", url="https://example.com/", alias="promo", expires_at=None)
    transport = httpx.ASGITransport(app=create_app(env.container), raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://short.test") as client:
        for query in ("from=0001-01-01T00:00:00%2B05:00", "to=0001-01-01T00:00:00Z"):
            response = await client.get(f"/api/v1/urls/promo/analytics?{query}", headers=AUTH)
            assert response.status_code == 422, query
