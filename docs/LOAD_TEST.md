# Load Test (T20): status, preliminary measurements, and limits

**Status: partially done. The tooling is built and verified; one measurement series was run once; it was not repeated or
completed, so there is no defensible capacity claim.** The engineer paused further runs because the laptop CPU was
saturated. The provisional design target (5,000 redirects/s) is **not claimed, not supported, and not refuted** by anything
below.

## 1. What exists (committed tooling)

| File | Purpose |
|---|---|
| `url-shortener/loadtest/locustfile.py` | Locust scenarios (`FastHttpUser`, closed loop): `hot` (50 cached links), `cold` (every request a cache miss on a never-seen code, 300,000 seeded rows), `create`, `mixed` (94 % hot, 5 % cold, 1 % create, roughly the 100:1 read:write of assumption A2) |
| `url-shortener/loadtest/run_scenario.py` | Orchestrator: restarts the stack with the overlay, resets and seeds data, runs Locust **inside the compose network**, samples container CPU/memory, waits for the click worker to drain, compares the click pipeline's counts, writes JSON + Locust CSVs to `loadtest/results/` |
| `url-shortener/loadtest/seed.py`, `seed.sql` | Hot links via the API; cold rows via SQL |
| `url-shortener/docker-compose.loadtest.yml` | Overlay: Locust runner (image pinned by digest), rate limits effectively off, DNS check off, `max_connections=200`, `WEB_CONCURRENCY` for uvicorn processes in the app container |

Run one scenario: `python loadtest/run_scenario.py --scenario hot --users 64 --duration 60 --web-concurrency 4 --label hot-w4`.
Locust measures from inside the Docker network, so the figures exclude Windows/WSL2 port-forwarding overhead.

## 2. Environment (every number below depends on it)

| Item | Value |
|---|---|
| Machine | Laptop, Intel Core i5-1235U (10 cores / 12 threads, 2 performance + 8 efficiency cores), 7.7 GB RAM |
| Docker | Docker Desktop 29.9.3 on WSL2: 12 CPUs and **3.9 GB RAM** visible to the VM |
| Everything shared | The Locust runners, app, worker, PostgreSQL 16, and Redis 7 all ran on this one machine, competing for the same CPUs |
| App | One container; 1, 4, or 8 uvicorn processes (`WEB_CONCURRENCY`); request logging at INFO (one JSON line per request); `LOG_LEVEL` was not lowered |
| Load | Closed loop (zero think time), 4 Locust processes, 60 s per run, users as noted |
| Not representative of | Production hardware, a network between tiers, managed PostgreSQL/Redis, warmed JIT/OS caches over hours, or realistic traffic skew |

## 3. Preliminary results (single run each, **raw result files were not retained**)

I deleted `loadtest/results/` while preparing a rerun that was then not approved, so these figures come from the summary
printed at the time (they are real measurements, but no longer independently reproducible from stored artifacts).
**The CPU of the Locust runners was not sampled in this series.**

| Run | Users | uvicorn procs | Requests | Failures | req/s | avg ms | p50 | p95 | p99 | max |
|---|---|---|---|---|---|---|---|---|---|---|
| hot, cached redirect | 32 | 1 | 22,081 | 0 | 366 | 87 | 75 | 140 | 250 | 451 |
| hot | 8 | 4 | 39,431 | 0 | 652 | 12 | 11 | 23 | 37 | 455 |
| hot | 64 | 4 | 33,990 | 0 | 563 | 113 | 110 | 210 | 340 | 1,896 |
| hot | 96 | 8 | 40,045 | 0 | 662 | 143 | 72 | 410 | 950 | 6,373 |
| cold redirect (DB lookup + cache write) | 64 | 4 | 18,678 | 0 | 316 | 201 | 170 | 370 | 610 | 3,923 |
| create link | 32 | 4 | 11,589 | 0 | 196 | 161 | 140 | 300 | 430 | 2,501 |
| mixed: hot / cold / create | 64 | 4 | 27,424 / 736 / 84 | 0 | 463 / 12 / 1.4 | 128 / 239 / 695 | 73 / 110 / 650 | 380 / 800 / 1,100 | 570 / 1,200 / 2,300 | 2,535 |

Calibration runs (single uvicorn process, 15 s): **1 user: 176 req/s, avg 5.5 ms, p50 5, p95 11, p99 17**; 16 users with
request logging at WARNING: 384 req/s (avg 41 ms), versus 252 req/s with 2 Locust processes and INFO logging in the very
first calibration, so per-request logging costs a measurable but not dominant share.

Average container CPU during the 4-process hot run: app about 360-380 % (of one core), worker about 44 %, PostgreSQL about
48 %, Redis about 41 %. Peak memory: app 312 MiB (4 processes), worker 66 MiB, PostgreSQL 47-175 MiB, Redis 12 MiB.

