# Engineering Summary

Audience: a reviewer who wants to know what was built, which decisions mattered, how AI was used, and what is and is not proven. Companion documents: [SELF_REVIEW.md](SELF_REVIEW.md) (claim audit and open gaps), [TESTING.md](TESTING.md), [PHASE_STATUS.md](PHASE_STATUS.md). Every figure below is traceable to a document or command named next to it. Date of evidence: 2026-11-20.

## 1. The task and how the work was run

**Task.** Part B: build a scalable URL shortener with APIs, persistence and analytics. Part A: an Engineering Copilot prototype (CLI and minimal web UI, seven-stage engine, providers, redaction, offline mock mode, three examples).

**Process.** Six phases, each ending at an engineer approval gate; nothing in a later phase started before the engineer said so:

| Phase | Output | Commit |
|---|---|---|
| 1 Requirements and decomposition | [PHASE1_REQUIREMENTS_AND_PLAN.md](PHASE1_REQUIREMENTS_AND_PLAN.md): 13 labelled assumptions, 14 acceptance criteria, 23 tasks with the AI/engineer split | `45c7948` (with Phase 2) |
| 2 Architecture, contract, schema | [ARCHITECTURE.md](ARCHITECTURE.md), six ADRs, [DB_SCHEMA.md](DB_SCHEMA.md), `openapi.yaml`, migration | `45c7948` |
| 3 Implementation (T05-T18) | `url-shortener/`, Dockerfile, compose, CI workflow, 384 tests | `ae592ce` |
| 4 Validation, security, load, risks | [VALIDATION.md](VALIDATION.md), [LOAD_TEST.md](LOAD_TEST.md), [RISKS.md](RISKS.md), fixes with tests | `47c847d` (**partial**, closed by the engineer) |
| 5 Engineering Copilot | `copilot-prototype/`, `examples/`, [COPILOT_PROTOTYPE.md](COPILOT_PROTOTYPE.md) | `47c847d` |
| 6 Final docs and self-review | README, [TESTING.md](TESTING.md), this file, [SELF_REVIEW.md](SELF_REVIEW.md) | see [PHASE_STATUS.md](PHASE_STATUS.md) |

## 2. What was built (Part B)

A stateless FastAPI service: `POST /api/v1/urls` (optional alias and expiry), `GET /{code}` (302), metadata, owner-only delete, per-link analytics, health and readiness, RFC 7807 errors. PostgreSQL 16 is the system of record; Redis 7 provides the read-through cache, rate limiting and a Redis Stream that a separate worker drains into PostgreSQL. Layered code (domain, services, infra, api, worker), `mypy --strict`, Docker (non-root, read-only root filesystem, all capabilities dropped), compose, and a CI workflow (never run on GitHub; see section 6).

### Decisions that shaped the design (all engineer-approved; details in the ADRs)

| Decision | Why | Cost accepted |
|---|---|---|
| **302, not 301** (ADR-001) | Every click stays observable; delete and expiry take effect immediately | Browsers do not cache redirects, so more load on the service |
| **Random Base62 codes from `secrets`, uniqueness enforced by the database** (ADR-002) | No check-then-insert race; the 50-way concurrent alias test yields exactly one winner | Retry on collision; bounded, then a 503 |
| **Analytics through a Redis Stream and a worker** (ADR-004) | A redirect never waits for, or fails because of, an analytics write | At-least-once delivery with idempotent storage; documented loss windows (best-effort emit, crash, stream trim) |
| **Owner-scoped static API keys** (ADR-006) | Closes the public-DELETE hole with minimal machinery | No rotation tooling; key revocation needs a restart (RISKS R09) |
| **Raw click rows, no rollups or partitioning in v1** (ADR-003) | Simplicity; honest about the ceiling | Unbounded growth; the 90-day retention job is documented but **not built** (RISKS R12) |
| **Fail-open rate limiter and Redis outage fallback to PostgreSQL** | Availability over strictness | Abuse is unthrottled while Redis is down (R01) |
| **Fail-closed credential guard** (added in Phase 4) | The one-command demo needs published dev keys; the app refuses to start with them unless `BASE_URL` is localhost | A deliberate break-glass flag exists for tests |

