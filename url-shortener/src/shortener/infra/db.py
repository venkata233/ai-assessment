"""Connection factories and health probes."""

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager

import sqlalchemy as sa
from redis.asyncio import Redis
from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff
from sqlalchemy.exc import InterfaceError, OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from shortener.config import Settings
from shortener.domain.errors import StorageUnavailable


def create_db_engine(settings: Settings) -> AsyncEngine:
    return create_async_engine(
        settings.database_url.get_secret_value(),
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=3,
        pool_pre_ping=True,
        connect_args={
            "timeout": 5,
            "command_timeout": 10,
            "server_settings": {"application_name": "shortener", "statement_timeout": "8000"},
        },
    )


def create_redis(settings: Settings, *, blocking: bool = False) -> Redis:
    """Redis client for the app (short timeouts, no retries: Redis is an optimisation, not a hard dependency).

    The worker passes `blocking=True` because XREADGROUP BLOCK legitimately waits longer than the
    app-side socket timeout.
    """
    return Redis.from_url(
        settings.redis_url.get_secret_value(),
        decode_responses=True,
        socket_timeout=10 if blocking else settings.redis_socket_timeout_seconds,
        socket_connect_timeout=5 if blocking else settings.redis_socket_timeout_seconds,
        retry=Retry(NoBackoff(), 0),
        health_check_interval=30,
    )


@contextmanager
def storage_errors() -> Iterator[None]:
    """Translate connectivity failures into the domain's StorageUnavailable."""
    try:
        yield
    except (OperationalError, InterfaceError, PoolTimeoutError, OSError, TimeoutError) as exc:
        raise StorageUnavailable from exc


async def database_is_up(engine: AsyncEngine, limit_seconds: float = 2.0) -> bool:
    async def _probe() -> None:
        async with engine.connect() as conn:
            await conn.execute(sa.text("SELECT 1"))

    try:
        await asyncio.wait_for(_probe(), limit_seconds)
    except Exception:
        return False
    return True


async def redis_is_up(redis: Redis, limit_seconds: float = 1.0) -> bool:
    try:
        await asyncio.wait_for(redis.ping(), limit_seconds)
    except Exception:
        return False
    return True
