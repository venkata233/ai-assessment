from datetime import timedelta

import pytest

from fakes import NOW, Env
from shortener.domain.errors import (
    AliasConflict,
    CodeGenerationExhausted,
    LinkExpired,
    LinkNotFound,
    StorageUnavailable,
    ValidationFailed,
)
from shortener.domain.models import Link

OWNER = "demo"
OTHER = "other"


async def create(env: Env, url: str = "https://example.com/a", **kwargs: object) -> Link:
    params: dict[str, object] = {"owner": OWNER, "url": url, "alias": None, "expires_at": None}
    params.update(kwargs)
    return await env.link_service.create(**params)  # type: ignore[arg-type]


# ----- create ---------------------------------------------------------------------------------------


async def test_create_generated_code_persists_and_warms_the_cache() -> None:
    env = Env()
    env.use_codes(["aB3x9Zk"])
    link = await create(env)
    assert (link.code, link.owner_key_id, link.is_custom_alias, link.click_count) == (
        "aB3x9Zk",
        OWNER,
        False,
        0,
    )
    assert link.created_at == NOW
    entry, ttl = env.cache.data["aB3x9Zk"]
    assert (entry.state, entry.url_id, entry.target) == ("active", link.id, "https://example.com/a")
    assert 3240 <= ttl <= 3960  # 3600 s +-10 % jitter


async def test_create_with_alias_is_marked_custom() -> None:
    env = Env()
    link = await create(env, alias="promo")
    assert (link.code, link.is_custom_alias) == ("promo", True)
    assert env.generator.calls == 0


async def test_alias_conflict_leaves_the_original_untouched() -> None:
    env = Env()
    first = await create(env, "https://example.com/one", alias="promo")
    with pytest.raises(AliasConflict):
        await create(env, "https://example.com/two", alias="promo", owner=OTHER)
    assert env.links_repo.rows["promo"] == first
    assert env.links_repo.insert_calls == 2  # exactly one attempt, no retry loop for caller-chosen aliases


async def test_deleted_alias_stays_reserved() -> None:
    env = Env()
    await create(env, alias="promo")
    await env.link_service.delete(owner=OWNER, code="promo")
    with pytest.raises(AliasConflict):
        await create(env, alias="promo")


async def test_generated_collision_is_retried() -> None:
    env = Env()
    env.use_codes(["aaaaaaa", "aaaaaaa", "bbbbbbb"])
    await create(env)  # takes aaaaaaa
    second = await create(env, "https://example.com/b")  # aaaaaaa collides, bbbbbbb succeeds
    assert second.code == "bbbbbbb"


async def test_generated_collisions_are_bounded() -> None:
    env = Env()
    await create(env, alias="taken01")
    env.use_codes(["taken01"] * 10)
    before = env.links_repo.insert_calls
    with pytest.raises(CodeGenerationExhausted):
        await create(env)
    assert env.links_repo.insert_calls - before == 5


async def test_all_field_errors_are_reported_together_and_nothing_is_persisted() -> None:
    env = Env(dns_check_enabled=True)
    with pytest.raises(ValidationFailed) as info:
        await create(env, "javascript:alert(1)", alias="api", expires_at=NOW - timedelta(days=1))
    assert {(i.field, i.code) for i in info.value.issues} == {
        ("url", "scheme-not-allowed"),
        ("alias", "alias-reserved"),
        ("expires_at", "expiry-in-past"),
    }
    assert env.links_repo.insert_calls == 0
    assert env.dns.calls == []  # DNS is only consulted once the offline checks pass


async def test_naive_expiry_is_rejected() -> None:
    env = Env()
    with pytest.raises(ValidationFailed) as info:
        await create(env, expires_at=NOW.replace(tzinfo=None) + timedelta(days=1))
    assert info.value.issues[0].code == "expiry-timezone-required"


async def test_hostname_resolving_to_a_private_address_is_rejected() -> None:
    env = Env(dns_check_enabled=True)
    env.dns.mapping = {"evil.example.com": ["93.184.216.34", "10.0.0.5"]}  # one bad record is enough
    with pytest.raises(ValidationFailed) as info:
        await create(env, "https://evil.example.com/x")
    assert info.value.issues[0].code == "private-address"
    assert env.links_repo.insert_calls == 0