## 4. What the data does and does not show

**Supported (observed, with the caveats above):**

1. **No request failed** in any run (0 of about 194,000 across the seven runs), even when the machine was clearly saturated; latency degraded
   (queueing) rather than errors appearing.
2. **The click pipeline was lossless after publish:** in every run the number of events in the stream equals the number of
   rows stored in `clicks` and the per-link counters, with nothing in the dead-letter stream; the worker drained a backlog
   within 0.7-5 s after the load stopped. (Whether *emit* drops occurred under load is **not** established: see section 5.)
3. **Cache hits are cheaper than misses:** at the same concurrency and processes, hot redirects ran at 563 req/s versus 316
   req/s for cold ones (every cold request adds a PostgreSQL round trip and a cache write).
4. **One uvicorn process saturates at roughly 370-390 req/s on this machine** (CPU about 98 % in the single-process run;
   about 3.6 ms of application CPU per cached redirect in the 1-user run: three Redis round trips for rate limit, cache
   read, and click publish, plus FastAPI/ASGI overhead). A cProfile run showed a flat profile with no single hotspot
   (asyncio, redis-py and FastAPI plumbing).
5. **Redis memory per click event was at most about 300 bytes** (12 MiB peak for a stream of 40,000 entries; this is an upper
   bound because the peak also includes Redis's baseline memory and the sampling was coarse). At the
   application default of 1,000,000 entries that exceeds the compose Redis `maxmemory` of 256 MB, so compose now sets
   `CLICK_STREAM_MAXLEN=200000` (see ARCHITECTURE section 15 and RISKS R06).

**Not supported / unexplained:**

- **Scaling is unexplained and looked poor:** 1 process gave 366 req/s, 4 processes 652 (not about 1,460), 8 processes 662.
  CPU per request rose from about 2.7 ms (1 process) to 5.5 ms (4) and 9 ms (8). The most likely cause is that the whole
  stack, including **four Locust processes that were not measured**, competes for a small number of effective cores (12
  threads of which 8 are slower efficiency cores) inside a WSL2 VM, so the machine, not the application, was the limit.
  This explanation is a hypothesis; the experiment to test it (sample the Locust CPU, run the generator on a second
  machine, or pin CPUs) was not done.
- **Any statement about 5,000 redirects/s.** Extrapolating from these runs would be invalid in either direction.
- **p95 targets from Phase 1** (cache-hit redirect p95 < 50 ms): the 4-process / 8-user run met it (p95 23 ms), but at
  saturating concurrency it did not (p95 140-410 ms). Latency under closed-loop saturation measures queueing, not the
  service time a user would see at a lower arrival rate. An open-loop (fixed arrival rate) test was not done.
- **Emit loss under load:** the comparison "Locust-reported 302s minus stream entries" differed by +2 and +38 in two runs
  (possible emit drops from the 50 ms publish timeout under CPU starvation) and was **negative** in others (Locust
  under-counting at shutdown). The two effects cannot be separated from the data. A server-side request log count was tried
  and found unreliable (it undercounted by about 6 % while Locust, the stream, and the database agreed; cause not investigated), so it was abandoned.
- **Replication, variance, long duration (soak), failover under load, and PostgreSQL growth:** none were measured.

## 5. Harness limitations found while building it

- Locust workers report statistics every few seconds, so totals can be off at shutdown.
- `docker stats` CPU percentages are sampled every ~2-3 s and are coarse; the sampler did not include the Locust containers
  in the first series (fixed in the committed `run_scenario.py`, not yet used for a full series).
- The `/metrics` counters are per uvicorn process, so they give exact drop counts only for single-process runs.
- Request logging at INFO writes one line per request to stdout through Docker's log driver, which both costs CPU and loses
  lines under heavy load; production would ship logs differently.
- The overlay relaxes rate limits (they would otherwise answer 429 to a single load generator) and disables the DNS check;
  the rate limiter's Redis call is still made, so its cost is included.

## 6. To finish T20 properly (not done)

1. Rerun with the Locust runners' CPU sampled; keep raw CSV/JSON under version control or in the session artifacts.
2. Separate the load generator from the system under test (second machine, or at least CPU pinning with `cpuset`).
3. Add an open-loop run at fixed arrival rates (for example 100, 200, 400 req/s) to read latency at a stated load.
4. Repeat each run at least three times and report the spread.
5. Re-measure after any hot-path optimisation (candidates: fewer Redis round trips per redirect, for example one Lua
   script for the rate-limit counter, an optional in-process L1 cache with pub/sub invalidation as listed in ADR-005).
