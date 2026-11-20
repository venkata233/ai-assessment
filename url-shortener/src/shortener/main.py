"""Composition root: the only module that knows the concrete classes."""

import asyncio
import os
import signal
import socket
import uuid

from fastapi import FastAPI
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from shortener.api.app import create_app
from shortener.api.container import Container
from shortener.config import Settings
from shortener.domain.codes import RandomBase62Generator
from shortener.domain.url_validator import own_origins_from_base_url
from shortener.infra.circuit_breaker import CircuitBreaker
from shortener.infra.db import create_db_engine, create_redis, database_is_up, redis_is_up
from shortener.infra.redis_adapters import (
    CLICK_STREAM,
    RedisClickPublisher,
    RedisLinkCache,
    RedisRateLimitStore,
)
from shortener.infra.repositories import SqlClickRepository, SqlLinkRepository
from shortener.infra.system import SystemClock, SystemDnsResolver
from shortener.logging_config import configure_logging
from shortener.metrics import Metrics
from shortener.services.analytics_service import AnalyticsService
from shortener.services.auth import ApiKeyAuthenticator
from shortener.services.link_service import LinkService
from shortener.services.rate_limiter import RateLimiter
from shortener.worker.consumer import GROUP, ClickConsumer


def build_container(settings: Settings, engine: AsyncEngine, redis: Redis) -> Container:
    metrics = Metrics()
    clock = SystemClock()
    breaker = CircuitBreaker()  # one breaker for the single Redis behind cache, limiter and publisher
    links = LinkService(
        links=SqlLinkRepository(engine),
        cache=RedisLinkCache(redis, metrics, breaker),
        clock=clock,
        generator=RandomBase62Generator(settings.code_length),
        dns=SystemDnsResolver(settings.dns_timeout_seconds),
        metrics=metrics,
        own_origins=own_origins_from_base_url(settings.base_url),
        cache_ttl_seconds=settings.cache_ttl_seconds,
        negative_cache_ttl_seconds=settings.negative_cache_ttl_seconds,
        dns_check_enabled=settings.dns_check_enabled,
    )

    async def readiness() -> tuple[bool, bool]:
        database_up, redis_up = await asyncio.gather(database_is_up(engine), redis_is_up(redis))
        return database_up, redis_up

    async def gauges() -> dict[str, int]:
        try:
            length = int(await redis.xlen(CLICK_STREAM))
            groups = await redis.xinfo_groups(CLICK_STREAM)
        except Exception:  # best effort: absent stream/group or Redis down
            return {}
        pending = next((int(g["pending"]) for g in groups if g["name"] == GROUP), 0)
        return {"click_stream_length": length, "click_stream_pending": pending}

    return Container(
        settings=settings,
        links=links,
        analytics=AnalyticsService(links=links, clicks=SqlClickRepository(engine), clock=clock),
        authenticator=ApiKeyAuthenticator(settings.api_key_hashes),
        rate_limiter=RateLimiter(RedisRateLimitStore(redis, breaker), clock, metrics),
        publisher=RedisClickPublisher(
            redis,
            metrics,
            timeout_ms=settings.click_emit_timeout_ms,
            maxlen=settings.click_stream_maxlen,
            breaker=breaker,
        ),
        clock=clock,
        metrics=metrics,
        readiness=readiness,
        gauges=gauges,
    )


def create_app_from_env() -> FastAPI:
    """Uvicorn factory: `uvicorn --factory shortener.main:create_app_from_env`."""
    settings = Settings()  # values come from the environment
    configure_logging(settings.log_level)
    engine = create_db_engine(settings)
    redis = create_redis(settings)
    container = build_container(settings, engine, redis)

    async def cleanup() -> None:
        await redis.aclose()
        await engine.dispose()

    return create_app(container, cleanup)


async def run_worker() -> None:
    settings = Settings()
    configure_logging(settings.log_level)
    engine = create_db_engine(settings)
    redis = create_redis(settings, blocking=True)
    consumer = ClickConsumer(
        redis,
        SqlClickRepository(engine),
        consumer_name=f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}",
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows event loops
            signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stop.set))
    try:
        await consumer.run(stop)
    finally:  # graceful shutdown: the in-flight batch has finished before run() returns
        await redis.aclose()
        await engine.dispose()
