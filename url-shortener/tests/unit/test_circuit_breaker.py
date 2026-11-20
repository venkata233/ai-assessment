from shortener.infra.circuit_breaker import CircuitBreaker


class Clock:
    def __init__(self) -> None:
        self.t = 100.0

    def __call__(self) -> float:
        return self.t


def test_closed_breaker_allows_calls_and_tolerates_isolated_failures() -> None:
    breaker = CircuitBreaker(failure_threshold=3, reset_seconds=5, clock=Clock())
    assert breaker.allow()
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.allow()
    breaker.record_success()  # a success resets the consecutive-failure count
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.allow()


def test_opens_after_consecutive_failures_and_half_opens_after_the_reset_window() -> None:
    clock = Clock()
    breaker = CircuitBreaker(failure_threshold=3, reset_seconds=5, clock=clock)
    for _ in range(3):
        breaker.record_failure()
    assert not breaker.allow()
    clock.t += 4.9
    assert not breaker.allow()
    clock.t += 0.2
    assert breaker.allow()  # half-open: one probe is let through
    breaker.record_failure()  # the probe failed: re-open immediately, no need for 3 more failures
    assert not breaker.allow()
    clock.t += 5.1
    assert breaker.allow()
    breaker.record_success()
    breaker.record_failure()
    assert breaker.allow()  # fully closed again
