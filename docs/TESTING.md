# Testing

How to run every check, what exists, and what it does and does not prove. Evidence dates: **2026-11-20**, Windows host, Python 3.11.4.

Legend: **Verified now** = run during Phase 6 in a fresh virtual environment. **Verified earlier** = run in the phase named, not repeated in Phase 6. **Not run** = defined but never executed.

## 1. Quick reference

| Goal | Command (from the project folder) | Needs Docker | Status |
|---|---|---|---|
| Service unit + contract tests | `python -m pytest tests/unit tests/contract -q` | No | **Verified now**: 372 passed in 9 s in a fresh venv after `pip install -r requirements-dev.txt` (about 2 min) |
| Lint, format | `ruff check src tests migrations` and `ruff format --check src tests` | No | **Verified now**: clean (53 files) |
| Types | `mypy` (strict, configured in `pyproject.toml`) | No | **Verified now**: no issues in 36 source files |
| SAST | `bandit -q -r src -c pyproject.toml` | No | **Verified now**: exit 0, no findings |
| Dependency audit | `pip-audit -r requirements.txt --progress-spinner off` | No (needs network) | **Verified earlier** (Phase 4): no known vulnerabilities. Point in time |
| Service integration tests | `docker compose up -d --wait postgres redis`, then `REQUIRE_INTEGRATION=1 python -m pytest tests/integration -q` | Yes | **Verified earlier** (Phase 4): 54 passed |
| Full suite + coverage gate | `REQUIRE_INTEGRATION=1 python -m pytest -q --cov --cov-fail-under=90` | Yes | **Verified earlier** (Phase 4): 426 passed, 96.0 % |
| Migrations | `alembic upgrade head`, `alembic downgrade base`, `alembic upgrade head`, `alembic check` | Yes | **Verified earlier** (Phase 3/4) |
| Whole stack (AC-13) | `docker compose up -d --build --wait` | Yes | **Verified earlier** (Phase 3): exit 0, `/health` 200 |
| Image scan | Trivy (`aquasec/trivy`) on `url-shortener:local` | Yes | **Verified earlier** (Phase 4): 0 HIGH/CRITICAL after fixes. Not in CI |
| Copilot prototype tests | `cd copilot-prototype`, set `PYTHONPATH=src`, `python -m pytest -q` | No | **Verified** in a venv containing only pytest (confirms it needs nothing else): 123 passed, 1 skipped, *before* the stage correction. After it, 132 tests are collected; `test_providers`, `test_engine`, `test_cli`, `test_web` and `test_examples` (82) were re-run and pass, while the 50 tests in four untouched files were **not re-run** |
| Copilot lint and types | `ruff check src tests`, `mypy` | No | **Verified earlier** (Phase 5): clean (17 source files) |
| Mutation sanity | `url-shortener/tools/mutation_check.py` (heavy: run only on a quiet machine; the integration mutants need Docker) | Needs Docker for some mutants | 22 original mutants **verified earlier** (20 killed, Phase 3); the 5 Phase 4 mutants are **Not run**. In Phase 6 the script was corrected (two stale patterns re-targeted, a skipped mutant is reported SKIPPED and never counted as killed, a mutant whose tests already fail unmutated is skipped) and **still has not been run**. `tests/unit/test_mutation_tool.py` (4 tests, no mutant is run) proves all 27 patterns match the current source |
| Load test | `url-shortener/loadtest/` (Locust via `docker-compose.loadtest.yml`) | Yes | **PARTIAL** ([LOAD_TEST.md](LOAD_TEST.md)); not run in Phase 6 |
| GitHub Actions workflow | `.github/workflows/ci.yml` | n/a | **Never run** (no remote) |

`make` is not installed on the development machine, so none of the `make` targets (the Makefile mirrors the rows above) was executed; the direct commands above were. The Makefile uses POSIX `VAR=1 command` syntax, which does not work in PowerShell: set `$env:REQUIRE_INTEGRATION = "1"` first.

`REQUIRE_INTEGRATION=1` matters: without it integration tests are **skipped** when PostgreSQL or Redis is unreachable, and a skipped test looks like a pass. CI sets it.

## 2. Test inventory (collected, Phase 6)

### Service (`url-shortener/tests`), 430 tests collected (426 verified in Phase 4 with Docker; the 4 added in Phase 6 ran alone)

| Layer | File | Tests | Covers |
|---|---|---:|---|
| Unit | `test_url_validator.py` | 101 | Scheme allow-list, userinfo, private/loopback/link-local/metadata ranges in many encodings, IDNA, control characters, self-reference |
| Unit | `test_api.py` | 50 | Routes, status codes, problem+json, headers, auth, error mapping (with in-memory fakes) |
| Unit | `test_security_review_fixes.py` | 40 | Regression tests for the Phase 4 review findings |
| Unit | `test_mutation_tool.py` | 4 | Guards for `tools/mutation_check.py` that run no mutant: every mutant pattern still matches the source, mutants are distinct, an inapplicable mutant is skipped, skipped is never counted as killed |
| Unit | `test_clicks.py` | 36 | Referrer origin reduction, user-agent family, event model, visitor hash |
| Unit | `test_link_service.py` | 33 | Create/resolve/delete rules, expiry, ownership, tombstones, cache behaviour |
| Unit | `test_codes_and_expiry.py` | 30 | Code generation, alias rules, reserved words, expiry arithmetic |
| Unit | `test_config_logging_worker_parse.py` | 26 | Settings validation, fail-closed credential guard, log redaction, worker parsing |
| Unit | `test_auth_ratelimit.py` | 24 | Key hashing and comparison, limiter, client-address logic |
| Unit | `test_analytics_service.py` | 13 | Aggregation and paging |
| Unit | `test_circuit_breaker.py` | 2 | Open/close timing |
| Contract | `test_openapi_contract.py` | 17 | Real responses validated against `openapi.yaml`'s JSON Schemas; operation parity |
| Integration | `test_api_e2e.py` | 16 | AC-1 to AC-12 and AC-14 against real PostgreSQL and Redis, including 50 concurrent creates of one alias (exactly one 201, 49 409) |
| Integration | `test_repositories.py` | 15 | SQL, constraints, counters, overlapping batches |
| Integration | `test_worker.py` | 12 | Redelivery, commit-then-crash, dead-lettering, reclaim, two workers, graceful stop |
| Integration | `test_redis_and_migrations.py` | 11 | Redis adapters (TTLs, tombstones, outage), migrations |

