# Final Self-Review (T24)

Date: 2026-11-20. Reviewer: the AI that drafted the work, so this is a **self**-review with the blind spots that implies (RISKS A03, A08). The engineer owns the conclusions. Method: re-read the requirements, trace each to evidence, audit every claim in the documents, and run only light checks (no Docker, load, stress or mutation runs).

## 1. Checks performed in this review

| Check | Result |
|---|---|
| Documented setup works from scratch: two new virtual environments (service dev dependencies; a venv with only `pytest` for the prototype) | **Pass.** Service: install exit 0 (about 2 min), 372 unit and contract tests pass, ruff, ruff format, mypy and bandit clean. Prototype: 123 passed, 1 skipped (before the stage correction); `redact`, scripted `run`, `verify`, interactive `run` from stdin, and `web` (one request: 200 with a CSP header; a wrong `Host` returned 400) all work |
| Test counts quoted in the docs equal the collected tests | **Pass** (re-collected after the stage correction). Service 430 (359 unit, 17 contract, 54 integration; 426 were verified with Docker in Phase 4, the 4 new ones are mutation-tool guards run alone); prototype 132 collected |
| Relative links in all Markdown files resolve | **Pass**: 22 files, 0 broken (re-run after every document existed; an earlier run correctly flagged only the two documents not yet written) |
| No document claims the 5,000 req/s target, CI success, or real-model verification | **Pass.** Every mention is labelled provisional, unverified or not run |
| Present-tense statements still true after the `47c847d` commit | **Found 5 stale statements; fixed** (see section 4) |
| Docker-dependent checks (compose stack, integration tests, Trivy), `pip-audit`, full coverage run | **Not repeated.** Their last verified results (Phases 3 and 4) are quoted with that date |

## 2. Requirement traceability (Part B)

Status words: **Verified** = a test or command ran against the real system; **Partial** = covered, but not exactly as worded; **Unverified** = not run or not measured.

| Requirement | Evidence | Status |
|---|---|---|
| FR-1 create (alias, expiry) | `test_ac1_ac2_create_generated_and_custom_alias`, `test_ac4_ac5_invalid_requests_persist_nothing` (integration); `test_api.py` | Verified |
| FR-2 redirect 302 / 404 / 410 | Integration AC-6, AC-7; unit `test_api.py`; contract tests | Verified |
| FR-3 metadata | `test_api.py`, contract tests | Verified |
| FR-4 owner-only delete, cache invalidated | `test_ac7_delete_invalidates_the_cache_and_reserves_the_alias`, `test_ac8_authentication_and_ownership` | Verified |
| FR-5 analytics (totals, series, referrers, UA families, countries, paged clicks) | `test_ac9_analytics_match_the_visits_once_the_worker_runs`, `test_analytics_service.py`, cursor fixes in `test_security_review_fixes.py` | Verified |
| FR-6 liveness and readiness | `test_readiness_reports_database_outage`, AC-11 test | Verified |
| FR-7 RFC 7807 errors | Contract tests; `test_api.py` | Verified |
| FR-8 429 with `Retry-After` | `test_ac12_rate_limit_returns_429_with_retry_after_and_counters_expire` | Verified |
| AC-3 concurrent alias (50 attempts, one winner) | `test_ac3_fifty_concurrent_creates_of_one_alias_yield_exactly_one_winner` | Verified |
| AC-10 analytics write failure does not affect the redirect | The write happens in the worker, never in the request path. Tested: response completes before a slow publish (`test_api.py`), redirect unaffected with no consumer (`test_ac10_...`), worker failure paths (`test_worker.py`). **No single test fails a PostgreSQL analytics write while a visitor follows a link** | **Partial** |
| AC-11 Redis down | Integration test, plus a live `docker compose stop redis` check (Phase 3) | Verified |
| AC-13 one-command start | `docker compose up -d --build --wait` from clean, exit 0 in 15 s (Phase 3). The CI step that repeats it has never run | Verified once; not in Phase 6 |
| AC-14 two replicas behave identically | `test_ac14_two_replicas_behave_identically` | Verified |

### Non-functional requirements (Phase 1, section 1.3)

| NFR | Status |
|---|---|
| Redirect p95 under 50 ms (cache hit), create p95 under 200 ms | **Unverified.** Only a one-user calibration (176 req/s, 5.5 ms mean) and a profile (about 3.6 ms CPU per cached redirect) exist; no valid p95 under load |
| 5,000 redirects/s, 50 creates/s | **Unverified** (partial load test; one process saturated near 370-390 req/s) |
| Availability 99.9 % | Design intent only; not measurable here |
| Redirect survives Redis outage; analytics never blocks | Verified (see AC-10 and AC-11) |
| Statelessness, horizontal scale | Two-replica test passes; real scale-out not tested |
| mypy strict, 85 % core coverage | Verified (36 files clean; 96.0 % overall, 99 % on `link_service.py`) |
| Non-root, multi-stage Docker, compose, migrations as a one-shot job | Verified (Phase 3 and 4); read-only root and capability drop verified live |
| OWASP review | Done with evidence ([VALIDATION.md](VALIDATION.md) section 4); no DAST, fuzzing or human pen test |
| Privacy and retention (A8) | Raw IP is never stored: the `clicks` table has only a 16-character `visitor_hash`, and a live log check found no raw client IP. **The 90-day purge job is not built**, so raw clicks accumulate (RISKS R12) |

