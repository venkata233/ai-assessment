"""Click consumer (ADR-004) against real Redis Streams and PostgreSQL."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from conftest import Stack
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from shortener.domain.errors import StorageUnavailable
from shortener.domain.models import ClickEvent
from shortener.infra.repositories import SqlClickRepository, SqlLinkRepository
from shortener.worker.consumer import DEAD_STREAM, GROUP, ClickConsumer

pytestmark = pytest.mark.integration


def fields(url_id: int, **kwargs: str) -> dict[str, str]:
    base = {
        "url_id": str(url_id),
        "ts_ms": str(int(datetime.now(UTC).timestamp() * 1000)),
        "referrer_origin": "",
        "ua": "UA",
        "ua_family": "chrome",
        "country": "",
        "visitor_hash": "",
    }
    base.update(kwargs)
    return base


async def make_link(engine: AsyncEngine, code: str = "promo") -> int:
    link = await SqlLinkRepository(engine).insert(
        code=code,
        target_url="https://example.com/",
        owner_key_id="demo",
        is_custom_alias=True,
        created_at=datetime.now(UTC),
        expires_at=None,
    )
    return link.id


async def stored(engine: AsyncEngine) -> tuple[int, int]:
    async with engine.connect() as conn:
        clicks = (await conn.execute(sa.text("SELECT count(*) FROM clicks"))).scalar_one()
        counter = (await conn.execute(sa.text("SELECT coalesce(sum(click_count), 0) FROM urls"))).scalar_one()
    return int(clicks), int(counter)


async def pending(redis: Redis) -> int:
    info = await redis.xpending("clicks", GROUP)
    return int(info["pending"])


def consumer(stack: Stack, name: str = "w1", repo: object = None, **kwargs: object) -> ClickConsumer:
    params: dict[str, object] = {"block_ms": 50, "claim_idle_ms": 0, "max_deliveries": 5}
    params.update(kwargs)
    return ClickConsumer(stack.redis, repo or SqlClickRepository(stack.engine), consumer_name=name, **params)  # type: ignore[arg-type]


async def test_happy_path_stores_acks_and_counts(stack: Stack) -> None:
    url_id = await make_link(stack.engine)
    worker = consumer(stack)
    await worker.setup()
    for _ in range(7):
        await stack.redis.xadd("clicks", fields(url_id, referrer_origin="https://a.example", country="DE"))
    assert await worker.run_once() == 7
    assert await stored(stack.engine) == (7, 7)
    assert await pending(stack.redis) == 0
    assert await worker.run_once() == 0  # nothing left


async def test_batches_larger_than_the_batch_size_drain_over_several_iterations(stack: Stack) -> None:
    url_id = await make_link(stack.engine)
    worker = consumer(stack, batch_size=3)
    await worker.setup()
    for _ in range(8):
        await stack.redis.xadd("clicks", fields(url_id))
    handled = 0
    while (n := await worker.run_once()) > 0:
        handled += n
    assert handled == 8 and await stored(stack.engine) == (8, 8)


async def test_crash_before_ack_is_redelivered_and_counted_once(stack: Stack) -> None:
    url_id = await make_link(stack.engine)
    crashed = consumer(stack, "crashed")
    await crashed.setup()
    for _ in range(3):
        await stack.redis.xadd("clicks", fields(url_id))
    # The first worker reads the entries and dies before processing or acknowledging anything.
    await stack.redis.xreadgroup(GROUP, "crashed", {"clicks": ">"}, count=10)
    assert await pending(stack.redis) == 3 and await stored(stack.engine) == (0, 0)
    assert await consumer(stack, "survivor").run_once() == 3  # XAUTOCLAIM takes the idle entries over
    assert await stored(stack.engine) == (3, 3)
    assert await pending(stack.redis) == 0


async def test_commit_then_crash_before_ack_does_not_double_count(stack: Stack) -> None:
    url_id = await make_link(stack.engine)
    worker = consumer(stack)
    await worker.setup()
    ids = [await stack.redis.xadd("clicks", fields(url_id)) for _ in range(2)]
    entries = await stack.redis.xreadgroup(GROUP, "dead-worker", {"clicks": ">"}, count=10)
    # The dying worker committed the batch to PostgreSQL but never sent XACK.
    committed = [
        ClickEvent(str(mid), url_id, datetime.now(UTC), None, "UA", "chrome", None, None)
        for mid, _ in entries[0][1]
    ]
    assert await SqlClickRepository(stack.engine).insert_batch(committed) == 2
    assert await worker.run_once() == 2  # redelivery
    assert await stored(stack.engine) == (2, 2)  # still 2: the primary key made it idempotent
    assert await pending(stack.redis) == 0
    assert len(ids) == 2


async def test_poison_entries_are_dead_lettered_without_blocking_good_ones(stack: Stack) -> None:
    url_id = await make_link(stack.engine)
    worker = consumer(stack)
    await worker.setup()
    await stack.redis.xadd("clicks", fields(url_id))
    await stack.redis.xadd("clicks", {"garbage": "yes"})  # malformed fields
    await stack.redis.xadd("clicks", fields(url_id, ts_ms="not-a-number"))
    await stack.redis.xadd("clicks", fields(999_999))  # references a link that does not exist (FK violation)
    await stack.redis.xadd("clicks", fields(url_id))
    assert await worker.run_once() == 5
    assert await stored(stack.engine) == (2, 2)  # both good events were stored
    assert await pending(stack.redis) == 0  # nothing stuck
    dead = await stack.redis.xrange(DEAD_STREAM)
    assert len(dead) == 3
    reasons = sorted(d[1]["reason"] for d in dead)
    assert any("url_id/ts_ms" in r for r in reasons) and any("database rejected" in r for r in reasons)
    assert all("source_id" in d[1] for d in dead)


async def test_hostile_referrer_is_sanitised_not_poisonous(stack: Stack) -> None:
    url_id = await make_link(stack.engine)
    worker = consumer(stack)
    await worker.setup()
    await stack.redis.xadd(
        "clicks", fields(url_id, referrer_origin="https://evil.example/steal?token=1", country="zz9")
    )
    assert await worker.run_once() == 1
    async with stack.engine.connect() as conn:
        row = (await conn.execute(sa.text("SELECT referrer, country FROM clicks"))).one()
    assert tuple(row) == ("https://evil.example", None)
    assert await stack.redis.xlen(DEAD_STREAM) == 0


class FlakyRepo:
    def __init__(self, inner: SqlClickRepository, failures: int) -> None:
        self.inner, self.failures, self.calls = inner, failures, 0

    async def insert_batch(self, events: list[ClickEvent]) -> int:
        self.calls += 1
        if self.failures > 0:
            self.failures -= 1
            raise StorageUnavailable
        return await self.inner.insert_batch(events)


async def test_database_outage_keeps_entries_pending_then_recovers(stack: Stack) -> None:
    url_id = await make_link(stack.engine)
    repo = FlakyRepo(SqlClickRepository(stack.engine), failures=2)
    worker = consumer(stack, repo=repo)
    await worker.setup()
    for _ in range(4):
        await stack.redis.xadd("clicks", fields(url_id))
    await worker.run_once()
    assert await pending(stack.redis) == 4 and await stored(stack.engine) == (0, 0)  # not acknowledged
    await worker.run_once()  # second failure: still safe
    assert await pending(stack.redis) == 4
    await worker.run_once()  # database is back: reclaimed and stored
    assert await stored(stack.engine) == (4, 4) and await pending(stack.redis) == 0
    assert await stack.redis.xlen(DEAD_STREAM) == 0


async def test_entries_delivered_too_many_times_move_to_the_dead_stream(stack: Stack) -> None:
    url_id = await make_link(stack.engine)
    repo = FlakyRepo(SqlClickRepository(stack.engine), failures=10_000)
    worker = consumer(stack, repo=repo, max_deliveries=3)
    await worker.setup()
    await stack.redis.xadd("clicks", fields(url_id))
    for _ in range(6):
        await worker.run_once()
    assert await pending(stack.redis) == 0
    dead = await stack.redis.xrange(DEAD_STREAM)
    assert len(dead) == 1 and "delivered" in dead[0][1]["reason"]
    assert await stored(stack.engine) == (0, 0)  # preserved in the dead stream, not silently lost


async def test_stale_entries_are_not_stolen_from_a_live_worker(stack: Stack) -> None:
    url_id = await make_link(stack.engine)
    slow = consumer(stack, "slow", claim_idle_ms=60_000)
    await slow.setup()
    await stack.redis.xadd("clicks", fields(url_id))
    await stack.redis.xreadgroup(GROUP, "busy", {"clicks": ">"}, count=10)
    assert await slow.run_once() == 0  # the entry is pending but not idle for 60 s yet
    assert await stored(stack.engine) == (0, 0)


async def test_two_workers_share_the_stream_without_duplicates(stack: Stack) -> None:
    url_id = await make_link(stack.engine)
    # A realistic idle timeout: with 0 ms a worker would steal the other's in-flight entries. That is still
    # harmless (inserts are idempotent: the inserted=0 batches in the log prove it) but it is not what we test here.
    a = consumer(stack, "a", batch_size=10, claim_idle_ms=60_000)
    b = consumer(stack, "b", batch_size=10, claim_idle_ms=60_000)
    await a.setup()
    for _ in range(60):
        await stack.redis.xadd("clicks", fields(url_id))

    async def drain(worker: ClickConsumer) -> int:
        total = 0
        while (n := await worker.run_once()) > 0:
            total += n
        return total

    handled = await asyncio.gather(drain(a), drain(b))
    assert sum(handled) == 60
    assert await stored(stack.engine) == (60, 60)


async def test_run_loop_stops_gracefully_and_survives_redis_hiccups(stack: Stack) -> None:
    url_id = await make_link(stack.engine)
    worker = consumer(stack)
    stop = asyncio.Event()
    task = asyncio.create_task(worker.run(stop))
    await asyncio.sleep(0.2)
    await stack.redis.xadd("clicks", fields(url_id))
    for _ in range(50):
        if await stored(stack.engine) == (1, 1):
            break
        await asyncio.sleep(0.1)
    assert await stored(stack.engine) == (1, 1)
    await stack.redis.delete("clicks")  # the stream (and group) vanish: NOGROUP must self-heal
    await asyncio.sleep(0.3)
    await stack.redis.xadd("clicks", fields(url_id))
    for _ in range(50):
        if (await stored(stack.engine))[0] == 2:
            break
        await asyncio.sleep(0.1)
    stop.set()
    await asyncio.wait_for(task, timeout=5)  # shuts down promptly once asked
    assert (await stored(stack.engine))[0] == 2


async def test_ingest_lag_uses_event_time_not_ingest_time(stack: Stack) -> None:
    url_id = await make_link(stack.engine)
    worker = consumer(stack)
    await worker.setup()
    old = int((datetime.now(UTC) - timedelta(hours=3)).timestamp() * 1000)
    await stack.redis.xadd("clicks", fields(url_id, ts_ms=str(old)))
    await worker.run_once()
    async with stack.engine.connect() as conn:
        clicked, ingested = (await conn.execute(sa.text("SELECT clicked_at, ingested_at FROM clicks"))).one()
    assert ingested - clicked > timedelta(hours=2, minutes=59)