### Copilot prototype (`copilot-prototype/tests`), 132 collected

| File | Tests | Covers |
|---|---:|---|
| `test_redaction.py` | 27 | Every detector, stable placeholders, counts-only reports, long-input timing (0.06 s), known miss (bare secrets) |
| `test_providers.py` | 31 | The seven required stage names in order; architecture as a subtask of stage 2 (not a stage); stage 3 prompts built from the *accepted* (edited) task table, an iteration log, per-task failure modes matched on whole words; stage 4 skeletons (skipped, one per acceptance criterion, following edited criteria) and fix sketches only when code was supplied; QA output verification and summary counts; mock determinism; OpenAI-compatible provider against a **local stub server** (https rule, no redirects, size cap, no key in errors) |
| `test_engine.py` | 20 | Gates G1-G6, rejection limits, redaction before storage and prompting |
| `test_web.py` | 16 | Host/Origin/CSRF checks, body limit, escaping, CSP, allow-listed artifacts (in-process HTTP client) |
| `test_cli.py` | 11 | Scripted/auto/interactive modes recorded correctly, resume, verify |
| `test_analyzers.py` | 10 | Brief analysis, brownfield code findings |
| `test_audit.py` | 7 | Hash chain, tamper detection, refusal to store secret-like data |
| `test_context.py` | 6 (1 skipped) | Brownfield file reading limits. The skipped test needs symlink privileges: **that path is unverified on this machine** |
| `test_examples.py` | 4 | The three examples regenerate byte for byte, audit chains verify, no fake secret appears in any output |

## 3. What the tests do and do not prove

- **Same-author tests.** The code and the tests were drafted by the same AI and reviewed by one engineer. Coverage (96 %) measures executed lines, not correctness: while it stood at 96 %, three real gaps existed (database-path expiry boundary, mapped-IPv6 policy, "publish after the response"). Mutation checks found them ([AI_USAGE_LOG.md](AI_USAGE_LOG.md) entry 026).
- **Mutation testing is a small sample.** 22 hand-chosen mutants, 20 killed (Phase 3); the 2 survivors concern a deadlock lock order whose necessity could not be demonstrated. The 5 mutants for the Phase 4 fixes were **never run**, so those 40 regression tests have not been mutation-checked. Mutants 3, 7 and 27 were re-targeted or sharpened in Phase 6 and have not been run either, so the 20-of-22 result predates them.
- **Integration scope.** One PostgreSQL 16 and one Redis 7 container on one machine; empty database; one migration. No replica, no failover, no TLS, no Redis authentication, no populated-table migration.
- **Concurrency.** The alias race (50 concurrent creates) and overlapping worker batches are tested. A deadlock was never reproduced.
- **Performance.** There are **no performance assertions** in the suite and no verified capacity figure. See [LOAD_TEST.md](LOAD_TEST.md).
- **Security.** Static analysis, a read-only AI security review with 70 payloads, and curated adversarial validator cases. No DAST, no fuzzing, no human penetration test.
- **Prototype.** The mock provider is rule-based, so tests prove the engine, gates, redaction and audit behave as designed; they say **nothing** about the quality of a real model's output. The `openai` provider has only met a local stub. The web UI was driven by an in-process client and, in Phase 6, by one manual request to a running server (200 with a CSP header; a wrong `Host` returned 400); never a real browser.
- **Platform.** Everything ran on Windows. Linux/macOS and GitHub's runners are untested.

## 4. Fresh-environment check performed in Phase 6

To check that the documented setup is complete, two new virtual environments were created in a temporary folder (since deleted):

1. `pip install -r url-shortener/requirements-dev.txt` (exit 0, about 2 minutes), then unit+contract tests, ruff, ruff format, mypy and bandit: all pass as listed above. Commands were run through the venv's own interpreter and scripts; the `Activate.ps1` activation step shown in the README was not exercised.
2. A venv containing **only** `pytest==9.2.0`: the prototype's 123 tests (at that time) pass; `python -m copilot redact`, `run --decisions`, `verify`, an interactive run fed from stdin, and `web` (one request) all work. This confirms the prototype has no third-party runtime dependency. It predates the stage correction, which changed no dependency.

Not repeated: anything involving Docker (compose stack, integration tests, Trivy), `pip-audit`, and the full coverage run.

## 5. Reproducing the examples

`copilot-prototype/tests/test_examples.py` regenerates each example into a temporary folder with `--deterministic` and compares every file byte for byte with `examples/*/output/`. `.gitattributes` keeps those files and the example inputs free of line-ending conversion; without it a Windows checkout would regenerate different bytes.
