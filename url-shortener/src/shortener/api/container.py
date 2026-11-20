"""Dependency container handed to the FastAPI app (built in main.py, or from fakes in tests)."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from shortener.config import Settings
from shortener.metrics import Metrics
from shortener.ports import ClickPublisher, Clock
from shortener.services.analytics_service import AnalyticsService
from shortener.services.auth import ApiKeyAuthenticator
from shortener.services.link_service import LinkService
from shortener.services.rate_limiter import RateLimiter


@dataclass
class Container:
    settings: Settings
    links: LinkService
    analytics: AnalyticsService
    authenticator: ApiKeyAuthenticator
    rate_limiter: RateLimiter
    publisher: ClickPublisher
    clock: Clock
    metrics: Metrics
    # Returns (database_up, redis_up).
    readiness: Callable[[], Awaitable[tuple[bool, bool]]]
    # Extra gauges for /metrics (stream length/pending); best effort.
    gauges: Callable[[], Awaitable[dict[str, int]]] | None = None