## 3. Part A against its specification (T23)

The only specification available was one line in the plan: *CLI/web UI, 7-stage engine, providers, redaction, mock mode, 3 examples; runs offline in mock mode; engineer designs HITL gates and redaction rules.*

| Item | Status |
|---|---|
| CLI and minimal web UI | Built and exercised (section 1). Web UI never opened in a real browser |
| Seven-stage engine | **Matches the engineer's required names** after the Phase 6 correction: Requirement Understanding, Task Decomposition, AI-Assisted Development, Output Generation, QA, Risk Awareness, Engineering Summary. Design decisions are a subtask of stage 2; stage 3 builds prompts from the accepted tasks and keeps an iteration log; stage 4 generates draft artifacts. Checked by `test_the_seven_stages_use_the_required_names_in_order` and by the regenerated examples. In mock mode these are rule-based templates, not model output; see [COPILOT_PROTOTYPE.md](COPILOT_PROTOTYPE.md) section 2 |
| Providers | Mock (default, offline) verified; OpenAI-compatible provider tested against a **local stub only** |
| Redaction | Designed with explicit gaps: bare unlabelled secrets and identifiers longer than 64 characters around a keyword are not detected |
| Mock mode offline, three examples | Verified; reproduced byte for byte by a test. The mock is a rule-based generator, **not an AI**, so the examples say nothing about model quality |
| HITL gates | Six gates enforced in the engine (not in the UI) and tested |

## 4. Defects found by this self-review

| # | Finding | Action |
|---|---|---|
| 1 | `COPILOT_PROTOTYPE.md`, `PHASE_STATUS.md` and the log section intros said "uncommitted" after the work was committed in `47c847d` | Updated; dated log entries 035, 036 and 043 were **left as written** (they are a historical record) and the commit is noted beside them |
| 2 | `ARCHITECTURE.md` still said "only Phase 4 load tests will say what the implementation achieves" although Phase 4 ended partial | Reworded: the targets are still unverified |
| 3 | `VALIDATION.md` said 4 new mutants; `url-shortener/tools/mutation_check.py` defines 5 | Corrected in Phase 5 staging; re-checked here |
| 4 | The staged brownfield example recorded hashes of CRLF input while its prompt files were LF, so the reproducibility test would fail on any other checkout | Fixed in Phase 5 close-out with `.gitattributes` and regenerated outputs (see log) |
| 5 | README and TESTING initially implied `make` was usable; it is not installed on the development machine | Every Makefile target now has a direct command; `make` targets are marked as not executed |
| 6 | The Copilot's stage names and scope did not match the engineer's required list (found by the engineer's review) | Corrected in code, generators, prompts, tests, docs and all three examples (Phase 6 correction) |
| 7 | The mutation script had two stale patterns, counted a skipped mutant as killed, and would have counted every integration mutant as killed with Docker down (all found by reading it and a static text match, without running it) | Patterns re-targeted; SKIPPED is never KILLED; a mutant whose tests already fail unmutated is skipped; a guard test checks all 27 patterns |

## 5. Open gaps, most important first

1. **Capacity is unknown.** The load test was partial; no valid p95 or throughput figure exists (RISKS R05).
2. **CI has never run.** Its steps ran locally one by one; the first real run may fail for environmental reasons (R15).
3. **The 5 Phase 4 mutants never ran**, so 40 regression tests are not mutation-checked. Mutants 3, 7 and 27 were re-targeted or sharpened in Phase 6 and have not run either, so the 20-of-22 result predates them.
4. **Evidence outside the repository (mostly resolved).** The mutation script now lives in `url-shortener/tools/mutation_check.py`, is tracked, and its reporting was corrected (SKIPPED never counts as killed). [VALIDATION.md](VALIDATION.md), [TESTING.md](TESTING.md) and the log still mention `.agent/venv` and `.vscode/tasks.json` (local tooling, untracked on purpose) as the way commands were run on the development machine. The script itself has **never been executed since the correction**.
5. **Retention purge and dead-letter re-drive are not built** (R04, R12).
6. **No Redis authentication or TLS** in the tested topology (R10); no DAST, fuzzing, Semgrep, secrets scan or licence scan; the security reviewer was the same model family as the author.
7. **Part A is unproven with a real model.** The `openai` provider has only met a stub; stages 3 and 4 are templates in mock mode, so whether a real model does better is untested; the web UI has not been seen in a browser; the symlink handling test is skipped on this machine; the 50 prototype tests in files the correction did not touch were not re-run after it. One known quirk remains: a Markdown heading at the start of a brief is read as part of the first requirement sentence.
8. **Single platform.** Everything ran on one Windows laptop.
9. **Risk ratings** in [RISKS.md](RISKS.md): the Phase 6 changes (R04 and R12 likelihood, A09 split, the derived Priority column) were approved by you; every other rating is still the AI's draft.
10. **Redaction is pattern-based**; a team handling real secrets should not rely on it alone.

## 6. Verdict

The service does what the approved requirements say, to the extent tests against real PostgreSQL and Redis can show; its security posture was reviewed and improved with evidence; and the documents say plainly where they stop. It is **not** shown to meet its capacity target, has **not** been run through CI, and Part A is a verified **workflow** prototype whose model-backed path is untested. Nothing in this repository should be described as production-ready or as having met 5,000 redirects/s.
