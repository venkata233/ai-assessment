"""Fixtures for tests that need real PostgreSQL and Redis (`docker compose up -d postgres redis`).

By default these tests are skipped when the services are unreachable. Set REQUIRE_INTEGRATION=1 (CI does)
to turn that skip into a failure so a missing service can never hide a regression.
"""

import asyncio
import os
import subprocess
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
import sqlalchemy as sa
from redis.asyncio import Redis
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from fakes import DEMO_KEY_SHA256, OTHER_KEY_SHA256
from shortener.api.app import create_app
from shortener.api.container import Container
from shortener.config import Settings
from shortener.infra.db import create_db_engine, create_redis
from shortener.main import build_container

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ADMIN_URL = os.environ.get(
    "TEST_ADMIN_DATABASE_URL", "postgresql+asyncpg://shortener:shortener_dev_only@127.0.0.1:5432/postgres"
)
REDIS_URL = os.environ.get("TEST_REDIS_URL", "redis://127.0.0.1:6379/15")
TEST_DB = "shortener_test"


def unavailable(message: str) -> None:
    if os.environ.get("REQUIRE_INTEGRATION") == "1":
        pytest.fail(message, pytrace=False)
    pytest.skip(message)


async def _recreate_database(admin_url: str, name: str) -> None:
    engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as conn:
            await conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
            await conn.execute(sa.text(f'CREATE DATABASE "{name}"'))
    finally:
        await engine.dispose()


def database_url_for(name: str) -> str:
    return make_url(ADMIN_URL).set(database=name).render_as_string(hide_password=False)


def run_alembic(url: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        [sys.executable, "-m", "alembic", *args],
        cwd=PROJECT_ROOT,
        env={**os.environ, "DATABASE_URL": url},
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


@pytest.fixture(scope="session")
def database_url() -> str:
    try:
        asyncio.run(_recreate_database(ADMIN_URL, TEST_DB))
    except Exception as exc:
        unavailable(f"PostgreSQL is not reachable at the test admin URL: {type(exc).__name__}")
    url = database_url_for(TEST_DB)
    result = run_alembic(url, "upgrade", "head")
    assert result.returncode == 0, result.stderr
    return url


@pytest.fixture(scope="session")
def redis_url() -> str:
    async def ping() -> None:
        client: Redis = Redis.from_url(REDIS_URL, socket_connect_timeout=2)
        try:
            await client.ping()  # type: ignore[misc]
        finally:
            await client.aclose()

    try:
        asyncio.run(ping())
    except Exception as exc:
        unavailable(f"Redis is not reachable at {REDIS_URL}: {type(exc).__name__}")
    return REDIS_URL


def make_integration_settings(database_url: str, redis_url: str, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "database_url": database_url,
        "redis_url": redis_url,
        "api_keys": f"demo:{DEMO_KEY_SHA256},other:{OTHER_KEY_SHA256}",
        "base_url": "http://short.test",
        "analytics_secret": "integration-test-analytics-secret",
        "dns_check_enabled": False,
        "allow_dev_credentials": True,
        "metrics_enabled": True,
        "trust_geo_header": True,
        "rate_limit_create_per_min": 10_000,
        "rate_limit_api_per_min": 10_000,
        "rate_limit_redirect_per_min": 10_000,
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


@dataclass
class Stack:
    settings: Settings
    engine: AsyncEngine
    redis: Redis
    container: Container

    def client(self) -> httpx.AsyncClient:
        app = create_app(self.container)
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://short.test")


@pytest.fixture
def settings(database_url: str, redis_url: str) -> Settings:
    return make_integration_settings(database_url, redis_url)


@pytest.fixture
async def engine(settings: Settings) -> AsyncIterator[AsyncEngine]:
    eng = create_db_engine(settings)
    async with eng.begin() as conn:
        await conn.execute(sa.text("TRUNCATE clicks, urls RESTART IDENTITY CASCADE"))
    yield eng
    await eng.dispose()


@pytest.fixture
async def redis(settings: Settings) -> AsyncIterator[Redis]:
    client = create_redis(settings)
    await client.flushdb()
    yield client
    await client.aclose()


@pytest.fixture
async def stack(settings: Settings, engine: AsyncEngine, redis: Redis) -> Stack:
    return Stack(settings, engine, redis, build_container(settings, engine, redis))