## 3. What was built (Part A)

A stdlib-only package: a state machine with six human-in-the-loop gates, redaction before anything is sent or stored, a hash-chained audit log, a mock provider and an OpenAI-compatible provider, a CLI and a loopback-only web UI, and three generated examples reproduced byte for byte by a test. See [COPILOT_PROTOTYPE.md](COPILOT_PROTOTYPE.md).

Two things a reader must not miss:

1. The **mock provider is rule-based, not an AI**. The examples demonstrate the workflow, gates, redaction and audit trail, not model quality, and their decisions are scripted and recorded as such.
2. The **seven stages are the engineer's required names** (Requirement Understanding, Task Decomposition, AI-Assisted Development, Output Generation, QA, Risk Awareness, Engineering Summary). The first version used AI-derived names because the original brief was not in the workspace; the Phase 6 correction replaced them, moved architecture and design decisions into Task Decomposition as a subtask, made AI-Assisted Development prepare and record AI-assisted work from the accepted tasks, and added Output Generation (draft deliverables). See [COPILOT_PROTOTYPE.md](COPILOT_PROTOTYPE.md) section 2.

## 4. How AI was used, and who decided what

The AI drafted code, tests, documents and checklists; the engineer approved each phase, accepted or changed assumptions, and decided the trade-offs above. [AI_USAGE_LOG.md](AI_USAGE_LOG.md) records every interaction with a decision column (ACCEPT, MODIFY, REJECT) and the reasoning. The plan fixed in advance which parts are **non-delegable** (threat model, concurrency semantics, retention policy, what to claim) and which failure modes to look for in each AI draft (for example `random` instead of `secrets`, `==` key comparison, tautological tests).

## 5. Where the AI was wrong, and how it was caught

Selected from the log; each was found by running something, not by rereading.

| What went wrong | How it was found | Outcome |
|---|---|---|
| Used APIs that do not exist in the pinned libraries (`Redis[str]`, FastAPI route flattening) | `mypy --strict`, running against real services (RISKS A01) | Fixed; versions resolved from PyPI instead of memory |
| Redis outage made every redirect pay several failed connects (839 ms) | Live `docker compose stop redis` | Shared circuit breaker (first request 549 ms, then 5-30 ms) |
| Tests passed at 96 % coverage while three real gaps existed | Manual mutation testing, 22 mutants: 18 killed at first, 20 after new tests | Tests added; 2 survivors understood (deadlock lock order, **necessity unproven**) |
| Regex `$` accepted `"admin\n"`, bypassing the reserved-word list and causing a 500 | Independent read-only security review (0 High, 0 Medium, 6 Low) | `fullmatch` everywhere, with regression tests |
| Pydantic echoed submitted settings, including secret tails, in startup errors | Validation work | `hide_input_in_errors`, with a test |
| Three HIGH CVEs in the container image | Trivy | Patched zlib, removed pip/setuptools/wheel, rescanned clean |
| Redaction missed `ADMIN_PASSWORD=...`, `AWS_SECRET_ACCESS_KEY=...`, `GITHUB_TOKEN:...` | A probe with realistic names | Detector extended; the new test then exposed a **14.5 s** quadratic pattern. My first diagnosis blamed the new regex; the culprit was an older, unbounded URL-credentials pattern (now 0.06 s) |
| Brownfield scanner reported no hardcoded secrets because redaction ran first | Generating the example | Scanner now flags redacted placeholders |
| The committed brownfield example embedded hashes of CRLF input but LF prompt files, so it would have failed on any other platform | Reviewing the staged diff before commit, then `git ls-files --eol` | `.gitattributes` pins inputs and outputs; outputs regenerated; test passes |
| Docs drifted (a "4 new mutants" statement; "uncommitted" claims after the commit; a stale "only Phase 4 will say" sentence) | Self-review in Phase 6 ([SELF_REVIEW.md](SELF_REVIEW.md)) | Corrected |
| The Copilot's stage list did not match the required names, and the generated "implementation" stage only planned work | The engineer's Phase 6 review; a comparison of `stages.py`, generators and examples | Stages restructured and examples regenerated; while checking the output I also found failure modes matched on substrings (a hardcoded secret was tagged as a check-then-insert race) and fixed it with a regression test |
| The mutation script would have reported two stale mutants as killed, and every integration mutant as killed with Docker down | A static text match of all 27 patterns against the source (script not run), then reading its logic | Patterns re-targeted; a skipped mutant is never killed; a guard test checks all 27 patterns on every test run |

