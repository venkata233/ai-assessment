"""PostgreSQL adapters against a real database: constraints, analytics SQL, plans, outage mapping."""

import asyncio
import random
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine

from shortener.domain.errors import CodeTaken, StorageUnavailable
from shortener.domain.models import ClickEvent
from shortener.infra.db import create_db_engine
from shortener.infra.repositories import SqlClickRepository, SqlLinkRepository

pytestmark = pytest.mark.integration

T0 = datetime(2026, 11, 1, 10, 30, tzinfo=UTC)


async def add_link(repo: SqlLinkRepository, code: str = "promo", owner: str = "demo", **kwargs: object):  # type: ignore[no-untyped-def]
    params: dict[str, object] = {
        "code": code,
        "target_url": "https://example.com/",
        "owner_key_id": owner,
        "is_custom_alias": True,
        "created_at": datetime.now(UTC),
        "expires_at": None,
    }
    params.update(kwargs)
    return await repo.insert(**params)  # type: ignore[arg-type]


def event(n: int, url_id: int, at: datetime, **kwargs: object) -> ClickEvent:
    values: dict[str, object] = {
        "event_id": f"{int(at.timestamp() * 1000)}-{n}",
        "url_id": url_id,
        "clicked_at": at,
        "referrer": None,
        "user_agent": "UA",
        "ua_family": "chrome",
        "country": None,
        "visitor_hash": None,
    }
    values.update(kwargs)
    return ClickEvent(**values)  # type: ignore[arg-type]


async def test_link_round_trip_and_case_sensitive_codes(engine: AsyncEngine) -> None:
    repo = SqlLinkRepository(engine)
    upper = await add_link(repo, "Promo")
    lower = await add_link(repo, "promo")
    assert upper.id != lower.id
    fetched = await repo.get_by_code("Promo")
    assert fetched == upper and fetched.created_at.tzinfo is not None
    assert await repo.get_by_code("PROMO") is None


async def test_duplicate_code_maps_to_code_taken_even_after_soft_delete(engine: AsyncEngine) -> None:
    repo = SqlLinkRepository(engine)
    await add_link(repo, "promo")
    with pytest.raises(CodeTaken):
        await add_link(repo, "promo", owner="other")
    assert await repo.soft_delete(code="promo", owner_key_id="demo", deleted_at=datetime.now(UTC))
    with pytest.raises(CodeTaken):  # the tombstone: the unique constraint outlives the soft delete
        await add_link(repo, "promo")


async def test_soft_delete_is_owner_scoped_and_single_shot(engine: AsyncEngine) -> None:
    repo = SqlLinkRepository(engine)
    await add_link(repo, "promo")
    now = datetime.now(UTC)
    assert not await repo.soft_delete(code="promo", owner_key_id="other", deleted_at=now)
    assert not await repo.soft_delete(code="missing", owner_key_id="demo", deleted_at=now)
    assert await repo.soft_delete(code="promo", owner_key_id="demo", deleted_at=now)
    assert not await repo.soft_delete(code="promo", owner_key_id="demo", deleted_at=now)
    deleted = await repo.get_by_code("promo")
    assert deleted is not None and deleted.deleted_at is not None


async def test_database_check_constraints_back_up_the_application(engine: AsyncEngine) -> None:
    repo = SqlLinkRepository(engine)
    from sqlalchemy.exc import IntegrityError

    for kwargs in (
        {"code": "ab"},
        {"target_url": "javascript:alert(1)"},
        {"expires_at": datetime(2000, 1, 1, tzinfo=UTC)},
    ):
        with pytest.raises(IntegrityError):
            await add_link(repo, **{"code": "okcode", **kwargs})  # type: ignore[arg-type]


async def test_database_errors_do_not_carry_bound_values(engine: AsyncEngine) -> None:
    """Exception text (which can end up in logs) must not contain parameters such as target URLs."""
    from sqlalchemy.exc import IntegrityError

    repo = SqlLinkRepository(engine)
    with pytest.raises(IntegrityError) as info:
        await add_link(
            repo, "ab", target_url="https://example.com/SECRET-PATH-VALUE"
        )  # code too short: CHECK fails
    assert "SECRET-PATH-VALUE" not in str(info.value)
    assert (
        "hidden" in str(info.value).lower()
    )  # SQLAlchemy: "[SQL parameters hidden due to hide_parameters=True]"


async def test_unreachable_database_maps_to_storage_unavailable(settings) -> None:  # type: ignore[no-untyped-def]
    from conftest import make_integration_settings

    broken = create_db_engine(
        make_integration_settings("postgresql+asyncpg://u:p@127.0.0.1:6391/x", "redis://127.0.0.1:6379/15")
    )
    try:
        with pytest.raises(StorageUnavailable):
            await SqlLinkRepository(broken).get_by_code("promo")
        with pytest.raises(StorageUnavailable):
            await SqlClickRepository(broken).insert_batch([event(0, 1, T0)])
    finally:
        await broken.dispose()


