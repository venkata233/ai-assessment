"""PostgreSQL adapters (SQLAlchemy Core, parameterised statements only)."""

import asyncio
import random
from collections.abc import Mapping
from datetime import datetime
from typing import Any, Literal

import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from shortener.domain.errors import CodeTaken
from shortener.domain.models import ClickEvent, Link
from shortener.infra.db import storage_errors
from shortener.infra.tables import clicks, urls
from shortener.ports import ClickTotals, CountItem, TimeBucket, TopColumn

_RETRYABLE_SQLSTATES = frozenset({"40P01", "40001"})  # deadlock_detected, serialization_failure
_MAX_BATCH_ATTEMPTS = 4
_JITTER = random.SystemRandom()  # retry back-off jitter only; nothing security-relevant depends on it


def sqlstate(exc: BaseException) -> str | None:
    orig = getattr(exc, "orig", None)
    for candidate in (orig, getattr(orig, "__cause__", None)):
        state = getattr(candidate, "sqlstate", None)
        if isinstance(state, str):
            return state
    return None


def constraint_name(exc: BaseException) -> str | None:
    orig = getattr(exc, "orig", None)
    for candidate in (orig, getattr(orig, "__cause__", None)):
        name = getattr(candidate, "constraint_name", None)
        if isinstance(name, str):
            return name
    return None


def _to_link(row: Mapping[Any, Any]) -> Link:
    return Link(
        id=row["id"],
        code=row["code"],
        target_url=row["target_url"],
        owner_key_id=row["owner_key_id"],
        is_custom_alias=row["is_custom_alias"],
        click_count=row["click_count"],
        created_at=row["created_at"],
        expires_at=row["expires_at"],
        deleted_at=row["deleted_at"],
    )


class SqlLinkRepository:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def insert(
        self,
        *,
        code: str,
        target_url: str,
        owner_key_id: str,
        is_custom_alias: bool,
        created_at: datetime,
        expires_at: datetime | None,
    ) -> Link:
        stmt = (
            sa.insert(urls)
            .values(
                code=code,
                target_url=target_url,
                owner_key_id=owner_key_id,
                is_custom_alias=is_custom_alias,
                created_at=created_at,
                expires_at=expires_at,
            )
            .returning(*urls.c)
        )
        try:
            with storage_errors():
                async with self._engine.begin() as conn:
                    row = (await conn.execute(stmt)).mappings().one()
        except IntegrityError as exc:
            if sqlstate(exc) == "23505" and constraint_name(exc) in {"uq_urls_code", None}:
                raise CodeTaken from exc
            raise
        return _to_link(row)

    async def get_by_code(self, code: str) -> Link | None:
        with storage_errors():
            async with self._engine.connect() as conn:
                row = (await conn.execute(sa.select(urls).where(urls.c.code == code))).mappings().first()
        return _to_link(row) if row is not None else None

    async def soft_delete(self, *, code: str, owner_key_id: str, deleted_at: datetime) -> bool:
        stmt = (
            sa.update(urls)
            .where(urls.c.code == code, urls.c.owner_key_id == owner_key_id, urls.c.deleted_at.is_(None))
            .values(deleted_at=deleted_at)
            .returning(urls.c.id)
        )
        with storage_errors():
            async with self._engine.begin() as conn:
                return (await conn.execute(stmt)).first() is not None


# Static SQL: counters are bumped only for rows that were actually inserted, so redelivery is safe.
_INSERT_BATCH = sa.text(
    """
    WITH ins AS (
      INSERT INTO clicks (
        event_id, url_id, clicked_at, referrer, user_agent, ua_family, country, visitor_hash
      )
      SELECT * FROM unnest(
        CAST(:event_ids AS varchar[]), CAST(:url_ids AS bigint[]), CAST(:clicked_ats AS timestamptz[]),
        CAST(:referrers AS varchar[]), CAST(:user_agents AS varchar[]), CAST(:ua_families AS varchar[]),
        CAST(:countries AS char(2)[]), CAST(:visitor_hashes AS char(16)[])
      )
      ON CONFLICT (event_id) DO NOTHING
      RETURNING url_id
    ), agg AS (
      SELECT url_id, count(*) AS n FROM ins GROUP BY url_id
    ), bump AS (
      UPDATE urls u SET click_count = u.click_count + agg.n FROM agg WHERE u.id = agg.url_id
      RETURNING u.id
    )
    SELECT (SELECT count(*) FROM ins) AS inserted
    """
)

# Taking the row locks up front, in id order, gives every worker the same lock order, so two workers
# with overlapping links cannot deadlock on the counter UPDATE. NO KEY UPDATE does not conflict with the
# KEY SHARE locks that the clicks foreign key takes.
_LOCK_URLS = sa.text(
    "SELECT id FROM urls WHERE id = ANY(CAST(:ids AS bigint[])) ORDER BY id FOR NO KEY UPDATE"
)


