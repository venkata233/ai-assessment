"""Fixed-window rate limiting that fails open when the store is unavailable (ARCHITECTURE.md section 5)."""

import hashlib

import structlog

from shortener.domain.errors import RateLimited
from shortener.metrics import Metrics
from shortener.ports import Clock, RateLimitStore

_log = structlog.get_logger()


class RateLimiter:
    def __init__(
        self, store: RateLimitStore, clock: Clock, metrics: Metrics, window_seconds: int = 60
    ) -> None:
        self._store = store
        self._clock = clock
        self._metrics = metrics
        self._window = window_seconds

    async def check(self, scope: str, identity: str, limit: int) -> None:
        """Count one request; raise RateLimited when over `limit`. Store errors allow the request."""
        now = int(self._clock.now().timestamp())
        window_index, elapsed = divmod(now, self._window)
        # The identity (API-key label or client IP) is hashed so raw IPs never sit in Redis keys.
        digest = hashlib.sha256(identity.encode()).hexdigest()[:20]
        key = f"rl:{scope}:{digest}:{window_index}"
        try:
            count = await self._store.hit(key, self._window + 5)
        except Exception:
            self._metrics.inc("rate_limit_errors_total")
            _log.warning("rate_limit_store_unavailable", scope=scope)
            return
        if count > limit:
            self._metrics.inc("rate_limit_rejections_total", scope=scope)
            raise RateLimited(retry_after=self._window - elapsed)