async def test_dns_check_allows_public_unresolvable_and_garbage_answers() -> None:
    env = Env(dns_check_enabled=True)
    env.dns.mapping = {
        "ok.example.com": ["93.184.216.34", "2606:2800:220:1::1"],
        "weird.example.com": ["not-an-ip"],
    }
    await create(env, "https://ok.example.com/")
    await create(env, "https://weird.example.com/")
    await create(env, "https://unresolvable.example.com/")  # advisory check: lookup failure does not block
    assert env.dns.calls == ["ok.example.com", "weird.example.com", "unresolvable.example.com"]


async def test_ip_literals_skip_dns() -> None:
    env = Env(dns_check_enabled=True)
    await create(env, "http://8.8.8.8/")
    assert env.dns.calls == []


async def test_expiry_caps_the_cache_ttl() -> None:
    env = Env()
    link = await create(env, expires_at=NOW + timedelta(seconds=100))
    assert 1 <= env.cache.data[link.code][1] <= 100


async def test_sub_second_expiry_is_not_cached() -> None:
    env = Env()
    link = await create(env, expires_at=NOW + timedelta(milliseconds=400))
    assert link.code not in env.cache.data


async def test_create_still_works_when_redis_is_down() -> None:
    env = Env()
    env.cache.down = True
    link = await create(env)
    assert link.code in env.links_repo.rows


async def test_create_surfaces_database_outage() -> None:
    env = Env()
    env.links_repo.down = True
    with pytest.raises(StorageUnavailable):
        await create(env)


# ----- resolve (redirect) ---------------------------------------------------------------------------


async def test_resolve_hit_does_not_touch_the_database() -> None:
    env = Env()
    link = await create(env, alias="promo")
    reads = env.links_repo.get_calls
    resolved = await env.link_service.resolve("promo")
    assert (resolved.url_id, resolved.target) == (link.id, "https://example.com/a")
    assert env.links_repo.get_calls == reads
    assert env.metrics.get("cache_hits_total") == 1


async def test_warm_cache_never_overrides_expiry() -> None:
    env = Env()
    await create(env, alias="promo", expires_at=NOW + timedelta(minutes=5))
    await env.link_service.resolve("promo")  # warm and valid
    env.clock.advance(minutes=5, seconds=1)
    with pytest.raises(LinkExpired):
        await env.link_service.resolve("promo")  # still in cache, but past expires_at


async def test_expiry_boundary_is_exclusive() -> None:
    env = Env()
    await create(env, alias="promo", expires_at=NOW + timedelta(minutes=5))
    env.clock.advance(minutes=5)  # exactly at expires_at, warm cache
    with pytest.raises(LinkExpired):
        await env.link_service.resolve("promo")


async def test_expiry_boundary_is_exclusive_on_the_database_path_too() -> None:
    env = Env()
    await create(env, alias="promo", expires_at=NOW + timedelta(minutes=5))
    env.cache.data.clear()  # cold cache: the decision is made from the stored row
    env.clock.advance(minutes=5)
    with pytest.raises(LinkExpired):
        await env.link_service.resolve("promo")
    env.clock.advance(seconds=-1)
    assert (await env.link_service.resolve("promo")).target  # one second earlier it is still live
    link = await env.link_service.get(owner=OWNER, code="promo")
    assert link.status(NOW + timedelta(minutes=5)) == "expired"
    assert link.status(NOW + timedelta(minutes=5) - timedelta(microseconds=1)) == "active"


async def test_miss_reads_the_database_then_serves_from_cache() -> None:
    env = Env()
    await create(env, alias="promo")
    env.cache.data.clear()
    await env.link_service.resolve("promo")
    reads = env.links_repo.get_calls
    await env.link_service.resolve("promo")
    assert env.links_repo.get_calls == reads
    assert env.metrics.get("cache_misses_total") == 1


async def test_unknown_code_is_negative_cached() -> None:
    env = Env()
    with pytest.raises(LinkNotFound):
        await env.link_service.resolve("nope123")
    assert env.cache.data["nope123"][0].state == "absent"
    assert env.cache.data["nope123"][1] == 30
    reads = env.links_repo.get_calls
    with pytest.raises(LinkNotFound):
        await env.link_service.resolve("nope123")
    assert env.links_repo.get_calls == reads


async def test_creating_a_code_clears_its_negative_entry() -> None:
    env = Env()
    with pytest.raises(LinkNotFound):
        await env.link_service.resolve("fresh1")
    await create(env, alias="fresh1")
    assert (await env.link_service.resolve("fresh1")).target == "https://example.com/a"