async def test_batch_insert_is_idempotent_and_counts_only_new_rows(engine: AsyncEngine) -> None:
    links, clicks = SqlLinkRepository(engine), SqlClickRepository(engine)
    link = await add_link(links)
    batch = [event(0, link.id, T0), event(1, link.id, T0 + timedelta(seconds=1))]
    assert await clicks.insert_batch(batch) == 2
    assert await clicks.insert_batch(batch) == 0  # redelivery
    assert (
        await clicks.insert_batch([*batch, event(2, link.id, T0 + timedelta(seconds=2))]) == 1
    )  # one new event
    assert (await links.get_by_code("promo")).click_count == 3  # type: ignore[union-attr]
    assert await clicks.insert_batch([]) == 0


async def test_batch_insert_with_unknown_link_fails_atomically(engine: AsyncEngine) -> None:
    from sqlalchemy.exc import IntegrityError

    links, clicks = SqlLinkRepository(engine), SqlClickRepository(engine)
    link = await add_link(links)
    with pytest.raises(IntegrityError):
        await clicks.insert_batch([event(0, link.id, T0), event(1, 999_999, T0)])
    async with engine.connect() as conn:
        assert (
            await conn.execute(sa.text("SELECT count(*) FROM clicks"))
        ).scalar_one() == 0  # nothing half-applied
    assert (await links.get_by_code("promo")).click_count == 0  # type: ignore[union-attr]


async def test_database_rejects_a_referrer_that_is_not_an_origin(engine: AsyncEngine) -> None:
    from sqlalchemy.exc import IntegrityError

    links, clicks = SqlLinkRepository(engine), SqlClickRepository(engine)
    link = await add_link(links)
    with pytest.raises(IntegrityError):
        await clicks.insert_batch([event(0, link.id, T0, referrer="https://example.com/path?token=1")])


async def test_overlapping_concurrent_batches_neither_deadlock_nor_miscount(engine: AsyncEngine) -> None:
    links, clicks = SqlLinkRepository(engine), SqlClickRepository(engine)
    ids = [(await add_link(links, f"link{n:03d}")).id for n in range(6)]
    rng = random.Random(7)

    def make_batch(worker: int) -> list[ClickEvent]:
        order = ids[:]
        rng.shuffle(order)  # opposite lock orders are the classic deadlock recipe
        return [
            event(worker * 100 + i, url_id, T0 + timedelta(milliseconds=worker * 100 + i))
            for i, url_id in enumerate(order * 3)
        ]

    batches = [make_batch(w) for w in range(12)]
    results = await asyncio.gather(*[clicks.insert_batch(b) for b in batches])
    assert sum(results) == sum(len(b) for b in batches)
    async with engine.connect() as conn:
        counters = (await conn.execute(sa.text("SELECT click_count FROM urls ORDER BY id"))).scalars().all()
        stored = (await conn.execute(sa.text("SELECT count(*) FROM clicks"))).scalar_one()
    assert sum(counters) == stored == sum(len(b) for b in batches)
    assert all(c == len(batches) * 3 for c in counters)


async def test_deadlock_is_retried(engine: AsyncEngine) -> None:
    """The safety net: a 40P01 from PostgreSQL makes the repository retry the whole (idempotent) batch."""
    from sqlalchemy.exc import DBAPIError

    links, clicks = SqlLinkRepository(engine), SqlClickRepository(engine)
    link = await add_link(links)

    class FakeOrig(Exception):
        sqlstate = "40P01"

    real_begin, failures = engine.begin, {"left": 2}

    class FlakyBegin:
        def __init__(self) -> None:
            self.inner = real_begin()

        async def __aenter__(self):  # type: ignore[no-untyped-def]
            conn = await self.inner.__aenter__()
            if failures["left"] > 0:
                failures["left"] -= 1
                await self.inner.__aexit__(None, None, None)
                raise DBAPIError("stmt", {}, FakeOrig())
            return conn

        async def __aexit__(self, *exc):  # type: ignore[no-untyped-def]
            return await self.inner.__aexit__(*exc)

    class FlakyEngine:
        """Stands in for the engine: the first two transactions die with a deadlock."""

        begin = FlakyBegin

    flaky_clicks = SqlClickRepository(FlakyEngine())  # type: ignore[arg-type]
    assert await flaky_clicks.insert_batch([event(0, link.id, T0)]) == 1
    assert failures["left"] == 0
    assert (await links.get_by_code("promo")).click_count == 1  # type: ignore[union-attr]
    assert await clicks.insert_batch([]) == 0


# ----- analytics SQL against hand-computed fixtures -------------------------------------------------------


