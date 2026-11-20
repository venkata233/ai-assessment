# Validation and Security Review (T19)

Date: 2026-11-20. Scope: `url-shortener/` as committed in Phase 3 (`ae592ce`) plus the Phase 4 fixes described here (not yet
committed). Everything marked **verified** was run in this session; the last section lists what was *not* done.

## 1. Verdict

- No High or Medium vulnerability was found by the independent code review, Trivy, Bandit, or `pip-audit` in the *code*.
- The review produced **6 Low findings** and 9 theoretical concerns (15 items). Triage outcome: **8 fixed** (7 in code with
  regression tests, 1 compose configuration), **2 partly fixed** (#3 delete invalidation, #5 compose defaults) and **5
  documented or accepted without a code change** (section 3).
- Validation itself found **4 more issues** that the review did not (section 3.3), among them a real secret-leakage path
  (pydantic echoed submitted settings, including secret tails, in startup errors) and 3 HIGH CVEs in the container image.
- Residual risk is recorded in [RISKS.md](RISKS.md). Capacity is **not** validated (see [LOAD_TEST.md](LOAD_TEST.md)).

## 2. Evidence (what was run, and what each check cannot tell you)

| Check | Command / method | Result | Limits |
|---|---|---|---|
| Tests | `REQUIRE_INTEGRATION=1 pytest` against real PostgreSQL 16 + Redis 7 | **426 passed** (355 unit, 17 contract, 54 integration) | Single machine; tests written by the same author |
| Coverage | `pytest --cov --cov-fail-under=90` | **96.0 %** statement+branch (gate 90 %); `link_service.py` 99 % | Coverage counts executed lines, not correctness (see mutation row) |
| Contract | Responses validated against `openapi.yaml`'s own JSON Schemas, operation/operationId parity, declared headers/statuses, the spec's examples | 17 tests pass | Covers the documented behaviour only |
| Lint / format | `ruff check`, `ruff format --check` | clean (53 files) | Style/bug patterns, not logic |
| Types | `mypy --strict` | clean (36 source files) | Does not check runtime data |
| SAST | `bandit -r src` | **0 issues** (after fixing B101, B311 in Phase 3) | Pattern-based; Semgrep was not run |
| Dependency audit | `pip-audit -r requirements.txt` | **no known vulnerabilities** | Pinned runtime packages only |
| Image scan | Trivy (`aquasec/trivy`) on `url-shortener:local`, fixed vulnerabilities | **Before:** 1 HIGH (`zlib1g` CVE-2026-85091) + 2 HIGH (`jaraco.context` CVE-2026-23949, `wheel` CVE-2026-24049, both vendored in pip/setuptools). **After fixes:** 0 HIGH/CRITICAL and 0 fixable at any severity | Point-in-time; unfixed CVEs were excluded (`--ignore-unfixed`); no CI job yet |
| Independent code review | Read-only security-review agent, 70 SSRF/validator payloads run against the real module | 0 High, 0 Medium, 6 Low (section 3) | Same model family as the author; did not run Redis/PostgreSQL/Docker |
| Live checks | Running compose stack: 401/404 ownership, delete, Redis outage and recovery, logs contain no key or raw client IP, read-only root filesystem (`touch` fails), fail-closed guard in the container, worker graceful stop (1.4 s) | as expected | Manual spot checks |
| Mutation sanity | `.agent/mutation_check.py`: 22 deliberate bugs in a scratch copy | **20 killed**, 2 survived (deadlock lock-ordering; see ARCHITECTURE section 15) | **Not re-run after the Phase 4 fixes** (CPU), so the new tests have not been mutation-checked; 5 new mutants are defined in the script for the next run |
| Migrations | upgrade, downgrade, upgrade, `alembic check`, single head | pass | One migration, empty database |

## 3. Triage of every finding

### 3.1 Confirmed findings from the independent review (all Low)

| # | Finding | Decision | Change | Regression test |
|---|---|---|---|---|
| 1 | `CODE_PATTERN` used `re.match` with `$`, so `"admin\n"` passed alias validation and bypassed the reserved list; PostgreSQL's CHECK would reject it as a **500**; `GET /abc%0A` hit storage and wrote a newline key into Redis | **Fixed** | `fullmatch` for every anchored pattern (codes, config, referrer origin, country, request id, cursor event id, worker visitor hash, validator labels/numbers) | `test_security_review_fixes.py`: newline codes invalid, API returns 422 not 500, `/abc%0A` is 404 with no storage or cache write |
| 2 | Self-reference guard ignored the scheme (`BASE_URL=https://x` allowed `http://x/...`) and did not IDNA-normalise `BASE_URL` | **Fixed** | `own_origins_from_base_url()`: IDNA-normalised host, configured port plus both default ports | http/https/trailing-dot/explicit-port variants refused; IDN base compared in punycode; other hosts still allowed |
| 3 | A delete during a Redis outage leaves a warm cache entry redirecting for up to about 66 min; ADR-005 promised a delete-specific metric/log that did not exist | **Partly fixed** | `LinkCache.put` now reports success; a failed tombstone increments `cache_invalidation_failures_total` and logs `delete_cache_invalidation_failed` with the stale bound. The exposure itself remains (RISKS R03) | Unit (failure counted, success not) and integration (`put` returns `False` on a dead Redis) |
| 4 | `/metrics` unauthenticated and on by default | **Fixed** | `METRICS_ENABLED` defaults to **false**; compose enables it for loopback-only local use. `/health/ready` stays public by design | Default-off and "route absent unless enabled" tests |
| 5 | Compose defaults to published demo keys and dev passwords; Redis has no password; no container hardening | **Partly fixed** | (a) App **refuses to start** with the demo key hashes or `*_dev_only` passwords unless `BASE_URL` is localhost (fail closed, keeps the one-command start); (b) `read_only`, `tmpfs /tmp`, `cap_drop: ALL`, `no-new-privileges` on migrate/app/worker. **Not done:** Redis password (documented, RISKS R10) | Guard tests (key hashes, passwords, localhost vs. not, break-glass flag); verified live: read-only root, caps dropped, container refuses to start |
| 6 | Country header trusted from any client | **Fixed** | `TRUST_GEO_HEADER` defaults to **false** (operator opts in only when the edge strips and rewrites the header) | Ignored by default, honoured when trusted |

### 3.2 Theoretical concerns

| Concern | Decision | Notes |
|---|---|---|
| `TRUSTED_PROXY_HOPS=0` behind a proxy makes every visitor share one address | **Documented**, no code change | Deployment risk (RISKS R11); default is safe-by-ignoring the header |
| `TRUSTED_PROXY_HOPS>0` and app directly reachable lets clients spoof the limit | **Documented** | Same; plus: multiple `X-Forwarded-For` header lines were only partly read, now **fixed** and tested |
| Stream size vs. Redis `maxmemory` (1M entries is about 300 MB+) | **Fixed for compose** (`CLICK_STREAM_MAXLEN=200000`), documented for others | RISKS R06; measured about 300 B per event at most |
| Redis unauthenticated, cache values not re-validated on read | **Documented**, no change | Compose binds to loopback; production needs Redis auth/TLS (R10). A defence-in-depth re-validation of cached targets was not added |
| Python `ipaddress.is_global` accepted `192.0.0.8`, `192.88.99.1`, `3fff::1` on 3.11.4 | **Fixed** | Explicit blocklist (`192.0.0.0/24`, `192.88.99.0/24`, `198.18.0.0/15`, `3fff::/20`) independent of the interpreter; tested |
| DNS check is advisory; `getaddrinfo` threads can outlive the timeout | **Documented** | Create is rate limited (30/min per key); DNS rebinding after creation is a stated residual risk |
| `exc_info` logging could print SQL parameters (target URLs) | **Fixed** | `hide_parameters=True` on the engine; test shows a DB error message contains no bound value |
| 409 on alias creation discloses existence | **Accepted** | Inherent to unique aliases (RISKS R08) |
| Out-of-range `from`/`to`/cursor timestamps could raise `OverflowError` (500) | **Fixed** | Mapped to a 422 validation error; unit and HTTP tests |

### 3.3 Additional findings from the validation work (not in the review)

| Finding | Severity | Fix |
|---|---|---|
| Pydantic's validation error printed `input_value=...` with the head **and tail** of the submitted settings (could leak the end of a real secret into startup logs) | Medium (information disclosure) | `hide_input_in_errors=True`; test asserts no secret fragment or `input_value` appears |
| Container image: `zlib1g` HIGH CVE in the pinned Debian base | High (OS package) | `apt-get install --only-upgrade zlib1g` in the Dockerfile (remove when the base digest is bumped) |
| Container image: `jaraco.context` and `wheel` HIGH CVEs (vendored in pip/setuptools, unused at runtime) | High (but not reachable by the service) | Removed pip, setuptools, and wheel from the venv and from the interpreter; re-scan clean |
| Redis memory sizing vs. default stream length (from the load data) | Medium (availability) | See concern above |

## 4. OWASP Top 10 (2021) review

| Risk | Control and evidence | Status |
|---|---|---|
| A01 Broken access control | Owner-scoped keys; non-owner gets 404 on get/delete/analytics (unit, API, integration); soft delete SQL is owner-scoped; cursor always combined with the link's id | **Verified** |
| A02 Cryptographic failures | Only SHA-256 key hashes configured; CSPRNG for codes (mutation-checked); daily-salted HMAC for visitor hash; TLS is delegated to the ingress (not exercised) | Verified, except TLS (not tested) |
| A03 Injection | SQLAlchemy Core with bound parameters only (reviewed); closed `Literal` for dynamic column/granularity; strict Pydantic bodies (`extra=forbid`); alias/code/cursor/request-id validated with `fullmatch`; log injection prevented by regex-validated request ids | **Verified** (review + tests) |
| A04 Insecure design | Threat-model driven URL guard (170+ adversarial payloads: encodings, IDNA, mapped IPv6, NAT64, 6to4, Teredo, userinfo, control/format characters, backslashes); fail-closed credential guard | **Verified**, with the DNS-rebinding residual risk |
| A05 Security misconfiguration | Non-root, read-only, caps dropped, no-new-privileges (verified live); docs off by default; metrics opt-in; security headers and CSP on every response (tested); loopback-only published ports | **Verified**; Redis auth missing (R10) |
| A06 Vulnerable components | `pip-audit` clean; Trivy clean after fixes; digests and runtime dependencies pinned | Verified at one point in time; no automated updates (R14) |
| A07 Authentication failures | Constant-time comparison without early exit (mutation-checked); key length cap; failed attempts throttled per client address; no key in logs (checked live) | **Verified** |
| A08 Integrity failures | Idempotent event ingestion (tested crash-before/after ACK); pinned action SHAs; image built from the repository | Verified, CI itself unproven (R15) |
| A09 Logging and monitoring failures | JSON logs with request id; redaction processor (tested); live log check found no key or raw client IP; counters for drops, cache errors, limiter errors, invalidation failures | **Verified**; no alerting is configured |
| A10 SSRF | The service never fetches targets; the risk is the open redirect to internal/dangerous targets, handled by the URL guard | **Verified**, plus the independent 70-payload run |

## 5. Checklist (Definition of Done records)

| Item | Status |
|---|---|
| Tests and coverage recorded | Yes (426 tests, 96 %) |
| SAST run | Bandit yes; Semgrep **no** |
| Dependency scan | `pip-audit` yes |
| Container scan | Trivy yes (fixed and re-scanned) |
| OWASP review recorded with evidence | Yes (section 4) |
| Findings triaged with accept/waive rationale | Yes (section 3) |
| Fixes have regression tests | Yes (`tests/unit/test_security_review_fixes.py`, 40 tests, plus 2 integration) |
| Risk register | Yes ([RISKS.md](RISKS.md)) |
| Load test | **Partial** ([LOAD_TEST.md](LOAD_TEST.md)) |

## 6. Not done (and why it matters)

- **No DAST** (for example ZAP) and **no fuzzing** of the HTTP surface or the URL parser; the review used curated payloads.
- **No human penetration test**; the independent review was an AI agent of the same family as the author (RISKS A03).
- **No Semgrep, secrets-scanner (gitleaks), or licence/SBOM scan**; only a manual pattern scan of the commit for credentials.
- **Mutation testing was not repeated** after the Phase 4 changes.
- **TLS, a real ingress, and Redis authentication** were not part of the tested topology.
- **CI has not run on GitHub**, so none of these checks is enforced automatically yet beyond the workflow's definition.
