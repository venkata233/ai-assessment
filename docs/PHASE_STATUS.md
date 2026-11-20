# Phase Status

Single place to see what is done, what is partial, and what is unverified. Updated 2026-11-20. Detailed evidence lives in the
linked documents; nothing here replaces them.

| Phase | Scope | Status | Evidence |
|---|---|---|---|
| 1 | Requirements and task decomposition (T01) | **Complete** (engineer-approved) | [PHASE1_REQUIREMENTS_AND_PLAN.md](PHASE1_REQUIREMENTS_AND_PLAN.md) |
| 2 | Architecture, API contract, schema (T02-T04) | **Complete** (engineer-approved) | [ARCHITECTURE.md](ARCHITECTURE.md), [adr/](adr), [DB_SCHEMA.md](DB_SCHEMA.md), `url-shortener/openapi.yaml` |
| 3 | Implementation (T05-T18) | **Complete, with caveats** (committed as `ae592ce`) | [ARCHITECTURE.md](ARCHITECTURE.md) section 15; `url-shortener/` |
| 4 | Validation, load test, risk register (T19-T21) | **PARTIAL: closed by the engineer's decision, not finished** (see below) | [VALIDATION.md](VALIDATION.md), [LOAD_TEST.md](LOAD_TEST.md), [RISKS.md](RISKS.md) |
| 5 | Part A: Engineering Copilot, CLI and minimal web UI (T23) | **Built and tested offline; committed in `47c847d`; the Phase 6 correction (seven required stage names, new AI-Assisted Development and Output Generation stages, regenerated examples) is staged, not committed; awaiting engineer review.** Mock mode only | [COPILOT_PROTOTYPE.md](COPILOT_PROTOTYPE.md); `copilot-prototype/`, `examples/` |
| 6 | Final documentation and self-review (T22, T24) | **Written, reviewed and corrected; staged for the engineer's final review, not committed.** Includes the mutation script moved to `url-shortener/tools/` and corrected (never executed), and the approved risk changes with a derived Priority column. Nothing in Phases 3-5 was re-verified beyond the light checks listed in [SELF_REVIEW.md](SELF_REVIEW.md) | [README.md](../README.md), [TESTING.md](TESTING.md), [ENGINEERING_SUMMARY.md](ENGINEERING_SUMMARY.md), [SELF_REVIEW.md](SELF_REVIEW.md), [RISKS.md](RISKS.md) |

## Phase 4: explicit partial-completion statement

Phase 4 is closed as **PARTIAL**. The engineer stopped further load, mutation, and stress testing because of laptop CPU load.
All evidence produced so far is preserved in the documents above; nothing was deleted by this closure.

| Task | Status | What is true |
|---|---|---|
| T19 Validation and security review | **Done, with limits** | 426 tests pass at 96 % coverage; ruff, mypy, bandit, pip-audit, Trivy clean after fixes; independent review triaged (8 fixed, 2 partly fixed, 5 documented). **No** DAST, fuzzing, Semgrep, licence scan, or human penetration test. |
| T20 Load test | **PARTIAL** | Tooling built and committed under `url-shortener/loadtest/`; one 7-run series executed once. Raw result files were **not retained** (deleted while preparing a rerun the engineer declined). Scaling behaviour is **unexplained**, the load generator's CPU was **not sampled**, and there was no replicate or open-loop run. |
| T21 Risk register | **Done** | [RISKS.md](RISKS.md): 17 product/operations and 9 AI-specific risks (A09 split into a mock-provider and a real-LLM row, 27 rated rows). The Phase 6 changes (R04 and R12 likelihood, A09, a derived P1-P4 Priority column) were approved by the engineer; the remaining ratings are the AI's draft pending engineer review. |

Explicit statements that must not be dropped from any later summary:

1. **The 5,000 redirects/s target is UNVERIFIED.** It is neither supported nor refuted. The only observation is that one uvicorn
   process saturated near 370-390 req/s on a shared laptop; nothing can be extrapolated from it.
2. **Load testing was partial** (see T20 above and [LOAD_TEST.md](LOAD_TEST.md) section 4, "Not supported / unexplained").
3. **Mutation testing was not completed after the Phase 4 fixes.** 20 of 22 original mutants were killed (the 2 survivors are
   understood); **5 additional mutants for the Phase 4 fixes are defined in `url-shortener/tools/mutation_check.py` but were never run**, so the
   new regression tests have not been mutation-checked. The script was corrected in Phase 6 (stale patterns fixed; skipped mutants are
   never counted as killed) but has not been run since either.
4. **GitHub Actions CI was not verified.** `.github/workflows/ci.yml` has never run (no remote exists). Its steps were run
   individually on the laptop, and its YAML parses; that is all.
5. Phase 4 changes (security fixes, Dockerfile/compose hardening, `loadtest/`, `VALIDATION.md`, `LOAD_TEST.md`, `RISKS.md`) were
   **uncommitted** when this statement was written; they are now committed (`47c847d`, together with Phase 5). That commit says
   nothing about the other four points: they remain true.

To finish Phase 4 later, in order: (a) run the five new mutants; (b) rerun the load series with the Locust runners' CPU sampled,
a replicate, and an open-loop run, preferably with the generator on another machine; (c) run CI once a remote exists.
