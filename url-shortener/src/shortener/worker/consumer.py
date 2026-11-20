"""Click stream consumer (ADR-004): at-least-once delivery + idempotent insert = effectively-once storage."""

import asyncio
import contextlib
import re
from datetime import UTC, datetime
from typing import Any

import structlog
from redis.asyncio import Redis
from redis.exceptions import RedisError, ResponseError
from sqlalchemy.exc import DBAPIError

from shortener.domain.clicks import country_from_header, referrer_origin, truncate_user_agent
from shortener.domain.errors import StorageUnavailable
from shortener.domain.models import ClickEvent
from shortener.infra.redis_adapters import CLICK_STREAM
from shortener.infra.repositories import sqlstate
from shortener.ports import ClickRepository

GROUP = "click-writers"
DEAD_STREAM = "clicks:dead"
_VISITOR_RE = re.compile(r"^[0-9a-f]{16}$")
_NON_RETRYABLE_SQLSTATES = frozenset(
    {"23503", "23514", "22001", "22P02", "23502"}
)  # FK, CHECK, length, type, NOT NULL
_log = structlog.get_logger()

Message = tuple[str, dict[str, str]]


class PoisonMessage(Exception):
    """A stream entry that can never be stored (malformed fields)."""


def parse_message(message_id: str, fields: dict[str, str]) -> ClickEvent:
    try:
        url_id = int(fields["url_id"])
        ts_ms = int(fields["ts_ms"])
    except (KeyError, ValueError) as exc:
        raise PoisonMessage("missing or invalid url_id/ts_ms") from exc
    if url_id <= 0 or not 0 < ts_ms < 4_102_444_800_000 or len(message_id) > 40:
        raise PoisonMessage("field out of range")
    visitor = fields.get("visitor_hash") or None
    return ClickEvent(
        event_id=message_id,
        url_id=url_id,
        clicked_at=datetime.fromtimestamp(ts_ms / 1000, UTC),
        # Re-sanitised here so a buggy or hostile producer cannot violate the DB CHECK constraints.
        referrer=referrer_origin(fields.get("referrer_origin") or None),
        user_agent=truncate_user_agent(fields.get("ua") or None),
        ua_family=((fields.get("ua_family") or "other")[:32]) or "other",
        country=country_from_header(fields.get("country") or None),
        visitor_hash=visitor if visitor and _VISITOR_RE.match(visitor) else None,
    )