async def test_delete_invalidates_a_warm_cache_entry() -> None:  # AC-7
    env = Env()
    await create(env, alias="promo")
    await env.link_service.resolve("promo")
    await env.link_service.delete(owner=OWNER, code="promo")
    with pytest.raises(LinkNotFound):
        await env.link_service.resolve("promo")
    assert env.cache.data["promo"][0].state == "deleted"


async def test_deleted_row_found_in_the_database_is_tombstoned() -> None:
    env = Env()
    await create(env, alias="promo")
    await env.link_service.delete(owner=OWNER, code="promo")
    env.cache.data.clear()
    with pytest.raises(LinkNotFound):
        await env.link_service.resolve("promo")
    assert env.cache.data["promo"][0].state == "deleted"


async def test_expired_row_found_in_the_database_is_gone_and_not_cached() -> None:
    env = Env()
    await create(env, alias="promo", expires_at=NOW + timedelta(minutes=1))
    env.cache.data.clear()
    env.clock.advance(minutes=2)
    with pytest.raises(LinkExpired):
        await env.link_service.resolve("promo")
    assert "promo" not in env.cache.data


async def test_slow_reader_cannot_resurrect_a_deleted_link() -> None:
    """ADR-005 race: a reader loads the row, the owner deletes, then the reader writes its stale copy back."""
    env = Env()
    await create(env, alias="promo")
    env.cache.data.clear()
    repo = env.links_repo
    original_get = repo.get_by_code

    async def get_then_delete(code: str) -> Link | None:
        link = await original_get(code)  # reader sees the live row ...
        repo.get_by_code = original_get  # type: ignore[method-assign]
        await env.link_service.delete(owner=OWNER, code=code)  # ... and the delete lands before it caches it
        return link

    repo.get_by_code = get_then_delete  # type: ignore[method-assign]
    assert (await env.link_service.resolve("promo")).target  # this reader still gets the pre-delete answer
    with pytest.raises(
        LinkNotFound
    ):  # but the stale value never reached the cache (SET NX loses to the tombstone)
        await env.link_service.resolve("promo")


async def test_redirect_falls_back_to_the_database_when_redis_is_down() -> None:  # AC-11
    env = Env()
    await create(env, alias="promo")
    env.cache.down = True
    assert (await env.link_service.resolve("promo")).target == "https://example.com/a"


async def test_cache_hit_survives_a_database_outage_but_a_miss_does_not() -> None:
    env = Env()
    await create(env, alias="promo")
    env.links_repo.down = True
    assert (await env.link_service.resolve("promo")).target
    env.cache.data.clear()
    with pytest.raises(StorageUnavailable):
        await env.link_service.resolve("promo")


# ----- get / delete ---------------------------------------------------------------------------------


async def test_get_is_owner_scoped_and_hides_existence() -> None:
    env = Env()
    await create(env, alias="promo")
    assert (await env.link_service.get(owner=OWNER, code="promo")).code == "promo"
    for owner, code in ((OTHER, "promo"), (OWNER, "missing1")):
        with pytest.raises(LinkNotFound):
            await env.link_service.get(owner=owner, code=code)


async def test_get_returns_expired_links_to_their_owner() -> None:
    env = Env()
    await create(env, alias="promo", expires_at=NOW + timedelta(minutes=1))
    env.clock.advance(minutes=2)
    link = await env.link_service.get(owner=OWNER, code="promo")
    assert link.status(env.clock.now()) == "expired"


async def test_non_owner_cannot_delete() -> None:
    env = Env()
    await create(env, alias="promo")
    with pytest.raises(LinkNotFound):
        await env.link_service.delete(owner=OTHER, code="promo")
    assert env.links_repo.rows["promo"].deleted_at is None
    assert env.cache.data["promo"][0].state == "active"


async def test_second_delete_is_not_found() -> None:
    env = Env()
    await create(env, alias="promo")
    await env.link_service.delete(owner=OWNER, code="promo")
    with pytest.raises(LinkNotFound):
        await env.link_service.delete(owner=OWNER, code="promo")
    with pytest.raises(LinkNotFound):
        await env.link_service.get(owner=OWNER, code="promo")


async def test_delete_that_loses_a_race_reports_not_found() -> None:
    env = Env()
    await create(env, alias="promo")

    async def refuse(**_: object) -> bool:
        return False

    env.links_repo.soft_delete = refuse  # type: ignore[method-assign,assignment]
    with pytest.raises(LinkNotFound):
        await env.link_service.delete(owner=OWNER, code="promo")
    assert env.cache.data["promo"][0].state == "active"  # no tombstone for a delete that did not happen
