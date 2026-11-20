# AI-assisted engineering assessment

Two deliverables built with AI assistance, with every AI output reviewed, changed or rejected by the engineer and logged:

| Part | What | Where |
|---|---|---|
| **B** | A production-style **URL shortener service** (FastAPI, PostgreSQL, Redis, Docker, CI) | [`url-shortener/`](url-shortener) |
| **A** | An **Engineering Copilot** prototype: a seven-stage, human-in-the-loop CLI and minimal local web UI that runs **offline in mock mode** | [`copilot-prototype/`](copilot-prototype), [`examples/`](examples) |

Start with [docs/ENGINEERING_SUMMARY.md](docs/ENGINEERING_SUMMARY.md) for the story and [docs/SELF_REVIEW.md](docs/SELF_REVIEW.md) for an honest account of what is and is not proven.

## Status at a glance

Details and evidence: [docs/PHASE_STATUS.md](docs/PHASE_STATUS.md).

| Claim | Status |
|---|---|
| Service implements the approved requirements, and 426 tests pass against real PostgreSQL 16 and Redis 7 | **Verified** (Phase 4, Windows + Docker). 430 tests are collected now; the 4 added in Phase 6 are guard tests for the mutation script and were run on their own |
| Container image scan, SAST, dependency audit | **Verified**, point in time (Trivy, Bandit, pip-audit) |
| The **5,000 redirects/s** design target | **UNVERIFIED.** Load testing was partial; one uvicorn process saturated near 370-390 req/s on a shared laptop |
| GitHub Actions CI | **NEVER RUN** (no remote exists); its steps were run locally one by one |
| Mutation checks of the Phase 4 fixes | **NOT RUN** (5 mutants defined, never executed; the script was corrected in Phase 6 but has still never run since) |
| Copilot prototype in **mock mode** | **Verified** (132 tests collected; the files touched by the Phase 6 stage correction pass, the untouched 50 were not re-run after it; 123 passed and 1 skipped before it). The mock provider is rule-based, **not an AI** |
| Copilot with a real model (`openai` provider) | **UNVERIFIED** (tested against a local stub only) |
| Web UI in a real browser | **NOT VERIFIED** (tested with an in-process HTTP client) |

## Repository layout

```
url-shortener/     Part B: src/, tests/{unit,contract,integration}, migrations/, openapi.yaml, Dockerfile, docker-compose.yml, loadtest/, tools/ (mutation_check.py)
copilot-prototype/ Part A: stdlib-only package (src/copilot), tests/
examples/          Part A: greenfield | ambiguous | brownfield (brief, scripted decisions, generated output; reproduced by a test)
docs/              requirements/plan, architecture, ADRs, DB schema, validation, load test, risks, testing, summaries, AI usage log
.github/workflows/ ci.yml (never run on GitHub)
```

## Prerequisites

- Python **3.11+** (developed on 3.11.4)
- Docker with Compose v2 (only for the compose stack and the integration tests; developed with Docker 29.9.3, client and server, under Docker Desktop)
- Git. `make` is optional: it is not installed on the development machine, so every Makefile target below has a direct command.

## Part B: URL shortener

### Run the stack (Docker)

```bash
cd url-shortener
docker compose up -d --build --wait     # PostgreSQL, Redis, one-shot migration, app (127.0.0.1:8000), worker
curl -s http://127.0.0.1:8000/health    # {"status":"ok"}
docker compose down -v                  # stop and delete the data volumes
```

*Verification:* `up --build --wait` from a clean state was run in Phase 3 (exit 0, migrations applied, `/health` 200). It was **not re-run in Phase 6**.

The compose file ships **development-only** credentials (API key `demo-key-local-dev-only`, `*_dev_only` database passwords). The application **refuses to start** with them unless `BASE_URL` is `localhost`, so they cannot silently reach a real deployment. For anything else copy [`.env.example`](url-shortener/.env.example) to `.env` and set real values; never commit `.env`.

### Try it (bash or Git Bash; PowerShell quoting of the JSON body was not tested)