class SqlClickRepository:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def insert_batch(self, events: list[ClickEvent]) -> int:
        if not events:
            return 0
        params = {
            "event_ids": [e.event_id for e in events],
            "url_ids": [e.url_id for e in events],
            "clicked_ats": [e.clicked_at for e in events],
            "referrers": [e.referrer for e in events],
            "user_agents": [e.user_agent for e in events],
            "ua_families": [e.ua_family for e in events],
            "countries": [e.country for e in events],
            "visitor_hashes": [e.visitor_hash for e in events],
        }
        for attempt in range(1, _MAX_BATCH_ATTEMPTS + 1):
            try:
                with storage_errors():
                    async with self._engine.begin() as conn:
                        await conn.execute(_LOCK_URLS, {"ids": sorted({e.url_id for e in events})})
                        return int((await conn.execute(_INSERT_BATCH, params)).scalar_one())
            except DBAPIError as exc:
                # The ordered locks above should prevent deadlocks; this is the safety net. The insert
                # is idempotent, so retrying the whole batch is safe (DB_SCHEMA.md section 4).
                if sqlstate(exc) in _RETRYABLE_SQLSTATES and attempt < _MAX_BATCH_ATTEMPTS:
                    await asyncio.sleep(_JITTER.uniform(0.01, 0.05) * attempt)
                    continue
                raise
        raise AssertionError("unreachable")  # pragma: no cover

    @staticmethod
    def _window(url_id: int, start: datetime, end: datetime) -> list[sa.ColumnElement[bool]]:
        return [clicks.c.url_id == url_id, clicks.c.clicked_at >= start, clicks.c.clicked_at < end]

    async def totals(self, url_id: int, start: datetime, end: datetime) -> ClickTotals:
        stmt = sa.select(
            sa.func.count().label("total"),
            sa.func.count(sa.distinct(clicks.c.visitor_hash)).label("unique"),
        ).where(*self._window(url_id, start, end))
        with storage_errors():
            async with self._engine.connect() as conn:
                row = (await conn.execute(stmt)).one()
        return ClickTotals(total=int(row.total), unique_visitors=int(row.unique))

    async def time_series(
        self, url_id: int, start: datetime, end: datetime, granularity: Literal["hour", "day"]
    ) -> list[TimeBucket]:
        bucket = sa.func.date_trunc(
            sa.literal(granularity, sa.Text()), clicks.c.clicked_at, sa.literal("UTC", sa.Text())
        )
        stmt = (
            sa.select(bucket.label("bucket"), sa.func.count().label("n"))
            .where(*self._window(url_id, start, end))
            .group_by(bucket)
            .order_by(bucket)
        )
        with storage_errors():
            async with self._engine.connect() as conn:
                rows = (await conn.execute(stmt)).all()
        return [TimeBucket(bucket_start=r.bucket, clicks=int(r.n)) for r in rows]

    async def top_values(
        self, url_id: int, column: TopColumn, start: datetime, end: datetime, limit: int
    ) -> list[CountItem]:
        col = clicks.c[column]
        stmt = (
            sa.select(col.label("value"), sa.func.count().label("n"))
            .where(*self._window(url_id, start, end))
            .group_by(col)
            .order_by(sa.func.count().desc(), col)
            .limit(limit)
        )
        with storage_errors():
            async with self._engine.connect() as conn:
                rows = (await conn.execute(stmt)).all()
        return [CountItem(value=r.value, clicks=int(r.n)) for r in rows]

    async def recent(
        self,
        url_id: int,
        start: datetime,
        end: datetime,
        cursor: tuple[datetime, str] | None,
        limit: int,
    ) -> list[ClickEvent]:
        conditions = self._window(url_id, start, end)
        if cursor is not None:
            conditions.append(sa.tuple_(clicks.c.clicked_at, clicks.c.event_id) < sa.tuple_(*cursor))
        stmt = (
            sa.select(clicks)
            .where(*conditions)
            .order_by(clicks.c.clicked_at.desc(), clicks.c.event_id.desc())
            .limit(limit)
        )
        with storage_errors():
            async with self._engine.connect() as conn:
                rows = (await conn.execute(stmt)).mappings().all()
        return [
            ClickEvent(
                event_id=r["event_id"],
                url_id=r["url_id"],
                clicked_at=r["clicked_at"],
                referrer=r["referrer"],
                user_agent=r["user_agent"],
                ua_family=r["ua_family"],
                country=r["country"],
                visitor_hash=r["visitor_hash"],
            )
            for r in rows
        ]
