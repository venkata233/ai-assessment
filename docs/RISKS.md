# Risk Register (T21)

Scope: the URL shortener (Part B) as built in Phase 3 and reviewed in Phase 4, plus the prompt-injection risk of Part A. Ratings
are the AI's **draft** judgement of likelihood (L) and impact (I, which doubles as severity) on a Low / Medium / High scale
**after** the mitigations listed. The Phase 6 changes in section 3 (R04 and R12 likelihood, the A09 ratings, the Priority column)
were approved by the engineer's instruction; every other rating is still the AI's draft pending the engineer's review.
"Evidence" points at what was actually run; where something is only reasoned about, it says so. Last updated 2026-11-20.
**Priority** (P1 most urgent to P4 least) is derived by a fixed rule from severity, exposure and mitigation (section 3), not
chosen by feel.

## 1. Product and operations risks

| ID | Risk | L | I | Priority | Mitigation in place (evidence) | Residual risk and the trade-off accepted | Next step |
|---|---|---|---|---|---|---|---|
| R01 | **Redis outage degrades the service** (slower redirects, no rate limiting, click events dropped, delete invalidation fails) | M | M | **P3** | Redirect falls back to PostgreSQL; limiter fails open; shared circuit breaker (3 failures, skip Redis for 5 s). Verified: live `docker compose stop redis` (first request 549 ms, then 5-30 ms; automatic recovery), integration test AC-11. | Fail-open limiter means abuse is unthrottled during an outage. With Redis *blackholed* (dropped packets, not refused) the first calls wait up to 0.25 s each; not measured. | Measure blackhole behaviour; alert on `cache_errors_total` and `rate_limit_errors_total` |
| R02 | **Click events are lost** (app to Redis is best effort; crash after response; Redis AOF `everysec` window; stream trimmed; emit timeout 50 ms) | M | L | **P4** | Loss windows documented (ADR-004), drops counted (`clicks_dropped_total{reason}`). Verified: after publish nothing was lost in the load runs (stream entries = stored rows, dead stream empty). | Analytics are approximate by design. Emit drops under CPU starvation could not be separated from Locust counting artifacts (LOAD_TEST.md section 4). | Re-measure with an exact server-side counter; consider a longer emit timeout under load |
| R03 | **A deleted link keeps redirecting** if Redis is down at delete time (up to cache TTL x 1.1, default about 66 min) | L | M | **P4** | Tombstone design (ADR-005); the failed invalidation is now counted (`cache_invalidation_failures_total`) and logged with the stale bound. Tested (unit + integration). | Revocation is not guaranteed during a Redis outage. Lowering `CACHE_TTL_SECONDS` shortens the window at the cost of more database reads. | Alert on the new counter; decide the TTL from the abuse-takedown SLA |
| R04 | **PostgreSQL outage** | M | H | **P1** | Cache hits still redirect; misses return `503` with `Retry-After`; `/health/ready` reports `503`; worker keeps events pending. Tested. | Entries delivered more than 5 times (about 5 reclaim cycles of 60 s) go to `clicks:dead`: kept, but not counted, and **no re-drive tool exists**. No HA/backup design in v1. | Build the re-drive command; define backup/restore and RPO/RTO |
| R05 | **Capacity is unknown**: the 5,000 redirects/s target is unverified; one uvicorn process saturated at about 370-390 req/s on a shared laptop; scaling behaviour was not understood | H | H | **P1** | Stateless design allows horizontal scale (AC-14 verified for two replicas); hot-path cost profiled (flat, about 3.6 ms CPU per cached redirect). | The load test is incomplete (LOAD_TEST.md): there is **no capacity claim**. Raw `clicks` rows grow without bound (see R12). | Finish T20 properly; reduce Redis round trips per redirect; plan partitioning (ADR-003) |
| R06 | **Redis memory exhaustion**: the stream plus keys exceed `maxmemory`; `XADD`, `SET`, `INCR` then fail (events drop, limiter fails open, tombstones cannot be written) | M | M | **P3** | `volatile-lru` so the cache cannot evict the stream; every cache/limiter key has a TTL (tested). Measured at most about 300 B per event; compose sets `CLICK_STREAM_MAXLEN=200000` (about 60-100 MB) under the 256 MB limit. | The application default (1,000,000 entries, about 300 MB+ with long user agents) is only safe with a larger `maxmemory` or the separate Redis instances recommended in ADR-004. | Alert on Redis memory and stream length; size per environment |
| R07 | **The service is used to distribute phishing or malware** (it redirects to arbitrary public URLs; no reputation checks, no abuse reporting) | M | H | **P1** | URL guard blocks non-http(s), credentials, private/loopback/link-local/metadata addresses in every tested encoding, internal names, self-reference; ownership via API keys; rate limits; deleting a link is immediate (R03 caveat). 100+ adversarial validator cases, 70 more in the independent review. | DNS rebinding after creation; malicious content at a public destination is out of scope. Only API-key holders can create links, which limits but does not remove abuse. | Add a blocklist/reputation hook and an abuse-report path; re-validate stored targets periodically |
| R08 | **Alias squatting / enumeration**: custom aliases can be taken first; a `409` reveals that an alias exists (even another owner's or a deleted one) | M | L | **P4** | Reserved-word list, case-sensitive codes, permanent reservation of deleted aliases (ADR-003). | Existence disclosure is inherent to unique aliases. A short, valuable alias can be squatted by any key holder. | Per-owner quotas; alias policy |
| R09 | **API-key lifecycle**: static keys in configuration, no rotation tooling or expiry; a leaked key grants full owner access | M | H | **P1** | Only SHA-256 hashes are configured; constant-time comparison over all keys; failed attempts throttled per client address; keys never logged (tested end to end). | SHA-256 without salt/stretching is appropriate only for high-entropy keys. Revocation needs a config change and restart. | Key rotation procedure; per-key expiry; consider a database-backed key store |
| R10 | **Published dev credentials get deployed** (demo keys and `*_dev_only` passwords are in the repository) | L | H | **P3** | The application **refuses to start** with them unless `BASE_URL` is localhost (tested; verified in the container); compose binds to 127.0.0.1; hardened containers (read-only root, `cap_drop: ALL`, `no-new-privileges`). | Redis has no password in compose; `ALLOW_DEV_CREDENTIALS` is a deliberate break-glass switch. | Require Redis auth/TLS outside local development |
| R11 | **Client identity for rate limiting is wrong** (`TRUSTED_PROXY_HOPS` misconfigured): all visitors share one limit, or an attacker bypasses it | M | M | **P3** | Last-N entries logic is spoof-resistant (tested incl. multi-line headers); header ignored by default. | Behind an unconfigured proxy everyone shares one address and one redirect limit; with hops above 0 and the app directly reachable, the limit is bypassable. Deployment-time risk. | Deployment checklist; startup warning |
| R12 | **Personal data and retention**: user agents (up to 255 chars), a daily-salted visitor hash, and referrer origins are stored; the documented 90-day retention job **is not built**, so raw clicks accumulate indefinitely | H | M | **P1** | No raw IP stored or logged; referrer reduced to origin (DB CHECK); UA truncated; hash rotates daily; log redaction tested. | **No purge job exists.** The privacy rule (Phase 1, A8) says raw clicks are kept 90 days, but nothing deletes them: the purge or partition drop was documented and never built, so retention is unbounded from the first day of operation. Any deployment older than 90 days therefore holds data the stated policy says should be gone (the likelihood is certain, hence High). Storing user agents may also need a lawful basis and a notice. | Implement the purge job (or partition drop); privacy review |
| R13 | **Unauthenticated operational endpoints** (`/metrics` off by default; `/health/ready` always on) reveal dependency state and counters | L | L | **P4** | `/metrics` is opt-in (compose enables it on loopback only); `/docs` and `/openapi.json` are off by default. | `/health/ready` is intentionally public (orchestrators need it). | Restrict at the ingress |
| R14 | **Supply chain and base image drift**: pinned digests and pinned runtime dependencies go stale; no automated update PRs | M | M | **P2** | Runtime requirements fully pinned; `pip-audit` clean; Trivy initially found 1 HIGH OS CVE and 2 HIGH Python CVEs in the image, **fixed** (zlib upgraded; pip/setuptools/wheel removed) and re-scanned clean; GitHub Actions pinned to commit SHAs. | The zlib fix is an `apt-get --only-upgrade` line that should be removed when the base digest is bumped. Dev tool dependencies are only partly pinned. No image scan in CI yet. | Add Trivy and Dependabot to CI; schedule base-image bumps |
| R15 | **CI is unproven**: the workflow has never run on GitHub (no remote) | M | M | **P2** | Each step was run locally; YAML parses; permissions are `contents: read`. | First real run may fail for environment reasons (for example `docker compose --wait`, caches). | Run it once a remote exists |
| R16 | **Migration risk**: forward-only in production; locks on large tables; rolling deploys across versions | L | M | **P3** | Round trip and drift checks (`alembic check`); expand/contract rules (DB_SCHEMA section 6). | Only one migration exists, so none of this has been exercised on a populated table. | Test on a representative data volume before the second migration |
| R17 | **Clock skew between replicas** (expiry decided by each replica's clock; `created_at` is app time) | L | L | **P4** | Expiry compared on every request with a single UTC clock per process; DB CHECK `expires_at > created_at`. | Seconds of skew can change when a link expires at the boundary. Accepted. | NTP on hosts |

## 2. Risks specific to AI-assisted engineering

| ID | Risk | L | I | Priority | How it showed up in this project | Mitigation and what remains |
|---|---|---|---|---|---|---|
| A01 | **Plausible but wrong or stale API usage** (training data older than the pinned libraries) | H | M | **P2** | `Redis[str]` (generic that does not exist in redis-py 8), `RowMapping` typing, `sqlalchemy` without the asyncio extra, FastAPI 0.165 not flattening included routers (`/metrics` could not be disabled) | Versions resolved from PyPI, not memory; `mypy --strict`, tests, and running against real services caught each one. Residual: anything not executed. |
| A02 | **Tests that mirror the implementation** give false confidence | H | H | **P1** | 100 validator cases passed on the first run; coverage was 96 % while three real gaps existed (database-path expiry boundary, mapped-IPv6 policy, "publish after the response") | Manual mutation testing: 22 mutants, 18 killed at first, 20 after adding tests; the two survivors are understood (deadlock lock-ordering). Residual: 22 mutants is a small sample; mutations were chosen by the same author. |
| A03 | **Security code written and reviewed by the same family of models** shares blind spots | M | H | **P2** | The validator held against 170+ adversarial payloads, yet an independent review still found a regex anchoring bug (`$` accepts a trailing newline) and a scheme-blind self-reference check | A separate read-only security review plus Trivy and Bandit; all findings triaged (VALIDATION.md). Residual: no human penetration test, no fuzzing, no DAST scanner. |
| A04 | **Over-claiming** (stating that something works or scales without running it) | M | H | **P2** | Early drafts would have claimed the 5,000/s target and a deadlock fix; the deadlock lock could not be shown necessary; the first load series had an unexplained scaling shape and unsampled load-generator CPU | Every claim in the docs is tagged verified, unverified, or not done; `LOAD_TEST.md` and `ARCHITECTURE.md` section 15 state the limits; the usage log records errors the AI made. |
| A05 | **Secrets or private data reaching prompts and repositories** | L | H | **P3** | Demo credentials are committed on purpose (dev only) | They are labelled and cannot start a non-local deployment (R10); no real secret, key, or personal data was used or committed (commit scans). Residual: reviewers must not paste real keys into AI tools. |
| A06 | **Licence and provenance of generated code and dependencies** | L | M | **P3** | Dependencies resolved automatically; code is AI-drafted and engineer-reviewed | No licence scan (SBOM/licence check) was run. |
| A07 | **Documentation drifts from code** | M | M | **P3** | Several docs were updated after fixes (for example `/metrics` default, circuit breaker) | Contract tests tie the code to `openapi.yaml`; other docs rely on review. Residual: ARCHITECTURE diagrams were never render-checked. |
| A08 | **Automation bias in review**: green checks treated as proof | M | M | **P3** | Coverage and static analysis passed while real defects existed (see A02, A03) | Checks are reported with what they do not cover (VALIDATION.md section 6). |
| A09a | **Prompt injection, current mock provider**: untrusted brief or brownfield text steering the tool | L | L | **P4** | The rule-based mock provider (the only provider run so far) is not a language model and cannot follow instructions. Injected text can only be copied into drafts as ordinary text, where injection-like phrases are flagged. Nothing the tool produces is executed. | The engineer reviews every stage (gates G1-G4). Residual: the flag is a keyword heuristic that misses rephrased text; a nuisance today, not a control. |
| A09b | **Prompt injection, a future real LLM provider** (the `openai` provider, never run against a real model) | M | M | **P2** | Not observed: no real model has been used. In Part A the brief and brownfield files are untrusted input to the model. | Design mitigations only: untrusted inputs are fenced and the system prompt tells the model not to obey them; human gates G1-G4; redaction before sending plus an outbound confirmation gate; nothing is executed. Residual: **unmeasured**; fencing reduces the risk but does not remove it. See [COPILOT_PROTOTYPE.md](COPILOT_PROTOTYPE.md) section 10. |

## 3. Phase 6 changes to the ratings, and how Priority is derived

Method: each row was compared with the evidence in [VALIDATION.md](VALIDATION.md), [LOAD_TEST.md](LOAD_TEST.md) and
[ARCHITECTURE.md](ARCHITECTURE.md) section 15. The three rating changes below follow directly from the register's own text, were
approved by the engineer, and all **raise** a rating (the cautious direction).

| Row | Change | Reason |
|---|---|---|
| R04 PostgreSQL outage | L from Low to **Medium** | The listed mitigations (cache hits, 503 with `Retry-After`, pending events) reduce the **impact** of an outage, not its likelihood, and the same row states that v1 has no HA or backup design (a single PostgreSQL node) |
| R12 Personal data and retention | L from Medium to **High** | **No purge job exists.** The privacy rule (Phase 1, A8) keeps raw clicks 90 days, but nothing deletes them, so retention beyond 90 days is certain for any deployment older than that, not contingent. Impact stays Medium (no raw IP; only user agent, referrer origin and a daily-salted hash) |
| A09 Prompt injection | Split into **A09a** (current mock provider: L = Low, I = Low) and **A09b** (a future real LLM provider: L = Medium, I = Medium) | The old row said "n/a" although Part A now exists. The rule-based mock cannot follow instructions, so the risk today is a nuisance; a real model could be steered by untrusted text, and that path has never been run, so its rating is a design-based estimate, not a measurement |
| Priority column | Added (P1 to P4) | Derived by the rule below for every row |

**Unchanged on purpose, as instructed:** R03, R15 and R05 keep their L and I ratings. Their priorities follow from the rule: R05 is
P1; R15 is P2 because its mitigation is unproven; R03 is P4 by the rule even though a stale redirect after a delete could matter
for taking down malicious links (that exposure is carried by R07, P1), so the engineer may want to override it.

### Priority rule

1. **Base** from severity x exposure (I and L scored High 3, Medium 2, Low 1; multiply): 9 gives P1, 6 gives P2, 3 or 4 gives P3, 1 or 2 gives P4.
2. **Mitigation modifier:** if the row's own text says the mitigation that matters most is **unbuilt or unverified** (a gap), move one level
   more urgent (never above P1). If it was verified by a test or a live check, the base stands.

| ID | Severity (I) | Exposure (L) | Base | Main mitigation | Priority |
|---|---|---|---|---|---|
| R01 | M | M | P3 | Verified (live Redis stop, integration test) | **P3** |
| R02 | L | M | P4 | Verified (drops counted, tested) | **P4** |
| R03 | M | L | P4 | Verified (counted, tested) | **P4** |
| R04 | H | M | P2 | **Gap:** no HA or backup, no dead-letter re-drive tool | **P1** |
| R05 | H | H | P1 | **Gap:** capacity unverified | **P1** |
| R06 | M | M | P3 | Verified (sizing measured, TTL test) | **P3** |
| R07 | H | M | P2 | **Gap:** no reputation, blocklist or abuse-report path | **P1** |
| R08 | L | M | P4 | Verified (reserved words, tombstones) | **P4** |
| R09 | H | M | P2 | **Gap:** no key rotation or expiry tooling | **P1** |
| R10 | H | L | P3 | Verified (start-up guard tested in the container) | **P3** |
| R11 | M | M | P3 | Verified (logic tested; the risk is at deployment time) | **P3** |
| R12 | M | H | P2 | **Gap:** the 90-day purge is not built | **P1** |
| R13 | L | L | P4 | Verified (opt-in, off by default) | **P4** |
| R14 | M | M | P3 | **Gap:** no automated updates, no image scan in CI | **P2** |
| R15 | M | M | P3 | **Gap:** CI has never run | **P2** |
| R16 | M | L | P4 | **Gap:** never exercised on a populated table | **P3** |
| R17 | L | L | P4 | Accepted | **P4** |
| A01 | M | H | P2 | Verified (caught by mypy, tests and live runs) | **P2** |
| A02 | H | H | P1 | **Gap:** the 5 Phase 4 mutants never ran | **P1** |
| A03 | H | M | P2 | Verified (independent review run); residual gaps listed in the row | **P2** |
| A04 | H | M | P2 | Verified (claims labelled verified, unverified or not done) | **P2** |
| A05 | H | L | P3 | Verified (commit scans) | **P3** |
| A06 | M | L | P4 | **Gap:** no licence scan | **P3** |
| A07 | M | M | P3 | Verified for the API contract (contract tests) | **P3** |
| A08 | M | M | P3 | Verified (limits of each check stated) | **P3** |
| A09a | L | L | P4 | Verified in design (the mock cannot follow instructions) | **P4** |
| A09b | M | M | P3 | **Gap:** never run with a real model | **P2** |

Distribution: P1 x 6 (R04, R05, R07, R09, R12, A02), P2 x 6, P3 x 9, P4 x 6. Priority orders attention; it is not a probability.
It is the AI's derivation and the engineer may override any row.