async def seed_analytics(engine: AsyncEngine):  # type: ignore[no-untyped-def]
    links, clicks = SqlLinkRepository(engine), SqlClickRepository(engine)
    link = await add_link(links)
    other = await add_link(links, "other1")
    base = datetime(2026, 11, 1, 23, 30, tzinfo=UTC)  # chosen so day/hour bucket boundaries matter
    events = [
        event(
            0,
            link.id,
            base,
            referrer="https://a.example",
            ua_family="chrome",
            country="DE",
            visitor_hash="a" * 16,
        ),
        event(
            1,
            link.id,
            base + timedelta(minutes=20),
            referrer="https://a.example",
            ua_family="chrome",
            country="DE",
            visitor_hash="a" * 16,
        ),
        event(
            2,
            link.id,
            base + timedelta(minutes=45),
            referrer="https://b.example",
            ua_family="firefox",
            country="US",
            visitor_hash="b" * 16,
        ),  # next day (UTC)
        event(
            3,
            link.id,
            base + timedelta(hours=2),
            referrer=None,
            ua_family="curl",
            country=None,
            visitor_hash=None,
        ),
        event(4, link.id, base + timedelta(days=3), ua_family="chrome"),
        event(5, other.id, base, referrer="https://other.example"),
    ]
    await clicks.insert_batch(events)
    return link, clicks, base


async def test_totals_series_and_top_values_match_hand_computed_results(engine: AsyncEngine) -> None:
    link, clicks, base = await seed_analytics(engine)
    start, end = (
        base - timedelta(hours=1),
        base + timedelta(days=1),
    )  # excludes the +3 days click and the other link
    totals = await clicks.totals(link.id, start, end)
    assert (totals.total, totals.unique_visitors) == (4, 2)

    daily = await clicks.time_series(link.id, start, end, "day")
    assert [(b.bucket_start, b.clicks) for b in daily] == [
        (datetime(2026, 11, 1, tzinfo=UTC), 2),
        (datetime(2026, 11, 2, tzinfo=UTC), 2),
    ]
    hourly = await clicks.time_series(link.id, start, end, "hour")
    assert [(b.bucket_start.hour, b.clicks) for b in hourly] == [(23, 2), (0, 1), (1, 1)]
    assert all(b.bucket_start.utcoffset() == timedelta(0) for b in hourly)

    referrers = await clicks.top_values(link.id, "referrer", start, end, 10)
    assert [(i.value, i.clicks) for i in referrers] == [
        ("https://a.example", 2),
        ("https://b.example", 1),
        (None, 1),
    ]
    agents = await clicks.top_values(link.id, "ua_family", start, end, 10)
    assert [(i.value, i.clicks) for i in agents] == [("chrome", 2), ("curl", 1), ("firefox", 1)]
    countries = await clicks.top_values(link.id, "country", start, end, 1)
    assert [(i.value, i.clicks) for i in countries] == [("DE", 2)]  # limit applies


async def test_window_is_half_open(engine: AsyncEngine) -> None:
    link, clicks, base = await seed_analytics(engine)
    assert (
        await clicks.totals(link.id, base, base + timedelta(minutes=20))
    ).total == 1  # [from, to): excludes +20 min
    assert (await clicks.totals(link.id, base, base + timedelta(minutes=20, microseconds=1))).total == 2
    assert (await clicks.totals(link.id, base + timedelta(days=9), base + timedelta(days=10))).total == 0


async def test_keyset_pagination_is_complete_ordered_and_disjoint(engine: AsyncEngine) -> None:
    link, clicks, base = await seed_analytics(engine)
    start, end = base - timedelta(days=1), base + timedelta(days=10)
    pages, cursor = [], None
    while True:
        page = await clicks.recent(link.id, start, end, cursor, 2)
        if not page:
            break
        pages.append(page)
        cursor = (page[-1].clicked_at, page[-1].event_id)
    flat = [e for p in pages for e in p]
    assert [len(p) for p in pages] == [2, 2, 1]
    assert len({e.event_id for e in flat}) == 5
    assert [e.clicked_at for e in flat] == sorted((e.clicked_at for e in flat), reverse=True)
    assert all(e.url_id == link.id for e in flat)
    assert flat[0].referrer is None and flat[0].ua_family == "chrome"


async def test_hot_paths_use_indexes(engine: AsyncEngine) -> None:
    """Plans on tiny tables with seqscan disabled prove the indexes are *usable*, not fast at scale."""
    link, _, base = await seed_analytics(engine)
    async with engine.connect() as conn:
        await conn.execute(sa.text("SET enable_seqscan = off"))
        lookup = "\n".join(
            r[0] for r in await conn.execute(sa.text("EXPLAIN SELECT * FROM urls WHERE code = 'promo'"))
        )
        window = "\n".join(
            r[0]
            for r in await conn.execute(
                sa.text(
                    "EXPLAIN SELECT * FROM clicks WHERE url_id = :u AND clicked_at >= :a AND clicked_at < :b ORDER BY clicked_at DESC, event_id DESC LIMIT 20"
                ),
                {"u": link.id, "a": base - timedelta(days=1), "b": base + timedelta(days=10)},
            )
        )
    assert "uq_urls_code" in lookup
    assert "ix_clicks_url_id_clicked_at" in window
    assert "Sort" not in window  # the index already delivers the requested order