Pattern: green checks were repeatedly insufficient (RISKS A02, A03, A08). The defects that mattered were found by an independent reviewer, a live failure, a probe or an adversarial test, not by the checks that already existed.

## 6. What is and is not proven

| Area | Proven (and how) | Not proven |
|---|---|---|
| Functional behaviour | AC-1 to AC-12 and AC-14 pass against real PostgreSQL and Redis; 426 tests, 96.0 % statement and branch coverage; contract tests against `openapi.yaml` | AC-10 as literally worded ("analytics DB write fails") is covered indirectly (see SELF_REVIEW) |
| Start-up | `docker compose up --build --wait` from clean: exit 0 in 15 s (Phase 3) | Not repeated in Phase 6 |
| Security | Bandit, pip-audit, Trivy (clean after fixes), independent AI review, 170+ adversarial validator cases | No DAST, fuzzing, Semgrep, secrets scanner, licence scan or human penetration test; reviewer is the same model family as the author |
| Capacity | One uvicorn process saturated near 370-390 req/s on a shared laptop; hot-path cost about 3.6 ms CPU per cached redirect | **The 5,000 redirects/s target is unverified.** The load test was partial, raw results are gone, scaling behaviour is unexplained, and the load generator's CPU was not sampled |
| CI | Each step run locally; YAML parses; action versions pinned to commit SHAs | **The workflow has never run on GitHub** (no remote) |
| Test strength | 20 of 22 original mutants killed (Phase 3) | The 5 mutants for the Phase 4 fixes were **never run**; the script was corrected in Phase 6 and has not run since, so the 20 of 22 predates three re-targeted mutants |
| Part A | 132 tests collected; the files touched by the stage correction and the examples test pass; the 50 untouched tests were not re-run after it (123 passed, 1 skipped on the last full run); examples reproducible; fresh-venv run needs only pytest | Real model output; the `openai` provider against any real service (local stub only); a real browser; Linux/macOS; the skipped symlink path |

## 7. Limits and risks to carry forward

The full list is [RISKS.md](RISKS.md) (17 product and operations risks, 9 AI-specific). The ones most likely to matter: unknown capacity (R05), no Redis authentication or TLS in the tested topology (R10), unbuilt retention job (R12), unproven CI (R15), and indefinite growth of raw click rows. Risk ratings in that file: the Phase 6 changes (R04 and R12 likelihood, A09 split into mock and real-LLM rows, a derived P1-P4 Priority column) were approved by the engineer; the rest are the AI's draft pending review.

## 8. If work continued, in order

1. Run the 5 pending Phase 4 mutants (they use only the unit suite, so no Docker; run on a quiet machine), then the rest.
2. Create a remote and run CI once; fix whatever the first real run shows.
3. Re-run the load series with the generator's CPU sampled, a replicate and an open-loop run, ideally from another machine; only then state a capacity figure.
4. Build the retention purge and the dead-letter re-drive tool; add Redis auth.
5. Try the `openai` provider against a real endpoint and review its output as a person; then check whether the model's stage 3 and 4 output is better than the templates.
