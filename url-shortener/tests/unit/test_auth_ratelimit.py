import hashlib

import pytest

from fakes import DEMO_KEY, DEMO_KEY_SHA256, NOW, OTHER_KEY, FakeClock, InMemoryRateLimitStore
from shortener.domain.errors import RateLimited, Unauthorized
from shortener.metrics import Metrics
from shortener.services.auth import ApiKeyAuthenticator
from shortener.services.client_ip import client_ip
from shortener.services.rate_limiter import RateLimiter


def test_fixture_hash_matches_the_key() -> None:
    assert hashlib.sha256(DEMO_KEY.encode()).hexdigest() == DEMO_KEY_SHA256


def test_authenticate_returns_the_owner_label() -> None:
    auth = ApiKeyAuthenticator(
        {DEMO_KEY_SHA256: "demo", hashlib.sha256(OTHER_KEY.encode()).hexdigest(): "other"}
    )
    assert auth.authenticate(DEMO_KEY) == "demo"
    assert auth.authenticate(OTHER_KEY) == "other"


@pytest.mark.parametrize(
    "presented", [None, "", "wrong", DEMO_KEY.upper(), DEMO_KEY + " ", DEMO_KEY_SHA256, "x" * 300]
)
def test_authenticate_rejects_everything_else(presented: str | None) -> None:
    with pytest.raises(Unauthorized):
        ApiKeyAuthenticator({DEMO_KEY_SHA256: "demo"}).authenticate(presented)


def test_authenticate_uses_constant_time_comparison(monkeypatch: pytest.MonkeyPatch) -> None:
    import hmac

    from shortener.services import auth as auth_module

    calls: list[tuple[str, str]] = []
    real = hmac.compare_digest
    monkeypatch.setattr(
        auth_module.hmac, "compare_digest", lambda a, b: (calls.append((a, b)), real(a, b))[1]
    )
    hashes = {DEMO_KEY_SHA256: "demo", "0" * 64: "z1", "1" * 64: "z2"}
    ApiKeyAuthenticator(hashes).authenticate(DEMO_KEY)
    assert len(calls) == 3  # every configured hash is compared; a match does not short-circuit


async def test_rate_limiter_allows_up_to_the_limit_then_rejects_with_retry_after() -> None:
    store, clock, metrics = InMemoryRateLimitStore(), FakeClock(), Metrics()
    limiter = RateLimiter(store, clock, metrics)
    for _ in range(3):
        await limiter.check("create", "demo", 3)
    with pytest.raises(RateLimited) as info:
        await limiter.check("create", "demo", 3)
    expected_wait = 60 - int(NOW.timestamp()) % 60
    assert info.value.retry_after == expected_wait
    assert metrics.get("rate_limit_rejections_total", scope="create") == 1


async def test_rate_limit_windows_and_identities_are_independent() -> None:
    store, clock = InMemoryRateLimitStore(), FakeClock()
    limiter = RateLimiter(store, clock, Metrics())
    await limiter.check("create", "demo", 1)
    await limiter.check("create", "other", 1)  # different identity
    await limiter.check("api", "demo", 1)  # different scope
    with pytest.raises(RateLimited):
        await limiter.check("create", "demo", 1)
    clock.advance(seconds=60)
    await limiter.check("create", "demo", 1)  # new window


async def test_rate_limiter_fails_open_when_the_store_is_down() -> None:
    store, metrics = InMemoryRateLimitStore(), Metrics()
    store.down = True
    await RateLimiter(store, FakeClock(), metrics).check("redirect", "203.0.113.9", 1)  # must not raise
    assert metrics.get("rate_limit_errors_total") == 1


async def test_rate_limit_keys_never_contain_the_raw_identity() -> None:
    store = InMemoryRateLimitStore()
    await RateLimiter(store, FakeClock(), Metrics()).check("redirect", "203.0.113.9", 5)
    assert all("203.0.113.9" not in key for key in store.counts)


@pytest.mark.parametrize(
    ("forwarded", "peer", "hops", "expected"),
    [
        (None, "10.0.0.1", 0, "10.0.0.1"),
        ("1.2.3.4", "10.0.0.1", 0, "10.0.0.1"),  # header ignored when no proxy is trusted
        ("1.2.3.4", "10.0.0.1", 1, "1.2.3.4"),
        (
            "6.6.6.6, 1.2.3.4",
            "10.0.0.1",
            1,
            "1.2.3.4",
        ),  # client-supplied left entries cannot spoof the address
        ("6.6.6.6, 1.2.3.4, 10.9.9.9", "10.0.0.1", 2, "1.2.3.4"),
        ("1.2.3.4", "10.0.0.1", 2, "10.0.0.1"),  # fewer entries than trusted hops: use the socket peer
        ("not-an-ip", "10.0.0.1", 1, "10.0.0.1"),
        ("2001:db8::1", "10.0.0.1", 1, "2001:db8::1"),
        ("", "10.0.0.1", 1, "10.0.0.1"),
        ("1.2.3.4", None, 0, None),
    ],
)
def test_client_ip(forwarded: str | None, peer: str | None, hops: int, expected: str | None) -> None:
    assert client_ip(forwarded, peer, hops) == expected