```bash
key=demo-key-local-dev-only
curl -sS -X POST http://127.0.0.1:8000/api/v1/urls -H "X-API-Key: $key" -H "Content-Type: application/json" \
     -d '{"url":"https://example.com/landing","alias":"demo"}'
curl -sS -o /dev/null -w '%{http_code} %{redirect_url}\n' http://127.0.0.1:8000/demo      # 302 https://example.com/landing
curl -sS -H "X-API-Key: $key" http://127.0.0.1:8000/api/v1/urls/demo/analytics           # eventually consistent (about 1 s)
```

API contract: [`url-shortener/openapi.yaml`](url-shortener/openapi.yaml). Errors are RFC 7807 `application/problem+json`. API docs (`/docs`) are off by default.

### Develop and test (no Docker needed for unit and contract tests)

```powershell
cd url-shortener
python -m venv .venv ; .\.venv\Scripts\Activate.ps1          # Linux/macOS: source .venv/bin/activate
python -m pip install -r requirements-dev.txt
python -m pytest tests/unit tests/contract -q                # same as `make unit`
ruff check src tests migrations ; ruff format --check src tests
mypy
bandit -q -r src -c pyproject.toml
```

Integration tests need PostgreSQL and Redis: `docker compose up -d --wait postgres redis`, then set `REQUIRE_INTEGRATION=1` (`$env:REQUIRE_INTEGRATION="1"` in PowerShell) and run `python -m pytest tests/integration -q`. Full details, counts and what the tests do not prove: [docs/TESTING.md](docs/TESTING.md).

### Makefile targets (Linux/macOS/CI)

`install lint format typecheck security unit integration test coverage deps-up deps-down up down migrate check`.

## Part A: Engineering Copilot (offline mock mode)

Standard library only; no install and no network needed in mock mode.

```powershell
cd copilot-prototype
$env:PYTHONPATH = "src"                                      # bash: export PYTHONPATH=src
python -m copilot run --brief ..\examples\greenfield\brief.md --out .\sessions   # interactive: you approve each of 7 stages
python -m copilot web --port 8765                            # minimal UI at http://127.0.0.1:8765 (loopback only)
python -m copilot verify <session-id> --out .\sessions       # checks the audit hash chain
python -m pytest -q                                          # 132 tests (123 passed, 1 skipped on the last full run, before the stage correction)
```

The three examples are regenerated and compared byte for byte by `tests/test_examples.py`. Design, gates, redaction rules and limits: [docs/COPILOT_PROTOTYPE.md](docs/COPILOT_PROTOTYPE.md). The seven stages are the required ones: Requirement Understanding, Task Decomposition, AI-Assisted Development, Output Generation, QA, Risk Awareness, Engineering Summary (architecture and design decisions are a subtask of Task Decomposition).

## Documentation map

| Document | Purpose |
|---|---|
| [ENGINEERING_SUMMARY.md](docs/ENGINEERING_SUMMARY.md) | What was built, key decisions, how AI was used, what went wrong |
| [SELF_REVIEW.md](docs/SELF_REVIEW.md) | Final self-review: requirement traceability, claim audit, open gaps |
| [PHASE1_REQUIREMENTS_AND_PLAN.md](docs/PHASE1_REQUIREMENTS_AND_PLAN.md) | Requirements, assumptions, task decomposition |
| [ARCHITECTURE.md](docs/ARCHITECTURE.md), [adr/](docs/adr), [DB_SCHEMA.md](docs/DB_SCHEMA.md) | Design, decisions, schema |
| [TESTING.md](docs/TESTING.md) | How to run each check; test inventory; limits |
| [VALIDATION.md](docs/VALIDATION.md), [LOAD_TEST.md](docs/LOAD_TEST.md), [RISKS.md](docs/RISKS.md) | Security review, partial load test, risk register |
| [COPILOT_PROTOTYPE.md](docs/COPILOT_PROTOTYPE.md) | Part A design and limits |
| [AI_USAGE_LOG.md](docs/AI_USAGE_LOG.md) | Every AI interaction with the engineer's decision |
| [PHASE_STATUS.md](docs/PHASE_STATUS.md) | Phase table and explicit partial-completion statement |
