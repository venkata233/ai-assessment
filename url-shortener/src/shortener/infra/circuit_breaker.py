"""A minimal circuit breaker so a dead Redis costs the request path almost nothing."""

import time
from collections.abc import Callable


class CircuitBreaker:
    """Opens after `failure_threshold` consecutive failures and rejects calls for `reset_seconds`.

    After that window one call is let through (half-open): success closes the breaker, failure
    re-opens it immediately. Without it, every request would pay a connect attempt per Redis call
    (rate limit, cache read, cache write) while Redis is down.
    """

    def __init__(
        self,
        failure_threshold: int = 3,
        reset_seconds: float = 5.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._threshold = failure_threshold
        self._reset = reset_seconds
        self._clock = clock
        self._failures = 0
        self._open_until = 0.0

    def allow(self) -> bool:
        return self._clock() >= self._open_until

    def record_success(self) -> None:
        self._failures = 0
        self._open_until = 0.0

    def record_failure(self) -> None:
        self._failures += 1
        if self._failures >= self._threshold:
            self._open_until = self._clock() + self._reset
