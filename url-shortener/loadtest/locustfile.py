"""Locust scenarios for the URL shortener (T20).

SCENARIO selects what is exercised (closed-loop: every user fires its next request as soon as the last one returns):

  hot     redirects for a small set of links that are always in the Redis cache
  cold    redirects for links that are never cached: every request is a cache miss, a PostgreSQL
          lookup and a cache write (each code is requested exactly once)
  create  POST /api/v1/urls with a unique target each time
  mixed   94 % hot redirects, 5 % cold redirects, 1 % creates (roughly the 100:1 read:write of the design assumption A2)

Environment: SCENARIO, LOAD_API_KEY, HOT_LINKS (default 50), COLD_TOTAL (rows seeded by seed.sql, default 300000),
LOAD_PROCS (number of Locust worker processes; used to give each one its own slice of the cold codes).
"""

import itertools
import os
import random
import uuid

from locust import FastHttpUser, constant, events, task
from locust.runners import WorkerRunner

SCENARIO = os.environ.get("SCENARIO", "mixed")
API_KEY = os.environ.get("LOAD_API_KEY", "demo-key-local-dev-only")
HOT_LINKS = int(os.environ.get("HOT_LINKS", "50"))
COLD_TOTAL = int(os.environ.get("COLD_TOTAL", "300000"))
PROCS = max(1, int(os.environ.get("LOAD_PROCS", "1")))

WEIGHTS = {
    "hot": (1, 0, 0),
    "cold": (0, 1, 0),
    "create": (0, 0, 1),
    "mixed": (94, 5, 1),
}[SCENARIO]

HOT_CODES = [f"hot-{i:03d}" for i in range(HOT_LINKS)]
_cold_counter = itertools.count(0)
_cold_end = COLD_TOTAL


@events.test_start.add_listener
def _assign_cold_slice(environment, **_kwargs):  # type: ignore[no-untyped-def]
    """Each Locust process walks its own contiguous slice of the seeded cold codes, so no code repeats."""
    global _cold_counter, _cold_end
    index = environment.runner.worker_index if isinstance(environment.runner, WorkerRunner) else 0
    size = COLD_TOTAL // PROCS
    _cold_counter = itertools.count(index * size)
    _cold_end = (index + 1) * size


class _Base(FastHttpUser):
    abstract = True
    wait_time = constant(0)
    connection_timeout = 10.0
    network_timeout = 10.0

    def _redirect(self, code: str, name: str) -> None:
        with self.client.get(f"/{code}", name=name, allow_redirects=False, catch_response=True) as response:
            if response.status_code == 302:
                response.success()
            else:
                response.failure(f"expected 302, got {response.status_code}")


class HotRedirect(_Base):
    weight = WEIGHTS[0]

    @task
    def hot(self) -> None:
        self._redirect(random.choice(HOT_CODES), "redirect (hot, cached)")  # noqa: S311


class ColdRedirect(_Base):
    weight = WEIGHTS[1]

    @task
    def cold(self) -> None:
        number = next(_cold_counter)
        if number >= _cold_end:
            with self.client.get(
                "/missing-pool-exhausted", name="cold pool exhausted", catch_response=True
            ) as r:
                r.failure("cold code pool exhausted: raise COLD_TOTAL")
            return
        self._redirect(f"C{number:08d}", "redirect (cold, DB lookup)")


class Create(_Base):
    weight = WEIGHTS[2]

    @task
    def create(self) -> None:
        body = {"url": f"https://example.com/load/{uuid.uuid4().hex}"}
        with self.client.post(
            "/api/v1/urls", json=body, headers={"X-API-Key": API_KEY}, name="create link", catch_response=True
        ) as response:
            if response.status_code == 201:
                response.success()
            else:
                response.failure(f"expected 201, got {response.status_code}")