class ClickConsumer:
    def __init__(
        self,
        redis: Redis,
        clicks: ClickRepository,
        *,
        consumer_name: str,
        stream: str = CLICK_STREAM,
        group: str = GROUP,
        dead_stream: str = DEAD_STREAM,
        batch_size: int = 500,
        block_ms: int = 1000,
        claim_idle_ms: int = 60_000,
        max_deliveries: int = 5,
    ) -> None:
        self._redis = redis
        self._clicks = clicks
        self._consumer = consumer_name
        self._stream = stream
        self._group = group
        self._dead = dead_stream
        self._batch = batch_size
        self._block_ms = block_ms
        self._claim_idle_ms = claim_idle_ms
        self._max_deliveries = max_deliveries
        self._claim_cursor = "0-0"

    async def setup(self) -> None:
        try:
            await self._redis.xgroup_create(self._stream, self._group, id="0", mkstream=True)
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    async def run(self, stop: asyncio.Event) -> None:
        await self.setup()
        _log.info("worker_started", consumer=self._consumer)
        backoff = 0.5
        while not stop.is_set():
            try:
                await self.run_once()
                backoff = 0.5
            except ResponseError as exc:
                if "NOGROUP" in str(exc):
                    await self.setup()
                    continue
                backoff = await self._pause(stop, backoff, exc)
            except (RedisError, OSError, StorageUnavailable) as exc:
                backoff = await self._pause(stop, backoff, exc)
        _log.info("worker_stopped", consumer=self._consumer)

    @staticmethod
    async def _pause(stop: asyncio.Event, backoff: float, exc: Exception) -> float:
        _log.warning("worker_dependency_error", error=type(exc).__name__, retry_in=backoff)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), backoff)
        return min(backoff * 2, 5.0)

    async def run_once(self) -> int:
        """One iteration: reclaim stale entries, then read new ones. Returns entries handled."""
        handled = await self._process(await self._reclaim_stale())
        handled += await self._process(await self._read_new())
        return handled

    async def _read_new(self) -> list[tuple[Message, int]]:
        response: Any = await self._redis.xreadgroup(
            self._group, self._consumer, {self._stream: ">"}, count=self._batch, block=self._block_ms
        )
        entries: list[tuple[Message, int]] = []
        for _, stream_entries in response or []:
            entries.extend(((str(mid), fields), 1) for mid, fields in stream_entries)
        return entries

    async def _reclaim_stale(self) -> list[tuple[Message, int]]:
        result: Any = await self._redis.xautoclaim(
            self._stream,
            self._group,
            self._consumer,
            min_idle_time=self._claim_idle_ms,
            start_id=self._claim_cursor,
            count=self._batch,
        )
        self._claim_cursor = str(result[0])  # "0-0" once a full scan of the pending list completes
        claimed: list[tuple[Message, int]] = []
        for mid, fields in result[1]:
            if fields is None:  # entry was trimmed away while pending
                await self._redis.xack(self._stream, self._group, mid)
                continue
            info = await self._redis.xpending_range(self._stream, self._group, min=mid, max=mid, count=1)
            deliveries = int(info[0]["times_delivered"]) if info else 1
            claimed.append(((mid, fields), deliveries))
        return claimed

    async def _process(self, entries: list[tuple[Message, int]]) -> int:
        if not entries:
            return 0
        valid: list[tuple[Message, ClickEvent]] = []
        for (mid, fields), deliveries in entries:
            if deliveries > self._max_deliveries:
                await self._dead_letter(mid, fields, f"delivered {deliveries} times")
                continue
            try:
                valid.append(((mid, fields), parse_message(mid, fields)))
            except PoisonMessage as exc:
                await self._dead_letter(mid, fields, str(exc))
        if not valid:
            return len(entries)
        events = [event for _, event in valid]
        try:
            inserted = await self._clicks.insert_batch(events)
        except (StorageUnavailable, DBAPIError) as exc:
            if isinstance(exc, DBAPIError) and sqlstate(exc) in _NON_RETRYABLE_SQLSTATES:
                await self._isolate_poison(valid)
            else:
                # Transient (database down, deadlock budget exhausted): do not ACK; the entries stay
                # pending and are reclaimed after the idle timeout.
                _log.warning("click_batch_failed", error=type(exc).__name__, size=len(valid))
            return len(entries)
        await self._redis.xack(self._stream, self._group, *[m[0] for m, _ in valid])
        lag = (datetime.now(UTC) - max(e.clicked_at for e in events)).total_seconds()
        _log.info(
            "click_batch_stored", received=len(valid), inserted=inserted, ingest_lag_seconds=round(lag, 3)
        )
        return len(entries)

    async def _isolate_poison(self, valid: list[tuple[Message, ClickEvent]]) -> None:
        """A permanent DB error rejected the batch: retry events one by one and dead-letter the culprits."""
        for (mid, fields), event in valid:
            try:
                await self._clicks.insert_batch([event])
            except DBAPIError as exc:
                if sqlstate(exc) in _NON_RETRYABLE_SQLSTATES:
                    await self._dead_letter(
                        mid, fields, f"database rejected event (sqlstate {sqlstate(exc)})"
                    )
                continue
            except StorageUnavailable:
                continue
            await self._redis.xack(self._stream, self._group, mid)

    async def _dead_letter(self, message_id: str, fields: dict[str, str], reason: str) -> None:
        payload: dict[Any, Any] = {**fields, "source_id": message_id, "reason": reason}
        await self._redis.xadd(self._dead, payload, maxlen=100_000, approximate=True)
        await self._redis.xack(self._stream, self._group, message_id)
        _log.warning("click_dead_lettered", source_id=message_id, reason=reason)
